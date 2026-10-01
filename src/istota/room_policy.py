"""Who hosts a room, how it treats guests, and who reads it (multiplayer Stage 11).

A room that more than one human reads, or that a guest writes in, has one
`room_policy` row. The row is made the first time the room needs it, from the
room as it is then, and never before, so a private room carries none and every
rule below is a no-op for it.

- **The host** is the istota user a guest's turn runs as (D2): the room's
  creator, or its first member when the creator has gone. When the host
  leaves, the room loses its host and goes record-only until a principal
  claims it with `!room host` (D14). Losing a host is sticky: a host who comes
  back has to claim the room like anyone else, since no hand-off is automatic.
- **`guest_reply`** decides what a guest-triggered answer does (D11): `off`
  records the turn and answers nothing, `held` proposes the answer in the
  host's side room, `direct` posts it in the room at room-safe reach.
- **The loop cap** (D9): at most `max_bot_turns_without_human` bot turns since
  a principal last spoke, after which a guest's turn is recorded only.
- **The audience class** (D3): `private` for one human, `principals` when
  every present human is a member, `mixed` once a guest is present.

Every function takes the caller's connection and writes nothing the caller did
not ask for, apart from the sticky host loss, which is a fact about the room
and is recorded the first time it is observed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db

GUEST_REPLY_VALUES = ("off", "held", "direct")
OFF, HELD, DIRECT = GUEST_REPLY_VALUES

PRIVATE = "private"
PRINCIPALS = "principals"
MIXED = "mixed"

# D11: chosen per room at creation from the surface the room lives on. Any
# surface not named here takes the conservative answer.
_DEFAULT_GUEST_REPLY = {"talk": DIRECT, "web": DIRECT, "whatsapp": HELD, "email": HELD}


@dataclass(frozen=True)
class RoomPolicy:
    room_token: str
    host_user_id: str | None
    speech_mode: str | None
    guest_reply: str
    record_guests: bool
    vetoed_by: int | None
    max_bot_turns_without_human: int


def default_guest_reply(surface: object) -> str:
    return _DEFAULT_GUEST_REPLY.get(surface if isinstance(surface, str) else "", HELD)


def _row_to_policy(row) -> RoomPolicy:
    return RoomPolicy(
        room_token=row["room_token"],
        host_user_id=row["host_user_id"],
        speech_mode=row["speech_mode"],
        guest_reply=row["guest_reply"],
        record_guests=bool(row["record_guests"]),
        vetoed_by=row["vetoed_by"],
        max_bot_turns_without_human=int(row["max_bot_turns_without_human"]),
    )


def get_policy(conn: sqlite3.Connection, room_token: str) -> RoomPolicy | None:
    row = conn.execute(
        "SELECT * FROM room_policy WHERE room_token = ?", (room_token,),
    ).fetchone()
    return _row_to_policy(row) if row else None


def host_present(conn: sqlite3.Connection, room_token: str, user_id: str) -> bool:
    """Whether ``user_id`` is still in the room: a member, and not someone every
    one of whose participant rows has ended (they left the surface's roster).

    A member who hid the room is still in it. Hiding drops web membership and
    writes a dismissal tombstone, but it is a view preference; reading it as
    leaving would take a room's host away because they tidied their sidebar.
    """
    if not (db.is_room_member(conn, room_token, user_id)
            or db.is_room_dismissed(conn, room_token, user_id)):
        return False
    rows = conn.execute(
        "SELECT left_at FROM room_participants WHERE room_token = ? AND user_id = ?",
        (room_token, user_id),
    ).fetchall()
    return not rows or any(row["left_at"] is None for row in rows)


def _first_present_member(conn: sqlite3.Connection, room: db.Room) -> str | None:
    if host_present(conn, room.token, room.user_id):
        return room.user_id
    rows = conn.execute(
        "SELECT user_id FROM room_members WHERE room_token = ? ORDER BY created_at, user_id",
        (room.token,),
    ).fetchall()
    for row in rows:
        if host_present(conn, room.token, row["user_id"]):
            return row["user_id"]
    return None


def ensure_policy(conn: sqlite3.Connection, room_token: str) -> RoomPolicy | None:
    """The room's policy, made now if it has none. None for no room or a side room.

    The host is fixed here, once: the creator if they are still in the room,
    else the first member who is.
    """
    policy = get_policy(conn, room_token)
    if policy is not None:
        return policy
    room = db.get_room(conn, room_token)
    if room is None or room.side_of:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO room_policy (room_token, host_user_id, guest_reply) "
        "VALUES (?, ?, ?)",
        (room_token, _first_present_member(conn, room), default_guest_reply(room.origin)),
    )
    return get_policy(conn, room_token)


def current_host(conn: sqlite3.Connection, policy: RoomPolicy | None) -> str | None:
    """The host, or None when there is none. A host found to have left is
    cleared on the row, so a later return does not restore them (D14)."""
    if policy is None or not policy.host_user_id:
        return None
    if host_present(conn, policy.room_token, policy.host_user_id):
        return policy.host_user_id
    lose_host(conn, policy.room_token, policy.host_user_id)
    return None


def lose_host(conn: sqlite3.Connection, room_token: str, user_id: str) -> None:
    """Record that ``user_id`` no longer hosts the room (D14). Sticky."""
    conn.execute(
        "UPDATE room_policy SET host_user_id = NULL "
        "WHERE room_token = ? AND host_user_id = ?",
        (room_token, user_id),
    )


def claim_host(conn: sqlite3.Connection, room_token: str, user_id: str) -> str:
    """``!room host``: a member claims a room with no host.

    Returns ``claimed``, ``already_host``, ``held_by_another`` (a present host
    is never displaced) or ``not_a_member``.
    """
    if not host_present(conn, room_token, user_id):
        return "not_a_member"
    policy = ensure_policy(conn, room_token)
    if policy is None:
        return "not_a_member"
    host = current_host(conn, policy)
    if host == user_id:
        return "already_host"
    if host is not None:
        return "held_by_another"
    claimed = conn.execute(
        "UPDATE room_policy SET host_user_id = ? WHERE room_token = ? AND host_user_id IS NULL",
        (user_id, room_token),
    ).rowcount
    return "claimed" if claimed else "held_by_another"


def settings_refusal(conn: sqlite3.Connection, room_token: str, user_id: str) -> str | None:
    """Why ``user_id`` may not change this room's standing settings, or None.

    A room's name, model, effort and brain, and whether it is also a Talk
    conversation, apply to every member's turn. In a room more than one human
    reads they are therefore the host's, the same authority `!room guests`
    takes; in a private room or a side room the one member changes them freely.
    Per-member choices (a colour, hiding the room) and the room's notes are not
    settings: every member has those.
    """
    room = db.get_room(conn, room_token)
    if room is None or room.side_of or not db.room_is_shared(conn, room_token):
        return None
    host = current_host(conn, ensure_policy(conn, room_token))
    if host == user_id:
        return None
    if host is None:
        return (
            "This room has no host, so nobody can change its settings. A member "
            "claims it with `!room host`."
        )
    return f"Only this room's host ({host}) can change its settings."


def guest_reply_refusal(conn: sqlite3.Connection, room_token: str, user_id: str) -> str | None:
    """Why ``user_id`` may not change how guests are answered here, or None.

    Host only, in a private room as in a shared one: the host is who a guest's
    turn runs as, so the choice is theirs. `!room guests` and the web PATCH
    both ask this.
    """
    policy = ensure_policy(conn, room_token)
    if policy is None:
        return "This room has no guest policy."
    if current_host(conn, policy) != user_id:
        return "Only this room's host can change how guests are answered."
    return None


def set_guest_reply(conn: sqlite3.Connection, room_token: str, value: str) -> RoomPolicy:
    if value not in GUEST_REPLY_VALUES:
        raise ValueError(f"guest_reply must be one of {GUEST_REPLY_VALUES}")
    policy = ensure_policy(conn, room_token)
    if policy is None:
        raise ValueError("no such room")
    conn.execute(
        "UPDATE room_policy SET guest_reply = ? WHERE room_token = ?", (value, room_token),
    )
    return get_policy(conn, room_token)


def audience_class(
    conn: sqlite3.Connection, room_token: str, *, is_group_chat: bool = False,
) -> str:
    """Who reads a turn in this room now. A present guest makes it ``mixed``
    whatever else holds; ``is_group_chat`` is the surface's own roster, which
    can say "group" before anyone is recorded."""
    guest = conn.execute(
        "SELECT 1 FROM room_participants WHERE room_token = ? AND kind = 'guest' "
        "AND left_at IS NULL LIMIT 1",
        (room_token,),
    ).fetchone()
    if guest is not None:
        return MIXED
    if is_group_chat or db.room_is_shared(conn, room_token):
        return PRINCIPALS
    return PRIVATE


@dataclass(frozen=True)
class RoomReaders:
    """Who reads a shared room now, for the room card (D7). Ids and a count only.

    Members are named by istota user id, which the operator assigns; guests are
    counted and never named, because a display name is text its owner chose and
    the card is in the system half.
    """
    members: tuple[str, ...]
    guests: int
    host: str | None


def room_readers(conn: sqlite3.Connection, room_token: str) -> RoomReaders:
    """Read-only: unlike `current_host`, a host found gone is reported, not cleared."""
    members = set(db.list_room_members(conn, room_token))
    rows = conn.execute(
        "SELECT kind, user_id, surface, surface_ref FROM room_participants "
        "WHERE room_token = ? AND left_at IS NULL AND kind != 'agent'",
        (room_token,),
    ).fetchall()
    guests = set()
    for row in rows:
        if row["kind"] == "principal" and row["user_id"]:
            members.add(row["user_id"])
        elif row["kind"] == "guest":
            guests.add((row["surface"], row["surface_ref"]))
    policy = get_policy(conn, room_token)
    if policy is None:
        # No row yet: the host `ensure_policy` would fix, without writing it.
        room = db.get_room(conn, room_token)
        host = _first_present_member(conn, room) if room is not None else None
    else:
        host = policy.host_user_id
        if host and not host_present(conn, room_token, host):
            host = None
    return RoomReaders(tuple(sorted(members)), len(guests), host)


def bot_turns_since_principal(conn: sqlite3.Connection, room_token: str) -> int:
    """Assistant turns in the room since a member last wrote one.

    A member is any row with an istota author; a guest's or an agent's turn
    carries none, so neither resets the count.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE room_token = ? AND role = 'assistant' "
        "AND id > COALESCE((SELECT MAX(id) FROM messages WHERE room_token = ? "
        "AND role = 'user' AND author_user_id IS NOT NULL), 0)",
        (room_token, room_token),
    ).fetchone()
    return int(row[0])
