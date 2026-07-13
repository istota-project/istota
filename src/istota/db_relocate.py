"""One-time relocation of per-user module DBs from the Nextcloud mount to
local disk.

Historically each module's SQLite DB lived inside the user's workspace on the
rclone FUSE mount (``{workspace}/{module}/data/{module}.db``), forced onto
``journal_mode=DELETE`` because WAL's mmap'd ``-shm`` SIGBUSes on that mount
(ISSUE-157). DELETE mode gives no reader/writer concurrency, so a per-minute
reader landing mid-contention could serialise the whole dispatch loop
(ISSUE-156).

Module DBs now live on local disk at ``Config.module_db_path(user, module)``
and run WAL. This migrator copies each existing on-mount DB to its new local
path, flips the journal to WAL via the module's own ``init_db`` (which also
re-asserts the schema), verifies it with ``quick_check``, and archives the old
file as ``<name>.migrated-<stamp>`` so a re-run is a no-op and the data stays
recoverable.

**Run with the scheduler / web / webhook services stopped.** Otherwise a live
process may create a fresh empty DB at the new local path first, and this
migrator would then skip (seeing the destination already present) and strand
the real data on the mount.

Idempotent: a destination that already exists is left untouched.

    python -m istota.db_relocate            # migrate all users x modules
    python -m istota.db_relocate --dry-run  # report what would move
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from .db_health import check_and_repair

logger = logging.getLogger(__name__)

MODULES = ("feeds", "health", "location", "money")


def _init_funcs() -> dict:
    """Lazy map module -> init_db (avoids importing heavy module deps unless run)."""
    from istota.feeds.db import init_db as feeds_init
    from istota.health.db import init_db as health_init
    from istota.location.db import init_db as location_init
    from istota.money.db import init_db as money_init

    return {
        "feeds": feeds_init,
        "health": health_init,
        "location": location_init,
        "money": money_init,
    }


def legacy_db_path(config, user_id: str, module: str) -> Path | None:
    """The old on-mount path for a module DB, or None if no mount is configured."""
    mount = getattr(config, "nextcloud_mount_path", None)
    if not mount:
        return None
    from istota.storage import get_user_bot_path

    user_root = Path(mount) / get_user_bot_path(
        user_id, config.bot_dir_name,
    ).lstrip("/")
    return user_root / module / "data" / f"{module}.db"


# schema/version bookkeeping tables carry rows even in a freshly-created DB, so
# they must not count toward "does this DB hold real user data".
_META_TABLES = {"schema_meta", "schema_migrations", "sqlite_sequence"}


def _rename_aside(path: Path, suffix: str) -> None:
    """Rename a file (and any WAL sidecars) out of the way with a `.suffix`."""
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if p.exists():
            p.rename(p.with_name(p.name + f".{suffix}"))


def _data_row_count(path: Path) -> int:
    """Total user-data rows across all tables, excluding schema/meta bookkeeping.

    0 means "schema only, no real data" — the signature of a DB a prematurely
    started service auto-created before relocation ran. Non-zero means the DB
    holds data we must not clobber.
    """
    conn = sqlite3.connect(path)
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        total = 0
        for t in tables:
            if t in _META_TABLES:
                continue
            total += conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
        return total
    except sqlite3.DatabaseError:
        # Unreadable/corrupt destination — treat as "has data" so we don't
        # clobber it; the operator gets a WARN via the skip path.
        return -1
    finally:
        conn.close()


def _perform_migration(old_path: Path, new_path: Path, module: str, user_id: str) -> dict:
    """Copy old→new, flip to WAL, quick_check, archive old. Returns a status dict."""
    result = {"user": user_id, "module": module, "new": str(new_path), "old": str(old_path)}
    new_path.parent.mkdir(parents=True, exist_ok=True)
    # DELETE-mode DB is a single file (no live -wal/-shm with services stopped),
    # but copy any sidecars defensively before flipping the journal.
    shutil.copy2(old_path, new_path)
    for suffix in ("-wal", "-shm"):
        side = old_path.with_name(old_path.name + suffix)
        if side.exists():
            shutil.copy2(side, new_path.with_name(new_path.name + suffix))

    # init_db flips journal_mode to WAL and re-asserts the schema/migrations.
    _init_funcs()[module](new_path)

    report = check_and_repair(new_path, label=f"{module}:{user_id}")
    if not report.ok:
        logger.error(
            "db_relocate_check_failed user=%s module=%s issues=%s",
            user_id, module, "; ".join(report.issues_after),
        )
        return {**result, "status": "check_failed", "issues": report.issues_after}

    stamp = time.strftime("%Y%m%d-%H%M%S")
    _rename_aside(old_path, f"migrated-{stamp}")
    logger.info(
        "db_relocated user=%s module=%s -> %s (old archived .migrated-%s)",
        user_id, module, new_path, stamp,
    )
    return {**result, "status": "migrated", "archived_stamp": stamp}


def relocate_module(config, user_id: str, module: str, *, dry_run: bool = False) -> dict:
    """Relocate one user's one module DB. Returns a status dict.

    status ∈ {skip_exists, no_source, migrated, migrated_over_empty,
    would_migrate, would_migrate_over_empty, skip_dest_has_data, check_failed}.

    The destination-exists case is the one that bit ISSUE-156's rollout: a
    service restarted with the new code (module_data_dir set) *before* this
    migrator ran, auto-creating an empty DB at the destination. Skipping on
    mere existence stranded the real data on the mount. So when the destination
    exists we compare data-row counts: an *empty* destination is backed up and
    replaced from a non-empty source; a destination that already holds data is
    left alone with a loud WARN (never silently clobbered).
    """
    new_path = config.module_db_path(user_id, module)
    old_path = legacy_db_path(config, user_id, module)
    result = {"user": user_id, "module": module, "new": str(new_path)}
    if old_path is not None:
        result["old"] = str(old_path)

    have_source = old_path is not None and old_path.exists()

    if new_path.exists():
        if not have_source:
            return {**result, "status": "skip_exists"}
        src_rows = _data_row_count(old_path)
        dst_rows = _data_row_count(new_path)
        if dst_rows == 0 and src_rows > 0:
            # Empty destination (premature service start) shadowing real source.
            if dry_run:
                return {**result, "status": "would_migrate_over_empty",
                        "src_rows": src_rows, "dst_rows": dst_rows}
            stamp = time.strftime("%Y%m%d-%H%M%S")
            _rename_aside(new_path, f"premature-{stamp}")
            logger.warning(
                "db_relocate_over_empty user=%s module=%s: destination was an "
                "empty auto-created DB (0 rows), source has %d rows; backed up "
                "destination as .premature-%s and migrating",
                user_id, module, src_rows, stamp,
            )
            migrated = _perform_migration(old_path, new_path, module, user_id)
            if migrated["status"] == "migrated":
                migrated["status"] = "migrated_over_empty"
            migrated["replaced_empty"] = True
            return migrated
        if dst_rows != 0:
            logger.warning(
                "db_relocate_skip_dest_has_data user=%s module=%s: destination "
                "already holds %d rows (src=%d); not overwriting",
                user_id, module, dst_rows, src_rows,
            )
            return {**result, "status": "skip_dest_has_data",
                    "src_rows": src_rows, "dst_rows": dst_rows}
        return {**result, "status": "skip_exists"}

    if not have_source:
        return {**result, "status": "no_source", "old": str(old_path) if old_path else None}

    if dry_run:
        return {**result, "status": "would_migrate"}

    return _perform_migration(old_path, new_path, module, user_id)


def relocate_all(config, *, dry_run: bool = False) -> list[dict]:
    """Relocate every configured user's module DBs. Never raises per-DB —
    one failure doesn't stop the sweep."""
    results: list[dict] = []
    for user_id in config.users:
        for module in MODULES:
            try:
                results.append(relocate_module(config, user_id, module, dry_run=dry_run))
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "db_relocate_failed user=%s module=%s err=%s",
                    user_id, module, exc,
                )
                results.append(
                    {"user": user_id, "module": module, "status": "error", "error": str(exc)}
                )
    return results


def main() -> int:
    import argparse

    from istota.config import load_config

    parser = argparse.ArgumentParser(prog="istota.db_relocate")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would move without copying anything.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config()

    results = relocate_all(config, dry_run=args.dry_run)
    _MOVED = {"migrated", "migrated_over_empty", "would_migrate", "would_migrate_over_empty"}
    moved = failed = warned = 0
    for r in results:
        status = r["status"]
        if status in _MOVED:
            moved += 1
            print(f"{r['user']}/{r['module']}: {status} -> {r['new']}")
        elif status == "skip_dest_has_data":
            warned += 1
            print(
                f"{r['user']}/{r['module']}: skip_dest_has_data "
                f"(dst={r.get('dst_rows')} rows already present, src={r.get('src_rows')})",
                file=sys.stderr,
            )
        elif status in ("check_failed", "error"):
            failed += 1
            print(f"{r['user']}/{r['module']}: {status}", file=sys.stderr)
    print(f"done: {moved} relocated, {warned} skipped-with-data, {failed} failed", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
