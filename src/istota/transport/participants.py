"""Who is in a room, and what kind of participant wrote a turn (multiplayer D1).

A surface normalizes its sender into a `ParticipantRef`; this module decides the
kind, deterministically and with no model:

- ``agent`` — a bot. The surface's own report (a Talk ``bots`` actor), or the
  bot itself on that surface, whatever the surface says.
- ``principal`` — an istota user who is a member of the room.
- ``guest`` — any other human, including an istota user who is not a member:
  authority comes from membership, identity only says who it would be.

It also holds the one multi-human predicate the speech gate and the classifier
pre-pass share, so the two cannot disagree about a room.
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING

from .. import db
from istota.rooms.surfaces import is_room_member_for
from ._types import ParticipantRef

if TYPE_CHECKING:
    from ..config import Config

PRINCIPAL = "principal"
GUEST = "guest"
AGENT = "agent"

#: Longest guest label stored on a message row.
MAX_LABEL_CHARS = 80

_UNSAFE_LABEL_CHARS = re.compile(r"[<>\x00-\x1f\x7f\u0085  ]+")


def is_the_bot(config: "Config", ref: ParticipantRef) -> bool:
    """Whether ``ref`` is this deployment's own identity on its surface."""
    ref_id = (ref.surface_ref or "").strip()
    if not ref_id:
        return False
    if ref.surface == "talk":
        return bool(config.talk.bot_username) and ref_id == config.talk.bot_username
    if ref.surface == "email":
        bot_email = (config.email.bot_email or "").strip().lower()
        return bool(bot_email) and ref_id.lower() == bot_email
    return False


def classify(
    conn: sqlite3.Connection, config: "Config", room_token: str, ref: ParticipantRef,
) -> str:
    """The kind of participant ``ref`` is in ``room_token``. Never raises."""
    if ref.is_bot or is_the_bot(config, ref):
        return AGENT
    if ref.user_id and db.is_room_member(conn, room_token, ref.user_id):
        return PRINCIPAL
    return GUEST


def guest_label(ref: ParticipantRef) -> str:
    """The label a non-user author's turn is stored under.

    The display name is text the participant chose, and it is rendered in the
    web transcript and in a later prompt's speaker position, so it is flattened
    to one line, stripped of angle brackets (the prompt renders a label inside
    them) and capped. Falls back to the surface ref, then to the unattributed
    sentinel: an empty label would render the turn as the room owner's.
    """
    for candidate in (ref.display_name, ref.surface_ref):
        text = _UNSAFE_LABEL_CHARS.sub(" ", str(candidate or ""))
        text = " ".join(text.split())[:MAX_LABEL_CHARS].strip()
        if text:
            return text
    return db.UNATTRIBUTED_SENDER


def is_multi_human(
    conn: sqlite3.Connection, *, surface: str, room_token: str, is_group_chat: bool,
    room_container: bool = False,
) -> bool:
    """Whether a turn on ``surface`` in ``room_token`` is in front of more than one human.

    The surface's own answer (``is_group_chat``, a Talk roster) or, for a
    surface that owns rooms, ``db.room_is_shared``. A guest surface (email)
    keeps its own answer only: its turn is mirrored into a room's transcript,
    but its reply goes back by mail, so the room's audience is not its own.
    ``room_container`` is a room on a surface that owns none in general — a
    WhatsApp group or a multi-party email thread (D6, D10) — and counts as one
    that does.
    """
    if is_group_chat:
        return True
    if not is_room_member_for(surface, room_container=room_container):
        return False
    return db.room_is_shared(conn, room_token)
