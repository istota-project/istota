"""Shared briefing blocks, read from chat by a shared-content writer.

``shared-block show <name>`` prints one ``shared_block_configs`` definition,
its whole directive, and the block's run state from
``briefings.shared_block_runs.run_state``. An agent block's prompt
lives only in that table, so this is how a conversation reads it before
proposing a change.

Runs host-side through the skill proxy, where the framework database is. The
identity is ``ISTOTA_USER_ID`` and the gate is ``Config.is_shared_kv_writer``,
the one the admin routes use, so a blank admins file lets nobody in.
"""

import os

from istota.skills._cli import error_envelope, fail, parse_and_resolve, run_skill_cli


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


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.briefings_config",
        description="Shared briefing blocks: read a definition and its last run",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_block = sub.add_parser("shared-block", help="One shared briefing block")
    block_sub = p_block.add_subparsers(dest="action", required=True)

    p_show = block_sub.add_parser(
        "show", help="The definition, its whole directive, and its last run",
    )
    p_show.add_argument("name", help="The shared block's name (a slug)")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parse_and_resolve(parser, argv)
    run_skill_cli(
        {("shared-block", "show"): cmd_show}, args,
        command=(args.command, args.action),
    )


if __name__ == "__main__":
    main()
