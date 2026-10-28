"""The caller's briefing schedule, and shared briefing blocks for a shared-content writer.

``schedule list|set|remove`` read and change the caller's own
``briefing_configs`` rows. A write runs through
``transport.route_checks.validate_briefing_schedule``, the check the web
briefing POST applies, then ``user_briefings.ensure_briefing``. A room or
surface refusal (``RouteRefused``) answers ``reason: route_refused``; any other
refusal is ``invalid_definition``. ``remove`` needs ``--confirmed`` and leaves
the briefing's blocks and archive, as the web delete does.

``shared-block show <name>`` prints one ``shared_block_configs`` definition,
its whole directive, and the block's run state from
``briefings.shared_block_runs.run_state``. ``shared-block set-prompt <name>
--prompt-file PATH`` replaces the directive and nothing else. An agent block's
prompt lives only in that table, so this is how a conversation changes it.

The new directive comes from a file rather than stdin because the skill proxy
forwards argv only; a proxied CLI never sees the task's stdin.

Runs host-side through the skill proxy, where the framework database is. The
identity is ``ISTOTA_USER_ID`` and the gate is ``Config.is_shared_kv_writer``,
the one the admin routes use, so a blank admins file lets nobody in.
"""

import logging
import os

from istota.skills._cli import error_envelope, fail, parse_and_resolve, run_skill_cli
from istota.skills._hostpath import READ, ReadRefused, host_path, read_resolved_text

logger = logging.getLogger(__name__)

# Far above any real task prompt; bounds what one call reads into memory.
MAX_PROMPT_FILE_BYTES = 256 * 1024


def _user_id() -> str:
    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        fail("ISTOTA_USER_ID not set")
    return user_id


def _db_path() -> str:
    path = os.environ.get("ISTOTA_DB_PATH", "")
    if not path:
        fail("ISTOTA_DB_PATH not set")
    return path


def _load_config():
    """The deployment's config, or None when it cannot be loaded.

    The same load `skills/kv` makes for its shared-write gate; None makes the
    gate refuse.
    """
    try:
        from pathlib import Path

        from istota.config import load_config

        cfg_path = os.environ.get("ISTOTA_CONFIG_PATH", "")
        return load_config(Path(cfg_path) if cfg_path else None)
    except Exception:  # noqa: BLE001
        return None


def _writer_refusal(config, user_id: str) -> dict | None:
    if config is not None and config.is_shared_kv_writer(user_id):
        return None
    return error_envelope(
        "shared briefing blocks can only be read or changed by an administrator",
        reason="not_a_shared_content_writer",
    )


def _log_write(action: str, user_id: str, target: str) -> None:
    logger.info("briefing_skill action=%s user=%s target=%s", action, user_id, target)


def _unavailable() -> dict:
    # The route checks read rooms and surfaces from the config; without it they
    # check nothing, so the schedule verbs refuse rather than run unchecked.
    return error_envelope(
        "the deployment's configuration could not be loaded", reason="unavailable",
    )


def _next_run(cron: str, timezone_name: str) -> str | None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from croniter import croniter

    try:
        tz = ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    # Naive wall-clock time, as `scheduler.check_briefings` evaluates the cron.
    now = datetime.now(tz).replace(tzinfo=None)
    try:
        fire = croniter(cron, now).get_next(datetime)
    except Exception:  # noqa: BLE001
        return None
    return fire.replace(tzinfo=tz).isoformat()


def _schedule_view(config, briefing, timezone_name: str) -> dict:
    from istota.user_briefings import hold_view

    held = hold_view(config, briefing.admin_hold_at, briefing.admin_hold_by)
    runs = briefing.enabled and held is None
    return {
        "id": briefing.id,
        "name": briefing.name,
        "title": briefing.title,
        "cron": briefing.cron,
        "timezone": timezone_name,
        "output": briefing.output,
        "conversation_token": briefing.conversation_token,
        "enabled": briefing.enabled,
        "held": held,
        "next_run": _next_run(briefing.cron, timezone_name) if runs else None,
    }


def cmd_schedule_list(args):
    from istota import user_briefings

    config = _load_config()
    if config is None:
        return _unavailable()
    user_id = _user_id()
    timezone_name = config.resolve_user_timezone(user_id)
    rows = user_briefings.list_briefings(_db_path(), user_id)
    return {
        "status": "ok",
        "timezone": timezone_name,
        "briefings": [_schedule_view(config, row, timezone_name) for row in rows],
    }


