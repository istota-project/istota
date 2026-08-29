"""Memory skill CLI — runtime writes to USER.md / CHANNEL.md / skill overlays.

Single write path through the curation ops engine (`apply_ops`, and
`curation.overlay` for the flat per-skill files). Used by the always-included
memory skill so durable memory writes don't bypass heading routing, dedup, or
the audit log.

Subcommands:
  append        Append a bullet under an existing `## heading` (optionally
                under one of its `### subheadings` via --subheading).
  add-heading   Add a new `## heading` with one or more bullets.
  remove        Remove a bullet (substring match, must be unique). Reaches
                into subsections.
  replace       Rewrite the single matching bullet in place.
  remove-heading Drop a whole `## ` section.
  show          Print USER.md, a CHANNEL.md or a skill overlay (or one
                section / `### ` subsection of it).
  headings      List the `## ` heading names in order.
  skills        Inventory of the per-skill overlay files: what is customized,
                and whether each one actually binds.

Each write subcommand can target the channel memory file by passing
`--channel TOKEN`. The TOKEN is validated against `ISTOTA_CONVERSATION_TOKEN`
when set, to refuse cross-channel writes from a runtime task that's
been scoped to a different conversation.

`--skill NAME` targets that skill's per-user overlay instead
(`config/skills/<name>.md`, appended to the skill's bundled body by
`skills._loader.load_skills`). Overlays are flat — no `## ` sections — so
`--heading` under `--skill` names a `### ` subsection of the overlay rather
than a section of USER.md, and `add-heading` / `remove-heading` have nothing
to act on and are refused.

Env vars used:
  ISTOTA_USER_ID            User whose USER.md is targeted.
  NEXTCLOUD_MOUNT_PATH      Mount root.
  ISTOTA_BOT_DIR_NAME       Bot directory name (e.g. "istota").
  ISTOTA_TASK_ID            Optional, used in audit log entries.
  ISTOTA_CONVERSATION_TOKEN Optional, used to validate --channel.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from istota.memory.curation.audit import (
    write_audit_log,
    write_last_seen,
)
from istota.memory.curation.file_lock import (
    MemoryMdLocked,
    deferred_lock_dir,
    memory_md_lock,
)
from istota.memory.curation.ops import apply_ops
from istota.memory.curation.overlay import (
    apply_overlay_op,
    parse_overlay_doc,
    serialize_overlay_doc,
)
from istota.memory.curation.parser import (
    parse_sectioned_doc,
    serialize_sectioned_doc,
)
from istota.memory.curation.types import (
    classify_line,
    subheading_text,
    subsection_region_indices,
)
from istota.skills._loader import (
    OVERLAY_READ_CAP_BYTES,
    inspect_overlay,
    read_overlay_bytes,
)

#: Target kinds. `_resolve_target` used to answer a bool; three destinations
#: with three different audit rules is one answer too many for one.
_USER = "user"
_CHANNEL = "channel"
_SKILL = "skill"

#: Ceiling on what this CLI will read back before editing. Distinct from the
#: loader's `OVERLAY_MAX_BYTES` and much larger, deliberately: a file over the
#: loader's cap does not load, and `remove` is the only way to bring it back
#: under — refusing to read it would leave the user with a file they can
#: neither use nor shrink. This bound exists only so a multi-gigabyte file
#: planted at the path cannot be pulled into the daemon's memory. Named here
#: for the local reason; owned by `_loader`, which reads under it too.
_MAX_OVERLAY_READ_BYTES = OVERLAY_READ_CAP_BYTES

#: How much of an overlay's first line the inventory shows.
_FIRST_LINE_CHARS = 120

#: "No write happened", distinct from a write of `None` (the file was emptied
#: and deleted, so its index rows go too).
_UNSET = object()

logger = logging.getLogger(__name__)


def _emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("status") == "ok" else 1


def _err(msg: str, **extra) -> int:
    payload = {"status": "error", "error": msg}
    payload.update(extra)
    return _emit(payload)


def _user_id() -> str:
    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        _err("ISTOTA_USER_ID not set")
        sys.exit(1)
    return user_id


def _mount_path() -> Path:
    mount = os.environ.get("NEXTCLOUD_MOUNT_PATH", "")
    if not mount:
        _err("NEXTCLOUD_MOUNT_PATH not set")
        sys.exit(1)
    return Path(mount)


def _bot_dir() -> str:
    bot = os.environ.get("ISTOTA_BOT_DIR_NAME", "")
    if bot:
        return bot
    # Fallback for ad-hoc CLI use only — refuse to guess when more than
    # one bot dir exists (ISSUE-077: silent writes to wrong USER.md under
    # multi-bot tenancy or stale rename leftovers).
    user_id = _user_id()
    base = _mount_path() / "Users" / user_id
    candidates: list[str] = []
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "config" / "USER.md").is_file():
                candidates.append(child.name)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        _err(
            "ISTOTA_BOT_DIR_NAME not set and multiple bot dirs found — refusing to guess",
            user_id=user_id,
            candidates=candidates,
        )
        sys.exit(1)
    _err(
        "ISTOTA_BOT_DIR_NAME not set and could not infer from mount",
        user_id=user_id,
    )
    sys.exit(1)


def _user_md_path() -> Path:
    return _mount_path() / "Users" / _user_id() / _bot_dir() / "config" / "USER.md"


def _channel_md_path(token: str) -> Path:
    if not token or "/" in token or "\\" in token or token.startswith("."):
        _err("invalid channel token", token=token)
        sys.exit(1)
    env_token = os.environ.get("ISTOTA_CONVERSATION_TOKEN", "")
    if not env_token:
        _err(
            "channel write requires ISTOTA_CONVERSATION_TOKEN",
            given=token,
        )
        sys.exit(1)
    if env_token != token:
        _err(
            "channel token mismatch — refusing cross-channel write",
            given=token, expected=env_token,
        )
        sys.exit(1)
    return _mount_path() / "Channels" / token / "CHANNEL.md"


def _overlay_dir() -> Path:
    """The user's per-skill overlay directory. May not exist."""
    return _mount_path() / "Users" / _user_id() / _bot_dir() / "config" / "skills"


