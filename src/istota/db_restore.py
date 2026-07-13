"""Restore cold DB snapshots produced by ``db_backup`` back to local disk.

Counterpart to ``db_backup``: pick a dated snapshot (newest good copy by
default, or an explicit ``--date``), copy the cold file back over the live
local path, and let ``init_db`` re-flip it to WAL on the next service start —
exactly what ``db_relocate`` does. A row-count sanity check refuses to restore
an empty snapshot over a live path unless ``--force`` is given, so a restore
drill can't quietly wipe data with a bad backup.

Restore the whole set after a disaster::

    python -m istota.db_restore --all        # newest good snapshot of every DB

Or a single DB / point in time::

    python -m istota.db_restore --user alice --module location --date 2026-07-11
    python -m istota.db_restore --framework

Run with the services stopped (the live files are being overwritten).
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

from .db_backup import (
    MODULES,
    _dated_dirs,
    _data_row_count,
    backup_destination,
)

logger = logging.getLogger(__name__)


def list_snapshots(config) -> list[dict]:
    """Available dated snapshot dirs, newest first."""
    root = backup_destination(config)
    if root is None:
        return []
    return [{"date": d.name, "dir": str(d)} for d in _dated_dirs(root)]


def _newest_snapshot(root: Path, rel: str, date: str | None) -> tuple[str, Path] | None:
    """Resolve the snapshot of ``rel`` to restore: the exact ``date`` if given,
    else the newest dated dir that holds a non-suspect copy. Returns
    ``(date_str, path)`` or None."""
    if date is not None:
        candidate = root / date / rel
        return (date, candidate) if candidate.exists() else None
    for d in _dated_dirs(root):  # newest first
        candidate = d / rel
        if candidate.exists():
            return (d.name, candidate)
    return None


def _target(config, *, label: str | None, user: str | None, module: str | None):
    """Map a restore request to (label, rel_path, dest_path)."""
    if label == "framework" or (user is None and module is None):
        return "framework", "framework/istota.db", Path(config.db_path)
    if not (user and module):
        raise ValueError("restore needs either --framework or both --user and --module")
    dest = config.module_db_path(user, module)
    return f"{module}:{user}", f"{user}/{module}.db", Path(dest)


def restore_database(
    config,
    *,
    label: str | None = None,
    user: str | None = None,
    module: str | None = None,
    date: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Restore one DB from its cold snapshot. Returns a status dict:
    restored / would_restore / no_snapshot / refused_empty / error."""
    root = backup_destination(config)
    resolved_label, rel, dest = _target(config, label=label, user=user, module=module)
    result: dict = {"label": resolved_label, "dest": str(dest)}
    if root is None:
        return {**result, "status": "no_destination"}

    found = _newest_snapshot(root, rel, date)
    if found is None:
        return {**result, "status": "no_snapshot"}
    snap_date, src = found
    rows = _data_row_count(src)
    result = {**result, "date": snap_date, "src": str(src), "rows": rows}

    if rows <= 0 and not force:
        # An empty (or unreadable) snapshot over a live path is almost certainly
        # a mistake; make the operator opt in.
        return {**result, "status": "refused_empty"}

    if dry_run:
        return {**result, "status": "would_restore"}

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    except OSError as exc:  # noqa: BLE001
        logger.error("db_restore_failed label=%s err=%s", resolved_label, exc)
        return {**result, "status": "error", "error": str(exc)}
    logger.info(
        "db_restore restored label=%s date=%s rows=%d dest=%s",
        resolved_label, snap_date, rows, dest,
    )
    return {**result, "status": "restored"}


def restore_all(config, *, date: str | None = None, dry_run: bool = False, force: bool = False) -> list[dict]:
    """Restore the framework DB + every configured user's module DBs from their
    newest good snapshot (or the given ``date``). Labels with no snapshot are
    reported ``no_snapshot``, not errors."""
    results = [
        restore_database(config, label="framework", date=date, dry_run=dry_run, force=force)
    ]
    for user_id in config.users:
        for module in MODULES:
            results.append(
                restore_database(
                    config, user=user_id, module=module,
                    date=date, dry_run=dry_run, force=force,
                )
            )
    return results


def main() -> int:
    import argparse

    from istota.config import load_config

    parser = argparse.ArgumentParser(prog="istota.db_restore")
    parser.add_argument("--list", action="store_true", help="List available snapshot dates and exit.")
    parser.add_argument("--all", action="store_true", help="Restore framework + every user's module DBs.")
    parser.add_argument("--framework", action="store_true", help="Restore the framework DB.")
    parser.add_argument("--user", help="User id (with --module).")
    parser.add_argument("--module", help="Module name (with --user).")
    parser.add_argument("--date", help="Restore this dated snapshot (default: newest good).")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be restored without copying.")
    parser.add_argument("--force", action="store_true", help="Restore even an empty/unreadable snapshot.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config()

    if args.list:
        snaps = list_snapshots(config)
        if not snaps:
            print("no snapshots found", file=sys.stderr)
            return 1
        for s in snaps:
            print(s["date"])
        return 0

    if args.all:
        results = restore_all(config, date=args.date, dry_run=args.dry_run, force=args.force)
    else:
        results = [
            restore_database(
                config,
                label="framework" if args.framework else None,
                user=args.user,
                module=args.module,
                date=args.date,
                dry_run=args.dry_run,
                force=args.force,
            )
        ]

    failed = 0
    for r in results:
        status = r["status"]
        line = f"{r['label']}: {status}"
        if "date" in r:
            line += f" (date={r['date']}, rows={r.get('rows')})"
        if status in ("error", "refused_empty", "no_destination"):
            failed += 1
            print(line, file=sys.stderr)
        elif status == "no_snapshot" and not args.all:
            failed += 1
            print(line, file=sys.stderr)
        else:
            print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
