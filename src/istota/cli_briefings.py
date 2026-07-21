"""Unified ``istota briefings`` operator CLI.

One tree over the briefings module:

* ``istota briefings schedule ensure|list|delete`` — the framework
  ``briefing_configs`` (schedule + delivery). Absorbs the deprecated
  ``istota briefing ensure|list|delete``; delegates to
  ``cli.run_briefing_schedule``.
* ``istota briefings blocks|sources|archive …`` — the per-user module DB
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

    # schedule ------------------------------------------------------------
    sched = sub.add_parser("schedule", help="Manage briefing schedule + delivery")
    sched.add_argument("action", choices=["list", "ensure", "delete"])
    sched.add_argument("-u", "--user")
    sched.add_argument("--name")
    sched.add_argument("--cron")
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

    # sources -------------------------------------------------------------
    src = sub.add_parser("sources", help="Manage a block's sources")
    src.add_argument("action", choices=["list", "add", "remove"])
    src.add_argument("-u", "--user", required=True)
    src.add_argument("--block", type=int)
    src.add_argument("--kind")
    src.add_argument("--config")
    src.add_argument("--id", type=int)

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
        RENDER_MODES,
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
        from istota.config import BriefingSharedBlock
        block = BriefingSharedBlock(
            name=row.name, cron=row.cron, title=row.title,
            directive=row.directive, render_mode=row.render_mode,
            enabled=row.enabled, trusted=row.trusted, sources=row.sources,
        )
        _generate_shared_block(config, block)
        with db.get_db(config.db_path) as conn:
            val = db.shared_kv_get(conn, "briefing_shared_blocks", row.name)
        if val:
            print(f"ran {row.name}: {len(val['value'])} bytes, updated {val['updated_at']}")
        else:
            print(f"ran {row.name}: no content written (kept prior / empty gather)")
        return 0

    # ensure
    if not args.name or not args.cron:
        print("Error: --name and --cron required for ensure", file=sys.stderr)
        return 1
    if args.render_mode not in RENDER_MODES:
        print(
            f"Error: --render-mode must be one of {sorted(RENDER_MODES)}",
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

    builders = {
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