def _skill_index():
    """Return `(skill_index, config)`, loading the config lazily.

    Deliberately not imported at module scope. This CLI is spawned per write
    and the rest of it deliberately reads env vars rather than a Config — see
    `_config_for_audit`. Only the `--skill` paths need to know which skill
    names exist, so only they pay for the TOML parse and the frontmatter scan.

    Guarded because it is the only thing in this CLI that can raise. Every
    other refusal here returns one JSON line on stdout, and `main` has no
    exception handling — so a malformed `config.toml` or an unreadable skills
    directory would hand the model a traceback on stderr and nothing on
    stdout, which reads as "the command did nothing" rather than as a failure.
    """
    from istota.config import load_config
    from istota.skills._loader import load_skill_index

    try:
        config = load_config()
        index = load_skill_index(
            config.skills_dir, bundled_dir=config.bundled_skills_dir
        )
    except Exception as e:  # noqa: BLE001 — the envelope contract is the point
        _err("skill_index_unavailable", detail=f"{type(e).__name__}: {e}")
        sys.exit(1)
    return index, config


def _skill_overlay_path(skill: str, *, verb: str) -> tuple[Path, object]:
    """Resolve `--skill NAME` to its overlay path and Config, or refuse and exit.

    Two refusals, and the first is the write-time half of the typo defense:
    `--skill develper` would otherwise create a file that binds to nothing and
    that nothing would ever report, so the name is checked against the loaded
    skill index and the known names come back with the error — mirroring how
    `heading_missing` returns `available_headings`.

    The second is the denylist. `sensitive_actions` and `untrusted_input` take
    no overlay: not a security boundary, since the user can already fork either
    document through the operator override, but a guard against a casual
    preference line landing in the safety layer. Only the verbs that *put text
    in* are refused — `append` and `replace`. `remove` and `show` cannot add a
    line, so a file hand-planted in one of those two slots is still readable
    and removable through the sanctioned path.

    The two refusals are deliberately not symmetric, and the asymmetry is
    about where the name comes from rather than about convenience. A
    denylisted name is one of two known strings, so `show --skill
    sensitive_actions` involves no unbounded input and the only question is
    whether the verb adds text. An unknown name is a caller-supplied path
    component, and the skill index is the whole of what bounds it — there is
    nothing else to check `develper` or `../../USER` against. So it is refused
    on every verb, including the ones that only read. The cost is that a
    misspelled file is visible in `memory skills` and removable only with the
    file tools, which is what the error says.
    """
    from istota.skills._loader import OVERLAY_DENYLIST, _denylist_key

    index, config = _skill_index()
    if skill not in index:
        _err(
            "unknown_skill",
            skill=skill,
            available_skills=sorted(index),
            hint=(
                "no overlay binds to this name. `memory skills` lists any file "
                "already filed under it; remove one with the file tools"
            ),
        )
        sys.exit(1)
    if verb in ("append", "replace") and _denylist_key(skill) in OVERLAY_DENYLIST:
        _err(
            "denylisted_skill",
            skill=skill,
            denylist=sorted(OVERLAY_DENYLIST),
        )
        sys.exit(1)
    return _check_overlay_dir() / f"{skill}.md", config


