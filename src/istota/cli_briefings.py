"""Unified ``istota briefings`` operator CLI.

One tree over the briefings module:

* ``istota briefings schedule ensure|list|delete`` — the framework
  ``briefing_configs`` (schedule + delivery). Absorbs the deprecated
  ``istota briefing ensure|list|delete``; delegates to
  ``cli.run_briefing_schedule``.
* ``istota briefings list|blocks|sources|archive …`` — the per-user module DB
  content model, routed through the module Click CLI (``briefings.cli``) via
  ``CliRunner`` with a resolved :class:`BriefingsContext`.

``main()`` peels ``briefings <group>`` off before the top-level argparse (the
same pattern as ``istota money <op>``), so this module owns the whole subtree.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="istota briefings")
    sub = parser.add_subparsers(dest="group", required=True)

    listing = sub.add_parser("list", help="List briefing names and content status")
    listing.add_argument("-u", "--user", required=True)

    # schedule ------------------------------------------------------------
    sched = sub.add_parser("schedule", help="Manage briefing schedule + delivery")
    sched.add_argument("action", choices=["list", "ensure", "delete"])
    sched.add_argument("-u", "--user")
    sched.add_argument("--name")
    sched.add_argument("--cron")
    sched.add_argument(
        "--title", default="",
        help="Display title (email subject / archive entry). Blank derives it from --name.",
    )
    sched.add_argument("--conversation-token")
    sched.add_argument("--output", default="talk")
    sched.add_argument("--disabled", action="store_true")

    # blocks --------------------------------------------------------------
    blk = sub.add_parser("blocks", help="Manage content blocks")
    blk.add_argument("action", choices=["list", "add", "remove", "reorder", "set"])
    blk.add_argument("-u", "--user", required=True)
    blk.add_argument("--briefing")
    blk.add_argument("--title")
    blk.add_argument("--directive")
    blk.add_argument("--render-mode", dest="render_mode")
    blk.add_argument("--id", type=int)
    blk.add_argument("--ids")
    blk.add_argument("--options")
    blk.add_argument("--merge-options", dest="merge_options")

    # sources -------------------------------------------------------------
    src = sub.add_parser("sources", help="Manage a block's sources")
    src.add_argument("action", choices=["list", "add", "set", "reorder", "remove"])
    src.add_argument("-u", "--user", required=True)
    src.add_argument("--block", type=int)
    src.add_argument("--kind")
    src.add_argument("--config")
    src.add_argument("--merge-config", dest="merge_config")
    src.add_argument("--id", type=int)
    src.add_argument("--ids")
    src_enabled = src.add_mutually_exclusive_group()
    src_enabled.add_argument("--enabled", dest="enabled", action="store_const", const=True)
    src_enabled.add_argument("--disabled", dest="enabled", action="store_const", const=False)
    src.set_defaults(enabled=None)

    # shared --------------------------------------------------------------
    shared = sub.add_parser(
        "shared", help="Manage module-owned shared briefing blocks (global)"
    )
    shared.add_argument("action", choices=["list", "ensure", "remove", "run"])
    shared.add_argument("--name")
    shared.add_argument("--cron")
    shared.add_argument("--title", default="")
    shared.add_argument("--directive")
    shared.add_argument("--render-mode", dest="render_mode", default="synthesis")
    shared_trust = shared.add_mutually_exclusive_group()
    shared_trust.add_argument("--trusted", dest="trusted", action="store_true")
    shared_trust.add_argument("--no-trusted", dest="trusted", action="store_false")
    shared.set_defaults(trusted=False)
    shared_en = shared.add_mutually_exclusive_group()
    shared_en.add_argument("--enabled", dest="enabled", action="store_true")
    shared_en.add_argument("--disabled", dest="enabled", action="store_false")
    shared.set_defaults(enabled=True)
    shared.add_argument(
        "--source-json", dest="source_json", action="append", default=[],
        help="A source as JSON, e.g. '{\"kind\":\"markets\",\"config\":{}}'. Repeatable.",
    )

    group = sub.add_parser("group", help="Manage group-owned briefing definitions")
    group.add_argument("action", choices=["list", "save"])
    group.add_argument("--group-id", required=True)
    group.add_argument("--actor", required=True, help="Enabled current owner, or admin for All users")
    group.add_argument("-f", "--file", help="JSON/TOML definition; use - for stdin")
    group.add_argument("--revision", type=int, help="Expected revision; 0 creates a disabled draft")

    migration = sub.add_parser("migrate", help="Review and retire legacy defaults or publishing jobs")
    migration.add_argument("action", choices=["report", "import", "review", "cutover", "rollback"])
    migration.add_argument("--name")
    migration.add_argument("--producer-user", help="Migrate this user's publishing job named by --name")
    migration.add_argument("-f", "--file", help="Publishing import: group definition, outputs and dependent_jobs")
    migration.add_argument("--confirm-cutover", action="store_true",
                           help="Confirm cutover after reviewing all dependencies, including scripts and prompts")
    migration.add_argument("--actor", required=True)
    migration.add_argument("--timezone", help="Explicit common timezone, required for import/review")
    migration.add_argument("--revision", type=int, help="Expected imported definition revision")
    migration.add_argument("--dispositions", help="JSON/TOML map: default user dispositions, or publishing consumer ids to replacement job ids")
    migration.add_argument("--accept-source-changes", action="store_true", help="Acknowledge removed/converted sources and delivery changes")

    # archive -------------------------------------------------------------
    arc = sub.add_parser("archive", help="Read the briefing archive")
    arc.add_argument("action", choices=["list", "show"])
    arc.add_argument("-u", "--user", required=True)
    arc.add_argument("--briefing")
    arc.add_argument("--limit", type=int, default=20)
    arc.add_argument("--id", type=int)

    return parser


def _run_schedule(args, config) -> int:
    from istota.cli import run_briefing_schedule

    run_briefing_schedule(
        config,
        args.action,
        user=args.user,
        name=args.name,
        cron=args.cron,
        title=args.title or "",
        conversation_token=args.conversation_token,
        output=args.output or "talk",
        disabled=args.disabled,
    )
    return 0


def _run_shared(args, config) -> int:
    """Operator CRUD + run-now for shared briefing block definitions.

    Parity with the admin web UI (Stage 3). Writes go to ``shared_block_configs``
    (framework DB); ``run`` invokes ``_generate_shared_block`` once immediately.
    """
    import json

    from istota import db
    from istota.briefings.models import (
        ALLOWED_SHARED_SOURCE_KINDS,
        SHARED_RENDER_MODES,
        validate_shared_block_payload,
    )

    action = args.action

    if action == "list":
        with db.get_db(config.db_path) as conn:
            rows = db.list_shared_block_configs(conn)
        if not rows:
            print("(no shared blocks)")
            return 0
        for r in rows:
            state = "" if r.enabled else " [disabled]"
            trust = "trusted" if r.trusted else "untrusted"
            print(
                f"{r.name}  cron={r.cron!r}  mode={r.render_mode}  {trust}"
                f"  sources={len(r.sources)}{state}"
            )
        return 0

    if action == "remove":
        if not args.name:
            print("Error: --name required", file=sys.stderr)
            return 1
        with db.get_db(config.db_path) as conn:
            removed = db.delete_shared_block_config(conn, args.name)
        print(f"{'removed' if removed else 'not found'}: {args.name}")
        return 0 if removed else 1

    if action == "run":
        if not args.name:
            print("Error: --name required", file=sys.stderr)
            return 1
        from istota.scheduler import _generate_shared_block
        with db.get_db(config.db_path) as conn:
            row = db.get_shared_block_config(conn, args.name)
        if row is None:
            print(f"Error: no shared block named {args.name!r}", file=sys.stderr)
            return 1
        from istota.config import shared_block_from_row
        from istota.briefings.shared_block_runs import IN_FLIGHT, QUEUED, REGENERATED, run_state
        outcome = _generate_shared_block(config, shared_block_from_row(row))
        with db.get_db(config.db_path) as conn:
            val = db.shared_kv_get(conn, "briefing_shared_blocks", row.name)
            agent = run_state(conn, row.name)["agent"] if outcome in (QUEUED, IN_FLIGHT) else None
        if agent:
            print(
                f"ran {row.name}: {outcome}, task #{agent.get('task_id')}; "
                "the daemon writes the block when the task finishes"
            )
        elif outcome == REGENERATED and val:
            print(f"ran {row.name}: {len(val['value'])} bytes, updated {val['updated_at']}")
        else:
            print(f"ran {row.name}: {outcome}, no content written")
        return 0

    # ensure
    if not args.name or not args.cron:
        print("Error: --name and --cron required for ensure", file=sys.stderr)
        return 1
    if args.render_mode not in SHARED_RENDER_MODES:
        print(
            f"Error: --render-mode must be one of {sorted(SHARED_RENDER_MODES)}",
            file=sys.stderr,
        )
        return 1
    sources: list = []
    for raw in args.source_json:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"Error: invalid --source-json {raw!r}: {e}", file=sys.stderr)
            return 1
        if not isinstance(parsed, dict) or "kind" not in parsed:
            print(f"Error: source must be a JSON object with a 'kind': {raw!r}", file=sys.stderr)
            return 1
        if parsed["kind"] not in ALLOWED_SHARED_SOURCE_KINDS:
            print(
                f"Error: source kind {parsed['kind']!r} not allowed for shared "
                f"blocks (allowed: {sorted(ALLOWED_SHARED_SOURCE_KINDS)})",
                file=sys.stderr,
            )
            return 1
        sources.append(parsed)
    if args.render_mode == "agent":
        try:
            validate_shared_block_payload({
                "name": args.name, "cron": args.cron, "directive": args.directive,
                "render_mode": args.render_mode, "trusted": args.trusted,
                "sources": sources,
            })
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    with db.get_db(config.db_path) as conn:
        row = db.upsert_shared_block_config(
            conn,
            name=args.name,
            cron=args.cron,
            title=args.title or "",
            directive=args.directive,
            render_mode=args.render_mode,
            enabled=args.enabled,
            trusted=args.trusted,
            sources=sources,
        )
    print(f"ensured shared block: {row.name} (id={row.id})")
    return 0


def _run_group(args, config) -> int:
    import json

    from istota import apply, db
    from istota.briefings import group_store as store
    from istota.groups.policy import GroupError
    from istota.settings_plan.engine import ExecutionError
    from istota.settings_plan.group_briefings import operator_is_admin, prepare_definition
    from istota.settings_plan.schema import parse_plan

    try:
        with db.get_db(config.db_path) as conn:
            if args.action == "list":
                store._require_author(conn, config, args.group_id, args.actor,
                                      operator_is_admin(conn, config, args.actor))
                result = store.list_definitions(conn, args.group_id)
            else:
                if not args.file or args.revision is None:
                    raise ValueError("save requires --file and --revision (0 for a new disabled draft)")
                declaration = {**apply.load_plan(args.file), "group_id": args.group_id}
                parse_plan({"group_briefings": [declaration]})
                conn.execute("BEGIN IMMEDIATE")
                value, old = prepare_definition(conn, config, declaration, actor=args.actor,
                                                expected_revision=args.revision)
                result = store.save_definition(
                    conn, config, args.group_id, value, actor_id=args.actor, expected_revision=args.revision,
                    definition_id=old["id"] if old else None,
                    is_admin=operator_is_admin(conn, config, args.actor), caller_transaction=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (GroupError, ExecutionError, apply.PlanError, ValueError, OSError) as error:
        message = error.code if isinstance(error, (GroupError, ExecutionError)) else "invalid_definition"
        print(f"Error: {message}", file=sys.stderr)
        return 1


def _run_migration(args, config):
    import json

    from istota import apply, db
    from istota.briefings import group_migrate as migration
    from istota.groups.policy import GroupError
    from istota.settings_plan.group_briefings import operator_is_admin

    try:
        with db.get_db(config.db_path) as conn:
            if not operator_is_admin(conn, config, args.actor):
                raise ValueError("migration requires an installation admin")
            if args.producer_user:
                if not args.name:
                    raise ValueError("--name is required")
                common = {"actor_id": args.actor}
                if args.action == "report":
                    result = migration.publisher_report(conn, config, args.producer_user, args.name, **common)
                elif args.action == "import":
                    if not args.file:
                        raise ValueError("--file is required; supply the replacement prompt and dependency inventory")
                    result = migration.import_publisher(conn, config, args.producer_user, args.name,
                                                        apply.load_plan(args.file), **common)
                elif args.action == "review":
                    raise ValueError("Use report, generate a preview of the current draft, then cutover --confirm-cutover")
                else:
                    if args.revision is None:
                        raise ValueError("--revision is required")
                    common["expected_revision"] = args.revision
                    if args.action == "cutover":
                        result = migration.cutover_publisher(conn, config, args.producer_user, args.name,
                            replacements=apply.load_plan(args.dispositions) if args.dispositions else {},
                            confirmed=args.confirm_cutover, **common)
                    else:
                        result = migration.rollback_publisher(conn, config, args.producer_user, args.name, **common)
            elif args.action == "report":
                result = migration.migration_report(conn, config, args.name)
            else:
                if not args.name:
                    raise ValueError("--name is required")
                if args.action in ("import", "review") and not args.timezone:
                    raise ValueError("--timezone is required; review each candidate's local timezone")
                if args.action != "import" and args.revision is None:
                    raise ValueError("--revision is required")
                if args.action == "import":
                    result = migration.import_default(conn, config, args.name, actor_id=args.actor, timezone=args.timezone)
                elif args.action == "review":
                    dispositions = apply.load_plan(args.dispositions) if args.dispositions else {}
                    result = migration.review_default(conn, config, args.name, actor_id=args.actor,
                        expected_revision=args.revision, timezone=args.timezone, dispositions=dispositions,
                        accept_source_changes=args.accept_source_changes)
                else:
                    operation = migration.cutover_default if args.action == "cutover" else migration.rollback_default
                    result = operation(conn, config, args.name, actor_id=args.actor, expected_revision=args.revision)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (GroupError, apply.PlanError, ValueError, OSError) as error:
        print("Error: " + (error.code if isinstance(error, GroupError) else str(error)), file=sys.stderr)
        return 1


def _invoke_module(config, user_id: str, module_argv: list[str]) -> int:
    """Resolve the user's BriefingsContext and run the module Click CLI."""
    from click.testing import CliRunner

    from istota.briefings import (
        UserNotFoundError,
        ensure_initialised,
        resolve_for_user,
    )
    from istota.briefings.cli import cli

    try:
        ctx = resolve_for_user(user_id, config)
    except UserNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    ensure_initialised(ctx, app_config=config)
    ctx.app_config = config

    runner = CliRunner()
    result = runner.invoke(
        cli, module_argv, obj=ctx, standalone_mode=False, catch_exceptions=True,
    )
    if result.output:
        print(result.output.strip())
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        print(f"Error: {result.exception}", file=sys.stderr)
        return 1
    return 0 if result.exit_code in (0, None) else 1


