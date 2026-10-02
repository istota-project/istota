"""Hold relay questions to other users from the trusted host-side skill process.

Identity comes from the proxy's environment and nothing else: the actor, the
task and the database path are never arguments, so a task cannot ask as
somebody else or attach a question to another task.
"""

import argparse
import os
from pathlib import Path

from .._cli import parse_and_resolve, run_skill_cli

VIA_CHOICES = ("room", "whatsapp", "sms")


def _dispatch(args):
    from istota import db
    from istota.relay import relays as message_relays
    from ...async_runtime import run_coro
    from ...config import load_config
    from istota.relay.requests import RequestError, get_request, hold_question, write_transaction

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
        config = load_config()
        if args.command == "ask":
            return hold_question(conn, config, actor_user_id=actor, task_id=int(task),
                                 recipient_user_id=args.user_id, request_key=args.request_key,
                                 text=args.text, via=args.via)
        row = None
        if args.command == "status":
            row = get_request(conn, actor_user_id=actor, request_id=args.request_id)
            if row is None or row["kind"] != "relay_question":
                raise RequestError("request_unavailable")
        # Relay content is readable only from a verified private conversation.
        # The Talk half is a live call from this process, authenticated by the
        # app password the manifest has the proxy inject (ISSUE-568).
        origin = message_relays.private_origin(conn, config, actor_user_id=actor,
                                               surface=owned_task["source_type"],
                                               conversation_token=owned_task["conversation_token"])
        try:
            run_coro(message_relays.verify_private_audience(config, actor_user_id=actor, origin=origin))
        except message_relays.AudienceUnavailable:
            raise RequestError("audience_unavailable") from None
        with write_transaction(conn):
            message_relays.validate_origin(conn, config, actor_user_id=actor, origin=origin)
            if args.command == "list":
                return {"status": "ok", "relays": message_relays.list_relays(conn, actor_user_id=actor)}
            row["relay"] = message_relays.get_relay(conn, actor_user_id=actor, relay_id=row["relay_id"])
        return {"status": "ok", "request": row}


def build_parser():
    parser = argparse.ArgumentParser(description="Ask another user a question and follow its answer")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask")
    ask.add_argument("user_id")
    ask.add_argument("--request-key", required=True)
    ask.add_argument("--via", choices=VIA_CHOICES, default=None)
    ask.add_argument("text")
    status = commands.add_parser("status")
    status.add_argument("request_id")
    commands.add_parser("list")
    return parser


def main(argv=None):
    args = parse_and_resolve(build_parser(), argv)
    return run_skill_cli({name: _dispatch for name in ("ask", "status", "list")}, args)
