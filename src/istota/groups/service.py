"""Group mutations own one immediate transaction, including policy and events.

Callers supply a trusted actor and authority, never values from a request body.
Connections must have no open transaction. File seeding happens after commit.
A membership change reconciles the group's group-owned rooms before commit, and
an archive stops the tasks in them; the workers of running tasks are signalled
after the commit, so no lock is held while a process stops.
"""
import json
import logging
import sqlite3
from contextlib import contextmanager

from istota import db, storage
from istota.groups import rooms as group_rooms
from istota.groups.policy import GroupError, group_capabilities, require_group_member

logger = logging.getLogger(__name__)


@contextmanager
def _transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _revision(group, expected_revision):
    if type(expected_revision) is not int or expected_revision < 0:
        raise GroupError(422, "invalid_revision", "A nonnegative group revision is required")
    if group["revision"] != expected_revision:
        raise GroupError(409, "revision_conflict", "Group changed; reload before saving",
                         revision=group["revision"])


def _managed_group(conn, group_id, actor_id, is_admin, is_operator):
    group = db.get_group(conn, group_id)
    if not group:
        raise GroupError(404, "group_not_found", "Group not found")
    if not (is_admin or is_operator):
        require_group_member(conn, group_id, actor_id)
        if not group_capabilities(conn, group_id, actor_id, is_admin=False)["manage"]:
            raise GroupError(403, "group_forbidden", "Group owner required")
    return group


def _enabled_user(conn, config, user_id):
    # Config includes the profile overlay. Native identities carry the only
    # account-disable flag; OAuth users need not have a native identity row.
    if not isinstance(user_id, str) or user_id not in config.users:
        raise GroupError(422, "invalid_user", "An existing enabled user is required")
    row = conn.execute("SELECT disabled FROM web_auth_identities WHERE user_id = ?", (user_id,)).fetchone()
    if row and row["disabled"]:
        raise GroupError(422, "invalid_user", "An existing enabled user is required")


