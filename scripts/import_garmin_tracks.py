#!/usr/bin/env python3
"""CLI wrapper for the Garmin GPS track importer.

The importer logic lives in ``istota.location.garmin_import`` (shared with
the web "Import GPS tracks" button and the location skill). This is the
cron / operator-shell entry point.

Runs where ``location.db`` is writable (scheduler/cron env), never inside a
task sandbox (read-only DB). ``--dry-run`` is read-only. Needs
``ISTOTA_DB_PATH`` (framework DB; also resolves the per-user location.db, so
the script is working-directory independent) and ``ISTOTA_SECRET_KEY``
(to decrypt the Garmin token blob).

Usage:
  import_garmin_tracks.py --user stefan --days-back 7
  import_garmin_tracks.py --user stefan --days-back 30 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from istota.location.garmin_import import ImportOptions, ImportResult, import_tracks

logger = logging.getLogger("import_garmin_tracks")


def _print_report(result: ImportResult) -> None:
    label = "DRY-RUN (no writes)" if result.dry_run else "imported"
    if not result.details:
        print(f"garmin track import [{label}]: no GPS activities in window")
        return
    print(f"garmin track import [{label}]:")
    for r in result.details:
        print(
            f"  {r['start']}  {r['type']:<14} "
            f"dist={r['distance_m'] or 0:.0f}m  "
            f"fetched={r['fetched']:<5} shadowed={r['shadowed']:<5} "
            f"insert={r['inserted']}"
        )
    if not result.dry_run:
        print(f"total inserted: {result.inserted_total}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user", default="stefan")
    p.add_argument("--days-back", type=int, default=7)
    p.add_argument("--guard-band", type=float, default=300.0,
                   help="temporal shadow band, seconds")
    p.add_argument("--guard-radius", type=float, default=150.0,
                   help="spatial shadow band, metres")
    p.add_argument("--downsample", type=float, default=10.0,
                   help="min seconds between kept points")
    p.add_argument("--maxpoly", type=int, default=4000)
    p.add_argument("--activity-types",
                   default="running,trail_running,hiking,walking")
    p.add_argument("--workspace", default=None,
                   help="workspace root (else resolved from config)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    from istota.health import garmin as gm

    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    framework_db = os.environ.get("ISTOTA_DB_PATH", "")
    if not framework_db:
        logger.error("ISTOTA_DB_PATH not set")
        return 2

    options = ImportOptions(
        days_back=args.days_back,
        guard_band=args.guard_band,
        guard_radius=args.guard_radius,
        downsample_sec=args.downsample,
        maxpoly=args.maxpoly,
        activity_types=args.activity_types,
        dry_run=args.dry_run,
        workspace=args.workspace,
    )
    try:
        result = import_tracks(
            args.user, framework_db_path=Path(framework_db), options=options,
        )
    except gm.GarminAuthError as exc:
        logger.error("Garmin auth failed: %s — reconnect via Settings → "
                     "Connected services", exc)
        return 2
    except gm.GarminRateLimited:
        logger.error("Garmin rate-limited — aborting; retry later")
        return 3
    _print_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
