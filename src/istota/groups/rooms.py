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

Creation (`create_group_room`) owns one immediate transaction, like the
`groups.service` mutations. Reconciliation (`reconcile_group_room_members`)
runs inside the caller's: every group membership mutation calls it before its
commit, so a group's membership and its rooms' rosters never disagree in
committed state.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from istota import db
from istota.groups.policy import GroupError, require_group_member
from istota.rooms import policy as room_policy

#: A client-generated idempotency token: a UUID fits, a person's typing does not.
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")


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


def _request_hash(actor_id: str, name: str) -> str:
    payload = json.dumps({"actor": actor_id, "name": name}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_group_room(
    conn: sqlite3.Connection, actor_id: str, group_id: str, name: str,
    request_id: str,
) -> dict:
    """Create a room whose membership is ``group_id``'s, or return the one this
    request already made.

    Owner only, in a live group. The connection must have no open transaction:
    the room, its link, every member's handle and presence, the host and the
    request record commit together. A replay of ``request_id`` by the same
    actor with the same name returns the same room; anything else is a 409.

    Returns ``{"room", "handle", "created"}``: the registry room, the actor's
    own `web_chat_rooms` handle, and whether this call made the room.
    """
    from istota.groups.service import _metadata, _transaction

    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        raise GroupError(422, "invalid_request_id",
                         "A request id of 8-128 letters, digits or ._:- is required")
    _metadata(name, "Room name")
    name = name.strip()
    request_hash = _request_hash(actor_id, name)
    with _transaction(conn):
        require_group_member(conn, group_id, actor_id)
        if not db.is_group_owner(conn, group_id, actor_id):
            raise GroupError(403, "group_forbidden", "Group owner required")
        row = conn.execute(
            "SELECT room_token, request_hash FROM group_room_requests "
            "WHERE group_id = ? AND request_id = ?",
            (group_id, request_id),
        ).fetchone()
        if row is not None:
            if row["request_hash"] != request_hash:
                raise GroupError(409, "request_conflict",
                                 "This request id was already used for another room")
            token = row["room_token"]
            created = False
        else:
            token = db.create_web_chat_room(conn, actor_id, name).token
            db.set_room_group(conn, token, group_id)
            db.set_room_membership_source(conn, token, "group")
            created = True
        room = db.get_room(conn, token)
        members = set(db.list_group_members(conn, group_id))
        if created:
            _reconcile_roster(conn, room, members)
            # Fixed now, while the creator is the one present member the
            # policy's host rule can find, rather than by whichever later read
            # first needs a policy.
            room_policy.ensure_policy(conn, token)
            conn.execute(
                "INSERT INTO group_room_requests "
                "(group_id, request_id, room_token, actor_id, request_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (group_id, request_id, token, actor_id, request_hash),
            )
        handle = db.ensure_web_chat_handle(conn, actor_id, token, room.name or name)
    return {"room": room, "handle": handle, "created": created}


def group_owned_room_tokens(conn: sqlite3.Connection, group_id: str) -> list[str]:
    """The tokens of ``group_id``'s group-owned rooms, oldest first."""
    rows = conn.execute(
        "SELECT token FROM rooms WHERE group_id = ? AND membership_source != 'room' "
        "ORDER BY created_at, token",
        (group_id,),
    ).fetchall()
    return [row["token"] for row in rows]


def reconcile_group_room_members(conn: sqlite3.Connection, group_id: str) -> list[dict]:
    """Make every group-owned room of ``group_id`` hold exactly its current
    members, and give a room whose host has left an owner as host.

    Runs in the caller's transaction and commits nothing. Every roster write
    goes through `db.add_web_room_member` and `db.drop_web_room_member`, so a
    membership, a present principal and a handle come and go together. An
    archived or missing group is left alone: archiving suspends its rooms
    (`group_room_access` refuses everyone) without emptying them.

    One entry per room: ``room_token``, ``added``, ``removed`` and ``host``.
    """
    if db.get_group(conn, group_id) is None:
        return []
    members = set(db.list_group_members(conn, group_id))
    changes = []
    for token in group_owned_room_tokens(conn, group_id):
        room = db.get_room(conn, token)
        change = _reconcile_roster(conn, room, members)
        change["host"] = _succeed_host(conn, token, group_id)
        changes.append(change)
    return changes


def _reconcile_roster(conn: sqlite3.Connection, room: db.Room, members: set[str]) -> dict:
    token = room.token
    on_roster = set(db.list_room_members(conn, token))
    present = {row["user_id"] for row in conn.execute(
        "SELECT user_id FROM room_participants WHERE room_token = ? "
        "AND surface = 'web' AND user_id IS NOT NULL AND left_at IS NULL",
        (token,),
    )}
    refs = db._room_ref_tokens(conn, token, include_surface_refs=False)
    placeholders = ", ".join("?" for _ in refs)
    holders = {row["user_id"] for row in conn.execute(
        f"SELECT user_id FROM web_chat_rooms WHERE token IN ({placeholders})", refs,
    )}
    # Joining the group acknowledged its rooms' history (the add-member
    # transaction refuses without it), so `add_web_room_member` starts no epoch.
    added = sorted(members - (on_roster & present))
    for user_id in added:
        db.add_web_room_member(conn, token, user_id)
    for user_id in sorted(members):
        db.ensure_web_chat_handle(conn, user_id, token, room.name or "room")
    removed = sorted((on_roster | present | holders) - members)
    for user_id in removed:
        db.drop_web_room_member(conn, token, user_id)
    return {"room_token": token, "added": added, "removed": removed}


def _succeed_host(conn: sqlite3.Connection, room_token: str, group_id: str) -> str | None:
    """The room's host, appointing one when the last has left.

    A present host stays, owner or not: hosting is an operational role and a
    present host is never displaced. A room with none takes the current owner
    with the oldest membership row who is in the room, so the choice does not
    depend on who asks or when. None when no owner is in the room.
    """
    host = room_policy.current_host(conn, room_policy.ensure_policy(conn, room_token))
    if host is not None:
        return host
    owners = conn.execute(
        "SELECT user_id FROM group_members WHERE group_id = ? AND role = 'owner' "
        "AND ended_at IS NULL ORDER BY id",
        (group_id,),
    ).fetchall()
    for row in owners:
        user_id = row["user_id"]
        if not (db.is_group_owner(conn, group_id, user_id)
                and room_policy.host_present(conn, room_token, user_id)):
            continue
        conn.execute(
            "UPDATE room_policy SET host_user_id = ? "
            "WHERE room_token = ? AND host_user_id IS NULL",
            (user_id, room_token),
        )
        return user_id
    return None
