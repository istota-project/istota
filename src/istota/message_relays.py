"""Directional recipient consent and durable one-question relay state."""

from __future__ import annotations

import json
import sqlite3

from .whatsapp_requests import CONTENT_RETENTION_DAYS, RequestError, write_transaction

RELAY_LIFETIME_SECONDS = 24 * 60 * 60
MAX_OPEN_PER_ASKER = 10
MAX_OPEN_PER_RECIPIENT = 20
MAX_REPLY_CANDIDATES = 20
REPLY_CANDIDATE_SECONDS = 600
OPEN_STATES = ("held", "queued", "sending", "waiting", "uncertain")
_OPEN_SQL = "('held','queued','sending','waiting','uncertain')"


def set_permission(conn: sqlite3.Connection, *, actor_user_id: str, asker_user_id: str) -> None:
    if not actor_user_id or not asker_user_id or actor_user_id == asker_user_id or asker_user_id == "*":
        raise RequestError("invalid_user")
    conn.execute(
        """INSERT INTO relay_permissions (recipient_user_id,asker_user_id) VALUES (?,?)
           ON CONFLICT (recipient_user_id,asker_user_id) DO UPDATE
           SET granted_at=datetime('now'),revoked_at=NULL""", (actor_user_id, asker_user_id),
    )


def has_permission(conn: sqlite3.Connection, *, actor_user_id: str, asker_user_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM relay_permissions WHERE recipient_user_id=? AND asker_user_id=? AND revoked_at IS NULL",
        (actor_user_id, asker_user_id),
    ).fetchone() is not None


def list_permissions(conn: sqlite3.Connection, *, actor_user_id: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT asker_user_id,granted_at FROM relay_permissions WHERE recipient_user_id=? "
        "AND revoked_at IS NULL ORDER BY asker_user_id", (actor_user_id,),
    )]


def revoke_permission(conn: sqlite3.Connection, *, actor_user_id: str, asker_user_id: str) -> int:
    with write_transaction(conn):
        conn.execute(
            "UPDATE relay_permissions SET revoked_at=datetime('now') WHERE recipient_user_id=? AND asker_user_id=?",
            (actor_user_id, asker_user_id),
        )
        rows = conn.execute(
            f"SELECT id FROM message_relays WHERE recipient_user_id=? AND asker_user_id=? AND state IN {_OPEN_SQL}",
            (actor_user_id, asker_user_id),
        ).fetchall()
        for row in rows:
            _close_relay(conn, row["id"], state="cancelled", reason="consent_revoked")
        return len(rows)


def _check_reservation(conn: sqlite3.Connection, *, actor_user_id: str, recipient_user_id: str) -> None:
    if not has_permission(conn, actor_user_id=recipient_user_id, asker_user_id=actor_user_id):
        raise RequestError("recipient_unavailable")
    if conn.execute(
        f"SELECT 1 FROM message_relays WHERE asker_user_id=? AND recipient_user_id=? AND state IN {_OPEN_SQL}",
        (actor_user_id, recipient_user_id),
    ).fetchone():
        raise RequestError("relay_already_open")
    for column, user, cap in (("asker_user_id", actor_user_id, MAX_OPEN_PER_ASKER),
                              ("recipient_user_id", recipient_user_id, MAX_OPEN_PER_RECIPIENT)):
        count = conn.execute(
            f"SELECT count(*) FROM message_relays WHERE {column}=? AND state IN {_OPEN_SQL}", (user,),
        ).fetchone()[0]
        if count >= cap:
            raise RequestError("relay_limit")


def _insert_relay(
    conn: sqlite3.Connection, *, relay_id: str, request_id: str, actor_user_id: str,
    recipient_user_id: str, question: str, provider: str, binding_fingerprint: str, snapshot: dict,
) -> None:
    conn.execute(
        """INSERT INTO message_relays
           (id,asker_user_id,recipient_user_id,request_id,question,asker_display,origin,audience,
            provider,binding_fingerprint,state,return_reference)
           VALUES (?,?,?,?,?,?,?,?,?,?,'held',?)""",
        (relay_id, actor_user_id, recipient_user_id, request_id, question, snapshot["asker_display"],
         json.dumps(snapshot["origin"], sort_keys=True), json.dumps(snapshot["audience"]),
         provider, binding_fingerprint, "relay-return:" + relay_id),
    )


