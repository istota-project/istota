"""Whether an answer quotes the message that asked it (ISSUE-641).

One rule for every surface with quoted replies: when the first part of an
answer is sent, quote the triggering message if anything else has been posted
in the room since it, and send unquoted while it is still the latest. "Anything
else" is a turn from anyone, another task's answer included, and never this
task's own posts (its acknowledgement and progress), which would otherwise make
every slow answer quote.

Each surface asks the question of its own copy of the room. Web, SMS and
WhatsApp read the canonical ``messages`` store, where every turn of a room
is recorded whether or not it was answered. Talk reads Talk, since a Talk room
holds posts the store never sees; ``talk_room_moved_on`` is the rule over a
page of Talk chat messages.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

#: Canonical rows that count as something posted since the trigger. A
#: ``system`` row is a notice the daemon wrote (a mirrored push, a relay
#: question), not a turn anybody took in the conversation.
_TURN_ROLES = ("user", "assistant")


@dataclass(frozen=True)
class TriggerTurn:
    """The canonical row of the message that started a task's turn."""

    message_id: int
    room_token: str
    external_ids: dict[str, str]


def trigger_turn(conn: sqlite3.Connection, task_id: int) -> TriggerTurn | None:
    """The task's own user row in the canonical store, or None.

    One task is started by one message, so this is that message; ``ORDER BY id
    DESC`` makes a second user row, should one ever be written for a task, the
    one quoted, which is the last message addressed to the bot.
    """
    row = conn.execute(
        "SELECT id, room_token, external_ids FROM messages "
        "WHERE task_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    external: dict[str, str] = {}
    if row["external_ids"]:
        try:
            parsed = json.loads(row["external_ids"])
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            external = {str(k): str(v) for k, v in parsed.items() if v is not None}
    return TriggerTurn(int(row["id"]), row["room_token"], external)


def room_moved_on(conn: sqlite3.Connection, trigger: TriggerTurn, task_id: int) -> bool:
    """Whether a turn other than this task's own was stored after ``trigger``."""
    placeholders = ",".join("?" for _ in _TURN_ROLES)
    row = conn.execute(
        "SELECT 1 FROM messages WHERE room_token = ? AND id > ? "
        f"AND role IN ({placeholders}) "
        "AND (task_id IS NULL OR task_id != ?) LIMIT 1",
        (trigger.room_token, trigger.message_id, *_TURN_ROLES, task_id),
    ).fetchone()
    return row is not None


def quoted_trigger(
    conn: sqlite3.Connection, task_id: int, room_token: str | None = None,
) -> TriggerTurn | None:
    """The trigger to quote, or None when the answer goes out unquoted.

    ``room_token`` restricts the answer to the room the trigger is in: an
    answer delivered anywhere else has no copy of the message to quote.
    """
    trigger = trigger_turn(conn, task_id)
    if trigger is None:
        return None
    if room_token is not None:
        from istota import db

        if (db._canonical_room_token(conn, trigger.room_token)
                != db._canonical_room_token(conn, room_token)):
            return None
    return trigger if room_moved_on(conn, trigger, task_id) else None


def talk_room_moved_on(
    messages: list[dict], *, trigger_id: int, task_id: int, bot_actor_ids: set,
) -> bool:
    """The rule over Talk chat messages fetched after ``trigger_id``.

    A system message (a join, a rename, an edit's bookkeeping) is not a post,
    and a message the bot posted with the reference ``istota:task:<this
    task>:...`` is this task's own acknowledgement, progress text or user-turn
    repost. The actor is checked too, since any participant can set a
    ``referenceId`` (the readback's rule, ISSUE-405).
    """
    own_prefix = f"istota:task:{task_id}:"
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        try:
            msg_id = int(msg.get("id"))
        except (TypeError, ValueError):
            continue
        if msg_id <= trigger_id:
            continue
        if msg.get("systemMessage") or msg.get("messageType") == "system":
            continue
        reference = msg.get("referenceId")
        if (
            isinstance(reference, str) and reference.startswith(own_prefix)
            and msg.get("actorType") == "users" and msg.get("actorId") in bot_actor_ids
        ):
            continue
        return True
    return False
