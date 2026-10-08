"""Dated credential backups encrypted to a user's age public key."""
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
import importlib.util
import io
import json
import logging
import os
import re
import secrets
import stat
import tarfile

from istota import db
from istota.credentials import audit, kdbx_export, vault
from istota.notifications.resolvers import task_alert
from istota.notifications.store import resolve_notification
from istota.skills._loader import open_overlay_dir

logger = logging.getLogger(__name__)
NAMESPACE = "_credential_backup"


class BackupRecipientError(ValueError):
    pass


@dataclass(frozen=True)
class BackupResult:
    user_id: str
    outcome: str
    reason: str | None = None
    file: str | None = None


def available() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("pyrage", "pykeepass"))


def parse_recipient(text: str) -> str:
    try:
        from pyrage import RecipientError
        from pyrage.x25519 import Recipient
    except ImportError:
        raise vault.VaultLibraryMissing("Install the vault extra") from None
    try:
        return str(Recipient.from_str(text.strip()))
    except (RecipientError, ValueError, TypeError, AttributeError):
        raise BackupRecipientError("backup_recipient_unsupported") from None


def recipient_for(conn, user_id) -> str | None:
    row = db.kv_get(conn, user_id, NAMESPACE, "recipient")
    return row["value"] if row else None


def last_run(conn, user_id) -> dict | None:
    row = db.kv_get(conn, user_id, NAMESPACE, "last_run")
    if not row:
        return None
    try:
        result = json.loads(row["value"])
        return result if isinstance(result, dict) else None
    except ValueError:
        return None


def last_run_time(config) -> float:
    if not config.db_path.exists():
        return 0.0
    with db.get_db(config.db_path) as conn:
        stamps = [(last_run(conn, uid) or {}).get("at", "") for uid in config.users]
    result = 0.0
    for stamp in stamps:
        try:
            result = max(result, datetime.fromisoformat(stamp).timestamp())
        except (ValueError, TypeError):
            pass
    return result


def set_recipient(conn, user_id, recipient: str | None, *, actor: str) -> None:
    recipient = parse_recipient(recipient) if recipient is not None else None
    if recipient == recipient_for(conn, user_id):
        return
    if recipient is None:
        db.kv_delete(conn, user_id, NAMESPACE, "recipient")
        action, detail = "backup_recipient_cleared", None
        body = "Scheduled credential backups have been turned off."
    else:
        db.kv_set(conn, user_id, NAMESPACE, "recipient", recipient)
        action, detail = "backup_recipient_set", {"recipient_suffix": recipient[-8:]}
        body = f"Credential backups now go to a new key ending {recipient[-8:]}."
    audit.record(conn, user_id, action=action, actor=actor, detail=detail)
    audit_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    task_alert.write(conn, user_id, dedup_key=f"credential-backup-recipient:{audit_id}",
                     title="Credential backup key changed", body=body)


def _destination(config, user_id) -> int:
    root = config.workspace_root(user_id)
    if root is None:
        raise OSError("destination_unavailable")
    fd = open_overlay_dir(root)
    if fd is None:
        raise OSError("destination_unavailable")
    try:
        for part in (config.bot_dir_name, "exports", "credential-backups"):
            if not part or part in (".", "..") or "/" in part or "\0" in part:
                raise OSError("destination_unavailable")
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_backup(config, user_id, filename, ciphertext):
    fd = _destination(config, user_id)
    temporary = filename + ".tmp"
    created = False
    try:
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        created = True
        with os.fdopen(out, "wb") as stream:
            stream.write(ciphertext)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, filename, src_dir_fd=fd, dst_dir_fd=fd)
        created = False
        files = []
        for name in os.listdir(fd):
            if re.fullmatch(r"istota-credentials-\d{4}-\d{2}-\d{2}\.tar\.age", name):
                if stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                    files.append(name)
        for name in sorted(files, reverse=True)[max(1, config.security.credential_backup_retention):]:
            os.unlink(name, dir_fd=fd)
        os.fsync(fd)
    finally:
        if created:
            os.unlink(temporary, dir_fd=fd)
        os.close(fd)


def _build(config, user_id, recipient, today):
    import pyrage
    password = secrets.token_urlsafe(32)
    data, summary = kdbx_export.build_kdbx(config.db_path, user_id, password=password,
                                          keyfile=None, options=kdbx_export.BACKUP)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, content in (("credentials.kdbx", data), ("PASSWORD.txt", (password + "\n").encode())):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(content), 0o600
            tar.addfile(info, io.BytesIO(content))
    encrypted = pyrage.encrypt(archive.getvalue(), [pyrage.x25519.Recipient.from_str(recipient)])
    filename = f"istota-credentials-{today.isoformat()}.tar.age"
    _write_backup(config, user_id, filename, encrypted)
    return filename, summary


def run_backup(config, user_id, *, today: date | None = None) -> BackupResult:
    with db.get_db(config.db_path) as conn:
        recipient = recipient_for(conn, user_id)
    if not recipient:
        return BackupResult(user_id, "skipped", "recipient_missing")
    summary = None
    acquired = kdbx_export.EXPORT_SLOT.acquire(timeout=30)
    try:
        if not acquired:
            result = BackupResult(user_id, "error", "export_busy")
        else:
            filename, summary = _build(config, user_id, parse_recipient(recipient), today or datetime.now(timezone.utc).date())
            result = BackupResult(user_id, "ok", file=filename)
    except ValueError as exc:
        result = BackupResult(user_id, "empty" if str(exc) == "export_empty" else "error",
                              "export_empty" if str(exc) == "export_empty" else "export_refused")
    except OSError:
        result = BackupResult(user_id, "error", "destination_unavailable")
    except (ImportError, vault.VaultLibraryMissing):
        result = BackupResult(user_id, "error", "library_missing")
    except Exception:
        result = BackupResult(user_id, "error", "export_failed")
    finally:
        if acquired:
            kdbx_export.EXPORT_SLOT.release()
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = last_run(conn, user_id) or {}
        failures = int(previous.get("failures", 0)) + 1 if result.outcome == "error" else 0
        db.kv_set(conn, user_id, NAMESPACE, "last_run", json.dumps({
            **asdict(result), "at": datetime.now(timezone.utc).isoformat(), "failures": failures,
        }))
        if summary:
            audit.record(conn, user_id, action="backup_export", actor="system",
                         detail={"credentials": summary.credentials, "file": result.file})
        if failures == 3:
            task_alert.write(conn, user_id, dedup_key="credential-backup-failing",
                             title="Credential backups are failing", body="Check the backup status in Settings → Credentials.")
        elif result.outcome in ("ok", "empty"):
            resolve_notification(conn, user_id, task_alert.SOURCE, "credential-backup-failing", by="producer")
    if result.outcome == "error":
        logger.warning("credential backup user=%s reason=%s", user_id, result.reason)
    return result


def run_all(config) -> list[BackupResult]:
    results = []
    for user_id in config.users:
        try:
            with db.get_db(config.db_path) as conn:
                if not recipient_for(conn, user_id):
                    continue
            results.append(run_backup(config, user_id))
        except Exception:
            logger.warning("credential backup user=%s reason=state_unavailable", user_id)
            results.append(BackupResult(user_id, "error", "state_unavailable"))
    return results