def cmd_schedule_set(args):
    from istota import user_briefings
    from istota.transport.route_checks import RouteRefused, validate_briefing_schedule

    config = _load_config()
    if config is None:
        return _unavailable()
    user_id = _user_id()
    db_path = _db_path()
    name = (args.name or "").strip()

    stored = user_briefings.get_briefing(db_path, user_id, name)
    if stored is None and args.cron is None:
        return error_envelope(
            f"there is no briefing named {name!r}; a new briefing needs --cron",
            reason="invalid_argument",
        )

    def pick(flag_value, stored_attr, default):
        if flag_value is not None:
            return flag_value
        return getattr(stored, stored_attr) if stored is not None else default

    payload = {
        "name": name,
        "cron": pick(args.cron, "cron", ""),
        "title": pick(args.title, "title", ""),
        "output": pick(args.output, "output", "talk"),
        "conversation_token": pick(args.conversation_token, "conversation_token", ""),
        "enabled": pick(args.enabled, "enabled", True),
    }
    try:
        cleaned = validate_briefing_schedule(
            config, payload, user_id=user_id, stored=stored, name_required=True,
        )
    except RouteRefused as exc:
        return error_envelope(str(exc), reason="route_refused")
    except ValueError as exc:
        return error_envelope(str(exc), reason="invalid_definition")

    try:
        briefing, state = user_briefings.ensure_briefing(
            db_path, user_id=user_id,
            # The schedule verbs do not edit components; keep what is stored.
            components=stored.components if stored is not None else None,
            **cleaned,
        )
    except ValueError as exc:
        return error_envelope(str(exc), reason="invalid_definition")

    if state != "noop":
        _log_write("schedule.set", user_id, briefing.name)
    timezone_name = config.resolve_user_timezone(user_id)
    return {
        "status": "ok",
        "state": state,
        "briefing": _schedule_view(config, briefing, timezone_name),
    }


def cmd_schedule_remove(args):
    from istota import user_briefings

    config = _load_config()
    if config is None:
        return _unavailable()
    user_id = _user_id()
    db_path = _db_path()
    name = (args.name or "").strip()

    stored = user_briefings.get_briefing(db_path, user_id, name)
    if stored is None:
        return error_envelope(f"no briefing named {name!r}", reason="not_found")
    if not args.confirmed:
        return error_envelope(
            "removing a briefing needs --confirmed; show the user what would be "
            "removed and ask first",
            reason="confirmation_required",
            would={"name": stored.name, "cron": stored.cron, "output": stored.output},
            note="Only the schedule is removed. The briefing's blocks and its "
                 "archive stay.",
        )
    if not user_briefings.delete_briefing(db_path, user_id, name):
        return error_envelope(f"no briefing named {name!r}", reason="not_found")
    _log_write("schedule.remove", user_id, name)
    return {"status": "ok", "removed": name}


def cmd_show(args):
    from istota import db
    from istota.briefings import shared_block_runs

    config = _load_config()
    refusal = _writer_refusal(config, _user_id())
    if refusal is not None:
        return refusal

    with db.get_db(_db_path()) as conn:
        row = db.get_shared_block_config(conn, args.name)
        if row is None:
            return error_envelope(
                f"no shared block named {args.name!r}", reason="not_found",
            )
        runs = shared_block_runs.run_state(conn, row.name)
        last_run_at = db.get_briefing_shared_block_last_run(conn, row.name)

    timezone_name = (config.briefings.shared_block_timezone or "").strip() or "UTC"
    return {
        "status": "ok",
        "shared_block": {
            "name": row.name,
            "title": row.title,
            "cron": row.cron,
            "timezone": timezone_name,
            "render_mode": row.render_mode,
            "enabled": row.enabled,
            "trusted": row.trusted,
            "sources": row.sources,
            "directive": row.directive or "",
            "updated_at": row.updated_at,
        },
        "run": {
            # Wall-clock time in `timezone`, the clock the cron runs on.
            "last_cron_run_at": last_run_at,
            "agent": runs["agent"],
            "run_now_pending": runs["pending"],
            "run_now_answered": runs["answered"],
        },
    }


