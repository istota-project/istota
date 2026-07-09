#!/usr/bin/env python3
"""Import Garmin watch GPS activity tracks into the per-user location.db.

Fills only the gaps where the phone-based Overland tracker has no native
pings — native always wins. See the spec
``garmin-track-import-to-location.md`` for the full design.

This is a **cron artifact**, not a sandboxed task: location.db is writable
in the scheduler/cron environment and read-only in the agent sandbox, so
the script must never run from inside a task sandbox. ``--dry-run`` is
read-only and safe anywhere.

Deployment: this canonical copy lives in the repo; deploy copies it to the
user's bot ``scripts/`` dir on the Nextcloud mount and wires the nightly
cron (see Stage 6 / the location docs).

Environment (all present for istota processes):
  ISTOTA_DB_PATH      framework istota.db (token secret + kv)
  ISTOTA_SECRET_KEY   Fernet key (used inside garmin.acquire_client)
  and a resolvable per-user location.db (via config or --workspace).

Usage:
  import_garmin_tracks.py --user stefan --days-back 7
  import_garmin_tracks.py --user stefan --days-back 30 --dry-run
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import json
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("import_garmin_tracks")


# ---------------------------------------------------------------------------
# Pure logic (importable + unit-tested without Garmin or a DB)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TrackPoint:
    timestamp: str          # UTC ISO '%Y-%m-%dT%H:%M:%SZ'
    lat: float
    lon: float
    altitude: float | None
    speed: float | None
    activity_type: str      # canonical parent: running / hiking / walking


# Garmin fine-grained typeKey -> canonical parent stored on the ping.
_SUBTYPE_TO_PARENT = {
    "running": "running",
    "trail_running": "running",
    "track_running": "running",
    "treadmill_running": "running",   # no GPS; filtered earlier, mapped anyway
    "hiking": "hiking",
    "walking": "walking",
    "casual_walking": "walking",
    "speed_walking": "walking",
    "cycling": "cycling",
    "road_biking": "cycling",
    "mountain_biking": "cycling",
    "gravel_cycling": "cycling",
}


def collapse_subtype(type_key: str) -> str:
    """Map a Garmin ``activityType.typeKey`` to the canonical parent stored
    on the ping (``trail_running`` -> ``running``). Unknown keys pass
    through unchanged so nothing is silently mislabelled."""
    return _SUBTYPE_TO_PARENT.get(type_key, type_key)


def parse_ts(value: Any) -> float:
    """Permissively parse a timestamp to a float epoch (seconds, UTC).

    Native (Overland) timestamps are heterogeneous: the webhook stores the
    raw string, which may carry a trailing ``Z``, an explicit offset, or —
    on the fallback path — microseconds and no ``Z`` at all. Garmin points
    are converted to ``...Z`` on parse. Never string-compare timestamps;
    always go through here so the shadow filter compares real instants.

    Accepts an ISO-8601 string or an epoch (int/float seconds). Raises
    ValueError on an unparseable value so a caller can skip that point.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"unparseable timestamp: {value!r}")
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = _dt.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        # Naive → assume UTC (Overland's fallback path emits offset-aware,
        # but a hand-seeded or legacy naive value should not blow up).
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()


