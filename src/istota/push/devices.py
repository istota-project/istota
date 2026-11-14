"""APNs registrations, scoped to the current account on each install.

Under relay delivery a registration also carries the relay route. Its send
capability is a bearer credential, so it is written to a 0600 file named for
the device (`<id>.cap`) in the push credentials directory, and the row holds
only that file name: never the database, a log line or the device list.
"""

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from istota.config import Config
    from istota.push.apns import ApnsResult

from istota.db import iso_utc_now
from istota.lib.atomic_write import write_bytes_atomic

logger = logging.getLogger(__name__)

_CREDENTIAL_REF = re.compile(r"[0-9]+\.cap")


def relay_credentials_dir(config: "Config") -> Path:
    """`[push] push_credentials_dir`, else `push/relay` beside the framework database.

    Not beside the signing key: the stack mounts the config directory read-only
    for every running service, and the web process writes these files at
    registration. The database's directory is writable by web and daemon alike
    and already masked from every sandbox.
    """
    if config.push.push_credentials_dir.strip():
        return Path(config.push.push_credentials_dir)
    return Path(config.db_path).parent / "push" / "relay"


def write_relay_capability(directory: Path | None, device_id: int, capability: str) -> str:
    """Write one device's send capability (0600, directory 0700) and return its file name."""
    if directory is None:
        raise FileNotFoundError("no push credentials directory")
    directory = Path(directory)
    if not directory.is_dir():
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
    name = f"{int(device_id)}.cap"
    write_bytes_atomic(directory / name, capability.encode("ascii"), mode=0o600, fsync=True)
    return name


def remove_relay_capability(directory: Path, ref: str | None) -> None:
    """Best-effort removal of a capability file named by a row; anything else is left alone."""
    if not ref or not _CREDENTIAL_REF.fullmatch(ref):
        return
    try:
        (Path(directory) / ref).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Push relay credential removal failed: %s", type(exc).__name__)


def register_device(
    conn: sqlite3.Connection, user_id: str, token: str, environment: str,
    platform: str = "ios", app_version: str = "", *,
    auth_method: str | None = None, auth_epoch: int | None = None,
    auth_session_id: str | None = None,
    connection_binding: str | None = None, connection_generation: int | None = None,
    private_version: int | None = None, private_content: str | None = None,
    encryption_public_key: str | None = None, encryption_key_id: str | None = None,
    sender_key_id: str | None = None, push_generation: str | None = None,
    relay: dict | None = None, credentials_dir: Path | None = None,
) -> int | None:
    """Move a token to its current account and seed only a new user's watermark.

    A registration without a connection binding clears any earlier one: the same
    token from an older binary is a legacy enrollment again. The private
    enrollment and relay columns follow the same rule, and every registration
    is `active` again.

    `relay` is the validated `{route_id, send_capability, lease_expires_at}`;
    its capability is written to `credentials_dir` inside the savepoint, so a
    file that cannot be written registers nothing. A registration that drops a
    route removes the old file once the row no longer names it.
    """
    try:
        # One savepoint makes the registration and watermark atomic, including
        # when the caller has already started a transaction.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("SAVEPOINT register_push_device")
        previous = conn.execute(
            "SELECT user_id, created_at, relay_credential_ref FROM push_devices WHERE token = ? AND environment = ?",
            (token, environment),
        ).fetchone()
        now = iso_utc_now()
        if previous and now <= previous["created_at"]:
            stamp = datetime.fromisoformat(previous["created_at"].replace("Z", "+00:00")) + timedelta(milliseconds=1)
            now = stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        row = conn.execute(
            """INSERT INTO push_devices
                (user_id, token, environment, platform, app_version, created_at, updated_at, auth_method, auth_epoch,
                 auth_session_id, connection_binding, connection_generation, private_version, private_content,
                 encryption_public_key, encryption_key_id, sender_key_id, push_generation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(token, environment) DO UPDATE SET
                    user_id = excluded.user_id, platform = excluded.platform,
                    app_version = excluded.app_version, last_badge = -1,
                    auth_method = excluded.auth_method, auth_epoch = excluded.auth_epoch,
                    auth_session_id = excluded.auth_session_id,
                    connection_binding = excluded.connection_binding,
                    connection_generation = excluded.connection_generation,
                    private_version = excluded.private_version, private_content = excluded.private_content,
                    encryption_public_key = excluded.encryption_public_key,
                    encryption_key_id = excluded.encryption_key_id, sender_key_id = excluded.sender_key_id,
                    push_generation = excluded.push_generation,
                    relay_route_id = NULL, relay_credential_ref = NULL, relay_lease_expires_at = NULL,
                    enrollment_state = 'active',
                    last_error = NULL, last_seen_at = NULL,
                    created_at = excluded.created_at, updated_at = excluded.updated_at
                RETURNING id""",
            (user_id, token, environment, platform, app_version, now, now, auth_method, auth_epoch, auth_session_id,
             connection_binding, connection_generation, private_version, private_content, encryption_public_key,
             encryption_key_id, sender_key_id, push_generation),
        ).fetchone()
        conn.execute(
            """INSERT INTO push_state (user_id, last_pushed_message_id)
                SELECT ?, COALESCE(MAX(id), 0) FROM messages WHERE 1
                ON CONFLICT(user_id) DO NOTHING""",
            (user_id,),
        )
        ref = None
        if relay is not None:
            # Last before the release, so a write that fails rolls back the row
            # and nothing after it can leave a row naming a file never written.
            ref = f"{int(row[0])}.cap"
            conn.execute(
                "UPDATE push_devices SET relay_route_id = ?, relay_credential_ref = ?, relay_lease_expires_at = ?"
                " WHERE id = ?",
                (relay["route_id"], ref, relay["lease_expires_at"], row[0]),
            )
            write_relay_capability(credentials_dir, row[0], relay["send_capability"])
        conn.execute("RELEASE register_push_device")
        stale_ref = previous["relay_credential_ref"] if previous else None
        if stale_ref and stale_ref != ref and credentials_dir is not None:
            remove_relay_capability(credentials_dir, stale_ref)
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


