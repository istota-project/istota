"""Finding a room a person named, among their own rooms (ISSUE-608).

One resolver for every place a user or a model names a room in words:
`!room notes <name>`, `memory --room`, `room post --room` and the
`nextcloud talk create` duplicate guard. Each used to match on its own, and
none of them could say "two rooms go by that name".

Rules, in order: a token (or a binding ref) matches its room exactly; then a
case-insensitive exact name; then a number from the list, for a caller that
showed the list; then a unique case-insensitive prefix. The candidate list is
ordered by ``(created_at, token)`` and numbered from 1, so a number stays the
same until the user's room list changes; nothing is remembered between calls.

Numbers are accepted only by `!room notes` (``by_number``), the one caller
whose list carries rooms the user left: a number read off that list and typed
into `room post --room` would otherwise pick another room from a shorter list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Union

from istota import db
from istota.rooms.scopes import canonical_token, is_current_member

_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_.@:/+-]+$")


@dataclass(frozen=True)
class Candidate:
    number: int
    token: str
    name: str
    room: db.Room
    #: The user has notes about this room (only filled when the caller passed
    #: the tokens it has notes for).
    has_notes: bool = False


@dataclass(frozen=True)
class Found:
    candidate: Candidate

    @property
    def room(self) -> db.Room:
        return self.candidate.room


@dataclass(frozen=True)
class Ambiguous:
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class NotFound:
    candidates: tuple[Candidate, ...]


RoomMatch = Union[Found, Ambiguous, NotFound]


def _was_ever_in(conn, token: str, user_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM room_members WHERE room_token = ? AND user_id = ? "
        "UNION ALL SELECT 1 FROM room_participants WHERE room_token = ? AND user_id = ? "
        "LIMIT 1",
        (token, user_id, token, user_id),
    ).fetchone() is not None


def room_candidates(
    conn, user_id: str, *, shared_only: bool = True,
    include_left_with_notes: Iterable[str] | None = None,
    include_dismissed: bool = False,
) -> list[Candidate]:
    """The rooms a query is matched against, numbered.

    ``shared_only`` keeps rooms the user is a current member of and more than
    one human reads. ``include_left_with_notes`` is the tokens the user has
    notes for; each such room is listed whatever its membership, since the
    notes are the user's and survive leaving.
    """
    noted: set[str] = set()
    for token in include_left_with_notes or ():
        token = canonical_token(conn, token) or token
        # A file name in a sandbox-writable directory is not evidence of
        # membership: only a room the user was once in is listed by name.
        if _was_ever_in(conn, token, user_id):
            noted.add(token)
    rooms: dict[str, db.Room] = {}
    for room in db.list_member_rooms(conn, user_id, include_dismissed=include_dismissed):
        if room.token in noted:
            rooms[room.token] = room
            continue
        if shared_only and not (
            is_current_member(conn, room.token, user_id)
            and db.room_is_shared(conn, room.token)
        ):
            continue
        rooms[room.token] = room
    for token in noted:
        if token not in rooms:
            room = db.get_room(conn, token)
            if room is not None:
                rooms[token] = room
    ordered = sorted(rooms.values(), key=lambda r: (r.created_at or "", r.token))
    return [
        Candidate(
            number=i, token=room.token,
            name=(db.room_display_name(room, None) or "").strip(),
            room=room, has_notes=room.token in noted,
        )
        for i, room in enumerate(ordered, start=1)
    ]


def resolve_room(
    conn, user_id: str, query: str, *, shared_only: bool = True,
    include_left_with_notes: Iterable[str] | None = None,
    include_dismissed: bool = False, names_only: bool = False,
    by_number: bool = False,
) -> RoomMatch:
    """Which of the user's rooms ``query`` names.

    ``names_only`` matches the exact name alone, for a caller asking "is there
    already a room called this" rather than "which room did they mean".
    """
    candidates = room_candidates(
        conn, user_id, shared_only=shared_only,
        include_left_with_notes=include_left_with_notes,
        include_dismissed=include_dismissed,
    )
    everything = tuple(candidates)
    query = (query or "").strip()
    if not query:
        return NotFound(everything)
    if not names_only and _TOKEN_SHAPE.match(query):
        token = canonical_token(conn, query)
        for c in candidates:
            if c.token == token:
                return Found(c)
    wanted = query.casefold()
    exact = [c for c in candidates if c.name and c.name.casefold() == wanted]
    if len(exact) == 1:
        return Found(exact[0])
    if exact:
        return Ambiguous(tuple(exact))
    if names_only:
        return NotFound(everything)
    if by_number and query.isdigit():
        number = int(query)
        if 1 <= number <= len(candidates):
            return Found(candidates[number - 1])
        return NotFound(everything)
    prefixed = [c for c in candidates if c.name and c.name.casefold().startswith(wanted)]
    if len(prefixed) == 1:
        return Found(prefixed[0])
    if prefixed:
        return Ambiguous(tuple(prefixed))
    return NotFound(everything)


def numbered_list(candidates: Iterable[Candidate], *, mark_notes: bool = False) -> str:
    """One line per candidate, ``n. name``, for a reply to the user."""
    lines = []
    for c in candidates:
        line = f"{c.number}. {c.name or c.token}"
        if mark_notes and c.has_notes:
            line += " (notes)"
        lines.append(line)
    return "\n".join(lines)
