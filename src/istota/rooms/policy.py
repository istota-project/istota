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
  records the turn and answers nothing, `held` proposes the answer to the
  host privately, `direct` posts it in the room at room-safe reach.
- **`speech_mode`** overrides ``[speech_gate] mode`` for this room's
  unaddressed turns, in either direction; NULL follows the deployment
  (ISSUE-640). Host only, and never set on an email thread room.
- **`disposition`** overrides ``[speech_gate] disposition`` for this room,
  under the same rule as ``speech_mode``; NULL follows the deployment
  (ISSUE-654). Read only while the room is on the classifier.
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

from istota import db
from .speech_gate import DISPOSITIONS as _DISPOSITIONS
from .speech_gate import MODES as _SPEECH_MODES
from .speech_gate import normalize_disposition, normalize_mode

GUEST_REPLY_VALUES = ("off", "held", "direct")
OFF, HELD, DIRECT = GUEST_REPLY_VALUES

#: What `set_speech_mode` takes: a gate mode, or ``default`` to follow the
#: deployment's ``[speech_gate] mode`` again.
DEFAULT_SPEECH_MODE = "default"
SPEECH_MODE_VALUES = (*_SPEECH_MODES, DEFAULT_SPEECH_MODE)

#: What `set_disposition` takes: a disposition, or ``default`` to follow the
#: deployment's ``[speech_gate] disposition`` again.
DEFAULT_DISPOSITION = "default"
DISPOSITION_VALUES = (*_DISPOSITIONS, DEFAULT_DISPOSITION)

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
    vetoed_by: int | None
    max_bot_turns_without_human: int
    vetoed_at: str | None = None
    veto_on_by: str | None = None
    announced_at: str | None = None
    disposition: str | None = None


def default_guest_reply(surface: object) -> str:
    return _DEFAULT_GUEST_REPLY.get(surface if isinstance(surface, str) else "", HELD)


def effective_speech_mode(
    conn: sqlite3.Connection, room_token: str | None, deployment_mode: str,
) -> str:
    """The speech mode a room's unaddressed turns are decided by (D5).

    The room's own ``speech_mode`` when it names a mode; otherwise the
    deployment's, except that an email thread room never defaults to the
    classifier: speaking there is a reply-all to everyone on the thread, so
    only the bot in To makes it speak. `speech_mode_refusal` keeps a host from
    setting one there, so only a row written before ISSUE-640 can hold one.
    """
    if not room_token:
        return deployment_mode
    policy = get_policy(conn, room_token)
    own = normalize_mode(policy.speech_mode) if policy is not None else None
    if own:
        return own
    if normalize_mode(deployment_mode) == "classifier":
        room = db.get_room(conn, room_token)
        if room is not None and room.origin == "email":
            return "mention"
    return deployment_mode


def speech_mode_source(
    conn: sqlite3.Connection, room_token: str | None, deployment_mode: str,
) -> tuple[str, bool]:
    """``effective_speech_mode`` plus whether the room's own value chose it."""
    policy = get_policy(conn, room_token) if room_token else None
    own = normalize_mode(policy.speech_mode) if policy is not None else None
    return effective_speech_mode(conn, room_token, deployment_mode), own is not None


def own_disposition(policy: RoomPolicy | None) -> str | None:
    """The room's stored disposition, or None when it follows the deployment.

    An unrecognised stored value is ``reserved``, the narrower one, as an
    unrecognised deployment value is.
    """
    if policy is None or policy.disposition is None:
        return None
    if not str(policy.disposition).strip():
        return None
    return normalize_disposition(policy.disposition)


def effective_disposition(
    conn: sqlite3.Connection, room_token: str | None, deployment_value: object,
) -> str:
    """The disposition the classifier uses for this room (ISSUE-654): the
    room's own when set, else ``[speech_gate] disposition``."""
    return disposition_source(conn, room_token, deployment_value)[0]


def disposition_source(
    conn: sqlite3.Connection, room_token: str | None, deployment_value: object,
) -> tuple[str, bool]:
    """``effective_disposition`` plus whether the room's own value chose it."""
    policy = get_policy(conn, room_token) if room_token else None
    own = own_disposition(policy)
    if own is not None:
        return own, True
    return normalize_disposition(deployment_value), False