def epoch_ms_to_iso_z(ms: Any) -> str:
    """Garmin polyline epoch-ms (GMT) → UTC ISO ``%Y-%m-%dT%H:%M:%SZ`` —
    the format native pings use. Raises ValueError on a bad value."""
    if not isinstance(ms, (int, float)):
        raise ValueError(f"bad epoch-ms: {ms!r}")
    dt = _dt.datetime.fromtimestamp(ms / 1000.0, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_polyline(details: dict, activity_type: str) -> list[TrackPoint]:
    """Extract normalized trackpoints from a ``get_activity_details``
    response. Empty / missing polyline → ``[]``. A point with a malformed
    timestamp or missing lat/lon is skipped, not fatal.

    Expected per-point keys (confirmed empirically in Stage 1's dry-run
    dump before this was hard-coded): ``lat``, ``lon``, ``altitude``,
    ``speed``, ``time`` (epoch ms, GMT).
    """
    parent = collapse_subtype(activity_type)
    dto = (details or {}).get("geoPolylineDTO") or {}
    raw = dto.get("polyline") or []
    out: list[TrackPoint] = []
    for pt in raw:
        if not isinstance(pt, dict):
            continue
        lat = pt.get("lat")
        lon = pt.get("lon")
        if lat is None or lon is None:
            continue
        try:
            ts = epoch_ms_to_iso_z(pt.get("time"))
        except ValueError:
            logger.debug("skipping polyline point with bad time: %r", pt.get("time"))
            continue
        out.append(TrackPoint(
            timestamp=ts,
            lat=float(lat),
            lon=float(lon),
            altitude=_opt_float(pt.get("altitude")),
            speed=_opt_float(pt.get("speed")),
            activity_type=parent,
        ))
    return out


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def downsample(points: list[TrackPoint], seconds: float) -> list[TrackPoint]:
    """Reduce to at most one point per ``seconds``, always keeping the first
    and last point of the activity. Input assumed time-sorted; a
    non-positive interval is a no-op (returns the input)."""
    if seconds <= 0 or len(points) <= 2:
        return list(points)
    kept: list[TrackPoint] = [points[0]]
    last_epoch = parse_ts(points[0].timestamp)
    for pt in points[1:-1]:
        e = parse_ts(pt.timestamp)
        if e - last_epoch >= seconds:
            kept.append(pt)
            last_epoch = e
    kept.append(points[-1])
    return kept


def filter_shadowed(
    points: list[TrackPoint],
    native: Iterable[tuple[float, float, float]],
    band_sec: float,
    radius_m: float,
) -> list[TrackPoint]:
    """Spatiotemporal native-coverage filter — the heart of the importer.

    A Garmin point is **shadowed** (dropped) iff some native point exists
    within ``band_sec`` seconds AND ``radius_m`` metres of it. Both
    conditions are required: a phone left at home keeps emitting *stationary*
    native pings for the whole activity window, so a temporal-only rule
    would wrongly shadow an entire watch-only run. The spatial gate means a
    native ping only shadows the position it actually recorded.

    ``native`` is an iterable of ``(epoch_seconds, lat, lon)``. The caller
    supplies native DB pings (source != 'garmin') PLUS this-run's
    already-inserted imports, so two overlapping activities can't
    double-insert. Two-pointer walk over time-sorted inputs; the haversine
    check runs only for native points inside the ``band_sec`` window.
    """
    from istota.geo import haversine

    nat = sorted(native, key=lambda n: n[0])
    if not nat:
        return list(points)

    # Decorate points with epoch, keep original order for output.
    decorated = []
    for pt in points:
        try:
            decorated.append((parse_ts(pt.timestamp), pt))
        except ValueError:
            logger.debug("skipping point with bad ts in filter: %r", pt.timestamp)
            continue
    decorated.sort(key=lambda d: d[0])

    kept: list[TrackPoint] = []
    lo = 0
    n = len(nat)
    for epoch, pt in decorated:
        # Advance the window's lower bound past native points too old.
        while lo < n and nat[lo][0] < epoch - band_sec:
            lo += 1
        shadowed = False
        j = lo
        while j < n and nat[j][0] <= epoch + band_sec:
            _, nlat, nlon = nat[j]
            if haversine(pt.lat, pt.lon, nlat, nlon) <= radius_m:
                shadowed = True
                break
            j += 1
        if not shadowed:
            kept.append(pt)
    return kept


# ---------------------------------------------------------------------------
# DB glue (needs a real location.db; tested against a temp file)
# ---------------------------------------------------------------------------


def load_native_points(
    conn, t0: str, t1: str, band_sec: float,
) -> list[tuple[float, float, float]]:
    """Native (source != 'garmin') pings in [t0 - band, t1 + band] as
    (epoch, lat, lon). Widen the SQL range generously and parse defensively
    — timestamps are string-heterogeneous, so a plain BETWEEN on strings is
    unsafe; we over-select and filter by parsed epoch."""
    lo_epoch = parse_ts(t0) - band_sec
    hi_epoch = parse_ts(t1) + band_sec
    rows = conn.execute(
        "SELECT timestamp, lat, lon FROM location_pings "
        "WHERE source != 'garmin'"
    ).fetchall()
    out: list[tuple[float, float, float]] = []
    for r in rows:
        try:
            e = parse_ts(r["timestamp"])
        except (ValueError, TypeError):
            continue
        if lo_epoch <= e <= hi_epoch:
            out.append((e, float(r["lat"]), float(r["lon"])))
    return out


def evict_activity_imports(conn, t0: str, t1: str) -> int:
    """Delete this window's prior Garmin imports so the run starts from a
    clean slate (discards imports a later native upload has since covered).
    Keyed on source='garmin' within the activity's time span; imports are
    the only source='garmin' rows."""
    lo_epoch = parse_ts(t0)
    hi_epoch = parse_ts(t1)
    rows = conn.execute(
        "SELECT id, timestamp FROM location_pings WHERE source = 'garmin'"
    ).fetchall()
    to_delete = []
    for r in rows:
        try:
            e = parse_ts(r["timestamp"])
        except (ValueError, TypeError):
            continue
        if lo_epoch <= e <= hi_epoch:
            to_delete.append(r["id"])
    for pid in to_delete:
        conn.execute("DELETE FROM location_pings WHERE id = ?", (pid,))
    return len(to_delete)


def insert_points(conn, points: list[TrackPoint]) -> int:
    """Insert surviving Garmin points as placeless (place_id=NULL) route
    breadcrumbs tagged source='garmin', with received_at set to the point's
    own historical timestamp (retention correctness)."""
    from istota.location import db as location_db

    for pt in points:
        location_db.insert_ping(
            conn, pt.timestamp, pt.lat, pt.lon,
            altitude=pt.altitude,
            speed=pt.speed,
            activity_type=pt.activity_type,
            place_id=None,
            source="garmin",
            received_at=pt.timestamp,
        )
    return len(points)


# ---------------------------------------------------------------------------
# Garmin fetch glue
# ---------------------------------------------------------------------------


def activity_span(activity: dict) -> tuple[str, str] | None:
    """(startISO, endISO) in UTC 'Z' form for an activity summary, or None
    if it lacks the fields. Uses startTimeGMT + duration."""
    start = activity.get("startTimeGMT")
    dur = activity.get("duration")
    if not start:
        return None
    # startTimeGMT is 'YYYY-MM-DD HH:MM:SS' (GMT, no tz marker).
    try:
        dt0 = _dt.datetime.strptime(start, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=_dt.timezone.utc,
        )
    except (ValueError, TypeError):
        return None
    dt1 = dt0 + _dt.timedelta(seconds=float(dur or 0))
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return dt0.strftime(fmt), dt1.strftime(fmt)


def activity_type_key(activity: dict) -> str:
    return ((activity.get("activityType") or {}).get("typeKey")) or ""


def has_gps(activity: dict) -> bool:
    if activity.get("hasPolyline") is True:
        return True
    # Fall back to presence of a start coordinate / distance.
    return bool(
        activity.get("startLatitude") is not None
        and activity.get("distance")
    )


def parent_types_for(type_keys: Iterable[str]) -> list[str]:
    """The parent activity types to actually query (get_activities_by_date
    accepts parent types only, not subtypes)."""
    return sorted({collapse_subtype(k) for k in type_keys})


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _daterange_local(days_back: int, today: _dt.date) -> tuple[str, str]:
    """Requested [start, end] local dates, widened one day each side so an
    activity whose local date differs from its UTC date isn't missed."""
    start = today - _dt.timedelta(days=days_back + 1)
    end = today + _dt.timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _resolve_location_db(user_id: str, workspace: str | None, framework_db: Path):
    """Return an initialised per-user location.db path.

    As a cron artifact the script can't rely on its working directory, so
    when resolving through config we pin ``config.db_path`` to the
    known-absolute framework DB (``ISTOTA_DB_PATH``). Otherwise
    ``load_config`` may return a *relative* ``db_path`` and the module-
    enabled check in ``resolve_for_user`` stats it against the wrong CWD.
    """
    from istota.location import db as location_db
    if workspace:
        from istota.location.workspace import synthesize_location_context
        ctx = synthesize_location_context(user_id, Path(workspace))
    else:
        from istota.config import load_config
        from istota.location._loader import resolve_for_user
        config = load_config()
        config.db_path = framework_db
        ctx = resolve_for_user(user_id, config)
    location_db.init_db(ctx.db_path)
    return ctx.db_path


def run(args: argparse.Namespace) -> int:
    from istota.health import garmin as gm
    from istota.location import db as location_db

    framework_db = Path(os.environ.get("ISTOTA_DB_PATH", ""))
    if not framework_db:
        logger.error("ISTOTA_DB_PATH not set")
        return 2

    try:
        adapter = gm.acquire_client(
            framework_db, args.user, persist_rotation=not args.dry_run,
        )
    except gm.GarminAuthError as exc:
        logger.error("Garmin auth failed: %s — reconnect via Settings → "
                     "Connected services", exc)
        return 2

    type_keys = [t.strip() for t in args.activity_types.split(",") if t.strip()]
    allow = set(type_keys)
    today = _dt.date.today()
    start_d, end_d = _daterange_local(args.days_back, today)

    # List activities per parent type, filter to the fine-grained allowlist.
    activities: list[dict] = []
    seen_ids: set = set()
    for parent in parent_types_for(type_keys):
        try:
            listed = adapter.get_activities_by_date(start_d, end_d, parent) or []
        except gm.GarminRateLimited:
            logger.error("Garmin rate-limited while listing %s — aborting", parent)
            return 3
        for act in listed:
            aid = act.get("activityId")
            if aid in seen_ids:
                continue
            if activity_type_key(act) not in allow:
                continue
            seen_ids.add(aid)
            activities.append(act)

    activities.sort(key=lambda a: a.get("startTimeGMT") or "")

    db_path = _resolve_location_db(args.user, args.workspace, framework_db)
    report: list[dict] = []

    if args.dry_run:
        # Read-only: no lock, no writes.
        with location_db.connect(db_path) as conn:
            run_occupancy: list[tuple[float, float, float]] = []
            for act in activities:
                row = _plan_activity(adapter, act, conn, args, run_occupancy)
                if row:
                    report.append(row)
        _print_report(report, dry_run=True)
        return 0

    total_inserted = 0
    with _user_lock(args.user):
        run_occupancy = []
        for act in activities:
            with location_db.connect(db_path) as conn:
                row = _import_one(adapter, act, conn, args, run_occupancy)
                conn.commit()
                if row:
                    report.append(row)
                    total_inserted += row["inserted"]
    _record_telemetry(framework_db, args.user, total_inserted, len(report))
    _print_report(report, dry_run=False)
    return 0


def _fetch_points(adapter, act, args):
    from istota.health import garmin as gm
    if not has_gps(act):
        return None, None
    span = activity_span(act)
    if span is None:
        return None, None
    aid = act.get("activityId")
    try:
        details = adapter.get_activity_details(str(aid), maxpoly=args.maxpoly)
    except gm.GarminRateLimited:
        raise
    if not details:
        return None, span
    pts = parse_polyline(details, activity_type_key(act))
    if not pts:
        return None, span  # hasPolyline true but empty → no-GPS
    pts = downsample(pts, args.downsample)
    return pts, span


def _plan_activity(adapter, act, conn, args, run_occupancy):
    """Dry-run: compute what would be inserted, no writes."""
    pts, span = _fetch_points(adapter, act, args)
    if not pts or span is None:
        return None
    t0, t1 = span
    native = load_native_points(conn, t0, t1, args.guard_band)
    kept = filter_shadowed(
        pts, list(native) + run_occupancy, args.guard_band, args.guard_radius,
    )
    run_occupancy.extend((parse_ts(p.timestamp), p.lat, p.lon) for p in kept)
    return _report_row(act, span, len(pts), len(kept))


def _import_one(adapter, act, conn, args, run_occupancy):
    pts, span = _fetch_points(adapter, act, args)
    if not pts or span is None:
        return None
    t0, t1 = span
    evict_activity_imports(conn, t0, t1)
    native = load_native_points(conn, t0, t1, args.guard_band)
    kept = filter_shadowed(
        pts, list(native) + run_occupancy, args.guard_band, args.guard_radius,
    )
    insert_points(conn, kept)
    run_occupancy.extend((parse_ts(p.timestamp), p.lat, p.lon) for p in kept)
    return _report_row(act, span, len(pts), len(kept))


def _report_row(act, span, fetched, kept):
    return {
        "activity_id": act.get("activityId"),
        "type": activity_type_key(act),
        "start": span[0],
        "distance_m": act.get("distance"),
        "fetched": fetched,
        "inserted": kept,
        "shadowed": fetched - kept,
    }


def _print_report(report, *, dry_run):
    label = "DRY-RUN (no writes)" if dry_run else "imported"
    if not report:
        print(f"garmin track import [{label}]: no GPS activities in window")
        return
    print(f"garmin track import [{label}]:")
    for r in report:
        print(
            f"  {r['start']}  {r['type']:<14} "
            f"dist={r['distance_m'] or 0:.0f}m  "
            f"fetched={r['fetched']:<5} shadowed={r['shadowed']:<5} "
            f"insert={r['inserted']}"
        )


def _record_telemetry(framework_db, user_id, inserted, activities):
    try:
        import sqlite3

        from istota import db as framework
        conn = sqlite3.connect(framework_db)
        conn.row_factory = sqlite3.Row
        try:
            framework.kv_set(
                conn, user_id, "garmin_import", "last_run",
                json.dumps({
                    "at": _dt.datetime.now(_dt.timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                    ),
                    "inserted": inserted,
                    "activities": activities,
                }),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — telemetry must never fail a run
        logger.debug("garmin_import telemetry write failed: %s", exc)


@contextmanager
def _user_lock(user_id: str):
    """Host-local exclusive lock so a manual deep backfill and the nightly
    cron can't interleave evict/insert on the same activity."""
    import fcntl
    import re
    import tempfile

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", user_id) or "_"
    lock_path = Path(tempfile.gettempdir()) / f"istota-garmin-import-{safe}.lock"
    with open(lock_path, "w") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


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
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
