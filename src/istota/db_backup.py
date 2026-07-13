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
from pathlib import Path

logger = logging.getLogger(__name__)

MODULES = ("feeds", "health", "location", "money")


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
    logger.info("db_backup complete: %d snapshotted, root=%s", ok, root)
    return results
