"""APNs registrations, scoped to the current account on each install.

Under relay delivery a registration also carries the relay route. Its send
capability is a bearer credential, so it is written to a 0600 file named for
the device (`<id>.cap`) in the push credentials directory, and the row holds
only that file name: never the database, a log line or the device list.

Removing a relay device, or re-enrolling it on a different route, queues the
old route in `push_relay_revocations` in the same transaction; the sweep in
`push/revocations.py` revokes it at the relay and then deletes its file. A
replaced route's capability is copied to `revoke-<route_id>.cap` first, since
the device's own file is about to hold the new one.
"""

import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from istota.config import Config
    from istota.push.apns import ApnsResult

from istota.db import iso_utc_now
from istota.push.envelope import b64url_decode
from istota.lib.atomic_write import write_bytes_atomic

logger = logging.getLogger(__name__)

_ROUTE_ID = re.compile(r"[A-Za-z0-9_-]{22}")
_CREDENTIAL_REF = re.compile(r"(?:[0-9]+|revoke-[A-Za-z0-9_-]{22})\.cap")
# A capability file holds one 43-character capability; anything longer is not one.
_CAPABILITY_FILE_MAX = 64
CREDENTIAL_MISSING = "relay_credential_missing"
# Relay results that leave a route unusable until the device enrolls again.
REPAIR_REASONS = frozenset({"unauthorized_route", "expired_route", CREDENTIAL_MISSING})
# How long a queued revocation is retried before the route is left to lapse with its lease.
REVOCATION_LIFETIME_SECONDS = 7 * 86_400


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


def _write_capability_file(directory: Path | None, name: str, capability: str) -> Path:
    if directory is None:
        raise FileNotFoundError("no push credentials directory")
    directory = Path(directory)
    if not directory.is_dir():
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
    write_bytes_atomic(directory / name, capability.encode("ascii"), mode=0o600, fsync=True)
    return directory / name


def write_relay_capability(directory: Path | None, device_id: int, capability: str) -> str:
    """Write one device's send capability (0600, directory 0700) and return its file name."""
    name = f"{int(device_id)}.cap"
    _write_capability_file(directory, name, capability)
    return name


