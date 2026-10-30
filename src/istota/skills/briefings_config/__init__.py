"""The caller's briefing schedule, shared briefing blocks, and group briefings.

``schedule list|set|remove`` read and change the caller's own
``briefing_configs`` rows. A write runs through
``transport.route_checks.validate_briefing_schedule``, the check the web
briefing POST applies, then ``user_briefings.ensure_briefing``. A room or
surface refusal (``RouteRefused``) answers ``reason: route_refused``; any other
refusal is ``invalid_definition``. ``remove`` needs ``--confirmed`` and leaves
the briefing's blocks and archive, as the web delete does.

``shared-block list`` renders what ``GET /shared-blocks`` lists, through
``briefings.shared_block_view``, with the content preview fenced unless the
stored value is trusted. ``shared-block show <name>`` prints one
``shared_block_configs`` definition, its whole directive, and the block's run
state from ``briefings.shared_block_runs.run_state``. ``shared-block set`` and
``set-prompt`` write in one ``BEGIN IMMEDIATE`` read-modify-write through
``validate_shared_block_payload``; ``set`` creates only with ``--create``.
``run`` and ``remove`` are the web routes' sequences; ``remove`` needs
``--confirmed``. An agent block's prompt lives only in that table, so this is
how a conversation changes it.

``group …`` reads and changes group briefing definitions, runs, editions,
the caller's own delivery mutes and their own source approvals, through
``briefings.group_service``, the functions the web group routes call. It runs
only from the caller's own turn in a private read (``_private_chat_refusal``),
which answers ``private_chat_only`` before any group id is read; then
``group_service.access`` applies the web's authority. Granting a source
approval and retrying deliveries stay on the web.

The new directive comes from a file rather than stdin because the skill proxy
forwards argv only; a proxied CLI never sees the task's stdin.

Runs host-side through the skill proxy, where the framework database is. The
identity is ``ISTOTA_USER_ID`` and the gate is ``Config.is_shared_kv_writer``,
the one the admin routes use, so a blank admins file lets nobody in. The
schedule and shared-block writes also need the caller's own turn
(``_own_turn_refusal``, ``reason: not_owner_turn``).
"""

import logging
import os
import uuid

from istota.briefings.models import TEXT_FILE_MAX_BYTES
from istota.skills._cli import error_envelope, fail, parse_and_resolve, run_skill_cli
from istota.skills._hostpath import READ, ReadRefused, host_path, read_resolved_text
from istota.skills._turn_access import calling_turn_passes

logger = logging.getLogger(__name__)


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


def _own_turn_refusal(user_id: str) -> dict | None:
    """Refuse a write on a turn somebody else wrote.

    A correspondent's admitted mail runs as the host; behind that turn's
    allowlist, it still must not change a schedule or a shared block, which reaches
    every user unfenced. An admin's own turn in a shared room passes.
    """
    if calling_turn_passes(user_id, config=_load_config()):
        return None
    return error_envelope(
        "this change can only be made on the user's own turn; the message being "
        "answered was not written by them",
        reason="not_owner_turn",
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

    refusal = _own_turn_refusal(_user_id())
    if refusal is not None:
        return refusal
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

    refusal = _own_turn_refusal(_user_id())
    if refusal is not None:
        return refusal
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


def _shared_block_timezone(config) -> str:
    return (config.briefings.shared_block_timezone or "").strip() or "UTC"


def _not_found(name: str) -> dict:
    return error_envelope(f"no shared block named {name!r}", reason="not_found")


def _block_view(conn, config, row) -> dict:
    """``GET /shared-blocks``' entry for one block, with the preview fenced.

    The preview is text an earlier run wrote from pages and mail, so it is
    fenced unless the stored value says the block is trusted.
    """
    from istota.briefings.shared_block_view import _map_shared_block, _shared_block_status
    from istota.lib.untrusted import frame_untrusted

    status = _shared_block_status(conn, row.name, _shared_block_timezone(config))
    if status["value_preview"] and not status["stored_trusted"]:
        status["value_preview"] = frame_untrusted(
            status["value_preview"], "shared block content",
        )
    return _map_shared_block(row, status)


def cmd_list(args):
    from istota import db
    from istota.briefings.models import ALLOWED_SHARED_SOURCE_KINDS, SHARED_RENDER_MODES

    config = _load_config()
    refusal = _writer_refusal(config, _user_id())
    if refusal is not None:
        return refusal

    with db.get_db(_db_path()) as conn:
        blocks = [_block_view(conn, config, row) for row in db.list_shared_block_configs(conn)]
    return {
        "status": "ok",
        "shared_blocks": blocks,
        "allowed_source_kinds": sorted(ALLOWED_SHARED_SOURCE_KINDS),
        "render_modes": list(SHARED_RENDER_MODES),
        "shared_block_timezone": config.briefings.shared_block_timezone or "UTC",
    }


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
            return _not_found(args.name)
        runs = shared_block_runs.run_state(conn, row.name)
        last_run_at = db.get_briefing_shared_block_last_run(conn, row.name)

    timezone_name = _shared_block_timezone(config)
    pending = runs["pending"]
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
            # The pending Run now request's id, to match against
            # `run_now_answered.id` once the daemon has answered it.
            "requested": pending["id"] if pending else None,
            "run_now_pending": pending,
            "run_now_answered": runs["answered"],
        },
    }


