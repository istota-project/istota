"""One-use, task-bound approval for filling a stored recovery code."""
import json

from istota import db
from istota.credentials import audit, generated
from istota.credentials.broker.bindings import get_binding
from istota.relay.requests import RequestError, _store_request, text_hash, write_transaction

INTERACTIVE_SOURCE_TYPES = ("talk", "web", "email", "sms", "whatsapp")


class RecoveryFillError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def eligible(conn, *, user_id, task_id, name, host):
    """Recheck live task, credential and code state before requesting or claiming."""
    task = db.get_task(conn, task_id)
    if task is None or task.user_id != user_id or task.source_type not in INTERACTIVE_SOURCE_TYPES:
        raise RecoveryFillError("recovery_fill_not_interactive")
    if task.status != "running":
        raise RecoveryFillError("task_unavailable")
    if not generated.is_generated(conn, user_id, name):
        raise RecoveryFillError("recovery_set_not_generated")
    hosts = (get_binding(conn, user_id, name) or {}).get("hosts", [])
    if not isinstance(host, str) or host not in hosts:
        raise RecoveryFillError("credential_origin_mismatch")
    state = generated.recovery_state(conn, user_id, name)
    if state is None or state["format"] != "codes":
        raise RecoveryFillError("recovery_fill_format")
    if state["remaining"] < 1:
        raise RecoveryFillError("recovery_none_left")
    return state


def request(conn, *, user_id, task_id, name, host) -> int:
    with write_transaction(conn):
        state = eligible(conn, user_id=user_id, task_id=task_id, name=name, host=host)
        old = conn.execute("SELECT id FROM recovery_fill_authorizations WHERE user_id=? AND task_id=? "
                           "AND name=? AND host=? AND state='held'", (user_id, task_id, name, host)).fetchone()
        if old:
            return old["id"]
        authorization_id = conn.execute(
            "INSERT INTO recovery_fill_authorizations (user_id,task_id,name,host,state) VALUES (?,?,?,?,'held')",
            (user_id, task_id, name, host),
        ).lastrowid
        preview = f"Use one recovery code for {name} on {host}? {state['remaining']} left."
        held = _store_request(
            conn, actor_user_id=user_id, task_id=task_id, request_key=f"recovery-fill-{authorization_id}",
            kind="recovery_fill", recipient_user_id=user_id, text=preview, service_body=preview,
            template_body=None, provider="credentials", binding_fingerprint="", preview=preview,
            destination={"kind": "recovery_fill", "authorization_id": authorization_id},
        )
        conn.execute("UPDATE recovery_fill_authorizations SET request_id=? WHERE id=?", (held["id"], authorization_id))
        return authorization_id


def authorize_held(conn, authorization_id, digest, *, minutes=15, actor="system"):
    with write_transaction(conn):
        row = conn.execute("SELECT a.*, r.preview_digest, r.preview, r.state AS request_state "
                           "FROM recovery_fill_authorizations a JOIN whatsapp_skill_requests r ON r.id=a.request_id "
                           "WHERE a.id=?", (authorization_id,)).fetchone()
        if (row is None or row["state"] != "held" or row["request_state"] != "held"
                or not digest or row["preview_digest"] != digest or text_hash(row["preview"] or "") != digest):
            raise RequestError("confirmation_unavailable")
        conn.execute("UPDATE recovery_fill_authorizations SET state='authorized', authorized_at=datetime('now'), "
                     "expires_at=datetime('now', ?) WHERE id=?",
                     (f"+{max(1, min(15, minutes))} minutes", authorization_id))
        state = generated.recovery_state(conn, row["user_id"], row["name"])
        audit.record(conn, row["user_id"], action="recovery_fill_approved", actor=actor, name=row["name"],
                     detail={"host": row["host"], "remaining": state["remaining"] if state else 0})


def claim(conn, *, user_id, task_id, name, host, enrollment=False) -> str | None:
    """Spend under the writer lock, before the caller receives any code."""
    with write_transaction(conn):
        state = eligible(conn, user_id=user_id, task_id=task_id, name=name, host=host)
        expire(conn)
        authorization = conn.execute(
            "SELECT id FROM recovery_fill_authorizations WHERE user_id=? AND task_id=? AND name=? AND host=? "
            "AND state='authorized' AND expires_at>datetime('now') ORDER BY id LIMIT 1",
            (user_id, task_id, name, host),
        ).fetchone()
        if not enrollment and authorization is None:
            return None
        codes = (generated.read_recovery(conn, user_id, name) or "").splitlines()
        index = next((i for i in range(len(codes)) if i not in state["spent"]), None)
        if index is None:
            raise RecoveryFillError("recovery_none_left")
        spent = sorted([*state["spent"], index])
        remaining = len(codes) - len(spent)
        conn.execute("UPDATE recovery_code_state SET spent=?,updated_at=datetime('now') WHERE user_id=? AND name=?",
                     (json.dumps(spent), user_id, name))
        if not enrollment:
            conn.execute("UPDATE recovery_fill_authorizations SET state='used',used_at=datetime('now') WHERE id=?",
                         (authorization["id"],))
        detail = {"host": host, "remaining": remaining}
        if enrollment:
            detail["exemption"] = "enrollment"
        audit.record(conn, user_id, action="recovery_fill", actor=f"task:{task_id}", name=name, detail=detail)
        if remaining < 3:
            from istota.notifications.resolvers import task_alert
            task_alert.write(conn, user_id, dedup_key=f"vault-recovery-low:{task_alert._slug(name, limit=64)}",
                             title=f"{name} has {remaining} recovery codes left",
                             body="Regenerate recovery codes on the site and capture the new set.", severity="warning")
        return codes[index]


def close_for_task(conn, task_id, state="cancelled"):
    if state not in ("declined", "expired", "cancelled"):
        raise ValueError("invalid recovery-fill close state")
    return conn.execute("UPDATE recovery_fill_authorizations SET state=? WHERE task_id=? "
                        "AND state IN ('held','authorized')", (state, task_id)).rowcount


def expire(conn):
    return conn.execute("UPDATE recovery_fill_authorizations SET state='expired' "
                        "WHERE state='authorized' AND expires_at<=datetime('now')").rowcount
