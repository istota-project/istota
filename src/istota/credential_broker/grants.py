"""User grants and immutable task admission snapshots. No values are returned.

Callers own commit/rollback. Snapshot admission runs under BEGIN IMMEDIATE, or
inside a write transaction the caller already holds; other writes share it. Live checks must run in the
same transaction as the caller's credential lookup before substitution.
"""

import json

from .. import db
from .bindings import get_binding, forge_bindings

DEFAULT_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH"]
METHODS = frozenset([*DEFAULT_METHODS, "DELETE", "OPTIONS"])
NAMESPACE = "_credential_grants"


def get_grant(conn, user_id, name):
    row = conn.execute("SELECT * FROM credential_grants WHERE user_id=? AND name=?",
                       (user_id, name)).fetchone()
    if row is None:
        return None
    grant = dict(row)
    grant["methods"] = json.loads(grant["methods"])
    grant["allow_scheduled"] = bool(grant["allow_scheduled"])
    grant["rooms"] = [r[0] for r in conn.execute(
        "SELECT conversation_token FROM credential_grant_rooms "
        "WHERE user_id=? AND name=? ORDER BY conversation_token", (user_id, name))]
    return grant


def put_grant(conn, user_id, name, *, scope_mode="all", methods=None,
              allow_scheduled=False, rooms=()):
    methods = DEFAULT_METHODS.copy() if methods is None else methods
    if (scope_mode not in ("all", "rooms") or type(allow_scheduled) is not bool
            or not isinstance(methods, list) or not methods
            or any(not isinstance(m, str) or m not in METHODS for m in methods)
            or not isinstance(rooms, (list, tuple))
            or any(not isinstance(r, str) or not r for r in rooms)):
        raise ValueError("invalid credential grant policy")
    if scope_mode == "all" and rooms:
        raise ValueError("all-room grants cannot name individual rooms")
    binding = get_binding(conn, user_id, name)
    if not binding or not binding["hosts"]:
        raise ValueError("credential is not bound")
    # Start the write lock before reading the revision. The reserved KV tombstone
    # survives grant deletion and task retention, so revisions never repeat.
    conn.execute("INSERT OR IGNORE INTO istota_kv (user_id, namespace, key, value) "
                 "VALUES (?, ?, ?, '0')", (user_id, NAMESPACE, "revision:" + name))
    revision = int(db.kv_get(conn, user_id, NAMESPACE, "revision:" + name)["value"]) + 1
    db.kv_set(conn, user_id, NAMESPACE, "revision:" + name, str(revision))
    conn.execute("""
        INSERT INTO credential_grants
            (user_id, name, scope_mode, methods, allow_scheduled, policy_revision)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, name) DO UPDATE SET scope_mode=excluded.scope_mode,
            methods=excluded.methods, allow_scheduled=excluded.allow_scheduled,
            policy_revision=excluded.policy_revision, updated_at=datetime('now')
    """, (user_id, name, scope_mode, json.dumps(list(dict.fromkeys(methods))),
          int(allow_scheduled), revision))
    conn.execute("DELETE FROM credential_grant_rooms WHERE user_id=? AND name=?", (user_id, name))
    conn.executemany("INSERT INTO credential_grant_rooms VALUES (?, ?, ?)",
                     [(user_id, name, room) for room in sorted(set(rooms))])
    return get_grant(conn, user_id, name)


def delete_grant(conn, user_id, name):
    conn.execute("DELETE FROM credential_grant_rooms WHERE user_id=? AND name=?", (user_id, name))
    conn.execute("DELETE FROM credential_grants WHERE user_id=? AND name=?", (user_id, name))


def grant_what_exists(conn, user_id):
    # INSERT first locks writers and makes the one-time action race-safe.
    inserted = conn.execute("INSERT OR IGNORE INTO istota_kv (user_id, namespace, key, value) "
                            "VALUES (?, ?, 'granted_existing', '1')", (user_id, NAMESPACE))
    if not inserted.rowcount:
        return 0
    count = 0
    for row in conn.execute("SELECT name, hosts FROM credential_bindings WHERE user_id=?", (user_id,)):
        if json.loads(row["hosts"]) and get_grant(conn, user_id, row["name"]) is None:
            put_grant(conn, user_id, row["name"], allow_scheduled=True)
            count += 1
    return count