def _read_prompt(path) -> tuple[str | None, dict | None]:
    """The directive in ``--prompt-file``, or the refusal to answer instead."""
    try:
        directive = read_resolved_text(
            path, flag="--prompt-file", max_bytes=TEXT_FILE_MAX_BYTES,
        )
    except ReadRefused as exc:
        return None, error_envelope(str(exc), reason="prompt_file_unreadable")
    if not directive.strip():
        return None, error_envelope(
            "--prompt-file is empty; the directive was not changed",
            reason="empty_prompt",
        )
    return directive, None


def cmd_set_prompt(args):
    from istota import db
    from istota.briefings.models import validate_shared_block_payload

    user_id = _user_id()
    refusal = _own_turn_refusal(user_id) or _writer_refusal(_load_config(), user_id)
    if refusal is not None:
        return refusal

    directive, refusal = _read_prompt(args.prompt_file)
    if refusal is not None:
        return refusal

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


_SET_FIELDS = ("cron", "title", "render_mode", "enabled", "trusted", "sources", "prompt_file")


def cmd_set(args):
    import json

    from istota import db
    from istota.briefings.models import validate_shared_block_payload

    config = _load_config()
    user_id = _user_id()
    refusal = _own_turn_refusal(user_id) or _writer_refusal(config, user_id)
    if refusal is not None:
        return refusal

    if not args.create and all(getattr(args, f) is None for f in _SET_FIELDS):
        return error_envelope(
            "nothing to change; pass at least one of --cron, --title, "
            "--render-mode, --enabled/--disabled, --trusted/--no-trusted, "
            "--sources or --prompt-file",
            reason="invalid_argument",
        )
    sources = None
    if args.sources is not None:
        try:
            sources = json.loads(args.sources)
        except ValueError as exc:
            return error_envelope(f"--sources is not JSON: {exc}", reason="invalid_argument")
    directive = None
    if args.prompt_file is not None:
        directive, refusal = _read_prompt(args.prompt_file)
        if refusal is not None:
            return refusal

    with db.get_db(_db_path()) as conn:
        # One write transaction from read to write, so a web edit landing in
        # between is not overwritten with the values read before it.
        conn.execute("BEGIN IMMEDIATE")
        row = db.get_shared_block_config(conn, args.name)
        if row is None and not args.create:
            return error_envelope(
                f"no shared block named {args.name!r}; pass --create to make one",
                reason="not_found",
            )
        if row is not None and args.create:
            return error_envelope(
                f"a shared block named {args.name!r} already exists; drop --create "
                "to change it",
                reason="invalid_argument",
            )
        if row is None and args.cron is None:
            return error_envelope(
                "a new shared block needs --cron", reason="invalid_argument",
            )

        def pick(flag_value, stored_attr, default):
            if flag_value is not None:
                return flag_value
            return getattr(row, stored_attr) if row is not None else default

        payload = {
            "name": args.name,
            "cron": pick(args.cron, "cron", ""),
            "title": pick(args.title, "title", ""),
            "directive": pick(directive, "directive", None),
            "render_mode": pick(args.render_mode, "render_mode", "synthesis"),
            "enabled": pick(args.enabled, "enabled", True),
            "trusted": pick(args.trusted, "trusted", False),
            "sources": pick(sources, "sources", []),
        }
        try:
            fields = validate_shared_block_payload(payload)
        except ValueError as exc:
            return error_envelope(str(exc), reason="invalid_definition")
        updated = db.upsert_shared_block_config(conn, **fields)
        view = _block_view(conn, config, updated)

    _log_write("shared_block.set", user_id, updated.name)
    return {
        "status": "ok",
        "state": "created" if row is None else "updated",
        "shared_block": view,
    }


