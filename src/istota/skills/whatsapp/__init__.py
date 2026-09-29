"""Persist WhatsApp intent from the trusted host-side skill process."""

import argparse
import os
from pathlib import Path

from .._cli import parse_and_resolve, run_skill_cli


def _dispatch(args):
    from ... import db
    from ...config import load_config
    from ...whatsapp_requests import enqueue_self_send, get_request, hold_question, RequestError, write_transaction
    from ... import message_relays
    from ...async_runtime import run_coro

    actor = os.environ.get("ISTOTA_USER_ID", "")
    task = os.environ.get("ISTOTA_TASK_ID", "")
    path = os.environ.get("ISTOTA_DB_PATH", "")
    if not actor or not task.isdecimal() or not path:
        raise RequestError("task_unavailable")
    with db.get_db(Path(path)) as conn:
        owned_task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND user_id=? AND status='running'",
            (int(task), actor),
        ).fetchone()
        if owned_task is None:
            raise RequestError("task_unavailable")
        if args.command == "send":
            return enqueue_self_send(conn, load_config(), actor_user_id=actor, task_id=int(task),
                                request_key=args.request_key, text=args.text)
        config = load_config()
        if args.command == "ask":
            return hold_question(conn, config, actor_user_id=actor, task_id=int(task),
                                 recipient_user_id=args.user_id, request_key=args.request_key, text=args.text)
        row = None if args.command == "relays" else get_request(conn, actor_user_id=actor, request_id=args.request_id)
        if args.command == "relays" or (row and row["kind"] == "relay_question"):
            origin = message_relays.private_origin(conn, config, actor_user_id=actor,
                                                   surface=owned_task["source_type"], conversation_token=owned_task["conversation_token"])
            run_coro(message_relays.verify_origin(config, actor_user_id=actor, origin=origin))
            with write_transaction(conn):
                message_relays.validate_origin(conn, config, actor_user_id=actor, origin=origin)
                if args.command == "relays":
                    return {"status": "ok", "relays": message_relays.list_relays(conn, actor_user_id=actor)}
                row["relay"] = message_relays.get_relay(conn, actor_user_id=actor, relay_id=row["relay_id"])
        if row is None:
            raise RequestError("request_unavailable")
        return {"status": "ok", "request": row}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Send WhatsApp messages and manage approved relay questions")
    commands = parser.add_subparsers(dest="command", required=True)
    send = commands.add_parser("send")
    send.add_argument("--request-key", required=True)
    send.add_argument("text")
    ask = commands.add_parser("ask")
    ask.add_argument("user_id")
    ask.add_argument("--request-key", required=True)
    ask.add_argument("text")
    commands.add_parser("relays")
    status = commands.add_parser("status")
    status.add_argument("request_id")
    args = parse_and_resolve(parser, argv)
    return run_skill_cli({name: _dispatch for name in ("send", "ask", "status", "relays")}, args)