class Target(NamedTuple):
    path: Path
    kind: str
    skill: str | None = None
    #: The loaded Config, on the `--skill` path only. Carried rather than
    #: re-derived because resolving an overlay target already loads one, and
    #: the re-index that follows the write needs the same object — a second
    #: `load_config()` in a CLI spawned per write is a second TOML parse and a
    #: second skills-directory scan for an answer already in hand.
    config: object | None = None


def _resolve_target(args, *, verb: str) -> Target:
    """Resolve the write/read destination from `--channel` / `--skill`.

    `--skill` and `--channel` name different files under different rules, and
    a caller that passed both has not said which it meant. Refused rather than
    given a precedence, so a typo cannot silently write to the other one.
    """
    token = getattr(args, "channel", None)
    skill = getattr(args, "skill", None)
    if token and skill:
        _err("skill_and_channel_are_exclusive", skill=skill, channel=token)
        sys.exit(1)
    if skill:
        path, config = _skill_overlay_path(skill, verb=verb)
        return Target(path, _SKILL, skill, config)
    if token:
        return Target(_channel_md_path(token), _CHANNEL)
    return Target(_user_md_path(), _USER)


def _config_for_audit():
    """Build a minimal Config-like shim for `write_audit_log`/`write_last_seen`.

    The audit module uses `_get_mount_path(config, get_user_memory_path(user_id, config.bot_dir_name))`
    to resolve the audit sidecar. We only need `nextcloud_mount_path` and
    `bot_dir_name`. Importing the full Config is heavy and pulls in TOML
    parsing for a CLI that runs hundreds of milliseconds end-to-end.
    """
    class _Shim:
        nextcloud_mount_path = _mount_path()
        bot_dir_name = _bot_dir()
    return _Shim()


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""
    except OSError as e:
        _err(f"failed to read {path}: {e}")
        sys.exit(1)


def _read_overlay_bytes(path: Path) -> tuple[bytes | None, str | None]:
    """`_loader.read_overlay_bytes` under this CLI's read ceiling.

    The hardening — `O_NOFOLLOW`, `S_ISREG` + `O_NONBLOCK`, and the size taken
    off the fd before the read — is the loader's, in the loader's own function,
    because this CLI is spawned **host-side** by the skill proxy with the
    daemon's filesystem view while the directory it reads sits under
    `{mount}/Users/{user_id}`, which `build_bwrap_cmd` binds **read-write** into
    that user's sandbox. Every entry in it is model-plantable, so a plain
    `read_text()` here is an arbitrary daemon-side file read whose result
    `memory show --skill …` hands straight back. Three surfaces face that same
    directory and there is one reader for it.

    What differs is what a refusal means, and that stays here: the loader
    degrades one to "no overlay" because its worst case is an inert
    customization, while here it must reach the caller. Writing through a
    refusal would replace whatever was planted with a file the user did not ask
    for, and reporting one as "file absent" would make `append` clobber it.

    The size the reader also returns is dropped, since the two callers left
    want the bytes or the refusal and nothing else; `inspect_overlay` takes it
    from the same call for the inventory.
    """
    data, reason, _size = read_overlay_bytes(path, max_bytes=_MAX_OVERLAY_READ_BYTES)
    return data, reason


def _read_overlay_text(path: Path) -> str:
    """`_read_overlay_bytes` with every refusal turned into an error envelope."""
    raw, reason = _read_overlay_bytes(path)
    if reason is not None:
        _err(reason, path=_mount_relative(path))
        sys.exit(1)
    assert raw is not None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        _err("overlay_not_utf8", path=_mount_relative(path))
        sys.exit(1)