def cmd_run(args):
    from istota import db
    from istota.briefings import shared_block_runs

    config = _load_config()
    user_id = _user_id()
    refusal = _own_turn_refusal(user_id) or _writer_refusal(config, user_id)
    if refusal is not None:
        return refusal

    with db.get_db(_db_path()) as conn:
        row = db.get_shared_block_config(conn, args.name)
    if row is None:
        return _not_found(args.name)
    made = shared_block_runs.request_run(config, row.name)
    _log_write("shared_block.run", user_id, row.name)
    return {
        "status": "ok",
        "id": made["id"],
        "note": "The daemon takes the request on its next tick. Run `show` later: "
                "`run.run_now_answered.id` equal to this id is its answer.",
    }


def cmd_remove(args):
    from istota import db
    from istota.briefings import shared_block_runs
    from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE

    config = _load_config()
    user_id = _user_id()
    refusal = _own_turn_refusal(user_id) or _writer_refusal(config, user_id)
    if refusal is not None:
        return refusal

    if not args.confirmed:
        with db.get_db(_db_path()) as conn:
            row = db.get_shared_block_config(conn, args.name)
            has_content = (
                row is not None
                and db.shared_kv_get(conn, SHARED_BLOCK_NAMESPACE, row.name) is not None
            )
        if row is None:
            return _not_found(args.name)
        return error_envelope(
            "removing a shared block needs --confirmed; show the user what would "
            "be removed and ask first",
            reason="confirmation_required",
            would={
                "name": row.name, "render_mode": row.render_mode,
                "has_content": has_content, "delete_value": bool(args.delete_value),
            },
            note="Without --delete-value the last published content stays, and "
                 "briefings that read it keep using it until it goes stale.",
        )

    # The web route's sequence (`DELETE /shared-blocks/{name}`), unchanged.
    with db.get_db(_db_path()) as conn:
        removed = db.delete_shared_block_config(conn, args.name)
        if removed:
            shared_block_runs.forget(conn, args.name)
        deleted_value = False
        if args.delete_value:
            deleted_value = db.shared_kv_delete(conn, SHARED_BLOCK_NAMESPACE, args.name)
    if not removed and not deleted_value:
        return _not_found(args.name)
    _log_write("shared_block.remove", user_id, args.name)
    if not removed:
        # Content left behind by a definition removed earlier without --delete-value.
        return {"status": "ok", "removed": False, "deleted_value": True}
    return {"status": "ok", "removed": args.name, "deleted_value": deleted_value}


# -- group briefings ---------------------------------------------------------

EDITION_LABEL = "group briefing edition"
# Fixed so a publish's request id is the same on every call; never change it.
_PUBLISH_REQUEST_NAMESPACE = uuid.UUID("6f0c2b8e-4d1a-5e7b-9c3f-1a2b3c4d5e6f")
_PERSONAL_SOURCE_KINDS = ("personal_file", "personal_calendar")