def classifier_in_use(conn: sqlite3.Connection, deployment_mode: str) -> bool:
    """Whether any room could be on the classifier: the deployment is, or a
    room opted in on its own (ISSUE-640).

    The classifier pre-passes ask this before anything per room, so a
    `mention` deployment with no opted-in room still costs no model call and
    no roster fetch, and one that has an opted-in room reaches the per-room
    check rather than returning before it.
    """
    if normalize_mode(deployment_mode) == "classifier":
        return True
    row = conn.execute(
        "SELECT 1 FROM room_policy "
        "WHERE LOWER(TRIM(speech_mode)) = 'classifier' LIMIT 1"
    ).fetchone()
    return row is not None


def _row_to_policy(row) -> RoomPolicy:
    return RoomPolicy(
        room_token=row["room_token"],
        host_user_id=row["host_user_id"],
        speech_mode=row["speech_mode"],
        guest_reply=row["guest_reply"],
        vetoed_by=row["vetoed_by"],
        max_bot_turns_without_human=int(row["max_bot_turns_without_human"]),
        vetoed_at=row["vetoed_at"],
        veto_on_by=row["veto_on_by"],
        announced_at=row["announced_at"],
        disposition=row["disposition"],
    )


def get_policy(conn: sqlite3.Connection, room_token: str) -> RoomPolicy | None:
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
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
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
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
    """The room's policy, made now if it has none. None for no room.

    The host is fixed here, once: the creator if they are still in the room,
    else the first member who is.
    """
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
    policy = get_policy(conn, room_token)
    if policy is not None:
        return policy
    room = db.get_room(conn, room_token)
    if room is None:
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
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
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
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
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
    takes; in a private room the one member changes them freely.
    Per-member choices (a colour, hiding the room) and the room's notes are not
    settings: every member has those.
    """
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
    room = db.get_room(conn, room_token)
    if room is None or not db.room_is_shared(conn, room_token):
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
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
    policy = ensure_policy(conn, room_token)
    if policy is None:
        return "This room has no guest policy."
    if current_host(conn, policy) != user_id:
        return "Only this room's host can change how guests are answered."
    return None


def group_link_refusal(
    conn: sqlite3.Connection, room_token: str, user_id: str, group_id: str | None,
) -> str | None:
    """Why ``user_id`` may not link this room to ``group_id`` (None: unlink), or None.

    The link decides whose shared memory every member's turn carries, so it is
    a room setting and takes `settings_refusal`'s authority: the host in a room
    more than one human reads, the one member of a private room. Linking also
    needs the caller to be a current member of a live group;
    every refusal of the group reads the same, as `kv --group`'s does, so the
    command is not a way to learn which groups exist.
    """
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
    room = db.get_room(conn, room_token)
    if room is None:
        return "This room isn't registered yet."
    if not host_present(conn, room_token, user_id):
        return "Only a member of this room can link it to a group."
    refusal = settings_refusal(conn, room_token, user_id)
    if refusal:
        return refusal
    if group_id is None:
        return None
    if not (db.is_valid_group_id(group_id)
            and db.is_group_member(conn, group_id, user_id)):
        return f"You are not a member of group '{group_id}'."
    return None


def set_guest_reply(conn: sqlite3.Connection, room_token: str, value: str) -> RoomPolicy:
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
    if value not in GUEST_REPLY_VALUES:
        raise ValueError(f"guest_reply must be one of {GUEST_REPLY_VALUES}")
    policy = ensure_policy(conn, room_token)
    if policy is None:
        raise ValueError("no such room")
    conn.execute(
        "UPDATE room_policy SET guest_reply = ? WHERE room_token = ?", (value, room_token),
    )
    return get_policy(conn, room_token)


def speech_mode_refusal(conn: sqlite3.Connection, room_token: str, user_id: str) -> str | None:
    """Why ``user_id`` may not change when the bot speaks here, or None.

    Host only, like `guest_reply_refusal`. Refused outright in an email thread
    room, where speaking is a reply-all to everyone on the thread, so only the
    bot in To makes it speak. `!room speak`, `!room disposition` and the web
    PATCH all ask this, since the disposition is part of the same decision.
    """
    from .scopes import is_email_thread_room

    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
    if is_email_thread_room(conn, room_token):
        return (
            "This room is an email thread: I reply only when the mail is "
            "addressed to me, and that is not a setting."
        )
    policy = ensure_policy(conn, room_token)
    if policy is None:
        return "This room has no speech setting."
    if current_host(conn, policy) != user_id:
        return "Only this room's host can change when I speak here."
    return None


def set_speech_mode(conn: sqlite3.Connection, room_token: str, value: str) -> RoomPolicy:
    """Set the room's own speech mode; ``default`` clears it (NULL), so the
    room follows ``[speech_gate] mode`` again."""
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
    if value not in SPEECH_MODE_VALUES:
        raise ValueError(f"speech_mode must be one of {SPEECH_MODE_VALUES}")
    policy = ensure_policy(conn, room_token)
    if policy is None:
        raise ValueError("no such room")
    conn.execute(
        "UPDATE room_policy SET speech_mode = ? WHERE room_token = ?",
        (None if value == DEFAULT_SPEECH_MODE else value, room_token),
    )
    return get_policy(conn, room_token)


def set_disposition(conn: sqlite3.Connection, room_token: str, value: str) -> RoomPolicy:
    """Set the room's own disposition; ``default`` clears it (NULL), so the
    room follows ``[speech_gate] disposition`` again."""
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
    if value not in DISPOSITION_VALUES:
        raise ValueError(f"disposition must be one of {DISPOSITION_VALUES}")
    policy = ensure_policy(conn, room_token)
    if policy is None:
        raise ValueError("no such room")
    conn.execute(
        "UPDATE room_policy SET disposition = ? WHERE room_token = ?",
        (None if value == DEFAULT_DISPOSITION else value, room_token),
    )
    return get_policy(conn, room_token)


def audience_class(
    conn: sqlite3.Connection, room_token: str, *, is_group_chat: bool = False,
) -> str:
    """Who reads a turn in this room now. A present guest makes it ``mixed``
    whatever else holds; ``is_group_chat`` is the surface's own roster, which
    can say "group" before anyone is recorded."""
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
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

    ``others`` counts the present participants that are neither: agents, and
    a principal row no user id was mapped onto. The card does not show them;
    the group gate (``room_scopes.task_group_ids``) treats any of them as a
    reader who is not a group member.
    """
    members: tuple[str, ...]
    guests: int
    host: str | None
    others: int = 0


