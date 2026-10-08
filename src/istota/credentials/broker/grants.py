"""User grants and immutable task admission snapshots. No values are returned.

Callers own commit/rollback. Snapshot admission runs under BEGIN IMMEDIATE, or
inside a write transaction the caller already holds; other writes share it. Live checks must run in the
same transaction as the caller's credential lookup before substitution.
"""

import json

from istota import db
from .bindings import get_binding, forge_bindings, credential_name, get_entry_binding

NAMESPACE = "_credential_grants"

AUTO_GRANT_PREFIX = "auto:"
AUTO_GRANT_DONE = "granted"
AUTO_GRANT_PENDING = "pending"
AUTO_GRANT_DECLINED = "declined"
AUTO_GRANT_PREEXISTING = "preexisting"
AUTO_GRANT_BASELINE = "auto_baseline"


def get_grant(conn, user_id, name):
    name = credential_name(conn, user_id, name)
    row = conn.execute("SELECT * FROM credential_grants WHERE user_id=? AND name=?",
                       (user_id, name)).fetchone()
    if row is None:
        return None
    grant = dict(row)
    option = db.kv_get(conn, user_id, NAMESPACE, "allow_http:" + name)
    grant["allow_http"] = bool(option and option["value"] == "true")
    # Keep the legacy column on disk, but grants no longer restrict methods.
    grant.pop("methods")
    grant["allow_scheduled"] = bool(grant["allow_scheduled"])
    grant["rooms"] = [r[0] for r in conn.execute(
        "SELECT conversation_token FROM credential_grant_rooms "
        "WHERE user_id=? AND name=? ORDER BY conversation_token", (user_id, name))]
    return grant


