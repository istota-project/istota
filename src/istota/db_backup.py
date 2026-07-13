"""Snapshot local SQLite DBs to durable (off-host) storage.

The framework ``istota.db`` and the per-user module DBs (feeds / health /
location / money) now live on local disk (``Config.module_db_path``) so they
can run WAL without SIGBUSing on the FUSE mount. That took them out of the
Nextcloud-synced workspaces, so this restores off-host durability: on a timer,
snapshot each live DB to a backup directory on the Nextcloud mount.

The snapshot uses SQLite's online backup API (``Connection.backup``), which
produces a consistent copy of a *live* WAL DB without stopping writers and
without copying the ``-wal`` / ``-shm`` sidecars. The destination is a plain
rollback-journal DB file — safe to sit on the FUSE mount because nothing ever
opens it in WAL there; it's cold storage. A ``wal_checkpoint(TRUNCATE)`` runs
first to keep the source WAL small.

Restore is a plain file copy back to the local path (then ``init_db`` re-flips
it to WAL on next start).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MODULES = ("feeds", "health", "location", "money")

# The last-run timestamp is persisted next to the framework DB (local disk) so
# the daily-backup clock survives scheduler restarts. Without this the in-memory
# clock reset to "now" on every boot, and a host that deploys more than once a
# day (auto-update cron) would defer the backup forever.
_LAST_RUN_FILENAME = ".db_backup_last_run"


def _last_run_path(config) -> Path | None:
    db_path = getattr(config, "db_path", None)
    if not db_path:
        return None
    return Path(db_path).parent / _LAST_RUN_FILENAME


def last_backup_time(config) -> float:
    """Epoch seconds of the last successful backup attempt, or 0.0 if never /
    unreadable. The scheduler seeds its in-memory clock from this at boot, so an
    overdue backup fires promptly and a recent one is not repeated."""
    path = _last_run_path(config)
    if path is None or not path.exists():
        return 0.0
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return 0.0


def _write_last_run(config, ts: float) -> None:
    path = _last_run_path(config)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(ts))
    except OSError as exc:  # best-effort — a write failure just risks an extra backup
        logger.warning("db_backup_last_run_write_failed path=%s err=%s", path, exc)


def backup_destination(config) -> Path | None:
    """Resolve the backup root, or None if backups can't be placed durably."""
    explicit = (getattr(config.scheduler, "db_backup_dir", "") or "").strip()
    if explicit:
        return Path(explicit)
    mount = getattr(config, "nextcloud_mount_path", None)
    if mount:
        return Path(mount) / "istota-db-backups"
    return None


def _snapshot_one(src: Path, dest: Path, label: str) -> dict:
    result = {"label": label, "src": str(src), "dest": str(dest)}
    if not src.exists():
        return {**result, "status": "skip_missing"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(src, timeout=30.0)
    dst_conn = sqlite3.connect(dest)
    try:
        # Shrink the WAL first so the snapshot is cheap; ignore failure (a
        # checkpoint can be blocked by a long reader — the backup still works).
        try:
            src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        src_conn.backup(dst_conn)
        # The backup API copies the source header verbatim, so the cold copy
        # inherits WAL journal_mode. Force it to DELETE: a WAL-headed file would
        # try to create a -shm on the FUSE mount if ever opened in place, which
        # SIGBUSes (the very reason the live DBs moved off the mount). The cold
        # copy is single-file rollback-journal; restore copies it back and
        # init_db re-flips to WAL locally.
        dst_conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst_conn.close()
        src_conn.close()
    return {**result, "status": "ok"}


def backup_databases(config) -> list[dict]:
    """Snapshot the framework DB + every user's module DBs. Never raises per
    DB — one failure doesn't abort the sweep. Returns per-DB status dicts."""
    if not getattr(config.scheduler, "db_backup_enabled", True):
        return []
    root = backup_destination(config)
    if root is None:
        logger.warning(
            "db_backup skipped: no db_backup_dir and no nextcloud_mount_path — "
            "local DBs have no durable backup target configured"
        )
        return []

    results: list[dict] = []

    def _run(src: Path, dest: Path, label: str) -> None:
        try:
            results.append(_snapshot_one(src, dest, label))
        except Exception as exc:  # noqa: BLE001
            logger.error("db_backup_failed label=%s err=%s", label, exc)
            results.append({"label": label, "status": "error", "error": str(exc)})

    if getattr(config, "db_path", None):
        _run(Path(config.db_path), root / "framework" / "istota.db", "framework")

    for user_id in config.users:
        for module in MODULES:
            try:
                src = config.module_db_path(user_id, module)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "db_backup_path_failed user=%s module=%s err=%s",
                    user_id, module, exc,
                )
                continue
            _run(src, root / user_id / f"{module}.db", f"{module}:{user_id}")

    ok = sum(1 for r in results if r["status"] == "ok")
    # Persist the clock only after a real attempt (root resolved). A disabled or
    # no-destination run returns earlier without writing, so the daemon keeps
    # retrying instead of marking a phantom backup.
    _write_last_run(config, time.time())
    logger.info("db_backup complete: %d snapshotted, root=%s", ok, root)
    return results
