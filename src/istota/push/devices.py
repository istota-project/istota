"""APNs registrations, scoped to the current account on each install."""

import logging
import sqlite3

from istota.db import iso_utc_now

logger = logging.getLogger(__name__)


def register_device(
    conn: sqlite3.Connection, user_id: str, token: str, environment: str,
    platform: str = "ios", app_version: str = "",
) -> int | None:
    """Move a token to its current account and seed only a new user's watermark."""
    try:
        # One savepoint makes the registration and watermark atomic, including
        # when the caller has already started a transaction.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("SAVEPOINT register_push_device")
        previous = conn.execute(
            "SELECT user_id FROM push_devices WHERE token = ? AND environment = ?",
            (token, environment),
        ).fetchone()
        now = iso_utc_now()
        row = conn.execute(
            """INSERT INTO push_devices
                (user_id, token, environment, platform, app_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(token, environment) DO UPDATE SET
                    user_id = excluded.user_id, platform = excluded.platform,
                    app_version = excluded.app_version, last_badge = -1,
                    last_error = NULL, last_seen_at = NULL,
                    created_at = excluded.created_at, updated_at = excluded.updated_at
                RETURNING id""",
            (user_id, token, environment, platform, app_version, now, now),
        ).fetchone()
        conn.execute(
            """INSERT INTO push_state (user_id, last_pushed_message_id)
                SELECT ?, COALESCE(MAX(id), 0) FROM messages WHERE 1
                ON CONFLICT(user_id) DO NOTHING""",
            (user_id,),
        )
        conn.execute("RELEASE register_push_device")
        if previous and previous[0] != user_id:
            logger.info("Push device %s moved from user %s to user %s", row[0], previous[0], user_id)
        return int(row[0])
    except Exception as exc:
        try:
            conn.execute("ROLLBACK TO register_push_device")
            conn.execute("RELEASE register_push_device")
        except sqlite3.Error:
            pass
        logger.warning("Push registration failed: %s", type(exc).__name__)
        return None


def list_devices(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    """Public device details; the full token never leaves the server."""
    try:
        return [dict(row) for row in conn.execute(
            """SELECT id, platform, app_version, environment,
                substr(token, -6) AS token_tail, created_at, last_seen_at, last_error
                FROM push_devices WHERE user_id = ? ORDER BY id""", (user_id,),
        )]
    except Exception as exc:
        logger.warning("Push device listing failed: %s", type(exc).__name__)
        return []


def delete_device(conn: sqlite3.Connection, device_id: int, user_id: str) -> bool:
    try:
        return conn.execute(
            "DELETE FROM push_devices WHERE id = ? AND user_id = ?", (device_id, user_id),
        ).rowcount > 0
    except Exception as exc:
        logger.warning("Push device removal failed: %s", type(exc).__name__)
        return False


def devices_for_user(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    """Private rows for the sender, including the token."""
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM push_devices WHERE user_id = ? ORDER BY id", (user_id,),
        )]
    except Exception as exc:
        logger.warning("Push sender device listing failed: %s", type(exc).__name__)
        return []


def any_devices(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute("SELECT 1 FROM push_devices LIMIT 1").fetchone() is not None
    except Exception as exc:
        logger.warning("Push device lookup failed: %s", type(exc).__name__)
        return False
