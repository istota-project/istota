"""What a task in a shared room may reach: the scope vocabulary and the grants.

A room more than one human reads is an egress channel, so a task there reaches
only what its sender has granted in that room (`room_data_grants`). A scope is a
skill whose manifest says ``shared_room: private`` (the default), or one of the
two synthetic scopes that are not skills:

- ``files``: the sender's workspace, ``{mount}/Users/{user_id}``, and their
  per-resource mounts, which the sandbox binds only with this grant.
- ``memory``: ``USER.md``, dated memories, recalled memories, playbooks,
  knowledge-graph facts and per-skill overlays.

A ``shared_room: safe`` skill is never a scope, and the room's own
``CHANNEL.md`` is shared by construction. A grant is consent to disclose to
the room's members, so while a guest is present (the ``mixed`` audience, D3)
every grant is ignored and every scope is withheld; what needs one is answered
in the principal's side room instead (`side_rooms.queue_side_answer`). What is withheld is enforced by what
the task can reach, at the seams ``execute_task`` and ``task_env`` apply it to;
nothing here asks the model to keep anything to itself.

Every read here fails toward withholding: a grant that cannot be read is no
grant, and a room whose audience cannot be read is treated as shared.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Mapping

from . import db

logger = logging.getLogger(__name__)

SYNTHETIC_SCOPES = ("files", "memory")
POLICY_OFF = "off"


def scope_names(skill_index: Mapping[str, object]) -> list[str]:
    """Every scope a sender could grant: private skills, then the two synthetic."""
    private = sorted(
        name for name, meta in skill_index.items()
        if getattr(meta, "shared_room", "private") != "safe"
        and name not in SYNTHETIC_SCOPES
    )
    return [*private, *SYNTHETIC_SCOPES]


def granted_scopes(
    conn: sqlite3.Connection, room_token: str, user_id: str,
) -> frozenset[str]:
    """What ``user_id`` has granted in ``room_token``. Empty on any error."""
    try:
        rows = conn.execute(
            "SELECT scope FROM room_data_grants WHERE room_token = ? AND user_id = ?",
            (room_token, user_id),
        ).fetchall()
    except Exception as exc:
        logger.warning(
            "room_scopes: could not read grants for %s in %s: %s",
            user_id, room_token, exc,
        )
        return frozenset()
    return frozenset(row[0] for row in rows)


def withheld_scopes(
    skill_index: Mapping[str, object], granted: frozenset[str],
) -> frozenset[str]:
    """The scopes not granted."""
    return frozenset(scope_names(skill_index)) - granted


def grant_scopes(
    conn: sqlite3.Connection, room_token: str, user_id: str, scopes: Iterable[str],
) -> None:
    """Record ``user_id``'s own grant of each scope in ``room_token``.

    The one writer. It takes no second user: a grant is the caller's consent,
    and nobody grants on another member's behalf. Callers validate the names
    against `scope_names` first.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO room_data_grants (room_token, user_id, scope) "
        "VALUES (?, ?, ?)",
        [(room_token, user_id, scope) for scope in scopes],
    )


def revoke_scopes(
    conn: sqlite3.Connection, room_token: str, user_id: str,
    scopes: Iterable[str] | None = None,
) -> None:
    """Withdraw ``user_id``'s grants in ``room_token``: the named ones, or all."""
    if scopes is None:
        conn.execute(
            "DELETE FROM room_data_grants WHERE room_token = ? AND user_id = ?",
            (room_token, user_id),
        )
        return
    conn.executemany(
        "DELETE FROM room_data_grants WHERE room_token = ? AND user_id = ? AND scope = ?",
        [(room_token, user_id, scope) for scope in scopes],
    )


def is_scope_granted(
    conn: sqlite3.Connection, room_token: str, user_id: str, scope: str,
) -> bool:
    return scope in granted_scopes(conn, room_token, user_id)


GRANTS_ACTIVE = "active"
GRANTS_POLICY_OFF = "policy_off"
GRANTS_GUESTS_PRESENT = "guests_present"
GRANTS_PRIVATE = "private"


