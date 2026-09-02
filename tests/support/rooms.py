"""The two shapes a room can have, built the way the product builds them.

A room borrows its canonical identity from the surface it was created on, so on
an ordinary Talk room `rooms.token` and the `talk` binding's `surface_ref` are
the same string. On a *promoted* room — created on web, later bound to Talk —
they differ, and that divergence is the only thing separating a call that
resolves the binding from one that hands the canonical token to the Talk API and
gets a 404 (ISSUE-400). Every delivery test in the tree used to build the first
shape, so no test could tell the two calls apart.

Both builders **reproduce** what the real producers write rather than inventing
a scheme of their own:

- `plain_talk_room` — `transport.ingest.record_inbound`'s room branch, for
  `surface="talk"`: register, bind `talk -> surface_ref`. `record_inbound` also
  calls `add_room_member` for its already-registered path; that is not repeated
  here because `_refuse_collision` below means `register_room` always inserts,
  and `register_room` makes the registering user a member itself.
- `promoted_room` — `db.create_web_chat_room` (the web handle, the registry row
  with `origin='web'`, the self-referential `web` binding) followed by
  `web_app._chat_promote_to_talk`'s binding write. Including that producer's
  name normalization, which is not decoration: it writes one normalized string
  to both rows, while the two primitives underneath it have different fallbacks
  and would let the handle and the registry row disagree.

Membership is part of that and is easy to leave out: a room with no
`room_members` row is invisible to `db.list_member_rooms`, so a test asserting
through the web room listing would fail for a reason that has nothing to do with
what it was testing. `tests/test_support_rooms.py` pins both builders against
those producers by running them and diffing every table in the database, rather
than against a hand-written list of expected rows or of expected tables.

Deliberately not reproduced: `record_inbound`'s `undismiss_room` call. It is a
DELETE that writes nothing on a fresh room, and doing it here would silently
clear a hide tombstone a test had just set up.

Depends only on `istota.db` — no pytest import, so this is usable from a plain
script.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from istota import db


@dataclass(frozen=True)
class RoomShape:
    """What a test needs to know about the room it just built."""

    canonical: str      # rooms.token — what a task's conversation_token holds
    talk_ref: str       # room_bindings.surface_ref for surface 'talk'
    origin: str         # 'talk' | 'web'
    name: str

    @property
    def diverges(self) -> bool:
        """Whether handing `canonical` to the Talk API would be wrong."""
        return self.canonical != self.talk_ref


def _new_talk_ref() -> str:
    """A Nextcloud Talk token's shape: 8 characters, here lowercase hex.

    A real token is mixed-case alphanumeric, so this generates a subset rather
    than the whole space — enough to be recognisable in a failure, and not
    enough to exercise case handling. A test that cares about that passes its
    own ref.
    """
    return uuid.uuid4().hex[:8]


def plain_talk_room(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    token: str | None = None,
    name: str | None = None,
) -> RoomShape:
    """An ordinary Talk room: the canonical token *is* the Talk ref.

    Today's shape, and the one every existing delivery test builds by hand.
    """
    token = token if token is not None else _new_talk_ref()
    # Unnormalized on purpose: `record_inbound` hands `channel_name` to
    # `register_room` exactly as Talk reported it. `promoted_room` below
    # normalizes because *its* producer does.
    name = name if name is not None else f"#{token}"
    _refuse_collision(conn, token, talk_ref=token)

    db.register_room(conn, token, user_id, origin="talk", name=name)
    db.add_room_binding(conn, token, "talk", token)
    return RoomShape(canonical=token, talk_ref=token, origin="talk", name=name)


def promoted_room(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    canonical: str | None = None,
    talk_ref: str | None = None,
    name: str | None = None,
) -> RoomShape:
    """A web room later bound to Talk: canonical and Talk ref differ.

    The generated canonical carries the `web-{user_id}-` prefix a real one has,
    because a caller reading a failure needs to recognise which token it is
    looking at.
    """
    suffix = uuid.uuid4().hex[:12]
    canonical = canonical if canonical is not None else f"web-{user_id}-{suffix}"
    talk_ref = talk_ref if talk_ref is not None else _new_talk_ref()
    name = name if name is not None else f"room-{suffix[:6]}"
    # `db.create_web_chat_room` normalizes once and writes the same string to
    # both the handle and the registry row. Passing the raw name to each would
    # let them disagree, because `ensure_web_chat_handle`'s own fallback is a
    # different word ("room") and `register_room` has none at all.
    name = name.strip() or "general"
    if canonical == talk_ref:
        raise ValueError(
            f"promoted_room needs two different tokens; got {canonical!r} twice. "
            "Use plain_talk_room for the non-diverging shape."
        )
    _refuse_collision(conn, canonical, talk_ref=talk_ref)

    # db.create_web_chat_room's writes, in its order.
    db.ensure_web_chat_handle(conn, user_id, canonical, name)
    db.register_room(conn, canonical, user_id, origin="web", name=name)
    db.add_room_binding(conn, canonical, "web", canonical)
    # web_app._chat_promote_to_talk's write (replace_room_binding with no
    # expected_ref, which is an INSERT OR IGNORE).
    db.add_room_binding(conn, canonical, "talk", talk_ref)
    return RoomShape(
        canonical=canonical, talk_ref=talk_ref, origin="web", name=name,
    )


def _refuse_collision(
    conn: sqlite3.Connection, canonical: str, *, talk_ref: str,
) -> None:
    """Two rooms in one test must not be the same room.

    Both writes are `INSERT OR IGNORE`, so a collision — a generated default
    that repeated, or a caller passing a token it already used — would silently
    return a `RoomShape` describing a room that is not the one on disk.
    """
    if db.get_room(conn, canonical) is not None:
        raise ValueError(f"room {canonical!r} is already registered")
    bound_to = db.resolve_room_token(conn, "talk", talk_ref)
    if bound_to is not None:
        raise ValueError(
            f"talk ref {talk_ref!r} is already bound to room {bound_to!r}"
        )
    # A Talk ref that is some *other* room's canonical token is ISSUE-400
    # planted in the fixture rather than exposed by it: `record_inbound`
    # resolves the binding first, so an inbound naming that token on Talk would
    # land in this room instead of the room the token names. A plain room binds
    # its own token, which is the one legitimate case.
    if talk_ref != canonical and db.get_room(conn, talk_ref) is not None:
        raise ValueError(
            f"talk ref {talk_ref!r} is another room's canonical token"
        )
    web_bound = db.resolve_room_token(conn, "web", canonical)
    if web_bound is not None:
        raise ValueError(
            f"canonical token {canonical!r} is already bound to room "
            f"{web_bound!r} on web"
        )