def _private_chat_refusal(user_id: str) -> dict | None:
    """The first two group checks: the caller's own turn, in a private read.

    Both run before any group id is read, so the refusal says nothing about
    which groups exist.
    """
    from istota.rooms.scopes import channel_notes_shared

    def reads_privately(conn, task) -> bool:
        return not channel_notes_shared(
            conn, task.conversation_token,
            guest_turn=task.guest_participant_id is not None,
            is_group_chat=bool(task.is_group_chat),
        )

    if calling_turn_passes(user_id, reads_privately, config=_load_config()):
        return None
    return error_envelope(
        "group briefings are read and changed from the user's private chat only; "
        "ask the user to continue there",
        reason="private_chat_only",
    )


def _group(args, operation: str, body, *, read: bool) -> dict:
    """Run ``body(conn, config, actor)`` behind the gate, as the web's `_run` does.

    A read runs in one ``BEGIN`` snapshot. A write opens no transaction here:
    each store function owns its own, as on the web, and `group set` opens the
    operator CLI's ``BEGIN IMMEDIATE`` itself.
    """
    import sqlite3

    from istota import db
    from istota.groups.policy import GroupError

    actor = _user_id()
    refusal = _private_chat_refusal(actor)
    if refusal is not None:
        return refusal
    config = _load_config()
    if config is None:
        return _unavailable()
    group_id = getattr(args, "group_id", None)
    try:
        with db.get_db(_db_path()) as conn:
            if read:
                conn.execute("BEGIN")
            result = body(conn, config, actor)
    except GroupError as exc:
        logger.info("Group request actor=%s group=%r operation=%s outcome=%s",
                    actor, group_id, operation, exc.code)
        return error_envelope(str(exc), reason=exc.code, **exc.details)
    except sqlite3.Error:
        logger.warning("Group request actor=%s group=%r operation=%s outcome=unavailable",
                       actor, group_id, operation)
        return error_envelope("group briefings are unavailable", reason="groups_unavailable")
    if result.get("status") == "error":
        return result
    if not read:
        logger.info("Group request actor=%s group=%s operation=%s outcome=ok",
                    actor, group_id, operation)
    return {"status": "ok", **result}


def _fenced(view: dict) -> dict:
    """An edition with its generated text fenced; `document` repeats it, so it goes."""
    from istota.lib.untrusted import frame_untrusted

    view = dict(view)
    view.pop("document", None)
    for key in ("subject", "body_md"):
        if view.get(key):
            view[key] = frame_untrusted(view[key], EDITION_LABEL)
    return view


def _read_file_text(path, flag: str, reason: str) -> tuple[str | None, dict | None]:
    try:
        return read_resolved_text(path, flag=flag, max_bytes=TEXT_FILE_MAX_BYTES), None
    except ReadRefused as exc:
        return None, error_envelope(str(exc), reason=reason)


def cmd_group_list(args):
    from istota.briefings import group_service

    def body(conn, config, actor):
        if args.group_id is not None:
            return group_service.definitions(conn, config, actor, args.group_id)
        groups = []
        for target in group_service.targets(conn, config, actor)["targets"]:
            if target["scope"] != "group":
                continue
            listed = group_service.definitions(conn, config, actor, target["group_id"])
            groups.append({"label": target["label"], **listed})
        return {"groups": groups}

    return _group(args, "list", body, read=True)


def cmd_group_show(args):
    from istota.briefings import group_service

    def body(conn, config, actor):
        return {"definition": group_service.definitions(
            conn, config, actor, args.group_id, args.name)}

    return _group(args, "show", body, read=True)