def grant_state(conn: sqlite3.Connection, room_token: str, *, policy: str) -> str:
    """Whether a member's grants in this room decide anything right now.

    ``policy_off``: the disclosure gate is off and nothing is withheld from a
    member's turn. ``guests_present``: a guest reads the room, so every grant is
    ignored (D3). ``private``: one human reads it, so a grant applies once
    somebody joins. ``active``: grants are what a member's turn may reach.
    `!room share` and the web grants pane both word their answer from this.
    """
    from . import room_policy

    if policy == POLICY_OFF:
        return GRANTS_POLICY_OFF
    if room_policy.audience_class(conn, room_token) == room_policy.MIXED:
        return GRANTS_GUESTS_PRESENT
    if not db.room_is_shared(conn, room_token):
        return GRANTS_PRIVATE
    return GRANTS_ACTIVE


def task_withheld_scopes(
    conn: sqlite3.Connection,
    *,
    policy: str,
    conversation_token: str,
    user_id: str,
    skill_index: Mapping[str, object],
    assume_shared: bool = False,
    assume_mixed: bool = False,
) -> frozenset[str]:
    """The scopes a task in this conversation may not reach; empty when none.

    Empty for a policy of exactly ``"off"``, for a task with no conversation,
    and for a room one human reads. The conversation token is mapped to its
    canonical room first, since a task can carry a surface's ref (an email
    continuation on a promoted room carries the Talk token) and grants are kept
    against the room.

    ``assume_shared`` is the task's own ``is_group_chat``: ingest set it from
    the surface's roster, which can say "group" on a turn where
    ``room_is_shared`` cannot yet (a Talk group's first turn, a batch whose
    roster fetch failed). Either signal restricts.

    ``assume_mixed`` is the audience stored with the turn. A guest present when
    the turn was written, or present now, makes it ``mixed`` and withholds
    every scope whatever was granted: the answer may be read by the guest who
    was there, and is read by whoever is there now.
    """
    if policy == POLICY_OFF or not conversation_token:
        return frozenset()
    from . import room_policy

    try:
        room_token = conversation_token
        if db.get_room(conn, room_token) is None:
            room_token = db.find_room_token_by_ref(conn, room_token) or room_token
        shared = assume_shared or db.room_is_shared(conn, room_token)
        mixed = assume_mixed or room_policy.audience_class(
            conn, room_token, is_group_chat=assume_shared,
        ) == room_policy.MIXED
    except Exception as exc:
        logger.warning(
            "room_scopes: could not read the audience of %s, withholding every "
            "scope: %s", conversation_token, exc,
        )
        return withheld_scopes(skill_index, frozenset())
    if mixed:
        return withheld_scopes(skill_index, frozenset())
    if not shared:
        return frozenset()
    return withheld_scopes(skill_index, granted_scopes(conn, room_token, user_id))


def withheld_for_task(
    conn: sqlite3.Connection | None,
    task: "db.Task",
    *,
    policy: str,
    skill_index: Mapping[str, object],
) -> frozenset[str]:
    """What one task may not reach, from its own row. The one derivation the
    executor's reach seams and the `skills` CLI's guard both read.

    A guest's turn (emissary mode, multiplayer D2) withholds every scope,
    whatever the host granted and whatever the disclosure policy says: a grant
    is consent to disclose when the host asks, and ``off`` switches the grant
    gate, not who a guest may speak for. Otherwise the room's answer for the
    task's own user, conversation, group flag and stored audience, which needs
    ``conn``; a guest's turn is answered from the row and reads none.
    """
    if task.guest_participant_id is not None:
        return withheld_scopes(skill_index, frozenset())
    if policy == POLICY_OFF or not task.conversation_token:
        return frozenset()
    return task_withheld_scopes(
        conn,
        policy=policy,
        conversation_token=task.conversation_token,
        user_id=task.user_id,
        skill_index=skill_index,
        assume_shared=bool(task.is_group_chat),
        assume_mixed=task.audience == "mixed",
    )


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
    principal participants.

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

    Independent of the room's grants. A grant is the sender's consent to
    disclose their own data and never reaches group material; the audience
    rule here is the only gate, and no grant widens it. Raises on a database
    error; the caller treats that as the empty set.
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
    return [
        g for g in groups if readers <= set(db.list_group_members(conn, g))
    ]
