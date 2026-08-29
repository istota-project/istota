"""Skills loader CLI — on-demand skill-body loader (the menu pull-in path).

``istota-skill skills show <name>`` prints the fully rendered documentation for
a menu skill (a skill not selected eager, so its body was left out of the
prompt as a one-line "load on demand" entry).
``istota-skill skills list`` enumerates the skills the caller is allowed to
load. Both re-apply the same guards the selection path enforces
(``disabled_skills`` instance + per-user, ``admin_only`` vs the caller's admin
status, the ``skill_<name>`` experimental gate, and unmet Python dependencies)
so a deferred body can never be used to bypass them. There is intentionally no
resource gate, matching ``eligible_skill_names`` (no bundled skill declares
``resource_types`` now; the former holdouts were doc-only conventions).

Invoked server-side by the skill proxy (or directly when the proxy is off), so
``load_config()`` and the admins file are reachable here.
"""

import argparse
import json
import os
import sys


def _output_error(msg: str, **extra) -> None:
    print(json.dumps({"status": "error", "error": msg, **extra}, ensure_ascii=False))
    sys.exit(1)


def _load_context():
    """Resolve (config, user_id, skill_index, disabled, is_admin, enabled_features).

    Returns a dict, or calls _output_error and exits on a fatal setup problem.
    """
    from istota.config import load_config
    from istota.experimental import enabled_features_from_env
    from istota.skills._loader import effective_disabled_skills, load_skill_index

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        _output_error("ISTOTA_USER_ID not set")

    config = load_config()
    skill_index = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)

    # Instance + per-user disabled, plus the capability gate (browse→browser,
    # devbox→devbox, …). Shared with the executor so `skills list` / `skills
    # show` agree with the menu the model was shown.
    disabled = effective_disabled_skills(config, user_id, skill_index)

    is_admin = config.is_admin(user_id)

    # The propagated env var is authoritative for the subprocess; fall back to
    # the loaded config for the direct (proxy-off) path where it may be unset.
    enabled_features = enabled_features_from_env()
    if not enabled_features and config.experimental.features:
        enabled_features = frozenset(config.experimental.features)

    return {
        "config": config,
        "user_id": user_id,
        "skill_index": skill_index,
        "disabled": disabled,
        "is_admin": is_admin,
        "enabled_features": enabled_features,
    }


def _guard_skill(name: str, ctx: dict) -> str | None:
    """Return an error message if the caller may not load ``name``, else None."""
    from istota.skills._loader import _check_dependencies

    skill_index = ctx["skill_index"]
    meta = skill_index.get(name)
    if meta is None:
        return f"unknown skill: {name!r}"
    if name in ctx["disabled"]:
        return f"skill {name!r} is disabled"
    if meta.admin_only and not ctx["is_admin"]:
        return f"skill {name!r} is restricted to admins"
    if meta.experimental and f"skill_{name}" not in ctx["enabled_features"]:
        return f"skill {name!r} is not available"
    if not _check_dependencies(meta):
        return f"skill {name!r} is unavailable (missing dependencies)"
    # No resource gate — matches eligible_skill_names. No bundled skill declares
    # resource_types now; the former holdouts (notes/spec/todos) were doc-only
    # conventions with sensible defaults.
    return None


def _scripts_dir(config, user_id: str) -> str:
    from istota.storage import get_user_scripts_path

    scripts_nc_path = get_user_scripts_path(user_id, config.bot_dir_name)
    if config.use_mount and config.nextcloud_mount_path is not None:
        return str(config.nextcloud_mount_path / scripts_nc_path.lstrip("/"))
    return f"{config.rclone_remote}:{scripts_nc_path}"


def _workspace_dir(config, user_id: str) -> str:
    from istota.storage import get_user_base_path

    base = get_user_base_path(user_id)
    if config.use_mount and config.nextcloud_mount_path is not None:
        return str(config.nextcloud_mount_path / base.lstrip("/"))
    return f"{config.rclone_remote}:{base}"