def _blocks_argv(args) -> list[str]:
    argv = ["blocks", args.action]
    if args.action == "list":
        argv += ["--briefing", args.briefing or ""]
    elif args.action == "add":
        argv += ["--briefing", args.briefing or "", "--title", args.title or ""]
        if args.directive:
            argv += ["--directive", args.directive]
        if args.render_mode:
            argv += ["--render-mode", args.render_mode]
        if args.options is not None:
            argv += ["--options", args.options]
    elif args.action == "set":
        argv += ["--id", str(args.id)]
        if args.title is not None:
            argv += ["--title", args.title]
        if args.directive is not None:
            argv += ["--directive", args.directive]
        if args.render_mode is not None:
            argv += ["--render-mode", args.render_mode]
        if args.options is not None:
            argv += ["--options", args.options]
        if args.merge_options is not None:
            argv += ["--merge-options", args.merge_options]
    elif args.action == "remove":
        argv += ["--id", str(args.id)]
    elif args.action == "reorder":
        argv += ["--briefing", args.briefing or "", "--ids", args.ids or ""]
    return argv


def _sources_argv(args) -> list[str]:
    argv = ["sources", args.action]
    if args.action == "list":
        argv += ["--block", str(args.block)]
    elif args.action == "add":
        argv += ["--block", str(args.block), "--kind", args.kind or ""]
        if args.config:
            argv += ["--config", args.config]
        if args.enabled is False:
            argv += ["--disabled"]
    elif args.action == "set":
        argv += ["--id", str(args.id)]
        if args.config is not None:
            argv += ["--config", args.config]
        if args.merge_config is not None:
            argv += ["--merge-config", args.merge_config]
        if args.enabled is not None:
            argv += ["--enabled" if args.enabled else "--disabled"]
    elif args.action == "reorder":
        argv += ["--block", str(args.block), "--ids", args.ids or ""]
    elif args.action == "remove":
        argv += ["--id", str(args.id)]
    return argv