def _task_context(conn, task_id, user_id):
    row = conn.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)).fetchone()
    if row is None:
        raise ValueError("credential task owner mismatch")
    scheduled = False
    ancestor = row
    seen = set()
    while ancestor is not None:
        if ancestor["id"] in seen:
            raise ValueError("credential task ancestry cycle")
        seen.add(ancestor["id"])
        scheduled |= ancestor["source_type"] in ("scheduled", "briefing", "heartbeat")
        scheduled |= ancestor["scheduled_job_id"] is not None
        parent = ancestor["parent_task_id"]
        if parent is None:
            break
        ancestor = conn.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (parent, user_id)).fetchone()
        if ancestor is None:
            # A pruned or foreign parent cannot prove interactive provenance.
            scheduled = True
    return row, scheduled


def _in_scope(grant, task, scheduled):
    return bool(task["conversation_token"] and (not scheduled or grant["allow_scheduled"])
                and (grant["scope_mode"] == "all" or task["conversation_token"] in grant["rooms"]))


def ensure_credential_grants(conn, task_id, user_id):
    """Freeze the maximum set once, even when it is empty. Caller commits.

    A task id with no row (a direct caller that never inserted one) freezes
    nothing and is granted nothing; a row owned by another user still raises.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
        return {}
    task, scheduled = _task_context(conn, task_id, user_id)
    if not task["credential_grants_initialized"]:
        for row in conn.execute("SELECT name FROM credential_grants WHERE user_id=?", (user_id,)):
            name = row["name"]
            grant = get_grant(conn, user_id, name)
            binding = get_binding(conn, user_id, name)
            if binding and binding["hosts"] and _in_scope(grant, task, scheduled):
                conn.execute("INSERT OR IGNORE INTO credential_task_grants VALUES (?, ?, ?, ?)",
                             (task_id, user_id, name, grant["policy_revision"]))
        conn.execute("UPDATE tasks SET credential_grants_initialized=1 WHERE id=? AND user_id=?",
                     (task_id, user_id))
    return {row["name"]: row["policy_revision"] for row in conn.execute(
        "SELECT name, policy_revision FROM credential_task_grants WHERE task_id=? AND user_id=?",
        (task_id, user_id))}


def check_credential_grant(conn, task_id, user_id, name, host, method, header, *, config=None):
    """Return a refusal reason, or None. Read live policy and binding on every use."""
    if not conn.in_transaction:
        conn.execute("BEGIN")
    task, scheduled = _task_context(conn, task_id, user_id)
    snapshot = conn.execute("SELECT policy_revision FROM credential_task_grants "
                            "WHERE task_id=? AND user_id=? AND name=?", (task_id, user_id, name)).fetchone()
    grant = get_grant(conn, user_id, name)
    if snapshot is None or grant is None:
        return "credential_not_granted"
    if snapshot[0] != grant["policy_revision"]:
        return "credential_changed"
    if not _in_scope(grant, task, scheduled):
        return "credential_not_granted"
    binding = get_binding(conn, user_id, name)
    if binding and binding["source"] == "config":
        if config is None or not config.developer.enabled or not config.is_admin(user_id):
            return "credential_not_granted"
        binding = forge_bindings(config.developer).get(name)
    elif binding and not conn.execute(
            "SELECT 1 FROM secrets WHERE user_id=? AND service='vault_entries' AND key=?",
            (user_id, name)).fetchone():
        binding = None
    if binding is None or host not in binding["hosts"]:
        return "credential_not_bound"
    if header.lower() not in binding["headers"]:
        return "credential_header_not_allowed"
    if method not in grant["methods"]:
        return "credential_method_not_allowed"
    return None
