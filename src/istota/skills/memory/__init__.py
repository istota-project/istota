"""Memory skill CLI — runtime writes to USER.md / CHANNEL.md.

Single write path through the curation ops engine (`apply_ops`). Used by
the always-included memory skill so durable memory writes don't bypass
heading routing, dedup, or the audit log.

Subcommands:
  append      Append a bullet under an existing `## heading`.
  add-heading Add a new `## heading` with one or more bullets.
  remove      Remove a bullet (substring match, must be unique).
  show        Print the current contents of USER.md (or one section).
  headings    List the `## ` heading names in order.

Each write subcommand can target the channel memory file by passing
`--channel TOKEN`. The TOKEN is validated against `ISTOTA_CONVERSATION_TOKEN`
when set, to refuse cross-channel writes from a runtime task that's
been scoped to a different conversation.

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
import os
import sys
from pathlib import Path

from istota.memory.curation.audit import (
    get_user_md_last_seen_path,
    write_audit_log,
    write_last_seen,
)
from istota.memory.curation.file_lock import MemoryMdLocked, memory_md_lock
from istota.memory.curation.ops import apply_ops
from istota.memory.curation.parser import (
    parse_sectioned_doc,
    serialize_sectioned_doc,
)


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
    # Fallback: find a single bot dir under /Users/{user_id}/. The exec
    # path always sets ISTOTA_BOT_DIR_NAME, so this is a defensive
    # fallback for ad-hoc CLI use.
    user_id = _user_id()
    base = _mount_path() / "Users" / user_id
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "config" / "USER.md").is_file():
                return child.name
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
    if env_token and env_token != token:
        _err(
            "channel token mismatch — refusing cross-channel write",
            given=token, expected=env_token,
        )
        sys.exit(1)
    return _mount_path() / "Channels" / token / "CHANNEL.md"


def _resolve_target(args) -> tuple[Path, bool]:
    """Return `(path, is_channel)`."""
    token = getattr(args, "channel", None)
    if token:
        return _channel_md_path(token), True
    return _user_md_path(), False


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


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _audit_for(args, op: dict, outcome_or_reason: str, *,
               user_md_path: Path, is_channel: bool, applied: bool) -> None:
    """Write a JSONL audit entry for runtime CLI writes against USER.md.

    Channel-memory writes are not currently audited — CHANNEL.md has no
    nightly curator and the audit module only knows about USER.md paths.
    """
    if is_channel:
        return
    config = _config_for_audit()
    user_id = _user_id()
    if applied:
        write_audit_log(
            config, user_id,
            applied=[{"op": op, "outcome": outcome_or_reason}],
            rejected=[],
            user_md_size_bytes=len(user_md_path.read_text().encode("utf-8"))
            if user_md_path.exists() else None,
            source="runtime",
        )
    else:
        write_audit_log(
            config, user_id,
            applied=[],
            rejected=[{"op": op, "reason": outcome_or_reason}],
            user_md_size_bytes=len(user_md_path.read_text().encode("utf-8"))
            if user_md_path.exists() else None,
            source="runtime",
        )


def _update_last_seen(path: Path, text: str, is_channel: bool) -> None:
    if is_channel:
        return
    import hashlib
    write_last_seen(
        _config_for_audit(), _user_id(),
        size_bytes=len(text.encode("utf-8")),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _do_op(args, op_dict: dict) -> int:
    path, is_channel = _resolve_target(args)
    try:
        with memory_md_lock(path, timeout_seconds=5.0):
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
                _audit_for(args, op_dict, reason,
                           user_md_path=path, is_channel=is_channel, applied=False)
                return _err(reason, **extras)

            entry = applied[0]
            outcome = entry.get("outcome", "applied")
            if outcome == "applied":
                new_text = serialize_sectioned_doc(new_doc)
                _atomic_write(path, new_text)
                _update_last_seen(path, new_text, is_channel)
            _audit_for(args, op_dict, outcome,
                       user_md_path=path, is_channel=is_channel, applied=True)
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


def cmd_append(args) -> int:
    return _do_op(args, {"op": "append", "heading": args.heading, "line": args.line})


def cmd_add_heading(args) -> int:
    return _do_op(
        args, {"op": "add_heading", "heading": args.heading, "lines": list(args.line)}
    )


def cmd_remove(args) -> int:
    return _do_op(args, {"op": "remove", "heading": args.heading, "match": args.match})


def cmd_show(args) -> int:
    path, _is_channel = _resolve_target(args)
    text = _read_text(path)
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


def cmd_headings(args) -> int:
    path, _ = _resolve_target(args)
    text = _read_text(path)
    doc = parse_sectioned_doc(text)
    print(json.dumps(
        {"status": "ok", "headings": [s.heading for s in doc.sections]},
        ensure_ascii=False,
    ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.memory",
        description="Runtime memory writes (USER.md / CHANNEL.md)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_channel_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--channel",
            help="Target /Channels/<token>/CHANNEL.md instead of USER.md.",
        )

    p_app = sub.add_parser("append", help="Append a bullet under an existing heading.")
    p_app.add_argument("--heading", required=True)
    p_app.add_argument("--line", required=True)
    _add_channel_flag(p_app)

    p_add = sub.add_parser("add-heading", help="Add a new heading with one or more bullets.")
    p_add.add_argument("--heading", required=True)
    p_add.add_argument("--line", action="append", required=True,
                       help="Bullet line; pass multiple times for multiple bullets.")
    _add_channel_flag(p_add)

    p_rm = sub.add_parser("remove", help="Remove a bullet under a heading (unique substring).")
    p_rm.add_argument("--heading", required=True)
    p_rm.add_argument("--match", required=True)
    _add_channel_flag(p_rm)

    p_show = sub.add_parser("show", help="Print USER.md (optionally filtered to one heading).")
    p_show.add_argument("--heading")
    _add_channel_flag(p_show)

    p_h = sub.add_parser("headings", help="List the `## ` heading names.")
    _add_channel_flag(p_h)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "append": cmd_append,
        "add-heading": cmd_add_heading,
        "remove": cmd_remove,
        "show": cmd_show,
        "headings": cmd_headings,
    }
    rc = commands[args.command](args)
    if rc:
        sys.exit(rc)


if __name__ == "__main__":
    main()
