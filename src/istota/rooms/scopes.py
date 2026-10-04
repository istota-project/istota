"""What a task in a shared room may reach, and the room-scoped answers beside it.

A turn runs with its sender's reach (ISSUE-576). A member's turn in a shared
room reaches everything it would in their private room: asking in a room they
know others read is the decision that the answer may be read there, so there is
no per-room grant to make first. A guest's turn (emissary mode, multiplayer D2)
runs as the room's host at room-safe reach: the guest has no data of their own
and the host asked nothing, so every scope is withheld. So does a task nobody
asked in the room, such as a subtask whose conversation is a shared room. A
member's own cron job or briefing aimed at a room they are in counts as that
member asking (ISSUE-594). A scope is a skill
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

An email thread room is the exception to both (`is_email_thread_room`): it is
the host's correspondence, so every admitted turn on it runs as the host at
full reach with their ambient memory loaded, as an email turn always has. The
mail is fenced as untrusted input, and the admit gate and the outbound gate are
what bound such a turn, not a scope rail.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping

from istota import db

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
    """Every scope, which is what a restricted task withholds."""
    return frozenset(scope_names(skill_index))


def withheld_for_task(
    conn: sqlite3.Connection | None,
    task: "db.Task",
    *,
    skill_index: Mapping[str, object],
) -> frozenset[str]:
    """What one task may not reach, from its own row. The one derivation the
    executor's reach seams and the `skills` CLI's guard both read.

    Every scope on a guest's turn. Every scope, too, on a task in a room more
    than one human reads that no member asked there: one with no origin
    surface (a subtask, a CLI task, a heartbeat) unless it is the user's own
    cron job or briefing and the user is a current member. Such a task's
    answer lands in the room with no member asking, so the consent a member's
    turn carries does not reach it. Nothing otherwise: a member's turn runs at
    full reach in every room (ISSUE-576), and nothing is withheld in an email
    thread room, which is the host's correspondence (`is_email_thread_room`).

    ``conn`` is ``None`` for a database that does not exist, which holds no
    room. Any error reading the room withholds every scope.
    """
    from .surfaces import origin_surface_for_source_type

    if task.guest_participant_id is not None:
        return all_scopes(skill_index)
    if not task.conversation_token:
        return frozenset()
    if conn is not None:
        try:
            if is_email_thread_room(conn, task.conversation_token):
                return frozenset()
        except Exception as exc:
            logger.warning(
                "room_scopes: could not read the room %s, withholding every "
                "scope: %s", task.conversation_token, exc,
            )
            return all_scopes(skill_index)
    member_surface = origin_surface_for_source_type(task.source_type) is not None
    if not member_surface and (task.is_group_chat or task.audience == "mixed"):
        return all_scopes(skill_index)
    if conn is None:
        return frozenset()
    try:
        token = canonical_token(conn, task.conversation_token)
        if token is None or not (
            db.room_is_shared(conn, token) or task.is_group_chat
            or task.audience == "mixed"
        ):
            return frozenset()
        if not member_surface and not _members_own_schedule(conn, task, token):
            return all_scopes(skill_index)
    except Exception as exc:
        logger.warning(
            "room_scopes: could not read the room %s, withholding every "
            "scope: %s", task.conversation_token, exc,
        )
        return all_scopes(skill_index)
    return frozenset()


def _members_own_schedule(
    conn: sqlite3.Connection, task: "db.Task", room_token: str,
) -> bool:
    """Whether this task is the task's user's own CRON.md job or briefing,
    aimed at a room they are a current member of (ISSUE-594).

    The job lives in the user's workspace and names this room as where its
    answer is read, which is the member asking in advance. Under bwrap a
    guest's turn and a restricted task cannot write that file. Without it
    (Docker, macOS, standalone) a guest's tools can, but such a job could
    already be aimed at the host's private room at full reach, so this adds
    nothing a guest could not reach before. A ``scheduled`` task must point at
    a job row owned by its user; a briefing is only ever created by the
    scheduler from the user's own config.

    Membership is `is_current_member`.
    """
    if task.source_type == "scheduled":
        if task.scheduled_job_id is None:
            return False
        row = conn.execute(
            "SELECT user_id FROM scheduled_jobs WHERE id = ?",
            (task.scheduled_job_id,),
        ).fetchone()
        if row is None or row[0] != task.user_id:
            return False
    elif task.source_type == "briefing":
        if not task.briefing_name:
            return False
    else:
        return False
    return is_current_member(conn, room_token, task.user_id)


def is_current_member(conn: sqlite3.Connection, room_token: str, user_id: str) -> bool:
    """Whether ``user_id`` is in ``room_token`` now, not only once.

    A ``room_members`` row and, where the user has any principal participant
    row, a present one: Talk records a departure only as ``left_at`` and never
    removes the member row.
    """
    if not db.is_room_member(conn, room_token, user_id):
        return False
    present = conn.execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE left_at IS NULL) "
        "FROM room_participants WHERE room_token = ? AND user_id = ? "
        "AND kind = 'principal'",
        (room_token, user_id),
    ).fetchone()
    return present[0] == 0 or present[1] > 0


def _written_by_someone_else(conn: sqlite3.Connection, task: "db.Task") -> bool:
    """Whether the stored turn this task answers names an author other than
    the task's user. A row with neither column set predates attribution and is
    read as the user's, as every history reader reads it. Read by
    `private_replies.my_notes_room`: a correspondent's mail on an email thread
    runs as the host, and carries none of the host's private notes."""
    row = conn.execute(
        "SELECT author_user_id, author_label FROM messages "
        "WHERE task_id = ? AND role = 'user' ORDER BY id LIMIT 1",
        (task.id,),
    ).fetchone()
    if row is None:
        return False
    author_user_id, author_label = row[0], row[1]
    if author_user_id is None:
        return bool(author_label)
    return author_user_id != task.user_id


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

    Not in an email thread room, asked ahead of the audience and the roster
    (the poller marks every thread turn a group chat): the thread is the
    host's correspondence and their memory loads as on any email turn.
    """
    if task.guest_participant_id is not None:
        return True
    if task.conversation_token and conn is not None:
        try:
            if is_email_thread_room(conn, task.conversation_token):
                return False
        except Exception as exc:
            logger.warning(
                "room_scopes: could not read the room %s, leaving ambient "
                "memory out: %s", task.conversation_token, exc,
            )
            return True
    if task.audience == "mixed" or task.is_group_chat:
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


def is_email_thread_room(conn, token: str | None) -> bool:
    """Whether ``token`` names a room bound to an email thread.

    The one rule the three seams ask (the guest branch at ingest, the withheld
    scopes, the ambient memory): such a room is the host's correspondence, so
    an admitted turn on it runs as the host at full reach. The token form of
    `transport.email.threads.thread_room_for_task`. Raises what the reads
    raise; each caller chooses its failure direction.

    The user's private email room is bound to email too, under its creator's
    own token rather than a Message-ID, and is not a thread: its turns are the
    user's own (`transport.email.private_room`).
    """
    from istota.transport.email.private_room import is_private_email_ref

    room = canonical_token(conn, token)
    binding = db.get_room_binding(conn, room, "email") if room is not None else None
    if binding is None:
        return False
    owner = db.get_room(conn, room)
    return not is_private_email_ref(binding.surface_ref, owner.user_id if owner else None)


def canonical_token(conn, token: str | None) -> str | None:
    """The registry token a conversation token names, or None for no room.

    The one copy: `private_replies` re-exports it, and the memory skill CLI
    reaches it here without importing `private_replies`.
    """
    if not token:
        return None
    token = db._canonical_room_token(conn, token)
    return token if db.get_room(conn, token) is not None else None


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
    from istota.transport.sms import sms_conversation_token
    from istota.transport.whatsapp import whatsapp_conversation_token

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

    from istota.rooms import policy as room_policy

    room_token = canonical_token(conn, task.conversation_token)
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
