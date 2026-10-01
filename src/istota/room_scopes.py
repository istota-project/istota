"""What a task in a shared room may reach, and the room-scoped answers beside it.

A turn runs with its sender's reach (ISSUE-576). A member's turn in a shared
room reaches everything it would in their private room: asking in a room they
know others read is the decision that the answer may be read there, so there is
no per-room grant to make first. A guest's turn (emissary mode, multiplayer D2)
runs as the room's host at room-safe reach: the guest has no data of their own
and the host asked nothing, so every scope is withheld. So does a task nobody
asked in the room, such as a cron job whose conversation is a shared room. A scope is a skill
whose manifest says ``shared_room: private`` (the default), or one of the two
synthetic scopes that are not skills:

- ``files``: the workspace, ``{mount}/Users/{user_id}``, and the per-resource
  mounts, which the sandbox binds only when this is not withheld.
- ``memory``: ``USER.md``, dated memories, recalled memories, playbooks,
  knowledge-graph facts and per-skill overlays.

A ``shared_room: safe`` skill is never a scope, and the room's own
``CHANNEL.md`` is shared by construction. What is withheld is enforced by what
the task can reach, at the seams ``execute_task`` and ``task_env`` apply it to.

What a member's turn in a shared room loses is the *ambient* part of their
memory, which reaches the prompt without being asked for (`ambient_memory_off`):
the memory skill stays, so a member who asks for a note gets it.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping

from . import db

logger = logging.getLogger(__name__)

SYNTHETIC_SCOPES = ("files", "memory")


def scope_names(skill_index: Mapping[str, object]) -> list[str]:
    """Every scope: private skills, then the two synthetic."""
    private = sorted(
        name for name, meta in skill_index.items()
        if getattr(meta, "shared_room", "private") != "safe"
        and name not in SYNTHETIC_SCOPES
    )
    return [*private, *SYNTHETIC_SCOPES]


def all_scopes(skill_index: Mapping[str, object]) -> frozenset[str]:
    """Every scope, which is what a guest's turn withholds."""
    return frozenset(scope_names(skill_index))


def withheld_for_task(
    conn: sqlite3.Connection | None,
    task: "db.Task",
    *,
    skill_index: Mapping[str, object],
) -> frozenset[str]:
    """What one task may not reach, from its own row. The one derivation the
    executor's reach seams and the `skills` CLI's guard both read.

    Every scope on a guest's turn. Every scope, too, on a task no member asked
    for whose conversation is a room more than one human reads: a cron job, a
    briefing or a subtask run there has no sender asking in front of the room,
    so the consent a member's turn carries does not reach it, and its answer
    lands in the room (`transport.routing`). Nothing otherwise: a member's turn
    runs at full reach in every room (ISSUE-576).

    ``conn`` is read only for that second case. ``None`` is a database that
    does not exist, which holds no room; any error reading the audience
    withholds every scope.
    """
    from .surfaces import origin_surface_for_source_type

    if task.guest_participant_id is not None:
        return all_scopes(skill_index)
    if origin_surface_for_source_type(task.source_type) is not None:
        return frozenset()
    if not task.conversation_token:
        return frozenset()
    if task.is_group_chat or task.audience == "mixed":
        return all_scopes(skill_index)
    if conn is None:
        return frozenset()
    try:
        token = canonical_token(conn, task.conversation_token)
        shared = token is not None and db.room_is_shared(conn, token)
    except Exception as exc:
        logger.warning(
            "room_scopes: could not read the audience of %s, withholding every "
            "scope: %s", task.conversation_token, exc,
        )
        return all_scopes(skill_index)
    return all_scopes(skill_index) if shared else frozenset()


def ambient_memory_off(conn: sqlite3.Connection | None, task: "db.Task") -> bool:
    """Whether this task's prompt leaves out the sender's ambient memory.

    True in a room more than one human reads now: a guest's turn, a turn whose
    stored audience is ``mixed``, a surface roster saying "group", or a
    registered room with more than one member. `USER.md`, dated and recalled
    memories, knowledge-graph facts and playbooks reach the prompt without the
    member asking for them, so a question about lunch could come back carrying
    a health note; that is the one thing asking in the room did not consent to.
    A fixed rule, not a setting. Fails toward leaving the memory out: a room
    whose audience cannot be read counts as shared.
    """
    if task.guest_participant_id is not None or task.audience == "mixed":
        return True
    if task.is_group_chat:
        return True
    if not task.conversation_token or conn is None:
        return False
    try:
        token = canonical_token(conn, task.conversation_token)
        return token is not None and db.room_is_shared(conn, token)
    except Exception as exc:
        logger.warning(
            "room_scopes: could not read the audience of %s, leaving ambient "
            "memory out: %s", task.conversation_token, exc,
        )
        return True


