"""Garmin GPS track import into the per-user location.db.

Pulls GPS tracks for watch-recorded running / hiking / walking activities
and inserts them into ``location_pings`` as ``source='garmin'`` pings,
**only where the phone-based Overland tracker has no native coverage** —
native always wins. See the spec ``garmin-track-import-to-location.md``.

This module is the shared core: the standalone cron script
(``scripts/import_garmin_tracks.py``) and the web "Import GPS tracks"
button (``POST /api/garmin/import-tracks``) both call :func:`import_tracks`.
It must run where ``location.db`` is writable (scheduler/web/cron), never
inside a task sandbox (read-only DB). Dry-run is read-only.

The pure logic (``parse_ts`` / ``filter_shadowed`` / ``downsample`` /
``parse_polyline``) is unit-tested without Garmin or a DB.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Options / result
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ImportOptions:
    days_back: int = 7
    guard_band: float = 300.0            # temporal shadow band, seconds
    guard_radius: float = 150.0          # spatial shadow band, metres
    downsample_sec: float = 10.0         # min seconds between kept points
    maxpoly: int = 4000
    activity_types: str = "running,trail_running,hiking,walking"
    dry_run: bool = False
    workspace: str | None = None         # override config-based resolution


@dataclasses.dataclass
class ImportResult:
    dry_run: bool
    inserted_total: int
    activities: int                      # activities with a GPS track in window
    details: list[dict]                  # per-activity report rows

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "inserted": self.inserted_total,
            "activities": self.activities,
            "details": self.details,
        }


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

    Per-point keys (confirmed against the live API): ``lat``, ``lon``,
    ``altitude``, ``speed``, ``time`` (epoch ms, GMT).
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
    (epoch, lat, lon). Over-select and filter by parsed epoch — timestamps
    are string-heterogeneous, so a plain BETWEEN on strings is unsafe."""
    lo_epoch = parse_ts(t0) - band_sec
    hi_epoch = parse_ts(t1) + band_sec
    rows = conn.execute(
        "SELECT timestamp, lat, lon FROM location_pings WHERE source != 'garmin'"
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
    Keyed on source='garmin' within the activity's time span."""
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
# Garmin activity-summary helpers
# ---------------------------------------------------------------------------


def activity_span(activity: dict) -> tuple[str, str] | None:
    """(startISO, endISO) in UTC 'Z' form for an activity summary, or None
    if it lacks the fields. Uses startTimeGMT + duration."""
    start = activity.get("startTimeGMT")
    dur = activity.get("duration")
    if not start:
        return None
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


def _resolve_db_path(user_id: str, options: ImportOptions, config):
    """Return an initialised per-user location.db path.

    ``config.db_path`` is pinned to the framework DB by the caller so
    config-based resolution is CWD-independent; ``options.workspace``
    overrides it entirely (used by tests / ad-hoc runs)."""
    from istota.location import db as location_db
    if options.workspace:
        from istota.location.workspace import synthesize_location_context
        ctx = synthesize_location_context(user_id, Path(options.workspace))
    else:
        from istota.location._loader import resolve_for_user
        ctx = resolve_for_user(user_id, config)
    location_db.init_db(ctx.db_path)
    return ctx.db_path


def import_tracks(
    user_id: str,
    *,
    framework_db_path: Path,
    config=None,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Import (or, when ``options.dry_run``, plan) Garmin GPS tracks for
    ``user_id`` into their location.db. Returns a structured
    :class:`ImportResult` — no printing.

    ``config`` is the loaded :class:`istota.config.Config`; when ``None`` it
    is loaded and its ``db_path`` pinned to ``framework_db_path`` (the CLI
    path). The web endpoint passes the app-state config. Raises
    ``GarminAuthError`` / ``GarminRateLimited`` from the health.garmin layer
    for the caller to map to a response.
    """
    from istota.health import garmin as gm
    from istota.location import db as location_db

    options = options or ImportOptions()
    framework_db_path = Path(framework_db_path)

    if config is None:
        from istota.config import load_config
        config = load_config()
        config.db_path = framework_db_path

    adapter = gm.acquire_client(
        framework_db_path, user_id, persist_rotation=not options.dry_run,
    )

    type_keys = [t.strip() for t in options.activity_types.split(",") if t.strip()]
    allow = set(type_keys)
    today = _dt.date.today()
    start_d, end_d = _daterange_local(options.days_back, today)

    activities: list[dict] = []
    seen_ids: set = set()
    for parent in parent_types_for(type_keys):
        listed = adapter.get_activities_by_date(start_d, end_d, parent) or []
        for act in listed:
            aid = act.get("activityId")
            if aid in seen_ids:
                continue
            if activity_type_key(act) not in allow:
                continue
            seen_ids.add(aid)
            activities.append(act)
    activities.sort(key=lambda a: a.get("startTimeGMT") or "")

    db_path = _resolve_db_path(user_id, options, config)
    report: list[dict] = []
    total_inserted = 0

    if options.dry_run:
        with location_db.connect(db_path) as conn:
            occupancy: list[tuple[float, float, float]] = []
            for act in activities:
                row = _process_activity(
                    adapter, act, conn, options, occupancy, write=False,
                )
                if row:
                    report.append(row)
        return ImportResult(True, 0, len(report), report)

    with _user_lock(user_id):
        occupancy = []
        for act in activities:
            with location_db.connect(db_path) as conn:
                row = _process_activity(
                    adapter, act, conn, options, occupancy, write=True,
                )
                conn.commit()
                if row:
                    report.append(row)
                    total_inserted += row["inserted"]
    _record_telemetry(framework_db_path, user_id, total_inserted, len(report))
    return ImportResult(False, total_inserted, len(report), report)


def _fetch_points(adapter, act, options: ImportOptions):
    if not has_gps(act):
        return None, None
    span = activity_span(act)
    if span is None:
        return None, None
    details = adapter.get_activity_details(
        str(act.get("activityId")), maxpoly=options.maxpoly,
    )
    if not details:
        return None, span
    pts = parse_polyline(details, activity_type_key(act))
    if not pts:
        return None, span  # hasPolyline true but empty → no-GPS
    return downsample(pts, options.downsample_sec), span


def _process_activity(adapter, act, conn, options, occupancy, *, write: bool):
    """Shared plan/import path. ``write`` gates eviction + insertion."""
    pts, span = _fetch_points(adapter, act, options)
    if not pts or span is None:
        return None
    t0, t1 = span
    if write:
        evict_activity_imports(conn, t0, t1)
    native = load_native_points(conn, t0, t1, options.guard_band)
    kept = filter_shadowed(
        pts, list(native) + occupancy, options.guard_band, options.guard_radius,
    )
    if write:
        insert_points(conn, kept)
    occupancy.extend((parse_ts(p.timestamp), p.lat, p.lon) for p in kept)
    return {
        "activity_id": act.get("activityId"),
        "type": activity_type_key(act),
        "start": span[0],
        "distance_m": act.get("distance"),
        "fetched": len(pts),
        "inserted": len(kept),
        "shadowed": len(pts) - len(kept),
    }


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
    """Host-local exclusive lock so a manual/web backfill and the nightly
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
