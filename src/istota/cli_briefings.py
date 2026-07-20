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
    sched.add_argument("--components-json")
    sched.add_argument("--component", action="append")
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

    # archive -------------------------------------------------------------
    arc = sub.add_parser("archive", help="Read the briefing archive")
    arc.add_argument("action", choices=["list", "show"])
    arc.add_argument("-u", "--user", required=True)
    arc.add_argument("--briefing")
    arc.add_argument("--limit", type=int, default=20)
    arc.add_argument("--id", type=int)

    return parser


def _run_schedule(args, config) -> int:
    from istota.cli import _parse_components_arg, run_briefing_schedule

    run_briefing_schedule(
        config,
        args.action,
        user=args.user,
        name=args.name,
        cron=args.cron,
        conversation_token=args.conversation_token,
        output=args.output or "talk",
        components=_parse_components_arg(args),
        disabled=args.disabled,
    )
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