def device_content(content_mode: str, device: dict, sender_key_id: str | None) -> tuple[str, bool]:
    """The content mode a device gets, and whether its enrollment names an old sender key.

    `content_mode` is the installation's `[push] content`; `sender_key_id` is the
    current key's ID, or None when it cannot be read, which marks nothing stale.
    A stale encrypted enrollment gets generic content until the device re-enrolls.
    """
    if content_mode != "private":
        return "legacy", False
    if device.get("private_content") != "encrypted" or not device.get("encryption_public_key"):
        return "generic", False
    if sender_key_id is not None and device.get("sender_key_id") != sender_key_id:
        return "generic", True
    return "encrypted", False


def list_devices(
    conn: sqlite3.Connection, user_id: str, *, content_mode: str = "legacy", sender_key_id: str | None = None,
) -> list[dict]:
    """Public device details; the full token, keys and generations never leave the server."""
    try:
        listed = []
        for row in conn.execute(
            """SELECT id, platform, app_version, environment,
                substr(token, -6) AS token_tail, created_at, last_seen_at, last_error, enrollment_state,
                private_content, encryption_public_key, sender_key_id
                FROM push_devices WHERE user_id = ? ORDER BY id""", (user_id,),
        ):
            device = dict(row)
            content, stale = device_content(content_mode, device, sender_key_id)
            for private in ("private_content", "encryption_public_key", "sender_key_id"):
                del device[private]
            device["content"] = content
            device["stale_key"] = stale
            listed.append(device)
        return listed
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


def record_refusal(conn: sqlite3.Connection, device: dict, reason: str) -> None:
    """A payload refused before APNs; the row and its enrollment are otherwise left alone."""
    try:
        conn.execute(
            "UPDATE push_devices SET last_error = ? WHERE id = ? AND created_at = ?",
            (reason, device["id"], device["created_at"]),
        )
    except Exception as exc:
        logger.warning("Push refusal recording failed: %s", type(exc).__name__)


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