def canonical_token(conn, token: str | None) -> str | None:
    """The registry token a conversation token names, or None for no room.

    The one copy: `side_rooms` re-exports it, and the memory skill CLI reaches
    it here without importing `side_rooms`.
    """
    if not token:
        return None
    if db.get_room(conn, token) is not None:
        return token
    return db.find_room_token_by_ref(conn, token)


#: The fence label a shared room's `CHANNEL.md` carries, in the prompt block,
#: in recall and in `memory show --channel` (multiplayer D24).
CHANNEL_NOTES_LABEL = "room notes"


def channel_notes_shared(
    conn: sqlite3.Connection,
    conversation_token: str | None,
    *,
    guest_turn: bool = False,
    is_group_chat: bool = False,
) -> bool:
    """Whether a room's `CHANNEL.md` may have several authors (D24).

    A guest's turn, a surface roster saying "group", or a registered room more
    than one human has ever been in. Ever, not now: notes a member wrote stay
    theirs after they leave. Raises what the reads raise; each caller chooses
    its failure direction, and both choose the fence.
    """
    if guest_turn or is_group_chat:
        return True
    token = canonical_token(conn, conversation_token)
    return token is not None and db.room_was_ever_shared(conn, token)


def _is_own_push_token(user_id: str, token: str) -> bool:
    """Whether ``token`` is ``user_id``'s own SMS or WhatsApp 1:1 token."""
    from .transport.sms import sms_conversation_token
    from .transport.whatsapp import whatsapp_conversation_token

    return token in (
        sms_conversation_token(user_id), whatsapp_conversation_token(user_id),
    )


def task_group_ids(conn: sqlite3.Connection, task: "db.Task") -> list[str]:
    """The groups whose material this task may carry, sorted; the one answer
    the prompt's ``## Group memory``, the ``Groups/<id>`` binds and the
    ``kv --group`` gate (multiplayer D21) all read.

    A group is in it when ``task.user_id`` is a current member and everyone
    who reads the answer is too (groups spec D6). Off a room that is every
    group of the user, since the answer goes to the user alone (a shared room
    is refused as a delivery target, multiplayer Stage 15). In a room, only the
    groups whose current members cover the room's members and present
    principal participants; in a room linked to a group (``rooms.group_id``),
    that group alone, under the same rule.

    Nothing at all on a guest's turn (emissary mode), on a turn whose stored
    audience is ``mixed``, or while any guest or agent is present: the charter
    is "may be said in front of every member", and a guest or another bot is
    not one. Nothing either where the audience cannot be read: a room with no
    recorded readers, or a surface roster saying "group" over a room the
    registry records one person in. A token naming no registered room is the
    first case, so an email thread with no thread room loads nothing.

    The exception is the user's own SMS or WhatsApp 1:1 (multiplayer D23):
    its token is derived from ``task.user_id`` alone and names no room, and
    the conversation is as private as a room-less task, so it loads the same
    set. Recognised by deriving the token, never by its prefix, so another
    user's push token is still an unknown room.

    The audience rule here is the only gate on group material; a member's
    full reach in a shared room is their own data and never widens it. Raises
    on a database error; the caller treats that as the empty set.
    """
    if task.guest_participant_id is not None or task.audience == "mixed":
        return []
    groups = db.list_user_groups(conn, task.user_id)
    if not groups or not task.conversation_token:
        return groups

    from . import room_policy

    room_token = task.conversation_token
    if db.get_room(conn, room_token) is None:
        room_token = db.find_room_token_by_ref(conn, room_token)
        if room_token is None:
            if _is_own_push_token(task.user_id, task.conversation_token):
                return groups
            room_token = task.conversation_token
    room = room_policy.room_readers(conn, room_token)
    if room.guests or room.others:
        logger.debug(
            "group_memory_skipped reason=non_member_reader token=%s",
            task.conversation_token,
        )
        return []
    readers = set(room.members)
    if not readers or (task.is_group_chat and len(readers) < 2):
        logger.debug(
            "group_memory_skipped reason=room_members_unknown token=%s",
            task.conversation_token,
        )
        return []
    # A room linked to a group (multiplayer Stage 27) carries that group and no
    # other, still under the rule below: the link narrows the candidates, it
    # never makes a reader a member.
    registered = db.get_room(conn, room_token)
    if registered is not None and registered.group_id:
        groups = [g for g in groups if g == registered.group_id]
    return [
        g for g in groups if readers <= set(db.list_group_members(conn, g))
    ]