_PUBLIC_COLUMNS = """id,asker_user_id,recipient_user_id,surface,question,asker_display,
    state,created_at,approved_at,expires_at,answered_at,closed_at,answer_text,
    return_state,content_expires_at,content_cleared_at"""


def get_relay(conn: sqlite3.Connection, *, actor_user_id: str, relay_id: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_PUBLIC_COLUMNS} FROM message_relays WHERE id=? AND (asker_user_id=? OR recipient_user_id=?)",
        (relay_id, actor_user_id, actor_user_id),
    ).fetchone()
    return dict(row) if row is not None else None


def list_relays(conn: sqlite3.Connection, *, actor_user_id: str, limit: int = 50) -> list[dict]:
    return [dict(row) for row in conn.execute(
        f"SELECT {_PUBLIC_COLUMNS} FROM message_relays WHERE asker_user_id=? OR recipient_user_id=? "
        "ORDER BY created_at DESC,id DESC LIMIT ?", (actor_user_id, actor_user_id, max(0, min(limit, 100))),
    )]


def cancel_relay(conn: sqlite3.Connection, *, actor_user_id: str, relay_id: str) -> bool:
    with write_transaction(conn):
        relay = get_relay(conn, actor_user_id=actor_user_id, relay_id=relay_id)
        if relay is None:
            raise RequestError("relay_unavailable")
        return _close_relay(conn, relay_id, state="cancelled", reason="cancelled")


def _close_relay(conn: sqlite3.Connection, relay_id: str, *, state: str, reason: str) -> bool:
    if state not in ("failed", "cancelled", "expired"):
        raise ValueError("invalid_terminal_state")
    changed = conn.execute(
        f"""UPDATE message_relays SET state=?,closed_at=datetime('now'),content_expires_at=datetime('now',?)
            WHERE id=? AND state IN {_OPEN_SQL}""",
        (state, f"+{CONTENT_RETENTION_DAYS} days", relay_id),
    ).rowcount
    if changed:
        conn.execute(
            """UPDATE whatsapp_skill_requests SET state=?,error_code=?,closed_at=datetime('now'),
               updated_at=datetime('now') WHERE relay_id=?""", (state, reason, relay_id),
        )
        conn.execute(
            """UPDATE tasks SET whatsapp_confirmation_request_id=NULL
               WHERE whatsapp_confirmation_request_id IN
               (SELECT id FROM whatsapp_skill_requests WHERE relay_id=?)""", (relay_id,),
        )
    return bool(changed)


def store_reply_candidate(
    conn: sqlite3.Connection, *, actor_user_id: str, provider: str,
    inbound_id: str, quoted_id: str, text: str,
) -> str:
    """Persist an authenticated early quote, leaving task creation to ingest.

    Overflow and no-inflight results tell ingest to create the ordinary task
    immediately. Expired rows still count until reconciliation creates their
    task; silently dropping them would lose the recipient's message.
    """
    with write_transaction(conn):
        existing = conn.execute(
            "SELECT recipient_user_id FROM relay_reply_candidates WHERE provider=? AND inbound_id=?",
            (provider, inbound_id),
        ).fetchone()
        if existing is not None:
            if existing["recipient_user_id"] != actor_user_id:
                raise RequestError("candidate_unavailable")
            return "pending"
        if not conn.execute(
            "SELECT 1 FROM message_relays WHERE recipient_user_id=? AND provider=? AND state='sending'",
            (actor_user_id, provider),
        ).fetchone():
            return "no_inflight"
        count = conn.execute(
            "SELECT count(*) FROM relay_reply_candidates WHERE recipient_user_id=?", (actor_user_id,),
        ).fetchone()[0]
        if count >= MAX_REPLY_CANDIDATES:
            return "overflow"
        conn.execute(
            """INSERT INTO relay_reply_candidates
               (provider,inbound_id,recipient_user_id,quoted_id,answer_text,expires_at)
               VALUES (?,?,?,?,?,datetime('now',?))""",
            (provider, inbound_id, actor_user_id, quoted_id, text, f"+{REPLY_CANDIDATE_SECONDS} seconds"),
        )
        return "pending"
