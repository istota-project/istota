"""Side-room verbs from the trusted host-side skill process (multiplayer D4).

`whisper` puts a note in the principal's side room from a task in a shared
room; `post` asks, from a side room, to post into its parent, held for the
member's approval. Neither sends: both persist a request the daemon delivers.
Identity comes from the proxy's environment and nothing else, as in the relay
skill: the actor, the task and the database path are never arguments.
"""

import argparse
import os
from pathlib import Path

from .._cli import parse_and_resolve, run_skill_cli


def _dispatch(args):
    from ... import db, side_rooms
    from ...config import load_config
    from ...whatsapp_requests import RequestError

    actor = os.environ.get("ISTOTA_USER_ID", "")
    task = os.environ.get("ISTOTA_TASK_ID", "")
    path = os.environ.get("ISTOTA_DB_PATH", "")
    if not actor or not task.isdecimal() or not path:
        raise RequestError("task_unavailable")
    with db.get_db(Path(path)) as conn:
        config = load_config()
        verb = side_rooms.enqueue_whisper if args.command == "whisper" else side_rooms.hold_room_post
        return verb(conn, config, actor_user_id=actor, task_id=int(task),
                    request_key=args.request_key, text=args.text)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Write to your principal's side room, or post from a side room into its room")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("whisper", "post"):
        verb = commands.add_parser(name)
        verb.add_argument("--request-key", required=True)
        verb.add_argument("text")
    return parser


def main(argv=None):
    args = parse_and_resolve(build_parser(), argv)
    return run_skill_cli({"whisper": _dispatch, "post": _dispatch}, args)
