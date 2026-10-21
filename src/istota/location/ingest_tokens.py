"""Per-device location ingest tokens, stored as SHA-256 hashes in `istota.db`.

One row per device that may post to `/webhooks/location`. Kind `'app'` is a
token the in-app tracker minted for its own device id, unique per
`(user_id, device_id)`, so minting again for that device replaces the row in
place. Kind `'code'` is a token shown once as a QR code or pasted into
Overland, and each mint inserts a new row.

The plaintext leaves `mint` once and is never stored. The token shape and its
hash are `istota.lib.tokens`, shared with `istota.webui.device_tokens`; `hash_token`
is re-exported here for the receiver and the tests. The legacy shared `overland` / `ingest_token`
secret is not here; it stays in `secrets`.

Separate from `istota.push.devices` on purpose: push rows belong to a web
session and are deleted at logout, while an ingest token outlives sessions.
"""

import json
import sqlite3
from collections.abc import Iterable

from istota.db import iso_utc_now
from istota.lib.tokens import hash_token as hash_token
from istota.lib.tokens import new_token

MAX_TOKENS_PER_USER = 20
KINDS = ("app", "code")


class TooManyDevices(Exception):
    """The user already holds `MAX_TOKENS_PER_USER` rows."""


def mint(
    conn: sqlite3.Connection, user_id: str, kind: str, device_id: str | None, label: str,
) -> tuple[int, str]:
    """Create or replace a token row and return `(row_id, plaintext_token)`.

    Raises `ValueError` for a kind and device id that do not fit together, and
    `TooManyDevices` when a new row would pass the cap. Replacing an existing
    `'app'` row never counts against the cap.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown token kind: {kind!r}")
    if kind == "app" and not device_id:
        raise ValueError("an 'app' token needs a device id")
    if kind == "code" and device_id is not None:
        raise ValueError("a 'code' token takes no device id")
    if device_id is not None:
        device_id = device_id.upper()

    token, token_hash, token_tail = new_token()
    now = iso_utc_now()

    # The savepoint keeps the cap check and the write together, and a refusal
    # leaves whatever transaction the caller already had open untouched.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT mint_location_ingest_token")
    try:
        existing = None
        if device_id is not None:
            existing = conn.execute(
                "SELECT id FROM location_ingest_tokens WHERE user_id = ? AND device_id = ?",
                (user_id, device_id),
            ).fetchone()
        if existing is not None:
            row_id = int(existing[0])
            conn.execute(
                """UPDATE location_ingest_tokens
                    SET token_hash = ?, token_tail = ?, created_at = ?, last_used_at = NULL
                    WHERE id = ?""",
                (token_hash, token_tail, now, row_id),
            )
        else:
            count = conn.execute(
                "SELECT COUNT(*) FROM location_ingest_tokens WHERE user_id = ?", (user_id,),
            ).fetchone()[0]
            if count >= MAX_TOKENS_PER_USER:
                raise TooManyDevices(user_id)
            row = conn.execute(
                """INSERT INTO location_ingest_tokens
                    (user_id, token_hash, token_tail, kind, device_id, label, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    RETURNING id""",
                (user_id, token_hash, token_tail, kind, device_id, label, now),
            ).fetchone()
            row_id = int(row[0])
        conn.execute("RELEASE mint_location_ingest_token")
    except BaseException:
        conn.execute("ROLLBACK TO mint_location_ingest_token")
        conn.execute("RELEASE mint_location_ingest_token")
        raise
    return row_id, token


def list_tokens(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    """The user's rows, newest first. Never includes the hash."""
    rows = conn.execute(
        """SELECT id, kind, device_id, label, token_tail, created_at, last_used_at
            FROM location_ingest_tokens WHERE user_id = ?
            ORDER BY created_at DESC, id DESC""",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def delete_token(conn: sqlite3.Connection, user_id: str, row_id: int) -> bool:
    """Delete a row the user owns. False when there is no such row for them."""
    cursor = conn.execute(
        "DELETE FROM location_ingest_tokens WHERE id = ? AND user_id = ?", (row_id, user_id),
    )
    return cursor.rowcount > 0


def all_token_hashes(conn: sqlite3.Connection, user_ids: Iterable[str]) -> list[tuple[str, str, int]]:
    """`(token_hash, user_id, row_id)` for every row of the named users."""
    wanted = list(user_ids)
    if not wanted:
        return []
    rows = conn.execute(
        """SELECT token_hash, user_id, id FROM location_ingest_tokens
            WHERE user_id IN (SELECT value FROM json_each(?))""",
        (json.dumps(wanted),),
    ).fetchall()
    return [(row[0], row[1], int(row[2])) for row in rows]


def touch_last_used(conn: sqlite3.Connection, row_id: int, when: str) -> None:
    conn.execute("UPDATE location_ingest_tokens SET last_used_at = ? WHERE id = ?", (when, row_id))