def _overlay_dir(config, user_id: str):
    """The user's per-skill overlay directory, or None.

    One line, and deliberately not its own derivation: `executor` resolves the
    same directory for the eager path, and the two agreeing is what keeps an
    overlay from applying on one path and not the other.
    """
    from istota.storage import resolve_user_skill_overlays_dir

    return resolve_user_skill_overlays_dir(config, user_id)


def _render_companion_body(config, name: str, meta) -> str | None:
    """Render one companion skill's body (frontmatter stripped, placeholders
    substituted) WITHOUT the ``## Skills Reference`` wrapper — it rides under a
    delimiter beneath the primary skill. Returns None if the doc can't be read.

    Deliberately no per-skill user overlay here: a companion rides as a
    guardrail, not as the skill the user is configuring. An overlay for a skill
    currently being rendered as a companion still applies when that skill is
    loaded as the primary one.
    """
    from istota.skills._loader import _resolve_skill_doc_path, _strip_frontmatter

    doc_path = _resolve_skill_doc_path(name, meta, config.skills_dir, config.bundled_skills_dir)
    if doc_path is None:
        return None
    try:
        content = _strip_frontmatter(doc_path.read_text()).strip()
    except OSError:
        return None
    return content.replace("{BOT_NAME}", config.bot_name).replace("{BOT_DIR}", config.bot_dir_name)


def cmd_show(args) -> None:
    import logging

    from istota.skills._loader import expand_companions, load_skills

    ctx = _load_context()
    name = args.name
    err = _guard_skill(name, ctx)
    if err:
        _output_error(err)

    config = ctx["config"]
    skill_index = ctx["skill_index"]
    body = load_skills(
        config.skills_dir,
        [name],
        config.bot_name,
        config.bot_dir_name,
        skill_index=skill_index,
        bundled_dir=config.bundled_skills_dir,
        user_overlay_dir=_overlay_dir(config, ctx["user_id"]),
    )
    if not body:
        _output_error(f"no documentation found for skill {name!r}")

    # Append companion bodies (e.g. untrusted_input for an ingest skill) so a
    # menu-pulled skill always arrives with its guardrails in the SAME response
    # — companions are not optional and not at the model's discretion. Gate
    # filtering goes through the shared expand_companions so it can't drift from
    # selection-time companion expansion.
    parts = [body]
    meta = skill_index.get(name)
    declared = list(meta.companion_skills) if meta else []
    if declared:
        loadable = set(expand_companions(
            [name], skill_index,
            is_admin=ctx["is_admin"],
            disabled_skills=ctx["disabled"],
            enabled_experimental_features=ctx["enabled_features"],
        ))
        log = logging.getLogger("istota.skills")
        for comp in declared:
            if comp not in loadable:
                # Gated off (disabled/admin/experimental/deps) or missing from
                # the index. Never silently drop — emit a visible marker; a
                # missing safety companion is a config error.
                log.warning("skills show %s: companion %s unavailable", name, comp)
                parts.append(f"\n\n---\n<!-- companion {comp}: unavailable -->")
                continue
            cbody = _render_companion_body(config, comp, skill_index.get(comp))
            if not cbody:
                log.warning("skills show %s: companion %s body unreadable", name, comp)
                parts.append(f"\n\n---\n<!-- companion {comp}: unavailable -->")
                continue
            parts.append(f"\n\n---\n<!-- companion: {comp} -->\n\n{cbody}")

    out = "".join(parts)
    out = out.replace("{scripts_dir}", _scripts_dir(config, ctx["user_id"]))
    out = out.replace("{workspace}", _workspace_dir(config, ctx["user_id"]))
    out = out.replace("{storage}", config.storage_label)
    out = out.replace("{user_id}", ctx["user_id"])
    print(out)