def put_grant(conn, user_id, name, *, scope_mode="all",
              allow_scheduled=False, rooms=(), allow_http=False):
    name = credential_name(conn, user_id, name)
    if (scope_mode not in ("all", "rooms") or type(allow_scheduled) is not bool or type(allow_http) is not bool
            or not isinstance(rooms, (list, tuple))
            or any(not isinstance(r, str) or not r for r in rooms)):
        raise ValueError("invalid credential grant policy")
    if scope_mode == "all" and rooms:
        raise ValueError("all-room grants cannot name individual rooms")
    binding = get_entry_binding(conn, user_id, name)
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
        VALUES (?, ?, ?, '[]', ?, ?)
        ON CONFLICT(user_id, name) DO UPDATE SET scope_mode=excluded.scope_mode,
            methods=excluded.methods, allow_scheduled=excluded.allow_scheduled,
            policy_revision=excluded.policy_revision, updated_at=datetime('now')
    """, (user_id, name, scope_mode, int(allow_scheduled), revision))
    conn.execute("DELETE FROM credential_grant_rooms WHERE user_id=? AND name=?", (user_id, name))
    conn.executemany("INSERT INTO credential_grant_rooms VALUES (?, ?, ?)",
                     [(user_id, name, room) for room in sorted(set(rooms))])
    db.kv_set(conn, user_id, NAMESPACE, "allow_http:" + name, json.dumps(allow_http))
    return get_grant(conn, user_id, name)


def delete_grant(conn, user_id, name):
    name = credential_name(conn, user_id, name)
    db.kv_delete(conn, user_id, NAMESPACE, "allow_http:" + name)
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
        if auto_grant_marker(conn, user_id, credential_name(conn, user_id, row["name"])) == AUTO_GRANT_DECLINED:
            continue
        if json.loads(row["hosts"]) and get_grant(conn, user_id, row["name"]) is None:
            put_grant(conn, user_id, row["name"], allow_scheduled=True)
            count += 1
    return count


def auto_grant_marker(conn, user_id, owner):
    row = db.kv_get(conn, user_id, NAMESPACE, AUTO_GRANT_PREFIX + owner)
    return row["value"] if row else None


def baseline_auto_grants(conn, user_id, stored_owners):
    """Mark the entries stored before auto-grant existed, once per user.

    Without it, the first pass after an upgrade would grant every entry the
    user had left ungranted. Caller owns the transaction.
    """
    inserted = conn.execute("INSERT OR IGNORE INTO istota_kv (user_id, namespace, key, value) "
                            "VALUES (?, ?, ?, '1')", (user_id, NAMESPACE, AUTO_GRANT_BASELINE))
    if not inserted.rowcount:
        return
    for owner in stored_owners:
        conn.execute("INSERT OR IGNORE INTO istota_kv (user_id, namespace, key, value) "
                     "VALUES (?, ?, ?, ?)",
                     (user_id, NAMESPACE, AUTO_GRANT_PREFIX + owner, AUTO_GRANT_PREEXISTING))


def auto_grant_vault_entries(conn, user_id, owners, *, declined, scoped):
    """Grant vault entries the sync sees for the first time (ISSUE-590).

    ``owners`` are the entry names the file produced this pass, ``declined``
    the ones tagged ``istota:nogrant`` or created by a task under
    ``generated/``. The marker is the whole record of "first time" and is
    never cleared, like the ``revision:`` tombstone: deleting an entry and
    restoring it, which a task can do to the file, must not turn a grant the
    user narrowed or revoked back into a wide one. ``granted`` and
    ``preexisting`` are final; ``pending`` (no host yet, or an unscoped read)
    and ``declined`` are decided again on every pass. Nothing is granted from
    an unscoped read, where the whole file stands in for the ``istota`` group.
    Caller owns the transaction. Returns how many grants were created.
    """
    count = 0
    for owner in sorted(owners):
        marker = auto_grant_marker(conn, user_id, owner)
        if marker in (AUTO_GRANT_DONE, AUTO_GRANT_PREEXISTING):
            continue
        key = AUTO_GRANT_PREFIX + owner
        if owner in declined:
            if marker != AUTO_GRANT_DECLINED:
                db.kv_set(conn, user_id, NAMESPACE, key, AUTO_GRANT_DECLINED)
            continue
        if get_grant(conn, user_id, owner) is None:
            binding = get_entry_binding(conn, user_id, owner)
            if not scoped or not binding or not binding["hosts"] or binding["source"] not in ("vault", "local"):
                if marker != AUTO_GRANT_PENDING:
                    db.kv_set(conn, user_id, NAMESPACE, key, AUTO_GRANT_PENDING)
                continue
            put_grant(conn, user_id, owner, allow_scheduled=True)
            count += 1
        db.kv_set(conn, user_id, NAMESPACE, key, AUTO_GRANT_DONE)
    return count


def grant_created_entry(conn, user_id, owner, task_id):
    """Grant an entry a task just created to that task's conversation (ISSUE-684).

    Rooms scope, scheduled use off, no HTTP: later turns in the conversation
    that made the account can sign in with it, and widening it is the user's.
    The marker is set to ``granted`` so the sync's ``generated/`` decline
    never touches it again, and a grant the user narrows or revokes stays so.
    The creating task's own snapshot gains the grant when it is in scope, so a
    retry of an interrupted signup can still use the entry it made.
    No conversation, no host binding (an apply that failed, or no URL) or a
    grant already present: nothing changes. Returns ``(room, scheduled)``,
    where ``scheduled`` says the creator itself is outside the grant, or None.
    Caller owns commit.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    if conn.execute("SELECT 1 FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)).fetchone() is None:
        return None
    task, scheduled = _task_context(conn, task_id, user_id)
    room = task["conversation_token"]
    if not room:
        return None
    binding = get_entry_binding(conn, user_id, owner)
    if not binding or not binding["hosts"] or get_grant(conn, user_id, owner) is not None:
        return None
    grant = put_grant(conn, user_id, owner, scope_mode="rooms", rooms=[room], allow_scheduled=False)
    db.kv_set(conn, user_id, NAMESPACE, AUTO_GRANT_PREFIX + grant["name"], AUTO_GRANT_DONE)
    if _in_scope(grant, task, scheduled):
        conn.execute("INSERT OR REPLACE INTO credential_task_grants VALUES (?, ?, ?, ?)",
                     (task_id, user_id, grant["name"], grant["policy_revision"]))
    return room, scheduled


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


def _withheld(binding, withheld_scopes):
    # A guest's turn withholds every scope, which keeps the host's credentials
    # out of the task (multiplayer D2): a vault entry whenever anything is withheld,
    # as the skill proxy's vault map is emptied, and a deployment forge token
    # when the developer skill that declares it is.
    if not withheld_scopes:
        return False
    if binding["source"] == "config":
        return "developer" in withheld_scopes
    return True


def ensure_credential_grants(conn, task_id, user_id, *, withheld_scopes=frozenset()):
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
            binding = get_entry_binding(conn, user_id, name)
            if (binding and binding["hosts"] and _in_scope(grant, task, scheduled)
                    and not _withheld(binding, withheld_scopes)):
                conn.execute("INSERT OR IGNORE INTO credential_task_grants VALUES (?, ?, ?, ?)",
                             (task_id, user_id, name, grant["policy_revision"]))
        conn.execute("UPDATE tasks SET credential_grants_initialized=1 WHERE id=? AND user_id=?",
                     (task_id, user_id))
    return {row["name"]: row["policy_revision"] for row in conn.execute(
        "SELECT name, policy_revision FROM credential_task_grants WHERE task_id=? AND user_id=?",
        (task_id, user_id))}


def check_credential_use(conn, task_id, user_id, name):
    """The grant half of a live check: snapshot, revision and scope, no host.

    The private skill channel asks only this, because the host a skill hands
    the value to is the skill's own check (the browser fill's origin).
    """
    if not conn.in_transaction:
        conn.execute("BEGIN")
    task, scheduled = _task_context(conn, task_id, user_id)
    policy_name = credential_name(conn, user_id, name)
    snapshot = conn.execute("SELECT policy_revision FROM credential_task_grants "
                            "WHERE task_id=? AND user_id=? AND name=?", (task_id, user_id, policy_name)).fetchone()
    grant = get_grant(conn, user_id, name)
    if snapshot is None or grant is None:
        return "credential_not_granted"
    if snapshot[0] != grant["policy_revision"]:
        return "credential_changed"
    if not _in_scope(grant, task, scheduled):
        return "credential_not_granted"
    return None


def check_credential_grant(conn, task_id, user_id, name, host, header, *, config=None):
    """Return a refusal reason, or None. Read live policy and binding on every use."""
    reason = check_credential_use(conn, task_id, user_id, name)
    if reason:
        return reason
    grant = get_grant(conn, user_id, name)
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
    if host.startswith("http://") and not grant["allow_http"]:
        return "credential_https_required"
    if header.lower() not in binding["headers"]:
        return "credential_header_not_allowed"
    return None