def cmd_group_save(args):
    import json

    from istota.briefings import group_service
    from istota.groups.policy import payload_problem

    def body(conn, config, actor):
        text, refusal = _read_file_text(args.file, "--file", "invalid_argument")
        if refusal is not None:
            return refusal
        try:
            definition = json.loads(text)
        except ValueError as exc:
            return error_envelope(f"--file is not JSON: {exc}", reason="invalid_argument")
        if isinstance(definition, dict) and "revision" in definition:
            return error_envelope(
                "the file holds the definition only; pass the revision as --revision",
                reason="invalid_argument",
            )
        payload = {**definition, "revision": args.revision} if isinstance(definition, dict) else definition
        problem = payload_problem(payload, group_service.DEFINITION_FIELDS,
                                  group_service.DEFINITION_REQUIRED)
        if problem is not None:
            return error_envelope(f"--file: {problem}", reason="invalid_argument")
        name = None if args.revision == 0 else payload["name"]
        return {"definition": group_service.save(
            conn, config, actor, args.group_id, payload, name)}

    return _group(args, "save", body, read=False)


_GROUP_SET_FIELDS = ("title", "cron", "timezone", "channel", "enabled", "prompt_file",
                     "exclude", "include")


def cmd_group_set(args):
    from copy import deepcopy

    from istota.briefings import group_service
    from istota.briefings import group_store as store
    from istota.settings_plan.group_briefings import prepare_definition

    if all(getattr(args, field) is None for field in _GROUP_SET_FIELDS):
        return error_envelope(
            "nothing to change; pass at least one of --title, --cron, --timezone, "
            "--channel, --enabled/--disabled, --prompt-file, --exclude or --include",
            reason="invalid_argument",
        )

    def body(conn, config, actor):
        prompt = None
        if args.prompt_file is not None:
            prompt, refusal = _read_file_text(args.prompt_file, "--prompt-file", "prompt_file_unreadable")
            if refusal is not None:
                return refusal
            if not prompt.strip():
                return error_envelope("--prompt-file is empty; nothing was changed",
                                      reason="empty_prompt")
        # The operator CLI's sequence, in one write transaction.
        conn.execute("BEGIN IMMEDIATE")
        caps, _ = group_service.access(conn, config, actor, args.group_id, author=True)
        row = conn.execute("SELECT id FROM group_briefing_configs WHERE group_id=? AND name=?",
                           (args.group_id, args.name)).fetchone()
        old = store.get_definition(conn, row["id"]) if row else None

        patch = {}
        for field in ("title", "cron", "timezone", "channel", "enabled"):
            if getattr(args, field) is not None:
                patch[field] = getattr(args, field)
        if prompt is not None:
            if old is None:
                return error_envelope(
                    f"there is no definition named {args.name!r}; create one with `group save`",
                    reason="invalid_argument",
                )
            if old["generation_mode"] != "prompt":
                return error_envelope(
                    "this definition is built from blocks, not one prompt; change its "
                    "blocks with `group save`",
                    reason="invalid_argument",
                )
            generation = deepcopy(old["generation"])
            generation["prompt"] = prompt
            patch["generation"] = generation
        if args.exclude or args.include:
            excluded = set(old["excluded_user_ids"]) if old else set()
            excluded |= set(args.exclude or [])
            excluded -= set(args.include or [])
            patch["excluded_user_ids"] = sorted(excluded)

        value, old = prepare_definition(
            conn, config, {"group_id": args.group_id, "name": args.name, **patch},
            actor=actor, expected_revision=args.revision, is_admin=caps["author_briefings"],
        )
        definition = store.save_definition(
            conn, config, args.group_id, value, actor_id=actor, expected_revision=args.revision,
            definition_id=old["id"] if old else None, is_admin=caps["author_briefings"],
            caller_transaction=True,
        )
        return {"definition": group_service.definition_view(config, definition, caps)}

    return _group(args, "set", body, read=False)


def _publish_request_id(conn, group_id: str, name: str) -> str:
    """One publish per task and definition revision, so a model retry joins the run.

    The private-chat gate has already required a numeric ``ISTOTA_TASK_ID``.
    """
    row = conn.execute("SELECT revision FROM group_briefing_configs WHERE group_id=? AND name=?",
                       (group_id, name)).fetchone()
    revision = row["revision"] if row else 0
    task_id = os.environ["ISTOTA_TASK_ID"]
    return str(uuid.uuid5(_PUBLISH_REQUEST_NAMESPACE,
                          f"{task_id}:{group_id}:{name}:publish:{revision}"))


