"""Secret links: short-lived, single-use capability tokens for one action.

A link lets a user enter a secret (or, later, connect Google) from a chat
without a web session: opening it is the authentication. This module is the
token store only, with no HTTP. It follows `webui/auth.py`'s token discipline:
a SHA-256 digest is the only thing stored, a token is spent by an
``UPDATE ... WHERE used_at IS NULL`` whose rowcount decides who won, and a peek
writes nothing, because link previews fetch the page.

``issue``, ``live_for_update``, ``spend`` and ``record_failure`` take the
caller's connection, and the caller owns the transaction. ``peek`` and
``prune`` open their own.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from istota.db import get_db
from istota.webui.auth import _timestamp

INTENTS = ("credential", "google_connect")
OUTCOMES = ("stored", "cancelled", "failed", "superseded", "connected")
MAX_LIVE_PER_USER = 5
MAX_FAILURES = 5
PRUNE_AFTER_DAYS = 7


class SecretLinkError(ValueError):
    """A refusal with a fixed ``reason``: ``invalid`` or ``too_many_links``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SecretLink:
    id: int
    user_id: str
    intent: str
    params: dict
    task_id: int
    room_token: str
    expires_at: str


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _plausible(token: object) -> bool:
    return isinstance(token, str) and 0 < len(token) <= 256 and token.isascii()


def _link(row: sqlite3.Row) -> SecretLink:
    return SecretLink(
        id=row["id"],
        user_id=row["user_id"],
        intent=row["intent"],
        params=json.loads(row["params"]),
        task_id=row["task_id"],
        room_token=row["room_token"],
        expires_at=row["expires_at"],
    )


def _live_row(conn: sqlite3.Connection, token: object) -> sqlite3.Row | None:
    if not _plausible(token):
        return None
    return conn.execute(
        "SELECT * FROM secret_links WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
        (_digest(token), _timestamp()),
    ).fetchone()


def _check_request(user_id, intent, params, task_id, room_token, ttl_seconds) -> None:
    if not isinstance(user_id, str) or not user_id:
        raise SecretLinkError("invalid")
    if intent not in INTENTS or not isinstance(params, dict):
        raise SecretLinkError("invalid")
    if intent == "credential":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise SecretLinkError("invalid")
    if type(task_id) is not int or not isinstance(room_token, str):
        raise SecretLinkError("invalid")
    if type(ttl_seconds) is not int or ttl_seconds <= 0:
        raise SecretLinkError("invalid")


def issue(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    intent: str,
    params: dict,
    task_id: int,
    room_token: str,
    ttl_seconds: int,
) -> tuple[str, SecretLink]:
    """Store a new link and return its raw token, which is never stored.

    A ``credential`` link supersedes the user's live link for the same name.
    The cap of ``MAX_LIVE_PER_USER`` is counted without the link being
    superseded, and checked before anything is written.
    """
    _check_request(user_id, intent, params, task_id, room_token, ttl_seconds)
    try:
        params_json = json.dumps(params, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise SecretLinkError("invalid") from None
    now = _timestamp()
    superseded: list[int] = []
    if intent == "credential":
        superseded = [r[0] for r in conn.execute(
            "SELECT id FROM secret_links WHERE user_id = ? AND intent = 'credential' "
            "AND json_extract(params, '$.name') = ? AND used_at IS NULL AND expires_at > ?",
            (user_id, params["name"], now),
        )]
    live = conn.execute(
        "SELECT count(*) FROM secret_links WHERE user_id = ? AND used_at IS NULL AND expires_at > ?",
        (user_id, now),
    ).fetchone()[0]
    if live - len(superseded) >= MAX_LIVE_PER_USER:
        raise SecretLinkError("too_many_links")
    for link_id in superseded:
        spend(conn, link_id, "superseded")
    token = secrets.token_urlsafe(32)
    expires_at = _timestamp(ttl_seconds)
    cursor = conn.execute(
        "INSERT INTO secret_links (token_hash, user_id, intent, params, task_id, room_token, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_digest(token), user_id, intent, params_json, task_id, room_token, expires_at),
    )
    link = SecretLink(
        id=cursor.lastrowid,
        user_id=user_id,
        intent=intent,
        params=json.loads(params_json),
        task_id=task_id,
        room_token=room_token,
        expires_at=expires_at,
    )
    return token, link


def peek(db_path: Path, token: object) -> SecretLink | None:
    """The live link for ``token``, or None. Writes nothing."""
    if not _plausible(token):
        return None
    with get_db(db_path) as conn:
        row = _live_row(conn, token)
        return _link(row) if row is not None else None


def live_for_update(conn: sqlite3.Connection, token: object) -> SecretLink | None:
    """The live link for ``token``, read inside the caller's ``BEGIN IMMEDIATE``."""
    row = _live_row(conn, token)
    return _link(row) if row is not None else None


def spend(conn: sqlite3.Connection, link_id: int, outcome: str) -> bool:
    """Mark the link used. True only when this call is the one that spent it."""
    if outcome not in OUTCOMES:
        raise ValueError("Unknown secret link outcome")
    cursor = conn.execute(
        "UPDATE secret_links SET used_at = ?, outcome = ? WHERE id = ? AND used_at IS NULL",
        (_timestamp(), outcome, link_id),
    )
    return cursor.rowcount == 1


def record_failure(conn: sqlite3.Connection, link_id: int) -> int:
    """Count one failed submit and return the new count.

    The ``MAX_FAILURES``th spends the link with ``outcome = 'failed'`` in the
    same statement. A link that is already spent counts nothing and returns 0.
    """
    row = conn.execute(
        "UPDATE secret_links SET failures = failures + 1, "
        "used_at = CASE WHEN failures + 1 >= ? THEN ? ELSE used_at END, "
        "outcome = CASE WHEN failures + 1 >= ? THEN 'failed' ELSE outcome END "
        "WHERE id = ? AND used_at IS NULL RETURNING failures",
        (MAX_FAILURES, _timestamp(), MAX_FAILURES, link_id),
    ).fetchone()
    return row[0] if row is not None else 0


def prune(db_path: Path, *, older_than_days: int = PRUNE_AFTER_DAYS) -> int:
    """Delete links that expired more than ``older_than_days`` ago.

    A spent link is spent before it expires, so its expiry bounds both.
    """
    if type(older_than_days) is not int or older_than_days < 0:
        raise ValueError("Invalid prune window")
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return conn.execute(
            "DELETE FROM secret_links WHERE expires_at <= ?",
            (_timestamp(-older_than_days * 86400),),
        ).rowcount
