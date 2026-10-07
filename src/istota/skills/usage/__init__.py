"""Host-side usage reads with the same user scope as the chat usage command."""

import argparse

from istota import db
from istota.skills._cli import fail, parse_and_resolve, run_skill_cli
from istota.skills.skills import _guard_skill, _load_context
from istota.usage.window import usage_window

_NO_SUCH_USER = "\x00"


def cmd_usage(args):
    ctx = _load_context()
    if ctx["withheld"]:
        fail("Usage is personal data and is unavailable when scopes are withheld")
    refusal = _guard_skill("usage", ctx)
    if refusal:
        fail(refusal)
    if not ctx["is_admin"] and (args.user is not None or args.by == "user"):
        fail("--user and --by user require admin access")
    if args.user == "":
        fail("--user must not be empty")
    scope = args.user if ctx["is_admin"] else (ctx["user_id"] or _NO_SUCH_USER)
    try:
        since, until, since_sql, until_sql = usage_window(args)
    except ValueError as exc:
        fail(str(exc) if str(exc).startswith("--") else "Dates must be YYYY-MM-DD")
    with db.get_db(ctx["config"].db_path) as conn:
        if args.by:
            groups = db.usage_summary(
                conn, since=since, until=until, user_id=scope, group_by=args.by,
            )
        else:
            groups = [db.usage_summary(conn, since=since, until=until, user_id=scope)]
            groups[0]["key"] = "all"
        unmeasured = db.unmeasured_task_count(
            conn, since=since_sql, until=until_sql, user_id=scope,
        )
    return {
        "status": "ok", "since": since, "until": until, "user_id": scope,
        "group_by": args.by, "unmeasured_tasks": unmeasured, "groups": groups,
    }


def build_parser():
    parser = argparse.ArgumentParser(
        prog="istota-skill usage", description="Read token and cost usage",
    )
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, default=30, help="Window in days (default: 30)")
    window.add_argument("--since", help="Start date, YYYY-MM-DD (UTC)")
    parser.add_argument("--until", help="Inclusive end date, YYYY-MM-DD (UTC)")
    parser.add_argument("--by", choices=["model", "origin", "source", "brain", "user"])
    parser.add_argument("--user", help="Filter by user (admin only)")
    parser.add_argument("--json", action="store_true", help="Emit JSON (the default)")
    return parser


def main(argv=None):
    args = parse_and_resolve(build_parser(), argv)
    run_skill_cli({"usage": cmd_usage}, args, command="usage")