def cmd_list(args) -> None:
    ctx = _load_context()
    skill_index = ctx["skill_index"]
    skills = []
    for name in sorted(skill_index):
        if _guard_skill(name, ctx) is not None:
            continue
        meta = skill_index[name]
        skills.append({
            "name": name,
            "description": meta.description,
            "cli": meta.cli,
        })
    print(json.dumps({"status": "ok", "skills": skills}, indent=2, ensure_ascii=False))


#: How much of an overlay's first line the inventory shows.
_FIRST_LINE_CHARS = 120


def _mount_relative(ctx, path):
    """Render an overlay path relative to the mount, or None.

    The rest of this CLI keeps daemon-side absolute paths out of its output,
    and the surface this inventory replaces did the same. It matters slightly
    more here than as a house rule: the path handed in is the *resolved* one,
    so printing it verbatim tells the model where a symlinked `config/`
    actually lands on the host.
    """
    if path is None:
        return None
    mount = getattr(ctx["config"], "nextcloud_mount_path", None)
    if mount is None:
        return str(path)
    # Both sides resolved: `path` arrives resolved from `contained_overlay_dir`,
    # and the mount itself is reached through a symlink on some hosts, so
    # comparing a resolved path to an unresolved root reads as "outside" and
    # falls through to printing the absolute path this function exists to hide.
    from pathlib import Path

    try:
        return str(Path(os.path.realpath(path)).relative_to(os.path.realpath(mount)))
    except (ValueError, OSError):
        return str(path)


def _resolve_overlay_dir(ctx):
    """The user's overlay directory, or refuse.

    `resolve_user_skill_overlays_dir` **degrades** to None — right for the
    loader, whose worst case is a prompt without a customization, and wrong
    here, where the caller asked a direct question and "no overlays" would be
    a false answer to it. So the two None cases are separated: no mount is a
    fact about the deployment and reports as an empty inventory, while a
    directory that resolves outside the user's own tree is a planted symlink
    and is named as one.

    Containment itself is not re-derived. `contained_overlay_dir` behind that
    resolver is the same rule the loader and the search reindex apply, and it
    returns the **resolved** path so a caller cannot re-walk by the unresolved
    name after the check — which matters most here, since this CLI runs
    host-side under the skill proxy with the daemon's filesystem view while
    `{mount}/Users/{user_id}` is bound read-write into that user's sandbox.
    Every component of the path is model-plantable and the bytes go straight
    back to the model.
    """
    config = ctx["config"]
    if not config.use_mount:
        return None
    d = _overlay_dir(config, ctx["user_id"])
    if d is None:
        _output_error("overlay_dir_outside_user_tree")
    return d


def _overlay_path(name: str, ctx):
    """`(path, overlay_dir)` for one skill's overlay, or refuse.

    The name is checked against the skill index and nothing else. It is a
    caller-supplied path component and the index is the whole of what bounds
    it — there is nothing else to check `develper` or `../USER` against — so
    the check is not about being helpful, though the known names come back
    with the error because it is cheap.

    Index membership, not `_guard_skill`: a disabled or admin-only skill can
    still have a file filed under its name, and a surface that refused to
    mention it would be the one place nothing reports it. `binds` is where
    that shows up instead, with a reason.

    The denylist is not applied. It stopped text going *in*, and reading adds
    nothing; a file hand-planted in the `sensitive_actions` slot has to stay
    visible or nothing reports it either.
    """
    index = ctx["skill_index"]
    if name not in index:
        _output_error(
            f"unknown skill: {name!r}",
            available_skills=sorted(index),
            hint="no overlay binds to this name",
        )
    # Index membership is the bound, but an index *key* is not guaranteed to be
    # a plain filename component: `_discover_directory_skills` keys on the
    # frontmatter `name`, so an operator override declaring `name: ../../USER`
    # would pass the check above and then escape the join below. Not
    # model-reachable — `config/` is bound into no sandbox — and the retired
    # `_skill_overlay_path` carried the same gap, so this closes a hole the
    # move inherited rather than one it opened.
    from pathlib import Path

    if name != Path(name).name or name in ("", ".", ".."):
        _output_error(f"skill name is not a filename component: {name!r}")
    d = _resolve_overlay_dir(ctx)
    if d is None:
        return None, None
    return d / f"{name}.md", d


