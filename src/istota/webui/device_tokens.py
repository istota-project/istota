"""Per-device bearer tokens for the iOS shell's native actions, stored as hashes.

One row per `(user_id, device_id)`, carrying a set of scopes. Each scope names
a capability the shell may use without a web session; `notes` (a quick note
from the home-screen action) is the first. A new native action adds a scope to
`KNOWN_SCOPES` rather than a credential of its own, so the user sees one row
per phone in Settings. Minting again for a device replaces its token in place,
so the old token is dead the moment the new one exists.

Which routes accept a token is decided by `webui.router_stubs.
require_session_or_device`, not here; a token authenticates as its user and
the route's scope check decides the rest.

Shape and hash are `istota.lib.tokens`, shared with `location.ingest_tokens`.
The two stay separate tables: ingest tokens are read by the location
receiver, a separate process with its own in-memory map, and released shells
post to `/webhooks/location` with them. Separate from `istota.push.devices`
too, since push rows die with their web session and a device token has to
outlive it, which is the case it exists for.
"""

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from istota.db import iso_utc_now
from istota.lib.tokens import hash_token, new_token

logger = logging.getLogger(__name__)

KNOWN_SCOPES = frozenset({"notes"})
MAX_TOKENS_PER_USER = 20
LAST_USED_INTERVAL_SECONDS = 60


class TooManyDevices(Exception):
    """The user already holds `MAX_TOKENS_PER_USER` rows."""


class UnknownScope(ValueError):
    """A scope that is not in `KNOWN_SCOPES`."""


@dataclass(frozen=True)
class DeviceToken:
    """A stored row. `token_hash` is for revoking by the presented token; never put it in a response."""

    id: int
    user_id: str
    device_id: str
    label: str | None
    scopes: frozenset[str]
    token_hash: str
    token_tail: str
    created_at: str
    last_used_at: str | None


def _token(row: sqlite3.Row) -> DeviceToken:
    return DeviceToken(
        id=int(row["id"]),
        user_id=row["user_id"],
        device_id=row["device_id"],
        label=row["label"],
        scopes=frozenset(row["scopes"].split()),
        token_hash=row["token_hash"],
        token_tail=row["token_tail"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


def validate_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """The scopes as a set. `ValueError` for none, `UnknownScope` for one not in `KNOWN_SCOPES`."""
    wanted = frozenset(scopes)
    if not wanted:
        raise ValueError("a device token needs at least one scope")
    unknown = sorted(wanted - KNOWN_SCOPES)
    if unknown:
        raise UnknownScope(unknown[0])
    return wanted


def mint(
    conn: sqlite3.Connection, user_id: str, device_id: str, label: str | None, scopes: Iterable[str],
) -> tuple[int, str]:
    """Create or replace the device's row and return `(row_id, plaintext_token)`.

    The plaintext leaves here once and is never stored. Raises `ValueError`
    (or its `UnknownScope`) for the scopes, and `TooManyDevices` when a new
    row would pass the cap. Replacing a device's row never counts against it.
    """
    if not device_id:
        raise ValueError("a device token needs a device id")
    stored_scopes = " ".join(sorted(validate_scopes(scopes)))
    device_id = device_id.upper()
    token, token_hash, token_tail = new_token()
    now = iso_utc_now()

    # The savepoint keeps the cap check and the write together, and a refusal
    # leaves whatever transaction the caller already had open untouched.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT mint_device_token")
    try:
        existing = conn.execute(
            "SELECT id FROM device_tokens WHERE user_id = ? AND device_id = ?", (user_id, device_id),
        ).fetchone()
        if existing is not None:
            row_id = int(existing[0])
            # A carried-over `last_used_at` would describe the old token.
            conn.execute(
                """UPDATE device_tokens
                    SET token_hash = ?, token_tail = ?, scopes = ?, label = ?,
                        created_at = ?, last_used_at = NULL
                    WHERE id = ?""",
                (token_hash, token_tail, stored_scopes, label, now, row_id),
            )
        else:
            count = conn.execute(
                "SELECT COUNT(*) FROM device_tokens WHERE user_id = ?", (user_id,),
            ).fetchone()[0]
            if count >= MAX_TOKENS_PER_USER:
                raise TooManyDevices(user_id)
            row = conn.execute(
                """INSERT INTO device_tokens
                    (user_id, device_id, token_hash, token_tail, scopes, label, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    RETURNING id""",
                (user_id, device_id, token_hash, token_tail, stored_scopes, label, now),
            ).fetchone()
            row_id = int(row[0])
        conn.execute("RELEASE mint_device_token")
    except BaseException:
        conn.execute("ROLLBACK TO mint_device_token")
        conn.execute("RELEASE mint_device_token")
        raise
    return row_id, token


def list_tokens(conn: sqlite3.Connection, user_id: str) -> list[DeviceToken]:
    """The user's rows, newest first."""
    rows = conn.execute(
        "SELECT * FROM device_tokens WHERE user_id = ? ORDER BY created_at DESC, id DESC", (user_id,),
    ).fetchall()
    return [_token(row) for row in rows]


def delete_token(conn: sqlite3.Connection, user_id: str, row_id: int) -> bool:
    """Delete a row the user owns. False when there is no such row for them."""
    cursor = conn.execute("DELETE FROM device_tokens WHERE id = ? AND user_id = ?", (row_id, user_id))
    return cursor.rowcount > 0


def delete_by_hash(conn: sqlite3.Connection, token_hash: str) -> bool:
    """Delete the row holding this token, for a device revoking itself.

    By hash rather than by row id: a re-mint keeps the row id, so deleting by
    id after a concurrent re-mint would revoke the token that replaced this one.
    """
    cursor = conn.execute("DELETE FROM device_tokens WHERE token_hash = ?", (token_hash,))
    return cursor.rowcount > 0


def authenticate(conn: sqlite3.Connection, plaintext: str) -> DeviceToken | None:
    """The row a presented token belongs to, or None.

    Records `last_used_at` at most once every `LAST_USED_INTERVAL_SECONDS`,
    best effort: a locked database does not fail the request it rides on.
    """
    if not plaintext:
        return None
    token_hash = hash_token(plaintext)
    row = conn.execute("SELECT * FROM device_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
    if row is None:
        return None
    now = iso_utc_now()
    try:
        conn.execute(
            """UPDATE device_tokens SET last_used_at = ?
                WHERE id = ? AND token_hash = ?
                AND (last_used_at IS NULL
                     OR julianday(?) - julianday(last_used_at) >= ? / 86400.0)""",
            (now, row["id"], token_hash, now, LAST_USED_INTERVAL_SECONDS),
        )
    except sqlite3.OperationalError as exc:
        logger.debug("device token %s: last_used_at not recorded: %s", row["token_tail"], exc)
    return _token(row)
