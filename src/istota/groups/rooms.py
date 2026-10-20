"""Group-owned rooms: rooms whose membership is a group's.

A room linked to a group (`rooms.group_id`) keeps its own roster and host
rules; the link only filters which group's memory its turns carry. A
group-owned room (`rooms.membership_source = 'group'`, `db.is_group_owned_room`)
holds every current member of the linked group and nobody else, and the
group's current owners govern its settings. The room's host stays an
operational role for guest-policy and task plumbing, never an authority.

This module answers the group half of every question about such a room. The
roster half stays with `db` (`add_web_room_member`, `drop_web_room_member`) and
the room rules with `rooms.policy`, which calls in here for a group-owned room.
"""

from __future__ import annotations

import sqlite3

from istota import db


def _canonical_room(conn: sqlite3.Connection, room_token: str) -> db.Room | None:
    token = db._canonical_room_token(conn, room_token, cross_surface=False)
    return db.get_room(conn, token)


def group_room_access(conn: sqlite3.Connection, room_token: str, user_id: str) -> bool:
    """Whether the group relationship lets ``user_id`` into this room.

    For a group-owned room: a current member of its live linked group, which
    `db.is_group_member` decides (an ended membership, an archived group and a
    disabled identity all refuse). A group-owned row with no live group fails
    closed. For any other room the answer is True, because the room's own
    roster decides and the group link never does. An unknown room is False.

    This is the backstop behind roster synchronization, so a caller asks it in
    addition to room membership, never instead of it.
    """
    room = _canonical_room(conn, room_token)
    if room is None:
        return False
    if not db.is_group_owned_room(room):
        return True
    if not room.group_id:
        return False
    return db.is_group_member(conn, room.group_id, user_id)


def owner_settings_refusal(
    conn: sqlite3.Connection, room: db.Room, user_id: str,
) -> str | None:
    """Why ``user_id`` may not change a group-owned room's settings, or None.

    The group's current owners decide, whatever the headcount: a group room's
    history is read by every future member, so even its one present member is
    not its sole reader. The host is not consulted. A room whose group is gone
    or archived refuses everyone.
    """
    if room.group_id and db.is_group_owner(conn, room.group_id, user_id):
        return None
    group = room.group_id or "its group"
    return f"Only an owner of group '{group}' can change this room's settings."


def link_refusal(room: db.Room) -> str:
    """The refusal for any change to a group-owned room's group link."""
    return (
        f"This room's membership follows group '{room.group_id}', so its group "
        "link cannot change. Membership is managed in Groups."
    )
