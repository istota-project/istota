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
from collections.abc import Callable
from urllib.parse import quote

from istota import db
from istota.groups.policy import GroupError, require_group_member
from istota.rooms import policy as room_policy

#: A user id to the display name a participant row records, or None.
DisplayName = Callable[[str], "str | None"]

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

    This is the backstop behind roster synchronization. The room-access
    resolver asks it for every reader: `db.is_room_member` and
    `db.list_member_rooms` carry its rule in SQL (`db.group_room_admits_sql`),
    which is the one spelling of it.
    """
    room = _canonical_room(conn, room_token)
    if room is None:
        return False
    return db.group_room_admits(conn, room.token, user_id)


def group_room_refuses(conn: sqlite3.Connection, room_token: str | None, user_id: str) -> bool:
    """Whether ``room_token`` names a group-owned room that refuses ``user_id``.

    The question for a caller holding something already addressed to a room,
    a chat handle or a task: an unknown token or an ordinary room is not
    refused here, since its roster or the caller's own checks decide it.
    """
    if not room_token:
        return False
    room = _canonical_room(conn, room_token)
    if room is None or not db.is_group_owned_room(room):
        return False
    return not db.group_room_admits(conn, room.token, user_id)


def task_delivery_refused(conn: sqlite3.Connection, task: db.Task) -> bool:
    """Whether a task's output may no longer reach its room: the task is in a
    group-owned room and its author is no longer let in.

    Read from the live group on every call, so it holds from the commit that
    removed the author (or archived the group) whether or not the task's
    cancellation has landed yet, and a crash cannot bring delivery back.
    """
    return group_room_refuses(conn, task.conversation_token, task.user_id)


#: The task states a removal or an archive stops. A park is answered nowhere
#: once its asker is out of the room, so it goes with the queued ones.
_STOPPABLE = ("pending", "locked", "running", "pending_confirmation")


def stop_room_tasks(
    conn: sqlite3.Connection, room_token: str, user_id: str | None = None,
) -> list[int]:
    """Stop the unfinished tasks in a room, ``user_id``'s alone or everyone's.

    In the caller's transaction, through the paths `!stop` and the web cancel
    use: a queued or parked task is cancelled outright (`db.cancel_task`), and
    a claimed or running one gets ``cancel_requested``, which the brain's
    cancel check and startup recovery both honour. Returns the worker pids to
    signal once the caller has committed (`signal_stopped_tasks`); nothing is
    signalled here, so no lock is held while a process stops.
    """
    refs = db.room_ref_tokens(conn, room_token, include_surface_refs=False)
    marks = ", ".join("?" for _ in refs)
    sql = (f"SELECT id, status, worker_pid FROM tasks WHERE conversation_token IN ({marks}) "
           f"AND status IN ({', '.join('?' for _ in _STOPPABLE)})")
    params: list = [*refs, *_STOPPABLE]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    pids = []
    for row in conn.execute(sql, params).fetchall():
        if row["status"] in ("pending", "pending_confirmation"):
            db.cancel_task(conn, row["id"])
            continue
        conn.execute("UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (row["id"],))
        if row["worker_pid"]:
            pids.append(row["worker_pid"])
    return pids


def signal_stopped_tasks(pids: list[int]) -> None:
    """SIGTERM each pid `stop_room_tasks` returned. Call after the commit.

    `signal_task_worker` signals only in the scheduler's own process; anywhere
    else (the web container) ``cancel_requested`` carries the stop, as for a
    web cancel.
    """
    import signal

    from istota.sandbox import process_group

    for pid in pids:
        process_group.signal_task_worker(pid, signal.SIGTERM)


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
    request_id: str, *, expected_revision: int | None = None,
    display_name: DisplayName | None = None,
) -> dict:
    """Create a room whose membership is ``group_id``'s, or return the one this
    request already made.

    Owner only, in a live group. The connection must have no open transaction:
    the room, its link, every member's handle and presence, the host, the
    request record, a ``room_created`` event and the group's revision bump
    commit together. A replay of ``request_id`` by the same actor with the same
    name returns the same room whatever the revision; anything else is a 409.
    ``expected_revision`` is checked for a new room only, since the creation
    itself moved it. The bump is what makes an add-member acknowledgement
    written against the old room list a revision conflict.

    Returns ``{"room", "handle", "created", "revision"}``: the registry room,
    the actor's own `web_chat_rooms` handle, whether this call made the room,
    and the group's revision after the call.
    """
    from istota.groups.service import _event, _finish, _metadata, _revision, _transaction

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
            if expected_revision is not None:
                _revision(db.get_group(conn, group_id), expected_revision)
            token = db.create_web_chat_room(conn, actor_id, name).token
            db.set_room_group(conn, token, group_id)
            db.set_room_membership_source(conn, token, "group")
            created = True
        room = db.get_room(conn, token)
        members = set(db.list_group_members(conn, group_id))
        if created:
            _reconcile_roster(conn, room, members, display_name)
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
            _event(conn, group_id, actor_id, "room_created",
                   details={"room_token": token, "name": name})
            group = _finish(conn, group_id)
        else:
            group = db.get_group(conn, group_id)
        handle = db.ensure_web_chat_handle(conn, actor_id, token, room.name or name)
    return {"room": room, "handle": handle, "created": created,
            "revision": group["revision"]}


def group_owned_room_tokens(conn: sqlite3.Connection, group_id: str) -> list[str]:
    """The tokens of ``group_id``'s group-owned rooms, oldest first."""
    rows = conn.execute(
        "SELECT token FROM rooms WHERE group_id = ? AND membership_source != 'room' "
        "ORDER BY created_at, token",
        (group_id,),
    ).fetchall()
    return [row["token"] for row in rows]


