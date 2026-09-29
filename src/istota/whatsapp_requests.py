"""Durable WhatsApp intents. Storage helpers never send or commit."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid

from . import db

MAX_CONTENT_CHARS = 2000
QUEUE_DEADLINE_SECONDS = 600
CONTENT_RETENTION_DAYS = 30
REQUEST_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class RequestError(ValueError):
    """A fixed, body-free refusal code, safe to pass to a caller."""


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """Serialize reads followed by writes, without committing caller work.

    Start an outer transaction before the savepoint: RELEASE must not commit.
    Existing transactions acquire the writer lock before reading state. A
    caller with a stale WAL read snapshot gets SQLite's conflict and must retry
    its whole transaction, never just a state-changing fragment.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("UPDATE whatsapp_skill_requests SET id=id WHERE 0")
    savepoint = "wa_request_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE {savepoint}")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def binding_fingerprint(provider: str, binding: db.WhatsAppBinding) -> str:
    """Freeze identity and destination, never last-seen/window timestamps."""
    values = [provider, binding.provider, binding.user_id, binding.bsuid,
              binding.jid, binding.bootstrap_phone_number, binding.send_id]
    return text_hash(json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def _validate_input(request_key: str, text: str) -> None:
    if not REQUEST_KEY_RE.fullmatch(request_key):
        raise RequestError("invalid_request_key")
    if not text or len(text) > MAX_CONTENT_CHARS:
        raise RequestError("invalid_text")
    if not any(not ch.isspace() and not unicodedata.category(ch).startswith("C") for ch in text):
        raise RequestError("invalid_text")


def _store_request(
    conn: sqlite3.Connection, *, actor_user_id: str, task_id: int,
    request_key: str, kind: str, recipient_user_id: str, text: str,
    service_body: str, template_body: str | None, provider: str,
    binding_fingerprint: str, preview: str | None = None,
    relay_snapshot: dict | None = None, request_id: str | None = None,
    relay_id: str | None = None,
) -> dict:
    """Internal insertion after rendering and surface validation by the host.

    This is not a skill entry point. Its caller supplies validated immutable
    snapshots; it enforces task ownership, replay, consent and reservations.
    """
    from . import message_relays

    _validate_input(request_key, text)
    if kind not in ("self_send", "relay_question"):
        raise RequestError("invalid_kind")
    if kind == "self_send" and recipient_user_id != actor_user_id:
        raise RequestError("recipient_unavailable")
    if kind == "relay_question" and recipient_user_id == actor_user_id:
        raise RequestError("use_send")
    if not service_body:
        raise RequestError("invalid_text")
    payload_hash = text_hash(json.dumps([kind, recipient_user_id, text], ensure_ascii=True))
    with write_transaction(conn):
        task = conn.execute("SELECT * FROM tasks WHERE id=? AND user_id=?",
                            (task_id, actor_user_id)).fetchone()
        if task is None or task["status"] != "running":
            raise RequestError("task_unavailable")
        existing = conn.execute(
            "SELECT * FROM whatsapp_skill_requests WHERE requester_user_id=? "
            "AND origin_task_id=? AND request_key=?", (actor_user_id, task_id, request_key),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != payload_hash:
                raise RequestError("request_conflict")
            return dict(existing)
        if kind == "relay_question":
            if not preview or relay_snapshot is None:
                raise RequestError("invalid_preview")
            if task["whatsapp_confirmation_request_id"] or conn.execute(
                "SELECT 1 FROM whatsapp_skill_requests WHERE origin_task_id=? AND state='held'",
                (task_id,),
            ).fetchone():
                raise RequestError("confirmation_pending")
            message_relays._check_reservation(conn, actor_user_id=actor_user_id,
                                              recipient_user_id=recipient_user_id)
        request_id = request_id or str(uuid.uuid4())
        relay_id = (relay_id or str(uuid.uuid4())) if kind == "relay_question" else None
        state = "held" if relay_id else "queued"
        conn.execute(
            """INSERT INTO whatsapp_skill_requests
            (id,requester_user_id,origin_task_id,request_key,kind,recipient_user_id,
             relay_id,text,content_hash,service_body,service_hash,template_body,
             template_hash,preview,preview_digest,provider,binding_fingerprint,state,queue_deadline)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    CASE WHEN ?='queued' THEN datetime('now', ?) END)""",
            (request_id, actor_user_id, task_id, request_key, kind, recipient_user_id,
             relay_id, text, payload_hash, service_body, text_hash(service_body), template_body,
             text_hash(template_body) if template_body is not None else None,
             preview, text_hash(preview) if preview else None, provider, binding_fingerprint,
             state, state, f"+{QUEUE_DEADLINE_SECONDS} seconds"),
        )
        if relay_id:
            message_relays._insert_relay(
                conn, relay_id=relay_id, request_id=request_id, actor_user_id=actor_user_id,
                recipient_user_id=recipient_user_id, question=text, provider=provider,
                binding_fingerprint=binding_fingerprint, snapshot=relay_snapshot,
            )
        return dict(conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=?", (request_id,)).fetchone())


def get_request(conn: sqlite3.Connection, *, actor_user_id: str, request_id: str) -> dict | None:
    row = conn.execute(
        """SELECT id,request_key,kind,recipient_user_id,relay_id,text,state,preview,
                  created_at,updated_at,approved_at,queue_deadline,closed_at,
                  content_cleared_at,error_code
           FROM whatsapp_skill_requests WHERE id=? AND requester_user_id=?""",
        (request_id, actor_user_id),
    ).fetchone()
    return dict(row) if row is not None else None


def associate_confirmation(
    conn: sqlite3.Connection, *, actor_user_id: str, task_id: int,
    request_id: str, preview_digest: str,
) -> None:
    """Attach only the exact held preview to its owner's parked task."""
    with write_transaction(conn):
        changed = conn.execute(
            """UPDATE tasks SET whatsapp_confirmation_request_id=?
               WHERE id=? AND user_id=? AND status='pending_confirmation'
               AND (whatsapp_confirmation_request_id IS NULL OR whatsapp_confirmation_request_id=?)
               AND EXISTS (SELECT 1 FROM whatsapp_skill_requests r WHERE r.id=?
                           AND r.origin_task_id=tasks.id AND r.requester_user_id=tasks.user_id
                           AND r.state='held' AND r.preview_digest=?)""",
            (request_id, task_id, actor_user_id, request_id, request_id, preview_digest),
        ).rowcount
        if not changed:
            raise RequestError("confirmation_unavailable")


def cleanup_content(conn: sqlite3.Connection, *, limit: int = 100) -> int:
    """Clear bounded content batches; identity/hashes continue to prevent replay.

    Undelivered answers have the same retention deadline as delivered answers.
    Their expiration closes the return outbox before erasing its payload.
    """
    if limit <= 0:
        return 0
    with write_transaction(conn):
        relays = conn.execute(
            """SELECT id,request_id FROM message_relays WHERE content_cleared_at IS NULL
               AND content_expires_at <= datetime('now')
               AND state IN ('answered','failed','cancelled','expired')
               ORDER BY content_expires_at,id LIMIT ?""", (limit,),
        ).fetchall()
        for row in relays:
            conn.execute(
                """UPDATE message_relays SET question=NULL,answer_text=NULL,asker_display=NULL,
                   origin=NULL,audience=NULL,content_cleared_at=datetime('now'),
                   return_state=CASE WHEN return_state IN ('pending','sending','blocked','uncertain')
                                     THEN 'expired' ELSE return_state END WHERE id=?""", (row["id"],),
            )
            _clear_request_content(conn, row["request_id"])
        remaining = limit - len(relays)
        requests = conn.execute(
            """SELECT id FROM whatsapp_skill_requests WHERE content_cleared_at IS NULL
               AND closed_at <= datetime('now', ?)
               AND state IN ('sent','failed','cancelled','expired','uncertain')
               AND NOT EXISTS (SELECT 1 FROM message_relays r WHERE r.request_id=whatsapp_skill_requests.id
                               AND r.content_cleared_at IS NULL)
               ORDER BY closed_at,id LIMIT ?""",
            (f"-{CONTENT_RETENTION_DAYS} days", remaining),
        ).fetchall()
        for row in requests:
            _clear_request_content(conn, row["id"])
        return len(relays) + len(requests)


def _clear_request_content(conn: sqlite3.Connection, request_id: str) -> None:
    conn.execute(
        """UPDATE whatsapp_skill_requests SET text=NULL,service_body=NULL,template_body=NULL,
           preview=NULL,content_cleared_at=datetime('now'),updated_at=datetime('now') WHERE id=?""",
        (request_id,),
    )
