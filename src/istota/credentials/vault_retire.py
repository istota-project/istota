"""Retire shared KeePass files after one non-deleting import.

Keep this migration until the release after the single-store upgrade.
"""
from dataclasses import dataclass, field
import json
import logging
import os
import time

from istota import db, storage
from istota.credentials import audit, kdbx_import, store, vault
from istota.notifications.resolvers import connected_service, task_alert
from istota.notifications.store import deliver_pending

logger = logging.getLogger(__name__)
NAMESPACE = "_credential_migration"
MARKER = "vault_retired"
RETRY_SECONDS = 7 * 86400
_FAILURE_TEXT = {
    "VaultLocked": "the stored passphrase did not unlock the file, or the file needs a key file",
    "VaultCorrupt": "the file could not be read as a KeePass database",
    "VaultPassphraseMissing": "no passphrase was stored",
    "VaultKeyUnusable": "the server could not decrypt the stored passphrase",
    "VaultPathRefused": "the configured file location was refused",
    "VaultLibraryMissing": "KeePass support is not installed on the server",
    "VaultIsolationRequired": "credential isolation is not available on this server",
    "VaultMissing": "the file remained unavailable for seven days",
    "VaultUnreadable": "the file remained unreadable for seven days",
    "OSError": "the file storage remained unavailable for seven days",
}


@dataclass(frozen=True)
class RetireResult:
    user_id: str
    outcome: str
    reason: str = ""
    imported: list[str] = field(default_factory=list)


def pending_users(config) -> list[str]:
    if not config.db_path or not config.users:
        return []
    with db.get_db(config.db_path) as conn:
        retired = {row[0] for row in conn.execute(
            "SELECT user_id FROM istota_kv WHERE namespace=? AND key=?", (NAMESPACE, MARKER))}
        configured = {row[0] for row in conn.execute(
            "SELECT user_id FROM secrets WHERE service='vault' AND key='passphrase'")}
    return [user_id for user_id in config.users if user_id not in retired
            and (user_id in configured or config.vault_path_for(user_id))]


def retire_user(config, user_id, *, now: float | None = None) -> RetireResult:
    now = time.time() if now is None else now
    with db.get_db(config.db_path) as conn:
        if db.kv_get(conn, user_id, NAMESPACE, MARKER) is not None:
            return RetireResult(user_id, "already_retired")
    if user_id not in pending_users(config):
        return RetireResult(user_id, "not_configured")
    data, passphrase, preview = None, None, None
    filename = "Your KeePass file"
    reason = ""
    try:
        refusal = vault.vault_isolation_refusal(config, user_id)
        if refusal:
            raise vault.VaultIsolationRequired("isolation required")
        resolution = storage.vault_location_for(config, user_id)
        if resolution.location is None:
            if resolution.refusal in (storage.VAULT_DIR_EMPTY, storage.VAULT_PATH_NO_SUCH_DIRECTORY):
                raise vault.VaultMissing("file unavailable")
            raise vault.VaultPathRefused("file selection unavailable")
        location = resolution.location
        filename = vault.label_for_display(location.path.name)
        try:
            data, _ = vault.read_vault_bytes(location.path, dir_fd=location.dir_fd)
        finally:
            if location.dir_fd is not None:
                os.close(location.dir_fd)
        passphrase = vault._resolve_passphrase(config.db_path, user_id)
        preview = kdbx_import.preview(config.db_path, user_id, data, passphrase)
    except (vault.VaultMissing, vault.VaultUnreadable, OSError) as exc:
        reason = type(exc).__name__
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            first = db.kv_get(conn, user_id, NAMESPACE, "first_attempt_at")
            if first is None:
                db.kv_set(conn, user_id, NAMESPACE, "first_attempt_at", str(now))
                first = {"value": str(now)}
        if now - float(first["value"]) < RETRY_SECONDS:
            logger.warning("Credential migration for %s will retry: %s", vault._label(user_id), reason)
            return RetireResult(user_id, "retry", reason)
    except (vault.VaultLocked, vault.VaultCorrupt, vault.VaultPassphraseMissing,
            vault.VaultKeyUnusable, vault.VaultPathRefused, vault.VaultLibraryMissing,
            vault.VaultIsolationRequired) as exc:
        reason = type(exc).__name__

    imported = []
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if db.kv_get(conn, user_id, NAMESPACE, MARKER) is not None:
            return RetireResult(user_id, "already_retired")
        if preview is not None:
            selected = []
            for item in preview.items:
                if item.origin == "generated":
                    meta = db.kv_get(conn, user_id, "_generated_credentials", item.name)
                    if meta is not None and json.loads(meta["value"]).get("state") == "retired":
                        continue
                    if item.status != "new":
                        continue
                elif item.status not in ("new", "changed"):
                    continue
                selected.append(item.name)
            if selected:
                imported = kdbx_import.apply(config.db_path, user_id, data, passphrase,
                    selected=selected, expected_digest=preview.digest, actor="migration",
                    migration=True, connection=conn).imported
        conn.execute("UPDATE credential_bindings SET source='local' WHERE user_id=? AND source='vault'",
                     (user_id,))
        store.delete_secret(None, user_id, "vault", "passphrase", connection=conn, actor="migration")
        conn.execute("DELETE FROM istota_kv WHERE user_id=? AND namespace IN (?, ?, ?)",
                     (user_id, "_vault_sync", "_vault_file", "_generated_credentials"))
        outcome = "skipped" if reason else "ok"
        db.kv_set(conn, user_id, NAMESPACE, MARKER, json.dumps(
            {"at": now, "outcome": outcome, "reason": reason, "imported": imported}))
        audit.record(conn, user_id, action="migration", actor="migration",
                     detail={"outcome": outcome, "reason": reason, "imported": len(imported)})
        connected_service.resolve_for_service(conn, user_id, "vault", by="migration")
        failure_text = _FAILURE_TEXT.get(reason, _FAILURE_TEXT["OSError"])
        ending = (f"The final import could not run because {failure_text}. "
                  "You can import the file by hand."
                  if reason else f"The final import saved {len(imported)} credentials. No credentials were deleted.")
        notice = task_alert.write(conn, user_id, dedup_key="vault-sync-retired",
            title="Live KeePass sync has ended",
            body=(f"{filename} is now yours alone. Istota will not read or write it again. "
                  "Use Settings, Credentials to import a file or export a new copy. " + ending),
            severity="warning", actionable=False, params={"status": "vault_sync_retired"})
    del data, passphrase
    if notice is not None:
        try:
            deliver_pending(config, [notice])
        except Exception:
            logger.warning("Credential migration notice could not be delivered for %s", vault._label(user_id))
    logger.info("Credential migration for %s: %s (%s)", vault._label(user_id), outcome, reason)
    return RetireResult(user_id, outcome, reason, imported)


def retire_all(config) -> list[RetireResult]:
    results = []
    for user_id in pending_users(config):
        try:
            results.append(retire_user(config, user_id))
        except Exception as exc:
            reason = type(exc).__name__
            logger.warning("Credential migration for %s failed: %s", vault._label(user_id), reason)
            results.append(RetireResult(user_id, "retry", reason))
    return results