def reconcile_group_room_members(
    conn: sqlite3.Connection, group_id: str, display_name: DisplayName | None = None,
) -> list[dict]:
    """Make every group-owned room of ``group_id`` hold exactly its current
    members, and give a room whose host has left an owner as host.

    Runs in the caller's transaction and commits nothing. Every roster write
    goes through `db.add_web_room_member` and `db.drop_web_room_member`, so a
    membership, a present principal and a handle come and go together. An
    archived or missing group is left alone: archiving suspends its rooms
    (`group_room_access` refuses everyone) without emptying them.

    A removed member's unfinished tasks in the room are stopped with them
    (`stop_room_tasks`); the pids to signal after the commit ride the entry.

    ``display_name`` names an added member's participant row; without it the
    row has none, as for a caller holding no config.

    One entry per room: ``room_token``, ``added``, ``removed``, ``pids`` and
    ``host``.
    """
    if db.get_group(conn, group_id) is None:
        return []
    members = set(db.list_group_members(conn, group_id))
    changes = []
    for token in group_owned_room_tokens(conn, group_id):
        room = db.get_room(conn, token)
        change = _reconcile_roster(conn, room, members, display_name)
        change["host"] = _succeed_host(conn, token, group_id)
        changes.append(change)
    return changes


def suspend_group_rooms(conn: sqlite3.Connection, group_id: str) -> list[int]:
    """Stop every unfinished task in ``group_id``'s group-owned rooms, in the
    caller's archive transaction. Access needs nothing written: an archived
    group admits nobody (`db.group_room_admits_sql`), and the rosters and
    transcripts stay for recovery. Returns the pids to signal after commit.
    """
    pids = []
    for token in group_owned_room_tokens(conn, group_id):
        pids.extend(stop_room_tasks(conn, token))
    return pids


def _reconcile_roster(
    conn: sqlite3.Connection, room: db.Room, members: set[str],
    display_name: DisplayName | None = None,
) -> dict:
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
        db.add_web_room_member(
            conn, token, user_id,
            display_name=display_name(user_id) if display_name else None,
        )
    for user_id in sorted(members):
        db.ensure_web_chat_handle(conn, user_id, token, room.name or "room")
    removed = sorted((on_roster | present | holders) - members)
    pids = []
    for user_id in removed:
        db.drop_web_room_member(conn, token, user_id)
        pids.extend(stop_room_tasks(conn, token, user_id))
    return {"room_token": token, "added": added, "removed": removed, "pids": pids}


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


def display_names(config) -> DisplayName:
    """The participant-row name for a user id, from the loaded config."""
    def name(user_id: str) -> str | None:
        profile = config.users.get(user_id) if config is not None else None
        return (getattr(profile, "display_name", None) or None) if profile else None
    return name


def manage_url(group_id: str) -> str:
    """Where in the web app a group's membership is managed."""
    return f"/istota/groups/?group={quote(group_id, safe='')}"


def _group_label(conn: sqlite3.Connection, room: db.Room) -> dict:
    group = db.get_group(conn, room.group_id) if room.group_id else None
    return {
        "group_id": room.group_id,
        "display_name": (group or {}).get("display_name") or room.group_id,
    }


def managed_by(conn: sqlite3.Connection, room: db.Room | None, user_id: str) -> dict | None:
    """The group a room's membership follows, as the Chat panes show it, or
    None for a room with its own roster.

    ``is_owner`` says whether ``user_id`` governs the room's settings now. It
    is display data: every write asks the policy again.
    """
    if not db.is_group_owned_room(room):
        return None
    return {
        **_group_label(conn, room),
        "manage_url": manage_url(room.group_id or ""),
        "is_owner": bool(room.group_id) and db.is_group_owner(conn, room.group_id, user_id),
    }


def managed_refusal(conn: sqlite3.Connection, room: db.Room, error: str | None = None) -> dict:
    """The 409 body for a Chat action on a group-owned room that belongs to
    the group's membership: ``reason="group_managed"`` and where in Groups to
    go instead."""
    return {
        "error": error or "This room's membership follows its group; manage it in Groups.",
        "reason": "group_managed",
        "group": _group_label(conn, room),
        "manage_url": manage_url(room.group_id or ""),
    }


def list_group_rooms(conn: sqlite3.Connection, group_id: str, user_id: str) -> list[dict]:
    """The rooms linked to ``group_id`` that ``user_id`` can open, each labelled
    with its ``membership_source``.

    The group's own rooms and the linked rooms whose roster has the user; a
    linked room they are not in is left out, name included. A hidden room is
    still listed, since hiding a group's room is not leaving it. ``room_id`` is
    the user's own chat handle, None where none has been minted yet.
    """
    rooms = []
    for room in db.list_member_rooms(conn, user_id, include_dismissed=True):
        if room.group_id != group_id:
            continue
        refs = db.room_ref_tokens(conn, room.token, include_surface_refs=False)
        marks = ", ".join("?" for _ in refs)
        handle = conn.execute(
            f"SELECT id FROM web_chat_rooms WHERE user_id = ? AND token IN ({marks}) "
            "ORDER BY id LIMIT 1",
            (user_id, *refs),
        ).fetchone()
        rooms.append({
            "token": room.token,
            "name": room.name or "",
            "origin": room.origin,
            "membership_source": "group" if db.is_group_owned_room(room) else "room",
            "room_id": handle["id"] if handle else None,
        })
    return rooms