def cmd_set_prompt(args):
    from istota import db
    from istota.briefings.models import validate_shared_block_payload

    refusal = _writer_refusal(_load_config(), _user_id())
    if refusal is not None:
        return refusal

    try:
        directive = read_resolved_text(
            args.prompt_file, flag="--prompt-file", max_bytes=MAX_PROMPT_FILE_BYTES,
        )
    except ReadRefused as exc:
        return error_envelope(str(exc), reason="prompt_file_unreadable")
    if not directive.strip():
        return error_envelope(
            "--prompt-file is empty; the directive was not changed",
            reason="empty_prompt",
        )

    with db.get_db(_db_path()) as conn:
        # One write transaction from read to write, so a web edit landing in
        # between is not overwritten with the values read before it.
        conn.execute("BEGIN IMMEDIATE")
        row = db.get_shared_block_config(conn, args.name)
        if row is None:
            return error_envelope(
                f"no shared block named {args.name!r}", reason="not_found",
            )
        try:
            validate_shared_block_payload({
                "name": row.name, "cron": row.cron, "title": row.title,
                "directive": directive, "render_mode": row.render_mode,
                "enabled": row.enabled, "trusted": row.trusted,
                "sources": row.sources,
            })
        except ValueError as exc:
            return error_envelope(
                f"the block would not be valid with this prompt: {exc}",
                reason="invalid_definition",
            )
        updated = db.upsert_shared_block_config(
            conn, name=row.name, cron=row.cron, title=row.title,
            directive=directive, render_mode=row.render_mode,
            enabled=row.enabled, trusted=row.trusted, sources=row.sources,
        )

    return {
        "status": "ok",
        "name": updated.name,
        "render_mode": updated.render_mode,
        "directive_chars": len(directive),
        "previous_directive_chars": len(row.directive or ""),
        "updated_at": updated.updated_at,
        "note": "The block's next run uses the new prompt.",
    }


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.briefings_config",
        description="Your briefing schedule, and shared briefing blocks",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_schedule = sub.add_parser("schedule", help="Your own briefing schedule")
    schedule_sub = p_schedule.add_subparsers(dest="action", required=True)

    schedule_sub.add_parser("list", help="Your briefings: cron, delivery, state")

    p_sset = schedule_sub.add_parser(
        "set", help="Create a briefing or change one; flags not given keep their value",
    )
    p_sset.add_argument("--name", required=True, help="The briefing's name")
    p_sset.add_argument("--cron", help="5-field cron in your timezone; required for a new name")
    p_sset.add_argument("--title", help="Display title; an empty string derives it from the name")
    p_sset.add_argument("--output", help="Delivery descriptor: talk, email, ntfy, or a comma list")
    p_sset.add_argument("--conversation-token", help="The Talk room for a talk output")
    enabled = p_sset.add_mutually_exclusive_group()
    enabled.add_argument("--enabled", dest="enabled", action="store_true", default=None)
    enabled.add_argument("--disabled", dest="enabled", action="store_false")

    p_sremove = schedule_sub.add_parser(
        "remove", help="Remove a briefing's schedule; blocks and archive stay",
    )
    p_sremove.add_argument("--name", required=True, help="The briefing's name")
    p_sremove.add_argument(
        "--confirmed", action="store_true",
        help="Remove it. Without this, prints what would be removed",
    )

    p_block = sub.add_parser("shared-block", help="One shared briefing block")
    block_sub = p_block.add_subparsers(dest="action", required=True)

    p_show = block_sub.add_parser(
        "show", help="The definition, its whole directive, and its last run",
    )
    p_show.add_argument("name", help="The shared block's name (a slug)")

    p_set = block_sub.add_parser(
        "set-prompt", help="Replace the directive with a file's contents",
    )
    p_set.add_argument("name", help="The shared block's name (a slug)")
    host_path(
        p_set, "--prompt-file", mode=READ, required=True,
        help="A UTF-8 file holding the whole new directive. Must live under "
             "$ISTOTA_DEFERRED_DIR or your workspace",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parse_and_resolve(parser, argv)
    run_skill_cli(
        {
            ("schedule", "list"): cmd_schedule_list,
            ("schedule", "set"): cmd_schedule_set,
            ("schedule", "remove"): cmd_schedule_remove,
            ("shared-block", "show"): cmd_show,
            ("shared-block", "set-prompt"): cmd_set_prompt,
        },
        args,
        command=(args.command, args.action),
    )


if __name__ == "__main__":
    main()