def read_relay_capability(directory: Path, ref: str | None) -> str | None:
    """The send capability a row's file holds, or None when it is missing, a link or not a capability."""
    if not ref or not _CREDENTIAL_REF.fullmatch(ref):
        return None
    try:
        fd = os.open(Path(directory) / ref, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        data = os.read(fd, _CAPABILITY_FILE_MAX + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        capability = data.decode("ascii")
        if len(capability) == 43 and len(b64url_decode(capability)) == 32:
            return capability
    except ValueError:
        pass
    return None


def remove_relay_capability(directory: Path, ref: str | None) -> None:
    """Best-effort removal of a capability file named by a row; anything else is left alone."""
    if not ref or not _CREDENTIAL_REF.fullmatch(ref):
        return
    try:
        (Path(directory) / ref).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Push relay credential removal failed: %s", type(exc).__name__)


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str):
    """One savepoint, so the statements inside are atomic within the caller's transaction too."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        try:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
        except sqlite3.Error:
            pass
        raise
    conn.execute(f"RELEASE {name}")


def queue_revocation(conn: sqlite3.Connection, route_id: str, credential_ref: str, relay_url: str) -> None:
    """Queue one relay route for revocation, due now; the caller's transaction holds it.

    `relay_url` is the relay that issued the route, as configured when it was
    queued; the sweep revokes there and drops what it cannot send.
    """
    now = int(time.time())
    conn.execute(
        """INSERT INTO push_relay_revocations
            (route_id, credential_ref, relay_url, next_attempt_at, attempts, expires_at, created_at)
            VALUES (?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(route_id) DO NOTHING""",
        (route_id, credential_ref or "", relay_url or "", now, now + REVOCATION_LIFETIME_SECONDS, now),
    )


def _queue_removed_routes(conn: sqlite3.Connection, removed, relay_url: str) -> None:
    # A removed device's id is never reused (AUTOINCREMENT), so its own file can stay as the sweep's.
    for route_id, ref in removed:
        if route_id:
            queue_revocation(conn, route_id, ref, relay_url)


def _queue_replaced_route(conn: sqlite3.Connection, directory: Path | None, route_id: str,
                          old_ref: str | None, relay_url: str) -> Path | None:
    """Queue a route a re-registration replaced, its capability copied to a file of its own.

    Returns the file written, so a registration that fails afterwards can take it back.
    """
    if directory is None or not _ROUTE_ID.fullmatch(route_id):
        return None
    if conn.execute("SELECT 1 FROM push_relay_revocations WHERE route_id = ?", (route_id,)).fetchone():
        return None
    capability = read_relay_capability(directory, old_ref)
    if capability is None:
        logger.warning("Push relay route replaced with no readable capability; it lapses with its lease")
        return None
    name = f"revoke-{route_id}.cap"
    written = _write_capability_file(directory, name, capability)
    queue_revocation(conn, route_id, name, relay_url)
    return written


def register_device(
    conn: sqlite3.Connection, user_id: str, token: str, environment: str,
    platform: str = "ios", app_version: str = "", *,
    auth_method: str | None = None, auth_epoch: int | None = None,
    auth_session_id: str | None = None,
    connection_binding: str | None = None, connection_generation: int | None = None,
    private_version: int | None = None, private_content: str | None = None,
    encryption_public_key: str | None = None, encryption_key_id: str | None = None,
    sender_key_id: str | None = None, push_generation: str | None = None,
    relay: dict | None = None, credentials_dir: Path | None = None, relay_url: str = "",
) -> int | None:
    """Move a token to its current account and seed only a new user's watermark.

    A registration without a connection binding clears any earlier one: the same
    token from an older binary is a legacy enrollment again. The private
    enrollment and relay columns follow the same rule, and every registration
    is `active` again.

    `relay` is the validated `{route_id, send_capability, lease_expires_at}`;
    its capability is written to `credentials_dir` inside the savepoint, so a
    file that cannot be written registers nothing. A registration that drops or
    replaces a route queues the old one for revocation at `relay_url`, and
    removes the device's old file once the row no longer names it.
    """
    revoke_file = None
    try:
        # One savepoint makes the registration and watermark atomic, including
        # when the caller has already started a transaction.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("SAVEPOINT register_push_device")
        previous = conn.execute(
            "SELECT user_id, created_at, relay_credential_ref, relay_route_id FROM push_devices"
            " WHERE token = ? AND environment = ?",
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
        replaced = previous["relay_route_id"] if previous else None
        if replaced and (relay is None or relay["route_id"] != replaced):
            revoke_file = _queue_replaced_route(conn, credentials_dir, replaced,
                                                previous["relay_credential_ref"], relay_url)
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
        if revoke_file is not None:
            revoke_file.unlink(missing_ok=True)
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


def delete_device(conn: sqlite3.Connection, device_id: int, user_id: str, *, created_at: str | None = None,
                  relay_url: str = "") -> bool:
    """Remove one registration; a relay route it held is queued for revocation in the same savepoint."""
    try:
        with _savepoint(conn, "delete_push_device"):
            removed = conn.execute(
                "DELETE FROM push_devices WHERE id = ? AND user_id = ? AND (? IS NULL OR created_at = ?)"
                " RETURNING relay_route_id, relay_credential_ref",
                (device_id, user_id, created_at, created_at),
            ).fetchall()
            _queue_removed_routes(conn, removed, relay_url)
        return bool(removed)
    except Exception as exc:
        logger.warning("Push device removal failed: %s", type(exc).__name__)
        return False


def delete_session_devices(conn: sqlite3.Connection, user_id: str, session_id: str, *,
                           relay_url: str = "") -> None:
    """A logout's registrations, their relay routes queued in the same savepoint; errors propagate."""
    if not session_id:
        return
    with _savepoint(conn, "delete_session_push_devices"):
        removed = conn.execute(
            "DELETE FROM push_devices WHERE user_id = ? AND auth_session_id = ?"
            " RETURNING relay_route_id, relay_credential_ref",
            (user_id, session_id),
        ).fetchall()
        _queue_removed_routes(conn, removed, relay_url)


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


def record_result(conn: sqlite3.Connection, device_id: int, result: "ApnsResult", *,
                  credentials_dir: Path | None = None) -> None:
    """Apply a response to the registration that was actually sent to APNs or the relay.

    A deleted row's relay capability file goes with it, from `credentials_dir`:
    the relay revokes the route itself when it reports the device invalid.
    A route result in `REPAIR_REASONS` marks the device `repair`.
    """
    try:
        if result.reason in {"BadTopic", "InvalidProviderToken"}:
            return  # Deployment mistakes must not change device state.
        generation = result.device_created_at
        guard = "id = ? AND (? IS NULL OR created_at = ?)"
        params = (device_id, generation, generation)
        deleted = None
        if result.status == 400 and result.reason in {"BadDeviceToken", "DeviceTokenNotForTopic"}:
            deleted = conn.execute(
                f"DELETE FROM push_devices WHERE {guard} RETURNING relay_credential_ref", params,
            ).fetchall()
        elif result.status == 410 and result.reason == "Unregistered":
            if result.timestamp is not None:
                invalidated = datetime.fromtimestamp(result.timestamp / 1000, timezone.utc).isoformat()
                deleted = conn.execute(
                    f"DELETE FROM push_devices WHERE {guard} AND julianday(created_at) <= julianday(?)"
                    " RETURNING relay_credential_ref",
                    (*params, invalidated),
                ).fetchall()
        elif result.reason in REPAIR_REASONS:
            conn.execute(
                f"UPDATE push_devices SET last_error = ?, enrollment_state = 'repair' WHERE {guard}",
                (f"{result.status} {result.reason}", *params),
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
        if deleted and credentials_dir is not None:
            for (ref,) in deleted:
                remove_relay_capability(credentials_dir, ref)
    except Exception as exc:
        logger.warning("Push result recording failed: %s", type(exc).__name__)