def room_readers(conn: sqlite3.Connection, room_token: str) -> RoomReaders:
    """Read-only: unlike `current_host`, a host found gone is reported, not cleared."""
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
    members = set(db.list_room_members(conn, room_token))
    rows = conn.execute(
        "SELECT kind, user_id, surface, surface_ref FROM room_participants "
        "WHERE room_token = ? AND left_at IS NULL",
        (room_token,),
    ).fetchall()
    guests = set()
    others = set()
    for row in rows:
        if row["kind"] == "principal" and row["user_id"]:
            members.add(row["user_id"])
        elif row["kind"] == "guest":
            guests.add((row["surface"], row["surface_ref"]))
        else:
            others.add((row["surface"], row["surface_ref"]))
    policy = get_policy(conn, room_token)
    if policy is None:
        # No row yet: the host `ensure_policy` would fix, without writing it.
        room = db.get_room(conn, room_token)
        host = _first_present_member(conn, room) if room is not None else None
    else:
        host = policy.host_user_id
        if host and not host_present(conn, room_token, host):
            host = None
    return RoomReaders(tuple(sorted(members)), len(guests), host, len(others))


def bot_turns_since_principal(conn: sqlite3.Connection, room_token: str) -> int:
    """Assistant turns in the room since a member last wrote one.

    A member is any row with an istota author; a guest's or an agent's turn
    carries none, so neither resets the count.
    """
    room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE room_token = ? AND role = 'assistant' "
        "AND id > COALESCE((SELECT MAX(id) FROM messages WHERE room_token = ? "
        "AND role = 'user' AND author_user_id IS NOT NULL), 0)",
        (room_token, room_token),
    ).fetchone()
    return int(row[0])