def _check_overlay_dir() -> Path:
    """Refuse a redirected `config/skills/`. Creates nothing.

    `O_NOFOLLOW` on the overlay file covers the *last* path component, and an
    `lstat` on `skills` covers one more. Neither covers an ancestor, and every
    ancestor is model-writable: `{mount}/Users/{user_id}` is bound read-write
    into that user's sandbox, so `mv config config.real && ln -s /anywhere
    config` is two commands from inside it. The link's target is a string
    resolved *host-side* by this unsandboxed CLI, so it need not exist in the
    namespace — which turned `append --skill developer` into a write of
    model-chosen content to a model-chosen directory, and `show --skill
    developer` into a read back out of one. `mkdir(parents=True)` obligingly
    created the missing `skills/` at the far end.

    So containment is checked, not just the leaf's shape: the resolved
    directory must sit under the resolved `{mount}/Users/{user_id}`, the same
    equality-under-a-known-root pattern `sandbox_cache_sweeper` and
    `repos_relocate` use. Both sides are resolved because the mount itself is
    reached through symlinks on some hosts, and comparing a resolved path to
    an unresolved root reads every path as outside.

    The leaf is held to the stricter of the two rules — it may not be a
    symlink at all — because it is the component this CLI creates and owns.
    An *ancestor* link that stays inside the user's own tree passes: that
    leads nowhere the user could not already reach, and refusing it would
    break someone who had reorganised their own config directory.

    Not atomic with the write that follows — the check and the `os.replace`
    are separated by the op, so a directory swapped in between is not covered.
    This closes the planted-link case, which is the one a model reaches with a
    couple of commands; a genuine race against the daemon is not addressed.
    """
    d = _overlay_dir()
    if d.is_symlink() or (d.exists() and not d.is_dir()):
        _err("overlay_dir_not_a_directory", path=_mount_relative(d))
        sys.exit(1)
    user_root = _mount_path() / "Users" / _user_id()
    resolved = Path(os.path.realpath(d))
    resolved_root = Path(os.path.realpath(user_root))
    if resolved != resolved_root and resolved_root not in resolved.parents:
        _err("overlay_dir_outside_user_tree", path=_mount_relative(d))
        sys.exit(1)
    return d


def _ensure_overlay_dir() -> Path:
    """`_check_overlay_dir`, then create it at 0755 if it is not there yet."""
    d = _check_overlay_dir()
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o755)
        except OSError:
            pass
    return d


