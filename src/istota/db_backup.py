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

Each run writes to its own dated directory (``<root>/<YYYY-MM-DD>/…``) so the
history isn't a single overwritten slot: a corrupted or emptied live DB can't
destroy the last good copy on the next run. Retention keeps the N newest dated
dirs, but never prunes a dir that still holds the newest *good* copy of a DB.

A collapse guard reuses ``db_relocate._data_row_count``: if a fresh snapshot of
a DB that previously held data comes back empty (or unreadable), the fresh file
is quarantined as ``*.suspect`` and flagged ``status="suspect"`` rather than
treated as latest-good — the same empty-shadow signal that protects relocation
(ISSUE-156/159). The check is deliberately exact-zero (prior had data, now has
none): the framework ``tasks`` table legitimately shrinks under retention
cleanup, so a fractional-drop guard would false-positive there.

Restore is a plain file copy back to the local path (see ``db_restore``); then
``init_db`` re-flips it to WAL on next start.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import date as _date
from pathlib import Path

logger = logging.getLogger(__name__)

MODULES = ("feeds", "health", "location", "money")

# A dated snapshot directory, e.g. ``2026-07-12``.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Cold snapshots aggregate every user's raw DB in one place on a mount with
# allow_other; lock the tree down so local OS users / group can't read it.
_DIR_MODE = 0o700
_FILE_MODE = 0o600

# The last-run timestamp is persisted next to the framework DB (local disk) so
# the daily-backup clock survives scheduler restarts. Without this the in-memory
# clock reset to "now" on every boot, and a host that deploys more than once a
# day (auto-update cron) would defer the backup forever.
_LAST_RUN_FILENAME = ".db_backup_last_run"


def _data_row_count(path: Path) -> int:
    """User-data row count of a cold snapshot (delegates to db_relocate's helper
    so backup and relocation share one definition of 'empty shadow')."""
    from .db_relocate import _data_row_count as _count

    return _count(path)


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


def _date_str(today: str | None) -> str:
    if today:
        return today
    return _date.today().isoformat()