def _metadata(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise GroupError(422, "invalid_metadata", f"{field} must contain 1–200 characters")
    return value


def _event(conn, group_id, actor_id, event_type, *, subject=None, details=None):
    conn.execute(
        "INSERT INTO group_events (group_id, actor_id, event_type, subject_user_id, details_json) VALUES (?, ?, ?, ?, ?)",
        (group_id, actor_id, event_type, subject, json.dumps(details or {})),
    )


def _finish(conn, group_id):
    conn.execute("UPDATE groups SET revision = revision + 1 WHERE group_id = ?", (group_id,))
    return db.get_group(conn, group_id, include_archived=True)


def history_disclosure(group_id: str, conn=None) -> dict:
    """What joining discloses. ``rooms`` names the group-owned rooms, whose whole
    history a new member reads; it needs ``conn`` and is empty without one."""
    rooms = []
    if conn is not None:
        rooms = [db.get_room(conn, token).name or token
                 for token in group_rooms.group_owned_room_tokens(conn, group_id)]
    return {"memory": f"Groups/{group_id}/GROUP.md", "rooms": rooms,
            "briefings": ["All retained group briefing editions, including those published before joining"]}


def reconcile_rooms(conn, group_id: str) -> list[int]:
    """`group_rooms.reconcile_group_room_members` inside a membership mutation.

    A database failure becomes a retryable 503, and the caller's transaction
    rolls the membership change back with it: a partial roster never commits.
    Returns the worker pids of a removed member's running tasks, for
    `group_rooms.signal_stopped_tasks` once the caller has committed.
    """
    try:
        changes = group_rooms.reconcile_group_room_members(conn, group_id)
    except sqlite3.Error as exc:
        logger.warning("Group room reconciliation failed: group=%s error=%s",
                       group_id, type(exc).__name__)
        raise GroupError(503, "rooms_unavailable",
                         "Group rooms could not be updated; try again",
                         retryable=True) from exc
    return [pid for change in changes for pid in change["pids"]]


def create_group(conn, config, group_id: str, *, actor_id: str, expected_revision: int,
                 owners: list[str], display_name: str, kind: str = "group",
                 is_admin: bool = False, is_operator: bool = False) -> dict:
    with _transaction(conn):
        if not (is_admin or is_operator):
            raise GroupError(403, "group_forbidden", "Administrator required")
        _revision({"revision": 0}, expected_revision)
        if not isinstance(owners, list) or not owners:
            raise GroupError(422, "owner_required", "At least one owner is required")
        for owner in owners:
            _enabled_user(conn, config, owner)
        _metadata(display_name, "Display name")
        _metadata(kind, "Kind")
        try:
            db.create_group(conn, group_id, kind=kind, display_name=display_name, created_by=actor_id)
        except ValueError as exc:
            raise GroupError(409 if db.is_valid_group_id(group_id) else 422, "invalid_group", str(exc)) from exc
        for owner in sorted(set(owners)):
            db.add_group_member(conn, group_id, owner, role="owner", added_by=actor_id)
        _event(conn, group_id, actor_id, "created", details={"owners": sorted(set(owners))})
        result = _finish(conn, group_id)
    try:
        result["storage_available"] = storage.ensure_group_directories(config, group_id, display_name=display_name)
    except OSError:
        logger.warning("Group storage unavailable: group=%s actor=%s", group_id, actor_id)
        result["storage_available"] = False
    return result


def update_group(conn, group_id: str, *, actor_id: str, expected_revision: int,
                 display_name: str | None = None, kind: str | None = None,
                 is_admin: bool = False, is_operator: bool = False) -> dict:
    with _transaction(conn):
        group = _managed_group(conn, group_id, actor_id, is_admin, is_operator)
        if group["system_key"]:
            raise GroupError(403, "system_group", "System group details are fixed")
        _revision(group, expected_revision)
        changes = {}
        for field, value in (("display_name", display_name), ("kind", kind)):
            if value is not None:
                _metadata(value, field)
                if value != group[field]:
                    changes[field] = {"old": group[field], "new": value}
                    conn.execute(f"UPDATE groups SET {field} = ? WHERE group_id = ?", (value, group_id))
        if not changes:
            return group
        _event(conn, group_id, actor_id, "updated", details=changes)
        return _finish(conn, group_id)


def change_membership(conn, config, group_id: str, user_id: str, *, actor_id: str,
                      expected_revision: int, role: str | None,
                      acknowledge_history: bool = False, replacement_owner: str | None = None,
                      is_admin: bool = False, is_operator: bool = False) -> dict:
    pids: list[int] = []
    with _transaction(conn):
        if user_id == actor_id and role is None and not (is_admin or is_operator):
            require_group_member(conn, group_id, actor_id)
            group = db.get_group(conn, group_id)
        else:
            group = _managed_group(conn, group_id, actor_id, is_admin, is_operator)
        if group["system_key"]:
            raise GroupError(403, "automatic_membership", "System group membership is automatic")
        _revision(group, expected_revision)
        if role is not None and role not in db.GROUP_ROLES:
            raise GroupError(422, "invalid_role", "Unknown group role")
        members = {h["user_id"]: h for h in db.group_membership_history(conn, group_id) if h["ended_at"] is None}
        current = members.get(user_id)
        if role is None and current is None:
            raise GroupError(404, "member_not_found", "Current member not found")
        if role is not None:
            _enabled_user(conn, config, user_id)
        if current is None and acknowledge_history is not True:
            raise GroupError(409, "history_acknowledgement_required", "Acknowledge access to existing group history",
                             disclosure=history_disclosure(group_id, conn))
        if replacement_owner is not None:
            _managed_group(conn, group_id, actor_id, is_admin, is_operator)
            if replacement_owner == user_id or replacement_owner not in members:
                raise GroupError(422, "invalid_replacement", "Replacement owner must be another current member")
            _enabled_user(conn, config, replacement_owner)
        if current and current["role"] == "owner" and role != "owner":
            other_owners = [h for uid, h in members.items() if uid != user_id and h["role"] == "owner"]
            if len(members) > 1 and not other_owners and replacement_owner is None:
                raise GroupError(409, "last_owner", "Appoint a replacement before removing the last owner")
        changed = False
        if replacement_owner is not None and members[replacement_owner]["role"] != "owner":
            conn.execute("UPDATE group_members SET role = 'owner' WHERE group_id = ? AND user_id = ? AND ended_at IS NULL", (group_id, replacement_owner))
            _event(conn, group_id, actor_id, "role_changed", subject=replacement_owner,
                   details={"old_role": members[replacement_owner]["role"], "new_role": "owner"})
            changed = True
        if current is None:
            db.add_group_member(conn, group_id, user_id, role=role, added_by=actor_id)
            _event(conn, group_id, actor_id, "member_added", subject=user_id,
                   details={"role": role, "acknowledge_history": True, "disclosure": history_disclosure(group_id, conn)})
            changed = True
        elif role is None:
            db.end_group_membership(conn, group_id, user_id, ended_by=actor_id)
            _event(conn, group_id, actor_id, "member_removed", subject=user_id, details={"old_role": current["role"]})
            changed = True
        elif role != current["role"]:
            conn.execute("UPDATE group_members SET role = ? WHERE group_id = ? AND user_id = ? AND ended_at IS NULL", (role, group_id, user_id))
            _event(conn, group_id, actor_id, "role_changed", subject=user_id,
                   details={"old_role": current["role"], "new_role": role})
            changed = True
        if changed:
            pids = reconcile_rooms(conn, group_id)
        result = _finish(conn, group_id) if changed else group
    group_rooms.signal_stopped_tasks(pids)
    return {**result, "changed": changed}


def archive_group(conn, group_id: str, *, actor_id: str, expected_revision: int,
                  confirm: bool = False, is_admin: bool = False, is_operator: bool = False) -> dict:
    with _transaction(conn):
        group = _managed_group(conn, group_id, actor_id, is_admin, is_operator)
        if not (is_admin or is_operator):
            raise GroupError(403, "group_forbidden", "Administrator required")
        if group["system_key"]:
            raise GroupError(403, "system_group", "System groups cannot be archived")
        _revision(group, expected_revision)
        if confirm is not True:
            raise GroupError(409, "confirmation_required", "Confirm archiving the group")
        db.archive_group(conn, group_id)
        pids = group_rooms.suspend_group_rooms(conn, group_id)
        _event(conn, group_id, actor_id, "archived")
        result = _finish(conn, group_id)
    group_rooms.signal_stopped_tasks(pids)
    return result
