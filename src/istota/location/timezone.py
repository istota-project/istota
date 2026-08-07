"""Follow the user's timezone when they travel (ISSUE-096).

`user_profiles.timezone` drives the `User timezone:` prompt header, and through
it every briefing, calendar read and scheduled prompt. Crossing a border used to
leave it on the home zone until the user changed it by hand in the web UI.

This module is the detection half: given a per-user `location.db`, decide
whether the user has *settled* somewhere long enough that its zone is now
theirs. Writing the answer, and telling the user it happened, is the scheduler's
job — see `scheduler.check_travel_timezone`. Nothing here writes anything.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone as _tz
from zoneinfo import ZoneInfo

from ..geo import haversine
from .garmin_import import parse_ts

logger = logging.getLogger(__name__)

# How long the user must have been in the new zone.
#
# The issue proposed 30 minutes; an hour is used because this rewrites a
# setting the user chose, and the cost of being slow (a briefing on the old
# clock) is smaller than the cost of being wrong.
DEFAULT_DWELL_MINUTES = 60

# How far apart the two samples may be and still count as "stayed put".
#
# This, not the dwell time, is what keeps a journey from moving the timezone: a
# flight covers ~800 km in the dwell window and a continental one spends over
# two hours above a single zone, so a dwell rule on its own writes the timezone
# of whatever the user is flying over. A day at home moves a few km, and a
# commute rarely more than fifty.
DEFAULT_SETTLED_RADIUS_M = 100_000.0

# A fix worse than this says little about which side of a border you are on.
# Overridden from `config.location.accuracy_threshold_m` by the caller, so the
# operator tunes one gate rather than two.
DEFAULT_ACCURACY_THRESHOLD_M = 100.0

# Past this, the newest ping is history rather than a position. A tracker that
# has been off since yesterday must not move the timezone today.
DEFAULT_MAX_PING_AGE_MINUTES = 120

# Enough rows to find the two anchors on any plausible sampling rate — at
# Overland's densest (~10s) this covers well over the dwell window.
_SCAN_LIMIT = 2000

_finder_instance = None
_import_warned = False


def _finder():
    """The shared TimezoneFinder, or None when the dependency is absent.

    Built once — construction loads the boundary data, which is the expensive
    part; a lookup against it is well under a millisecond.
    """
    global _finder_instance, _import_warned
    if _finder_instance is None:
        try:
            from timezonefinder import TimezoneFinder
        except ImportError:
            if not _import_warned:
                _import_warned = True
                logger.warning(
                    "timezonefinder is not installed, so following the timezone "
                    "on travel does nothing. Install the 'location' extra "
                    "(uv sync --extra location) to enable it."
                )
            return None
        _finder_instance = TimezoneFinder()
    return _finder_instance


def timezone_at(lat: float, lon: float) -> str | None:
    """The IANA zone containing a coordinate, or None.

    Returns None for a coordinate that resolves to no zone, for an `Etc/GMT±N`
    offset, and when `timezonefinder` is not installed.

    The `Etc/` exclusion is the interesting one: open ocean resolves to a fixed
    nautical offset rather than to a place, and that is exactly what a phone
    reports for most of a long overwater flight. Adopting it would set the
    user's timezone to somewhere nobody lives, halfway to their destination.
    """
    finder = _finder()
    if finder is None:
        return None
    try:
        name = finder.timezone_at(lat=lat, lng=lon)
    except Exception:  # pragma: no cover - defensive, the library is pure lookup
        # Deliberately no coordinates in the message: the app log is read
        # cross-user in the admin pane and carries no user attribution here,
        # and four decimals of latitude is a street address.
        logger.warning("Timezone lookup failed", exc_info=True)
        return None
    if not name or name.startswith("Etc/"):
        return None
    return name


def same_wall_clock(a: str, b: str, when: datetime) -> bool:
    """Whether two zone names put the clock at the same time at `when`.

    `US/Pacific` and `America/Los_Angeles` are the same zone under two names,
    and tzdata is full of such links — Nextcloud seeds several of them. Without
    this, a user who has not moved is told they have.

    An unknown name compares unequal rather than raising, so a junk stored
    timezone still lets a real zone be detected.
    """
    try:
        return ZoneInfo(a).utcoffset(when) == ZoneInfo(b).utcoffset(when)
    except Exception:
        return False


def _recent_pings(
    conn: sqlite3.Connection,
    accuracy_threshold_m: float,
) -> list[tuple[datetime, float, float]]:
    """Usable pings, newest first, as parsed instants.

    Rows are read and then sorted **in Python** rather than by SQL, because
    `location_pings.timestamp` holds whatever string the client sent: a
    trailing `Z`, an explicit `+02:00`, or microseconds and no zone at all.
    Comparing those as text orders `13:00+0200` above `05:00-0700` for the same
    instant, and a `WHERE timestamp <= <utc-now>` bound silently hides every
    row from a device stamping a positive offset — which would disable this
    whole feature for such a device and never say so. `garmin_import.parse_ts`
    is the module's existing answer to this and carries the same warning.
    """
    rows = conn.execute(
        """
        SELECT timestamp, lat, lon
        FROM location_pings
        WHERE accuracy IS NOT NULL AND accuracy <= ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (accuracy_threshold_m, _SCAN_LIMIT),
    ).fetchall()

    parsed: list[tuple[datetime, float, float]] = []
    for row in rows:
        try:
            epoch = parse_ts(row["timestamp"])
        except (ValueError, TypeError):
            continue
        parsed.append(
            (datetime.fromtimestamp(epoch, _tz.utc), row["lat"], row["lon"]),
        )
    parsed.sort(key=lambda p: p[0], reverse=True)
    return parsed