def _secure_mkdir(path: Path) -> None:
    """Create a directory (and parents) and lock it to 0700."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, _DIR_MODE)
    except OSError:  # some mounts ignore chmod — best-effort
        pass


def _dated_dirs(root: Path) -> list[Path]:
    """Existing ``<root>/<YYYY-MM-DD>`` dirs, newest first."""
    if not root.exists():
        return []
    dirs = [p for p in root.iterdir() if p.is_dir() and _DATE_RE.match(p.name)]
    return sorted(dirs, key=lambda p: p.name, reverse=True)


def _prior_good_snapshot(root: Path, date_str: str, rel: str) -> Path | None:
    """Newest non-suspect snapshot of ``rel`` in a dated dir strictly older than
    ``date_str``. ``rel`` is like ``framework/istota.db`` or ``alice/location.db``."""
    for d in _dated_dirs(root):
        if d.name >= date_str:
            continue
        candidate = d / rel
        if candidate.exists():
            return candidate
    return None


def _snapshot_one(src: Path, dest: Path, label: str) -> dict:
    result = {"label": label, "src": str(src), "dest": str(dest)}
    if not src.exists():
        return {**result, "status": "skip_missing"}
    _secure_mkdir(dest.parent)
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
    try:
        os.chmod(dest, _FILE_MODE)
    except OSError:
        pass
    return {**result, "status": "ok"}


def _apply_collapse_guard(root: Path, date_str: str, rel: str, result: dict) -> dict:
    """If ``rel`` previously held data but this fresh snapshot is empty/unreadable,
    quarantine the fresh file as ``*.suspect`` and mark the result suspect."""
    dest = Path(result["dest"])
    prior = _prior_good_snapshot(root, date_str, rel)
    if prior is None:
        return result  # first-ever snapshot — nothing to compare against
    prior_rows = _data_row_count(prior)
    new_rows = _data_row_count(dest)
    result = {**result, "prior_rows": prior_rows, "new_rows": new_rows}
    if prior_rows > 0 and new_rows <= 0:
        suspect = dest.with_name(dest.name + ".suspect")
        try:
            if suspect.exists():
                suspect.unlink()
            dest.rename(suspect)
        except OSError as exc:  # noqa: BLE001
            logger.error("db_backup_quarantine_failed label=%s err=%s", result["label"], exc)
        logger.error(
            "db_backup_suspect label=%s prior_rows=%d new_rows=%d — quarantined as .suspect",
            result["label"], prior_rows, new_rows,
        )
        result["status"] = "suspect"
    return result


def _good_db_relpaths(dated_dir: Path) -> set[str]:
    """Relative paths (POSIX) of non-suspect ``*.db`` snapshots under a dated dir."""
    out: set[str] = set()
    for p in dated_dir.rglob("*.db"):
        if p.is_file():
            out.add(p.relative_to(dated_dir).as_posix())
    return out


def _prune_old_snapshots(root: Path, keep: int) -> None:
    """Keep the ``keep`` newest dated dirs, plus any older dir that still holds
    the newest good copy of some DB (so a run of suspect snapshots can't age out
    the last good history)."""
    if keep <= 0:
        return
    dated = _dated_dirs(root)  # newest first
    if len(dated) <= keep:
        return

    # Walk newest -> oldest; a dir is "protected" if it contributes the newest
    # good copy of a relpath not yet seen in a newer dir.
    protected: set[Path] = set()
    seen: set[str] = set()
    for d in dated:
        contributes = False
        for rel in _good_db_relpaths(d):
            if rel not in seen:
                seen.add(rel)
                contributes = True
        if contributes:
            protected.add(d)

    for d in dated[keep:]:
        if d in protected:
            continue
        try:
            shutil.rmtree(d)
        except OSError as exc:  # noqa: BLE001
            logger.warning("db_backup_prune_failed dir=%s err=%s", d, exc)


def backup_databases(config, *, today: str | None = None) -> list[dict]:
    """Snapshot the framework DB + every user's module DBs into a dated dir.
    Never raises per DB — one failure doesn't abort the sweep. Returns per-DB
    status dicts (ok / skip_missing / suspect / error)."""
    if not getattr(config.scheduler, "db_backup_enabled", True):
        return []
    root = backup_destination(config)
    if root is None:
        logger.warning(
            "db_backup skipped: no db_backup_dir and no nextcloud_mount_path — "
            "local DBs have no durable backup target configured"
        )
        return []

    date_str = _date_str(today)
    dated_root = root / date_str
    # Lock down the aggregate tree (issue #4): the backup root and today's dated
    # dir to 0700 up front, then each per-DB subdir + file as it's written.
    _secure_mkdir(root)
    _secure_mkdir(dated_root)
    results: list[dict] = []

    def _run(src: Path, rel: str, label: str) -> None:
        dest = dated_root / rel
        try:
            res = _snapshot_one(src, dest, label)
            if res["status"] == "ok":
                res = _apply_collapse_guard(root, date_str, rel, res)
            results.append(res)
        except Exception as exc:  # noqa: BLE001
            logger.error("db_backup_failed label=%s err=%s", label, exc)
            results.append({"label": label, "status": "error", "error": str(exc)})

    if getattr(config, "db_path", None):
        _run(Path(config.db_path), "framework/istota.db", "framework")

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
            _run(src, f"{user_id}/{module}.db", f"{module}:{user_id}")

    _prune_old_snapshots(root, getattr(config.scheduler, "db_backup_retention", 7))

    ok = sum(1 for r in results if r["status"] == "ok")
    suspect = sum(1 for r in results if r["status"] == "suspect")
    errored = sum(1 for r in results if r["status"] == "error")
    # Persist the clock only after a real attempt (root resolved). A disabled or
    # no-destination run returns earlier without writing, so the daemon keeps
    # retrying instead of marking a phantom backup.
    _write_last_run(config, time.time())
    logger.info(
        "db_backup complete: %d snapshotted, %d suspect, %d error, root=%s",
        ok, suspect, errored, dated_root,
    )
    return results
