"""Persist WhatsApp intent from the trusted host-side skill process."""

import argparse
import os
from pathlib import Path

from .._cli import parse_and_resolve, run_skill_cli


def _dispatch(args):
    from ... import db
    from ...config import load_config
    from ...whatsapp_requests import enqueue_self_send, get_request, RequestError

    actor = os.environ.get("ISTOTA_USER_ID", "")
    task = os.environ.get("ISTOTA_TASK_ID", "")
    path = os.environ.get("ISTOTA_DB_PATH", "")
    if not actor or not task.isdecimal() or not path:
        raise RequestError("task_unavailable")
    with db.get_db(Path(path)) as conn:
        owned_task = conn.execute(
            "SELECT 1 FROM tasks WHERE id=? AND user_id=? AND status='running'",
            (int(task), actor),
        ).fetchone()
        if owned_task is None:
            raise RequestError("task_unavailable")
        if args.command == "send":
            return enqueue_self_send(conn, load_config(), actor_user_id=actor, task_id=int(task),
                                request_key=args.request_key, text=args.text)
        row = get_request(conn, actor_user_id=actor, request_id=args.request_id)
        if row is None:
            raise RequestError("request_unavailable")
        return {"status": "ok", "request": row}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Send to your own WhatsApp during a task")
    commands = parser.add_subparsers(dest="command", required=True)
    send = commands.add_parser("send")
    send.add_argument("--request-key", required=True)
    send.add_argument("text")
    status = commands.add_parser("status")
    status.add_argument("request_id")
    args = parse_and_resolve(parser, argv)
    return run_skill_cli({"send": _dispatch, "status": _dispatch}, args)