def _archive_argv(args) -> list[str]:
    argv = ["archive", args.action]
    if args.action == "list":
        if args.briefing:
            argv += ["--briefing", args.briefing]
        argv += ["--limit", str(args.limit)]
    elif args.action == "show":
        argv += ["--id", str(args.id)]
    return argv


def dispatch(argv: list[str], config) -> int:
    """Dispatch ``briefings <group> …`` (argv without the leading ``briefings``)."""
    args = build_parser().parse_args(argv)
    if args.group == "schedule":
        return _run_schedule(args, config)
    if args.group == "shared":
        return _run_shared(args, config)
    if args.group == "group":
        return _run_group(args, config)
    if args.group == "migrate":
        return _run_migration(args, config)

    builders = {
        "list": lambda args: ["list"],
        "blocks": _blocks_argv,
        "sources": _sources_argv,
        "archive": _archive_argv,
    }
    builder = builders.get(args.group)
    if builder is None:
        build_parser().print_help()
        return 1
    return _invoke_module(config, args.user, builder(args))


def main(argv: list[str] | None = None, config_path: str | None = None) -> int:
    from istota.config import load_config

    argv = list(sys.argv[1:] if argv is None else argv)
    config = load_config(Path(config_path) if config_path else None)
    return dispatch(argv, config)