def cmd_overlay(args) -> None:
    """Print one skill's per-user overlay, whole.

    Whole, with no heading filter (ISSUE-343). The filter existed to support
    targeted editing — find the subsection you are about to append a bullet
    to — and there are no bullet ops any more. An overlay is capped well below
    the point where reading it entire is a cost, and it is a document with
    prose and fenced blocks in it, so slicing one at a `### ` was never a good
    way to read it.
    """
    from istota.skills._loader import OVERLAY_READ_CAP_BYTES, read_overlay_bytes

    ctx = _load_context()
    path, _d = _overlay_path(args.name, ctx)
    if path is None:
        # No mount, so this deployment has no overlays for anyone. Printing an
        # empty body would answer "this skill has no overlay", which is a
        # different and false claim.
        _output_error("no_overlay_storage", skill=args.name)
    raw, refusal, _size = read_overlay_bytes(path, max_bytes=OVERLAY_READ_CAP_BYTES)
    if refusal is not None:
        # A refusal reaches the caller rather than degrading to "absent". The
        # loader can treat a planted inode as an inert customization; here it
        # is the answer to the question that was asked, and reporting it as an
        # empty file would hide the one thing worth saying about that path.
        _output_error(refusal, skill=args.name)
    # An absent file is `(b"", None, None)` from the reader, not a refusal — a
    # skill with no overlay is the normal case and prints nothing.
    if not raw:
        print("")
        return
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _output_error("overlay_not_utf8", skill=args.name)
    print(text, end="" if text.endswith("\n") else "\n")


def cmd_overlays(args) -> None:
    """Inventory the overlay directory: what is customized, and does it bind.

    Per-file layout is what makes this necessary: no single `cat` shows the
    whole configuration, so "what have I customized, and is any of it live?"
    needs an answer of its own. `binds` is the half that cannot be seen from
    `ls` — an overlay for a misspelled, disabled or denylisted skill is a file
    that looks configured and loads into nothing.

    With the write verbs gone this is also the gate itself. Verify-after-write
    beats refuse-at-write for an overlay, because binding is a property of the
    file rather than of the write: one command catches a `develper.md` typo, an
    over-cap file, a denylisted slot and a frontmatter-only file, where the
    write-time refusal only ever caught the first.

    The gates are `_loader.inspect_overlay`, not a list restated here, because
    `doctor` asks this same question across every user's directory and the
    failure mode of two copies is that one says a file binds while the prompt
    does not contain it.
    """
    from istota.skills._loader import OVERLAY_READ_CAP_BYTES, inspect_overlay

    ctx = _load_context()
    d = _resolve_overlay_dir(ctx)
    payload: dict = {"status": "ok", "dir": _mount_relative(ctx, d), "skills": []}
    if d is None or not d.is_dir():
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    rows: list[dict] = []
    for entry in sorted(d.glob("*.md")):
        found = inspect_overlay(
            entry,
            known_skills=ctx["skill_index"],
            disabled_skills=ctx["disabled"],
            max_read_bytes=OVERLAY_READ_CAP_BYTES,
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
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skills", description="On-demand skill documentation loader")
    sub = parser.add_subparsers(dest="command")

    show = sub.add_parser("show", help="Print full instructions for a skill")
    show.add_argument("name", help="Skill name")

    sub.add_parser("list", help="List skills you can load")

    overlay = sub.add_parser(
        "overlay", help="Print your own additions to one skill's instructions"
    )
    overlay.add_argument("name", help="Skill name")

    sub.add_parser(
        "overlays",
        help="Inventory your per-skill additions, and whether each one loads",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "show":
        cmd_show(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "overlay":
        cmd_overlay(args)
    elif args.command == "overlays":
        cmd_overlays(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