def detect_travel_timezone(
    conn: sqlite3.Connection,
    current_tz: str,
    *,
    dwell_minutes: int = DEFAULT_DWELL_MINUTES,
    settled_radius_m: float = DEFAULT_SETTLED_RADIUS_M,
    accuracy_threshold_m: float = DEFAULT_ACCURACY_THRESHOLD_M,
    max_ping_age_minutes: int = DEFAULT_MAX_PING_AGE_MINUTES,
    now: datetime | None = None,
) -> str | None:
    """The zone the user has settled into, or None to leave the timezone alone.

    Two samples decide it: the newest usable ping, and the newest usable ping at
    least `dwell_minutes` older. They must resolve to the same zone, sit within
    `settled_radius_m` of each other, and that zone must put the clock somewhere
    other than `current_tz` does.

    The radius is the load-bearing half. "Been in this zone an hour" is true of
    a plane over the Midwest, which is why a dwell-only rule sets the timezone
    to wherever the user is flying over and then again on landing. "Been in this
    zone an hour and has not gone anywhere" is the property actually wanted.

    Returning None is the safe answer and covers every ambiguity: no pings, a
    stale track, an arrival too recent to be a stay, a journey in progress, and
    a rename of the zone the user is already in.
    """
    now = now or datetime.now(_tz.utc)
    now = now.replace(tzinfo=_tz.utc) if now.tzinfo is None else now.astimezone(_tz.utc)

    pings = _recent_pings(conn, accuracy_threshold_m)
    if not pings:
        return None

    latest_at, lat, lon = pings[0]
    if now - latest_at > timedelta(minutes=max_ping_age_minutes):
        return None

    cutoff = latest_at - timedelta(minutes=dwell_minutes)
    earlier = next((p for p in pings if p[0] <= cutoff), None)
    if earlier is None:
        return None
    _, earlier_lat, earlier_lon = earlier

    if haversine(lat, lon, earlier_lat, earlier_lon) > settled_radius_m:
        return None

    zone = timezone_at(lat, lon)
    if zone is None or zone == current_tz:
        return None
    if timezone_at(earlier_lat, earlier_lon) != zone:
        return None
    if same_wall_clock(zone, current_tz, now):
        return None
    return zone
