"""Private-reply verbs from the trusted host-side skill process (ISSUE-608).

`whisper` puts a note for the principal in their own private chat with the bot
from a task in a shared room; `answer-privately` asks the principal's own
question again there, where it runs at their full reach; `post` asks, from the
principal's private chat, to post into a shared room, held for their approval.
None of them sends: each persists a request or a task the daemon delivers.
Identity comes from the proxy's environment and nothing else, as in the relay
skill: the actor, the task and the database path are never arguments.
"""

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path

from .._cli import parse_and_resolve, run_skill_cli
from .._turn_access import INCOGNITO_REFUSAL, calling_task_incognito


def _dispatch(args):
    from istota import db
    from istota.rooms import private_replies
    from ...config import load_config
    from istota.relay.requests import RequestError

    actor = os.environ.get("ISTOTA_USER_ID", "")
    task = os.environ.get("ISTOTA_TASK_ID", "")
    path = os.environ.get("ISTOTA_DB_PATH", "")
    if not actor or not task.isdecimal() or not path:
        raise RequestError("task_unavailable")
    if calling_task_incognito():
        raise RequestError(INCOGNITO_REFUSAL)
    config = load_config()
    if args.command in {"location", "locations", "location-stop"}:
        from istota.location import sharing

        config.db_path = Path(path)
        now = datetime.now(timezone.utc)
        if args.command == "location":
            return sharing.read_room_location(config, actor_user_id=actor, task_id=int(task),
                                              subject_user_id=args.user, now=now,
                                              map_card=args.map != "off")
        if args.command == "locations":
            return sharing.read_room_locations(config, actor_user_id=actor, task_id=int(task), now=now)
        with db.get_db(config.db_path) as conn:
            current = sharing._interactive_task(conn, config, actor_user_id=actor, task_id=int(task))
            room = private_replies._live_shared_room(conn, current, actor)
            return sharing.revoke_location_shares(
                conn, actor_user_id=actor, room_token=None if args.all else room,
                all_rooms=args.all, now=now)
    with db.get_db(Path(path)) as conn:
        if args.command == "answer-privately":
            return private_replies.queue_private_answer(
                conn, config, actor_user_id=actor, task_id=int(task))
        if args.command == "whisper":
            return private_replies.enqueue_whisper(
                conn, config, actor_user_id=actor, task_id=int(task),
                request_key=args.request_key, text=args.text)
        return private_replies.hold_room_post(
            conn, config, actor_user_id=actor, task_id=int(task),
            request_key=args.request_key, text=args.text, room=args.room)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Write privately to your principal from a shared room, or post into "
                    "a shared room from their private chat")
    commands = parser.add_subparsers(dest="command", required=True)
    whisper = commands.add_parser("whisper")
    whisper.add_argument("--request-key", required=True)
    whisper.add_argument("text")
    post = commands.add_parser("post")
    post.add_argument("--request-key", required=True)
    # A room token or a room's name; without it, the room the turn is linked to.
    post.add_argument("--room", default=None)
    post.add_argument("text")
    # No text: what is asked again is the principal's own turn, never the model's.
    commands.add_parser("answer-privately")
    location = commands.add_parser("location")
    location.add_argument("--user", required=True)
    location.add_argument("--map", choices=["auto", "off"], default="auto")
    commands.add_parser("locations")
    stop = commands.add_parser("location-stop")
    stop.add_argument("--all", action="store_true")
    return parser


def main(argv=None):
    args = parse_and_resolve(build_parser(), argv)
    return run_skill_cli(
        {command: _dispatch for command in ("whisper", "post", "answer-privately",
                                             "location", "locations", "location-stop")}, args)