def _atomic_write(path: Path, text: str) -> None:
    """Replace `path` with `text` via a uniquely-named staging file.

    The staging name is per-writer rather than `<name>.tmp`, because that fixed
    name is shared with the web save path (`storage.write_channel_memory`) and
    the lock anchor is per-user — so two members of a shared Talk room writing
    the same CHANNEL.md hold different locks and would interleave into one
    staging file, publishing a mixture of both. `os.replace` is atomic; the
    staging is what had to be made unique. UTF-8 is explicit for the same reason
    the readers pin it: the revision tag the web save compares is a UTF-8 hash.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            # mkstemp is 0600; this becomes a file the user reads over
            # Nextcloud. `fchmod` on the open descriptor rather than `chmod`
            # on the name: the staging file is created inside a directory
            # every entry of which is model-plantable, and the model is the
            # party that invoked this CLI, so it knows when the window between
            # the close and the mode change opens. A symlink swapped in there
            # would take the 0644 to whatever it names. The descriptor cannot
            # be redirected. `os.replace` needs no such care — rename does not
            # follow the final component.
            os.fchmod(fh.fileno(), 0o644)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _mount_relative(path: Path) -> str:
    """`path` as written from the mount root, falling back to the absolute form.

    The audit sidecar sits inside the tree this is relative to, so the absolute
    prefix would be the same on every entry and carry no information — and it
    would restate the deployment's filesystem layout in a file the user reads
    over Nextcloud.
    """
    try:
        return str(path.relative_to(_mount_path()))
    except ValueError:
        return str(path)


def _audit_for(args, op: dict, outcome_or_reason: str, *,
               target: Target, applied: bool) -> None:
    """Write a JSONL audit entry for a runtime CLI write.

    USER.md and overlay writes are audited into the same per-user sidecar;
    an overlay entry carries `skill` and `target_path` so the two are
    distinguishable, and no `user_md_size_bytes`, because the size of the file
    that was written is not USER.md's and recording it under that key would be
    a lie the growth curves are read off.

    Channel-memory writes are not audited — CHANNEL.md has no nightly curator
    and the audit module only knows about USER.md paths.
    """
    if target.kind == _CHANNEL:
        return
    config = _config_for_audit()
    user_id = _user_id()
    user_md_path = _user_md_path()
    size = None
    extra = None
    if target.kind == _SKILL:
        extra = {"skill": target.skill, "target_path": _mount_relative(target.path)}
    elif user_md_path.exists():
        size = len(user_md_path.read_text().encode("utf-8"))
    entries = [{"op": op, "outcome": outcome_or_reason}] if applied else []
    rejects = [] if applied else [{"op": op, "reason": outcome_or_reason}]
    write_audit_log(
        config, user_id,
        applied=entries,
        rejected=rejects,
        user_md_size_bytes=size,
        source="runtime",
        extra=extra,
    )


def _update_last_seen(path: Path, text: str, target: Target) -> None:
    """Refresh the USER.md fingerprint the nightly bypass detector reads.

    Only for a USER.md write. Stamping it after an overlay or channel write
    would record a fingerprint of a file the detector never compares against,
    masking a real out-of-band edit to USER.md itself.
    """
    if target.kind != _USER:
        return
    import hashlib
    write_last_seen(
        _config_for_audit(), _user_id(),
        size_bytes=len(text.encode("utf-8")),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _lock_dir() -> Path | None:
    """Shared anchor dir for the runtime CLI.

    Use the per-user deferred dir (`ISTOTA_DEFERRED_DIR`) so the anchor is the
    same inode whether this CLI runs host-side under the skill proxy or inside
    the bwrap sandbox (the deferred dir is bind-mounted in), matching the
    nightly curator's anchor. Falls back to the system-temp default for ad-hoc
    CLI runs with no task env (no concurrent curator to coordinate with then).
    """
    deferred = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    return deferred_lock_dir(Path(deferred)) if deferred else None


def _do_op(args, op_dict: dict, *, verb: str) -> int:
    target = _resolve_target(args, verb=verb)
    if target.kind == _SKILL:
        return _do_overlay_op(args, target, op_dict)
    path = target.path
    try:
        with memory_md_lock(path, timeout_seconds=5.0, lock_dir=_lock_dir()):
            current = _read_text(path)
            doc = parse_sectioned_doc(current)
            new_doc, applied, rejected = apply_ops(doc, [op_dict])
            if rejected:
                reason = rejected[0].get("reason", "rejected")
                # For heading-related rejects, surface the existing
                # heading list so the model can self-correct.
                extras = {}
                if reason in ("heading_missing", "heading_exists"):
                    extras["available_headings"] = [s.heading for s in doc.sections]
                _audit_for(args, op_dict, reason, target=target, applied=False)
                return _err(reason, **extras)

            entry = applied[0]
            outcome = entry.get("outcome", "applied")
            if outcome == "applied":
                new_text = serialize_sectioned_doc(new_doc)
                _atomic_write(path, new_text)
                _update_last_seen(path, new_text, target)
            _audit_for(args, op_dict, outcome, target=target, applied=True)
            payload = {
                "status": "ok",
                "outcome": outcome,
                "heading": op_dict.get("heading"),
            }
            if "line" in op_dict:
                payload["line"] = op_dict["line"]
            return _emit(payload)
    except MemoryMdLocked:
        return _err("locked", path=str(path))


def _do_overlay_op(args, target: Target, op_dict: dict) -> int:
    """Parse-modify-write one per-skill overlay file.

    Two behaviours the USER.md path has no equivalent of. `append` creates the
    file (and the directory) when neither exists, because an overlay's normal
    starting state is absent rather than empty. And a `remove` that takes the
    last bullet deletes the file instead of leaving a blank one, so `ls` on the
    directory stays an honest inventory of what is customized — an empty file
    there reads as "this skill is configured" and would need `memory skills` to
    contradict it.
    """
    from istota.skills._loader import (
        OVERLAY_MAX_BYTES,
        OVERLAY_WARN_BYTES,
        overlay_effective_body,
    )

    path = target.path
    # `None` is a real value here — it means the op emptied the file and it was
    # deleted — so "no write happened" needs a value of its own.
    applied_write: str | None | object = _UNSET
    try:
        with memory_md_lock(path, timeout_seconds=5.0, lock_dir=_lock_dir()):
            current = _read_overlay_text(path)
            section = parse_overlay_doc(current)
            new_section, outcome = apply_overlay_op(section, op_dict)
            if outcome not in ("applied", "noop_dup", "noop_no_match"):
                extras = {}
                if outcome == "subheading_missing":
                    extras["available_subheadings"] = _overlay_subheadings(section)
                _audit_for(args, op_dict, outcome, target=target, applied=False)
                return _err(outcome, skill=target.skill, **extras)

            payload: dict = {
                "status": "ok",
                "outcome": outcome,
                "skill": target.skill,
            }
            if outcome == "applied":
                new_text = serialize_overlay_doc(new_section)
                size = len(new_text.encode("utf-8"))
                # Refused *before* the write, because past this bound the file
                # can no longer be read by `_read_overlay_bytes` — and `remove`
                # is the only way to shrink one. Letting the write through left
                # a file the loader ignores and this CLI can neither show nor
                # edit, recoverable only from a host shell. Reachable by one
                # oversized `--line` or by enough ordinary appends.
                if size > _MAX_OVERLAY_READ_BYTES:
                    _audit_for(args, op_dict, "overlay_would_exceed_read_cap",
                               target=target, applied=False)
                    return _err(
                        "overlay_would_exceed_read_cap",
                        skill=target.skill,
                        bytes=size,
                        cap=_MAX_OVERLAY_READ_BYTES,
                    )
                # Emptiness is the loader's own reduction, not `.strip()`: a
                # file left holding nothing but frontmatter has bytes and
                # loads as nothing, so `ls` would call it configured forever.
                if overlay_effective_body(new_text):
                    _ensure_overlay_dir()
                    _atomic_write(path, new_text)
                    payload["bytes"] = size
                    # An overlay past the loader's hard cap is not loaded at
                    # all, so a write that crosses it produces a file that is
                    # silently inert. Nothing else would say so at write time.
                    if size > OVERLAY_MAX_BYTES:
                        payload["warning"] = (
                            f"overlay is {size} bytes, over the {OVERLAY_MAX_BYTES}-byte "
                            "cap — it will not be loaded"
                        )
                    elif size > OVERLAY_WARN_BYTES:
                        payload["warning"] = (
                            f"overlay is {size} bytes, over the "
                            f"{OVERLAY_WARN_BYTES}-byte guidance"
                        )
                else:
                    path.unlink(missing_ok=True)
                    payload["removed_file"] = True
                applied_write = None if payload.get("removed_file") else new_text
            _audit_for(args, op_dict, outcome, target=target, applied=True)
            if "line" in op_dict:
                payload["line"] = op_dict["line"]
    except MemoryMdLocked:
        return _err("locked", path=str(path))

    # Outside the lock, deliberately. The bytes are already on disk and the
    # index needs no file lock, while `index_file` can reach `embed_batch` and
    # a cold sentence-transformers load — 34 MB resident, and a weights
    # download on a host with no cached copy. This CLI is spawned per write, so
    # that load is not amortized the way it is in the daemon; holding a 5s
    # `memory_md_lock` across it would fail a concurrent writer with `locked`
    # for a reason that has nothing to do with the file.
    if applied_write is not _UNSET:
        _reindex_overlay(target, applied_write)
    return _emit(payload)


def _reindex_overlay(target: Target, text: str | None) -> None:
    """Refresh the memory-search index for one overlay, or drop it.

    Without this the rule is discoverable only by reading the file. An overlay
    reaches a prompt solely on a task that selected its skill, so "why does the
    bot always do X" has no other way to find one. `memory_search.reindex_all`
    is the bulk pass and is only ever run by hand; this is what makes a write
    findable at all, and a write is exactly when someone asks whether the rule
    took.

    `text is None` means the op emptied the file and it was deleted, so the
    rows go with it. Leaving them behind would have search returning a rule the
    prompt no longer carries, which is worse than not indexing at all.

    Called from **outside** `memory_md_lock`, and see `_do_overlay_op` for why.

    Best-effort in the same shape the nightly curator uses for USER.md: a
    memory index that cannot be opened must not fail a write that already
    landed on disk. Both gates are read because either one off means the
    operator asked for no automatic indexing.
    """
    path = target.path
    config = target.config
    try:
        if not (
            config.memory_search.enabled
            and config.memory_search.auto_index_memory_files
        ):
            return
        from istota import db
        from istota.memory.search import _delete_source_chunks, index_file

        with db.get_db(config.db_path) as conn:
            if text is None:
                _delete_source_chunks(conn, _user_id(), "skill_overlay", str(path))
            else:
                index_file(conn, _user_id(), str(path), text, "skill_overlay")
    except Exception:  # noqa: BLE001 - the write already landed; indexing is best-effort
        logger.debug("skill overlay re-index failed for %s", path, exc_info=True)


def _overlay_subheadings(section) -> list[str]:
    return [
        subheading_text(line)
        for line in section.lines
        if classify_line(line) == "subheading"
    ]


def _overlay_op(args, op: dict) -> dict:
    """Rewrite a USER.md-shaped op for an overlay's flatter address space.

    An overlay has no `## ` level, so `--heading` moves down one: it names a
    `### ` subsection, which is what `--subheading` names on USER.md. Passing
    both would be two spellings of one target, so `--subheading` is refused
    rather than silently ignored or silently preferred.
    """
    if getattr(args, "subheading", None):
        _err("subheading_not_valid_with_skill", skill=args.skill)
        sys.exit(1)
    op = {k: v for k, v in op.items() if k != "heading"}
    heading = getattr(args, "heading", None)
    if heading:
        op["subheading"] = heading
    return op


def _require_heading(args) -> None:
    """USER.md and CHANNEL.md ops must name a `## ` section; overlays must not.

    Enforced here rather than by `required=True` so the refusal is the same
    JSON envelope every other refusal in this CLI is, instead of argparse's
    exit-2 usage dump on stderr — which the model, reading stdout, sees as an
    empty answer.
    """
    if not getattr(args, "heading", None):
        _err(
            "heading_required",
            hint="--heading names a `## ` section of USER.md; use --skill for an overlay",
        )
        sys.exit(1)


def _refuse_skill(args, verb: str) -> None:
    if getattr(args, "skill", None):
        _err(
            "heading_ops_not_valid_with_skill",
            skill=args.skill,
            verb=verb,
            hint=(
                "overlays have no `## ` sections — use append/remove/replace to "
                "edit one, and `show --skill` / `skills` to inspect one"
            ),
        )
        sys.exit(1)


def cmd_append(args) -> int:
    if getattr(args, "skill", None):
        return _do_op(args, _overlay_op(args, {"op": "append", "line": args.line}),
                      verb="append")
    _require_heading(args)
    op = {"op": "append", "heading": args.heading, "line": args.line}
    subheading = getattr(args, "subheading", None)
    if subheading:
        op["subheading"] = subheading
    return _do_op(args, op, verb="append")


def cmd_add_heading(args) -> int:
    _refuse_skill(args, "add-heading")
    return _do_op(
        args, {"op": "add_heading", "heading": args.heading, "lines": list(args.line)},
        verb="add-heading",
    )


def cmd_remove(args) -> int:
    if getattr(args, "skill", None):
        return _do_op(args, _overlay_op(args, {"op": "remove", "match": args.match}),
                      verb="remove")
    _require_heading(args)
    return _do_op(args, {"op": "remove", "heading": args.heading, "match": args.match},
                  verb="remove")


def cmd_replace(args) -> int:
    if getattr(args, "skill", None):
        return _do_op(
            args,
            _overlay_op(args, {"op": "replace", "match": args.match, "line": args.line}),
            verb="replace",
        )
    _require_heading(args)
    return _do_op(
        args,
        {"op": "replace", "heading": args.heading, "match": args.match, "line": args.line},
        verb="replace",
    )


def cmd_remove_heading(args) -> int:
    _refuse_skill(args, "remove-heading")
    return _do_op(args, {"op": "remove_heading", "heading": args.heading},
                  verb="remove-heading")


def cmd_show(args) -> int:
    target = _resolve_target(args, verb="show")
    if target.kind == _SKILL:
        return _show_overlay(args, target)
    text = _read_text(target.path)
    if args.heading:
        doc = parse_sectioned_doc(text)
        section = doc.find(args.heading)
        if section is None:
            return _err(
                "heading_missing",
                available_headings=[s.heading for s in doc.sections],
            )
        # Return only the section block (heading + body) using the parser
        # by re-serializing a doc that contains just this section. Keeps
        # output round-trippable.
        from istota.memory.curation.types import SectionedDoc
        sub = SectionedDoc(preamble=[], sections=[section])
        body = serialize_sectioned_doc(sub)
        print(body, end="" if body.endswith("\n") else "\n")
        return 0
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def _show_overlay(args, target: Target) -> int:
    text = _read_overlay_text(target.path)
    if not args.heading:
        print(text, end="" if text.endswith("\n") else "\n")
        return 0
    section = parse_overlay_doc(text)
    region = subsection_region_indices(section, args.heading)
    if region is None:
        return _err(
            "subheading_missing",
            skill=target.skill,
            available_subheadings=_overlay_subheadings(section),
        )
    start, end = region
    # `start` is the line after the matched `### ` line; include the heading
    # itself so the output is a self-contained block, as the USER.md path is.
    body = "\n".join(section.lines[start - 1:end])
    print(body, end="" if body.endswith("\n") else "\n")
    return 0


def cmd_headings(args) -> int:
    _refuse_skill(args, "headings")
    target = _resolve_target(args, verb="headings")
    text = _read_text(target.path)
    doc = parse_sectioned_doc(text)
    print(json.dumps(
        {"status": "ok", "headings": [s.heading for s in doc.sections]},
        ensure_ascii=False,
    ))
    return 0


def cmd_skills(args) -> int:
    """Inventory of the overlay directory: what is customized, and does it bind.

    Per-file layout is what makes this command necessary and what it pays for:
    no single `cat` shows the whole configuration, so "what have I customized,
    and is any of it live?" needs an answer of its own. `binds` is the half
    that cannot be seen from `ls` — an overlay for a misspelled, disabled or
    denylisted skill is a file that looks configured and loads into nothing.

    The gates are `_loader.inspect_overlay`, not a list restated here, because
    `doctor` asks this same question across every user's directory and the
    failure mode of two copies is that one says a file binds while the prompt
    does not contain it.
    """
    from istota.skills._loader import effective_disabled_skills

    index, config = _skill_index()
    disabled = effective_disabled_skills(config, _user_id(), index)

    d = _check_overlay_dir()
    payload: dict = {"status": "ok", "dir": _mount_relative(d), "skills": []}
    if not d.is_dir():
        return _emit(payload)

    rows: list[dict] = []
    for entry in sorted(d.glob("*.md")):
        found = inspect_overlay(
            entry,
            known_skills=index,
            disabled_skills=disabled,
            max_read_bytes=_MAX_OVERLAY_READ_BYTES,
        )
        row: dict = {"skill": found.skill, "bytes": found.size}
        # Omitted rather than zeroed when there was no body to count: printing
        # `lines: 0` for a planted symlink would describe content never read.
        if found.lines is not None:
            row["lines"] = found.lines
            row["first_line"] = (found.first_line or "")[:_FIRST_LINE_CHARS]
        row["binds"] = found.binds
        if found.reason is not None:
            row["reason"] = found.reason
        if found.warnings:
            row["warnings"] = list(found.warnings)
        rows.append(row)

    payload["skills"] = rows
    return _emit(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.memory",
        description="Runtime memory writes (USER.md / CHANNEL.md / skill overlays)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_channel_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--channel",
            help="Target /Channels/<token>/CHANNEL.md instead of USER.md.",
        )

    def _add_skill_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--skill",
            help=(
                "Target this skill's per-user overlay (config/skills/<name>.md) "
                "instead of USER.md. Overlays are flat: --heading names a "
                "`### ` subsection of the overlay, not a `## ` section."
            ),
        )

    # `--heading` is deliberately not `required=True` on the ops that accept
    # `--skill`: it is required for USER.md and CHANNEL.md and optional for an
    # overlay, which argparse cannot express, and `_require_heading` refuses in
    # the CLI's own JSON envelope rather than argparse's stderr usage dump.
    p_app = sub.add_parser("append", help="Append a bullet under an existing heading.")
    p_app.add_argument("--heading")
    p_app.add_argument("--line", required=True)
    p_app.add_argument(
        "--subheading",
        help="Append under this `### subheading` of the heading instead of the top region.",
    )
    _add_channel_flag(p_app)
    _add_skill_flag(p_app)

    p_add = sub.add_parser("add-heading", help="Add a new heading with one or more bullets.")
    p_add.add_argument("--heading", required=True)
    p_add.add_argument("--line", action="append", required=True,
                       help="Bullet line; pass multiple times for multiple bullets.")
    _add_channel_flag(p_add)
    _add_skill_flag(p_add)

    p_rm = sub.add_parser("remove", help="Remove a bullet under a heading (unique substring).")
    p_rm.add_argument("--heading")
    p_rm.add_argument("--match", required=True)
    _add_channel_flag(p_rm)
    _add_skill_flag(p_rm)

    p_rep = sub.add_parser("replace", help="Rewrite the single matching bullet in place.")
    p_rep.add_argument("--heading")
    p_rep.add_argument("--match", required=True)
    p_rep.add_argument("--line", required=True)
    _add_channel_flag(p_rep)
    _add_skill_flag(p_rep)

    p_rmh = sub.add_parser("remove-heading", help="Drop a whole `## ` section.")
    p_rmh.add_argument("--heading", required=True)
    _add_channel_flag(p_rmh)
    _add_skill_flag(p_rmh)

    p_show = sub.add_parser("show", help="Print USER.md, a CHANNEL.md, or a skill overlay (--skill), optionally filtered to one heading.")
    p_show.add_argument("--heading")
    _add_channel_flag(p_show)
    _add_skill_flag(p_show)

    p_h = sub.add_parser("headings", help="List the `## ` heading names.")
    _add_channel_flag(p_h)
    _add_skill_flag(p_h)

    sub.add_parser(
        "skills",
        help="Inventory the per-skill overlay files and whether each one binds.",
    )

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "append": cmd_append,
        "add-heading": cmd_add_heading,
        "remove": cmd_remove,
        "replace": cmd_replace,
        "remove-heading": cmd_remove_heading,
        "show": cmd_show,
        "headings": cmd_headings,
        "skills": cmd_skills,
    }
    rc = commands[args.command](args)
    if rc:
        sys.exit(rc)


if __name__ == "__main__":
    main()