def cmd_group_run(args):
    from istota.briefings import group_service

    def body(conn, config, actor):
        if args.mode == "publish":
            request_id = _publish_request_id(conn, args.group_id, args.name)
        else:
            # A repeated preview delivers nothing, so each call is its own run.
            request_id = str(uuid.uuid4())
        view = group_service.runs(conn, config, actor, args.group_id, args.name,
                                  payload={"request_id": request_id, "mode": args.mode})
        return {
            "request_id": request_id,
            "edition": _fenced(view),
            "note": "The daemon generates the edition. Read it later with `group edition "
                    "--run-id` once its state is preview_ready or published.",
        }

    return _group(args, "run", body, read=False)


def cmd_group_runs(args):
    from istota.briefings import group_service

    def body(conn, config, actor):
        listed = group_service.runs(conn, config, actor, args.group_id, args.name,
                                    limit=args.limit)
        return {**listed, "items": [_fenced(item) for item in listed["items"]]}

    return _group(args, "runs", body, read=True)


def cmd_group_edition(args):
    from istota.briefings import group_service

    def body(conn, config, actor):
        view = group_service.read_edition(conn, config, actor, args.group_id, args.name,
                                          args.run_id)
        return {"edition": _fenced(view)}

    return _group(args, "edition", body, read=True)


def cmd_group_audience(args):
    from istota.briefings import group_service

    def body(conn, config, actor):
        return group_service.audience(conn, config, actor, args.group_id, args.name)

    return _group(args, "audience", body, read=True)


def cmd_group_delete(args):
    from istota.briefings import group_service

    def body(conn, config, actor):
        if args.confirmed:
            return group_service.delete(conn, config, actor, args.group_id, args.name,
                                        args.revision)
        _, definition = group_service.access(conn, config, actor, args.group_id, args.name,
                                             author=True)
        return error_envelope(
            "deleting a group briefing needs --confirmed; show the user what would be "
            "removed and ask first",
            reason="confirmation_required",
            would={"group_id": args.group_id, "name": definition["name"],
                   "enabled": definition["enabled"], "revision": definition["revision"]},
            note="Only a disabled definition can be deleted. Published editions stay "
                 "readable to members.",
        )

    return _group(args, "delete", body, read=not args.confirmed)


def cmd_group_delivery(args):
    from istota.briefings import group_service

    changes = {key: getattr(args, key) for key in group_service.PREFERENCE_FIELDS
               if getattr(args, key) is not None}

    def body(conn, config, actor):
        current = group_service.preferences(conn, config, actor, args.group_id, args.name)
        if not changes:
            return current
        merged = {key: current[key] for key in group_service.PREFERENCE_FIELDS}
        merged.update(changes)
        return group_service.preferences(conn, config, actor, args.group_id, args.name,
                                         payload=merged)

    return _group(args, "delivery", body, read=not changes)


def cmd_group_approvals(args):
    from istota.briefings import group_service
    from istota.briefings import group_store as store
    from istota.groups.policy import GroupError

    def body(conn, config, actor):
        _, definition = group_service.access(conn, config, actor, args.group_id, args.name)
        approvals = []
        for source in store.definition_sources(definition):
            if source["kind"] not in _PERSONAL_SOURCE_KINDS:
                continue
            try:
                approvals.append(group_service.approval(
                    conn, config, actor, args.group_id, args.name, source["id"]))
            except GroupError as exc:
                # The web answers this actor no approval for someone else's source.
                if exc.code != "source_not_found":
                    raise
        return {"group_id": args.group_id, "name": args.name, "approvals": approvals}

    return _group(args, "approvals", body, read=True)


def cmd_group_revoke_approval(args):
    from istota.briefings import group_service

    def body(conn, config, actor):
        if not args.confirmed:
            group_service.approval(conn, config, actor, args.group_id, args.name,
                                   args.source_id)
            return error_envelope(
                "revoking a source approval needs --confirmed; tell the user and ask first",
                reason="confirmation_required",
                would={"group_id": args.group_id, "name": args.name,
                       "source_id": args.source_id},
                note="The briefing's next run will refuse this source until it is "
                     "approved again on the web.",
            )
        return group_service.approval(
            conn, config, actor, args.group_id, args.name, args.source_id,
            payload={"expected_grant_revision": args.grant_revision}, revoke=True,
        )

    return _group(args, "revoke-approval", body, read=not args.confirmed)


