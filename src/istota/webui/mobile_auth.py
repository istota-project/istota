"""Short-lived browser approvals, independent of HTTP and session construction.

Approval callers must supply a live browser identity after CSRF verification.
Redemption returns only that validated authority; it never copies browser session
fields. The consumer creates a fresh session and a request-specific receipt.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3

from istota.db import get_db
from istota.webui import auth

REQUEST_TTL_SECONDS = 600
CODE_TTL_SECONDS = 60
MAX_CREATIONS = 10
MAX_PENDING = 5
MAX_REQUESTS = 10_000
PRUNE_LIMIT = 100


class HandoffError(ValueError):
    """A fixed reason: invalid, expired, replay, or throttled; never proof data."""


@dataclass(frozen=True)
class HandoffRequest:
    request_id: str
    state: str
    expires_at: str


@dataclass(frozen=True)
class Approval:
    request_id: str
    code: str
    state: str


@dataclass(frozen=True)
class Credential:
    user_id: str
    method: str
    epoch: int


def _valid_proof(value: object) -> bool:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
        return False
    decoded = base64.urlsafe_b64decode(value + "=")
    return base64.urlsafe_b64encode(decoded).decode().rstrip("=") == value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _prune(conn: sqlite3.Connection, now: str, limit: int) -> int:
    count = conn.execute(
        "DELETE FROM web_mobile_auth_requests WHERE request_id IN "
        "(SELECT request_id FROM web_mobile_auth_requests WHERE expires_at <= ? ORDER BY expires_at LIMIT ?)",
        (now, limit),
    ).rowcount
    conn.execute(
        "DELETE FROM web_auth_attempts WHERE id IN "
        "(SELECT id FROM web_auth_attempts WHERE kind='mobile_create' AND at <= ? ORDER BY at LIMIT ?)",
        (auth._timestamp(-REQUEST_TTL_SECONDS), limit),
    )
    return count


def prune_requests(db_path: Path, *, limit: int = MAX_REQUESTS) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_REQUESTS:
        raise ValueError("Invalid prune limit")
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _prune(conn, auth._timestamp(), limit)


def create_request(db_path: Path, *, challenge: str, state: str, source: str) -> HandoffRequest:
    if not _valid_proof(challenge) or not _valid_proof(state):
        raise HandoffError("invalid")
    if not isinstance(source, str) or not source or len(source) > 256:
        raise HandoffError("invalid")
    source_digest = hashlib.sha256(source.encode()).hexdigest()
    request_id = secrets.token_urlsafe(32)
    now, expires = auth._timestamp(), auth._timestamp(REQUEST_TTL_SECONDS)
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _prune(conn, now, PRUNE_LIMIT)
        pending = conn.execute(
            "SELECT count(*) FROM web_mobile_auth_requests WHERE source=? AND expires_at>? AND consumed_at IS NULL",
            (source_digest, now),
        ).fetchone()[0]
        total = conn.execute("SELECT count(*) FROM web_mobile_auth_requests").fetchone()[0]
        denied = (pending >= MAX_PENDING or total >= MAX_REQUESTS
                  or auth._attempts(conn, "mobile_create", source_digest, REQUEST_TTL_SECONDS) >= MAX_CREATIONS)
        if not denied:
            conn.execute("INSERT INTO web_auth_attempts(kind,key,at) VALUES('mobile_create',?,?)", (source_digest, now))
            conn.execute(
                "INSERT INTO web_mobile_auth_requests(request_id,challenge,state,source,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?)", (request_id, challenge, state, source_digest, now, expires),
            )
    # Commit bounded maintenance even when admission was refused.
    if denied:
        raise HandoffError("throttled")
    return HandoffRequest(request_id, state, expires)


def _request(conn: sqlite3.Connection, request_id: str, now: str) -> sqlite3.Row:
    if not _valid_proof(request_id):
        raise HandoffError("invalid")
    row = conn.execute("SELECT * FROM web_mobile_auth_requests WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        raise HandoffError("invalid")
    if row["expires_at"] <= now:
        raise HandoffError("expired")
    return row


def get_request(db_path: Path, request_id: str) -> HandoffRequest:
    with get_db(db_path) as conn:
        row = _request(conn, request_id, auth._timestamp())
        if row["consumed_at"] is not None:
            raise HandoffError("replay")
        return HandoffRequest(row["request_id"], row["state"], row["expires_at"])


def _validate_credential(conn: sqlite3.Connection, config, user_id: str, method: str, epoch: int) -> None:
    if not isinstance(user_id, str) or not user_id or type(epoch) is not int:
        raise HandoffError("invalid")
    if config.users and user_id not in config.users:
        raise HandoffError("invalid")
    if method == "none":
        if not config.web.has_method("none") or user_id != config.local_user_id or epoch != 0:
            raise HandoffError("invalid")
        identity = auth._get_identity(conn, user_id)
        if identity and identity.disabled:
            raise HandoffError("invalid")
        return
    try:
        auth.validate_session_credential(config.db_path, user_id, method, epoch, config.web, conn=conn)
    except ValueError:
        raise HandoffError("invalid") from None


def approve_request(config, request_id: str, *, user_id: str, method: str, epoch: int) -> Approval:
    code = secrets.token_urlsafe(32)
    with get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _request(conn, request_id, auth._timestamp())
        if row["code_digest"] is not None or row["consumed_at"] is not None:
            raise HandoffError("replay")
        _validate_credential(conn, config, user_id, method, epoch)
        conn.execute(
            "UPDATE web_mobile_auth_requests SET user_id=?,auth_method=?,auth_epoch=?,code_digest=?,code_expires_at=? "
            "WHERE request_id=?", (user_id, method, epoch, _digest(code),
                                  min(row["expires_at"], auth._timestamp(CODE_TTL_SECONDS)), request_id),
        )
        return Approval(request_id, code, row["state"])


def redeem_request(config, request_id: str, *, code: str, verifier: str) -> Credential:
    if not _valid_proof(code) or not _valid_proof(verifier):
        raise HandoffError("invalid")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    with get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = auth._timestamp()
        row = _request(conn, request_id, now)
        if (row["code_digest"] is None or not hmac.compare_digest(row["code_digest"], _digest(code))
                or not hmac.compare_digest(row["challenge"], challenge)):
            raise HandoffError("invalid")
        if row["consumed_at"] is not None:
            raise HandoffError("replay")
        if row["code_expires_at"] <= now:
            raise HandoffError("expired")
        _validate_credential(conn, config, row["user_id"], row["auth_method"], row["auth_epoch"])
        conn.execute("UPDATE web_mobile_auth_requests SET consumed_at=? WHERE request_id=?", (now, request_id))
        return Credential(row["user_id"], row["auth_method"], row["auth_epoch"])
