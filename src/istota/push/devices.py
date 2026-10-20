"""APNs registrations, scoped to the current account on each install."""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from istota.push.apns import ApnsResult

from istota.db import iso_utc_now

logger = logging.getLogger(__name__)


def register_device(
    conn: sqlite3.Connection, user_id: str, token: str, environment: str,
    platform: str = "ios", app_version: str = "", *,
    auth_method: str | None = None, auth_epoch: int | None = None,
    auth_session_id: str | None = None,
    connection_binding: str | None = None, connection_generation: int | None = None,
) -> int | None:
    """Move a token to its current account and seed only a new user's watermark.

    A registration without a connection binding clears any earlier one: the same
    token from an older binary is a legacy enrollment again.
    """
    try:
        # One savepoint makes the registration and watermark atomic, including
        # when the caller has already started a transaction.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("SAVEPOINT register_push_device")
        previous = conn.execute(
            "SELECT user_id, created_at FROM push_devices WHERE token = ? AND environment = ?",
            (token, environment),
        ).fetchone()
        now = iso_utc_now()
        if previous and now <= previous["created_at"]:
            stamp = datetime.fromisoformat(previous["created_at"].replace("Z", "+00:00")) + timedelta(milliseconds=1)
            now = stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        row = conn.execute(
            """INSERT INTO push_devices
                (user_id, token, environment, platform, app_version, created_at, updated_at, auth_method, auth_epoch,
                 auth_session_id, connection_binding, connection_generation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(token, environment) DO UPDATE SET
                    user_id = excluded.user_id, platform = excluded.platform,
                    app_version = excluded.app_version, last_badge = -1,
                    auth_method = excluded.auth_method, auth_epoch = excluded.auth_epoch,
                    auth_session_id = excluded.auth_session_id,
                    connection_binding = excluded.connection_binding,
                    connection_generation = excluded.connection_generation,
                    last_error = NULL, last_seen_at = NULL,
                    created_at = excluded.created_at, updated_at = excluded.updated_at
                RETURNING id""",
            (user_id, token, environment, platform, app_version, now, now, auth_method, auth_epoch, auth_session_id,
             connection_binding, connection_generation),
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


def delete_device(conn: sqlite3.Connection, device_id: int, user_id: str, *, created_at: str | None = None) -> bool:
    try:
        return conn.execute(
            "DELETE FROM push_devices WHERE id = ? AND user_id = ? AND (? IS NULL OR created_at = ?)",
            (device_id, user_id, created_at, created_at),
        ).rowcount > 0
    except Exception as exc:
        logger.warning("Push device removal failed: %s", type(exc).__name__)
        return False


def delete_session_devices(conn: sqlite3.Connection, user_id: str, session_id: str) -> None:
    if not session_id:
        return
    conn.execute(
        "DELETE FROM push_devices WHERE user_id = ? AND auth_session_id = ?",
        (user_id, session_id),
    )


def registration_authorized(config, device: dict) -> bool:
    """Recheck enrollment authority before every send; legacy rows fail closed."""
    from istota.webui.auth import credential_still_valid

    return credential_still_valid(config, device["user_id"], device.get("auth_method"), device.get("auth_epoch"))


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


def record_result(conn: sqlite3.Connection, device_id: int, result: "ApnsResult") -> None:
    """Apply a response to the registration that was actually sent to APNs."""
    try:
        if result.reason in {"BadTopic", "InvalidProviderToken"}:
            return  # Deployment mistakes must not change device state.
        generation = result.device_created_at
        guard = "id = ? AND (? IS NULL OR created_at = ?)"
        params = (device_id, generation, generation)
        if result.status == 400 and result.reason in {"BadDeviceToken", "DeviceTokenNotForTopic"}:
            conn.execute(f"DELETE FROM push_devices WHERE {guard}", params)
        elif result.status == 410 and result.reason == "Unregistered":
            if result.timestamp is not None:
                invalidated = datetime.fromtimestamp(result.timestamp / 1000, timezone.utc).isoformat()
                conn.execute(
                    f"DELETE FROM push_devices WHERE {guard} AND julianday(created_at) <= julianday(?)",
                    (*params, invalidated),
                )
        elif result.success:
            conn.execute(
                f"UPDATE push_devices SET last_seen_at = ?, last_error = NULL, "
                f"last_badge = COALESCE(?, last_badge) WHERE {guard}",
                (iso_utc_now(), result.badge, *params),
            )
        else:
            conn.execute(
                f"UPDATE push_devices SET last_error = ? WHERE {guard}",
                (f"{result.status} {result.reason}".strip(), *params),
            )
    except Exception as exc:
        logger.warning("Push result recording failed: %s", type(exc).__name__)