def _add_group_parser(sub):
    p_group = sub.add_parser(
        "group", help="Group briefings, from the user's private chat only",
    )
    group_sub = p_group.add_subparsers(dest="action", required=True)

    def named(name, help_text, *, needs_name=True):
        parser = group_sub.add_parser(name, help=help_text)
        parser.add_argument("--group-id", required=True, help="The group's id")
        if needs_name:
            parser.add_argument("--name", required=True, help="The definition's name")
        return parser

    p_list = group_sub.add_parser("list", help="Definitions in every group you can read")
    p_list.add_argument("--group-id", help="Only this group")

    named("show", "One definition; authors also see its generation")

    p_save = named("save", "Create or replace a definition from a JSON file",
                   needs_name=False)
    host_path(
        p_save, "--file", mode=READ, required=True,
        help="A JSON file holding the definition (the web editor's body, without "
             "revision). Must live under $ISTOTA_DEFERRED_DIR or your workspace",
    )
    p_save.add_argument("--revision", type=int, required=True,
                        help="The revision you last read; 0 creates a disabled draft")

    p_gset = named("set", "Change some fields of a definition; flags not given keep their value")
    p_gset.add_argument("--revision", type=int, required=True,
                        help="The revision you last read")
    p_gset.add_argument("--title")
    p_gset.add_argument("--cron", help="5-field cron in the definition's timezone")
    p_gset.add_argument("--timezone", help="An IANA timezone name")
    p_gset.add_argument("--channel", help="in_app, email, talk or ntfy")
    genabled = p_gset.add_mutually_exclusive_group()
    genabled.add_argument("--enabled", dest="enabled", action="store_true", default=None)
    genabled.add_argument("--disabled", dest="enabled", action="store_false")
    host_path(
        p_gset, "--prompt-file", mode=READ,
        help="A UTF-8 file holding the whole new generation prompt (prompt mode "
             "only). Must live under $ISTOTA_DEFERRED_DIR or your workspace",
    )
    p_gset.add_argument("--exclude", action="append", metavar="USER",
                        help="Stop delivering to this member; repeatable")
    p_gset.add_argument("--include", action="append", metavar="USER",
                        help="Deliver to this member again; repeatable")

    p_grun = named("run", "Generate a preview, or publish to every member")
    p_grun.add_argument("--mode", required=True, choices=("preview", "publish"))

    p_runs = named("runs", "Recent runs and editions")
    p_runs.add_argument("--limit", type=int, default=20, choices=range(1, 101),
                        metavar="N", help="1 to 100, default 20")

    p_edition = named("edition", "One edition with its body")
    p_edition.add_argument("--run-id", type=int, required=True)

    named("audience", "Who the definition delivers to")

    p_gdelete = named("delete", "Delete a disabled definition")
    p_gdelete.add_argument("--revision", type=int, required=True,
                           help="The revision you last read")
    p_gdelete.add_argument("--confirmed", action="store_true",
                           help="Delete it. Without this, prints what would be deleted")

    p_delivery = named("delivery", "Read or set your own delivery mutes", needs_name=False)
    p_delivery.add_argument("--name", help="One definition; without it, the whole group")
    outbound = p_delivery.add_mutually_exclusive_group()
    outbound.add_argument("--outbound-muted", dest="outbound_muted", action="store_true",
                          default=None)
    outbound.add_argument("--outbound-unmuted", dest="outbound_muted", action="store_false")
    in_app = p_delivery.add_mutually_exclusive_group()
    in_app.add_argument("--in-app-muted", dest="in_app_muted", action="store_true",
                        default=None)
    in_app.add_argument("--in-app-unmuted", dest="in_app_muted", action="store_false")

    named("approvals", "Source approvals you can see on a definition")

    p_revoke = named("revoke-approval", "Revoke your own approval of a personal source")
    p_revoke.add_argument("--source-id", required=True)
    p_revoke.add_argument("--grant-revision", type=int, required=True,
                          help="grant_revision from `group approvals`")
    p_revoke.add_argument("--confirmed", action="store_true",
                          help="Revoke it. Without this, prints what would change")


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.briefings_config",
        description="Your briefing schedule, shared briefing blocks, and group briefings",
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

    p_block = sub.add_parser("shared-block", help="Shared briefing blocks (administrators)")
    block_sub = p_block.add_subparsers(dest="action", required=True)

    block_sub.add_parser("list", help="Every shared block, with its status")

    p_bset = block_sub.add_parser(
        "set", help="Change a shared block, or make one with --create; flags not "
                    "given keep their value",
    )
    p_bset.add_argument("name", help="The shared block's name (a slug)")
    p_bset.add_argument("--create", action="store_true",
                        help="Make a new block; refused when the name exists")
    p_bset.add_argument("--cron", help="5-field cron in the shared-block timezone; "
                                       "required with --create")
    p_bset.add_argument("--title", help="Display title")
    p_bset.add_argument("--render-mode", help="synthesis, structured or agent")
    benabled = p_bset.add_mutually_exclusive_group()
    benabled.add_argument("--enabled", dest="enabled", action="store_true", default=None)
    benabled.add_argument("--disabled", dest="enabled", action="store_false")
    btrusted = p_bset.add_mutually_exclusive_group()
    btrusted.add_argument("--trusted", dest="trusted", action="store_true", default=None)
    btrusted.add_argument("--no-trusted", dest="trusted", action="store_false")
    p_bset.add_argument(
        "--sources",
        help='JSON list replacing the sources, [{"kind": ..., "config": {...}}]; '
             "'[]' clears them",
    )
    host_path(
        p_bset, "--prompt-file", mode=READ,
        help="A UTF-8 file holding the whole new directive. Must live under "
             "$ISTOTA_DEFERRED_DIR or your workspace",
    )

    p_run = block_sub.add_parser(
        "run", help="Ask the daemon to regenerate the block on its next tick",
    )
    p_run.add_argument("name", help="The shared block's name (a slug)")

    p_bremove = block_sub.add_parser("remove", help="Delete a shared block's definition")
    p_bremove.add_argument("name", help="The shared block's name (a slug)")
    p_bremove.add_argument("--delete-value", action="store_true",
                           help="Also delete the block's last published content")
    p_bremove.add_argument(
        "--confirmed", action="store_true",
        help="Remove it. Without this, prints what would be removed",
    )

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

    _add_group_parser(sub)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parse_and_resolve(parser, argv)
    run_skill_cli(
        {
            ("schedule", "list"): cmd_schedule_list,
            ("schedule", "set"): cmd_schedule_set,
            ("schedule", "remove"): cmd_schedule_remove,
            ("shared-block", "list"): cmd_list,
            ("shared-block", "show"): cmd_show,
            ("shared-block", "set"): cmd_set,
            ("shared-block", "set-prompt"): cmd_set_prompt,
            ("shared-block", "run"): cmd_run,
            ("shared-block", "remove"): cmd_remove,
            ("group", "list"): cmd_group_list,
            ("group", "show"): cmd_group_show,
            ("group", "save"): cmd_group_save,
            ("group", "set"): cmd_group_set,
            ("group", "run"): cmd_group_run,
            ("group", "runs"): cmd_group_runs,
            ("group", "edition"): cmd_group_edition,
            ("group", "audience"): cmd_group_audience,
            ("group", "delete"): cmd_group_delete,
            ("group", "delivery"): cmd_group_delivery,
            ("group", "approvals"): cmd_group_approvals,
            ("group", "revoke-approval"): cmd_group_revoke_approval,
        },
        args,
        command=(args.command, args.action),
    )


if __name__ == "__main__":
    main()
