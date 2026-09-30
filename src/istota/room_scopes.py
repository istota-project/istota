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
``CHANNEL.md`` is shared by construction. What is withheld is enforced by what
the task can reach, at the seams ``execute_task`` and ``task_env`` apply it to;
nothing here asks the model to keep anything to itself.

Every read here fails toward withholding: a grant that cannot be read is no
grant, and a room whose audience cannot be read is treated as shared.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping

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


def is_scope_granted(
    conn: sqlite3.Connection, room_token: str, user_id: str, scope: str,
) -> bool:
    return scope in granted_scopes(conn, room_token, user_id)


def task_withheld_scopes(
    conn: sqlite3.Connection,
    *,
    policy: str,
    conversation_token: str,
    user_id: str,
    skill_index: Mapping[str, object],
    assume_shared: bool = False,
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
    """
    if policy == POLICY_OFF or not conversation_token:
        return frozenset()
    try:
        room_token = conversation_token
        if db.get_room(conn, room_token) is None:
            room_token = db.find_room_token_by_ref(conn, room_token) or room_token
        shared = assume_shared or db.room_is_shared(conn, room_token)
    except Exception as exc:
        logger.warning(
            "room_scopes: could not read the audience of %s, withholding every "
            "scope: %s", conversation_token, exc,
        )
        return withheld_scopes(skill_index, frozenset())
    if not shared:
        return frozenset()
    return withheld_scopes(skill_index, granted_scopes(conn, room_token, user_id))
