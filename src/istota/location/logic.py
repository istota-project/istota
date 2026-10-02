"""Location query helpers shared between the web API and the location skill.

These functions are pure SQL + lightweight math — no FastAPI/HTTP/auth
dependencies — so they can be called from both `webui/app.py` and skill
subprocesses.

Per-user split: every helper takes a path to the per-user
``location.db`` (no ``user_id``). The file is the user scope.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from types import SimpleNamespace

from istota.geo import MAX_STOP_EXTENSION_SECONDS, _parse_ts, haversine, resolve_place
from istota.location import db as location_db

# The radius the day summary clusters a stop at, and so how far from a stop
# a point can sit and still be read as being there.
STOP_CLUSTER_RADIUS_M = 250


def _location_place_stats(db_path: str | Path, place_id: int) -> dict | None:
    """Visit statistics for a place, derived from ping data.

    Groups pings into visits by checking whether the user was seen
    elsewhere during gaps. A gap only splits a visit if there are pings
    at a different place (or unassigned pings far away) in between —
    GPS dropout while stationary indoors doesn't break a visit. Walk-bys
    (< 3 pings) are filtered out.
    """
    with location_db.connect(Path(db_path)) as conn:
        place = location_db.get_place_by_id(conn, place_id)
        if not place:
            return None

        rows = conn.execute(
            """
            SELECT timestamp FROM location_pings
            WHERE place_id = ?
            ORDER BY timestamp ASC
            """,
            (place_id,),
        ).fetchall()

        if not rows:
            return {
                "place_id": place_id,
                "total_visits": 0,
                "first_visit": None,
                "last_visit": None,
                "avg_duration_min": None,
                "total_duration_min": None,
                "longest_visit_min": None,
            }

        min_pings = 3  # filter out walk-bys
        segments: list[tuple[str, str, int]] = []
        visit_start = rows[0]["timestamp"]
        prev_ts = visit_start
        ping_count = 1

        for row in rows[1:]:
            ts = row["timestamp"]
            elsewhere = conn.execute(
                """
                SELECT 1 FROM location_pings
                WHERE place_id IS NOT ? AND place_id IS NOT NULL
                  AND timestamp > ? AND timestamp < ?
                LIMIT 1
                """,
                (place_id, prev_ts, ts),
            ).fetchone()
            if elsewhere:
                segments.append((visit_start, prev_ts, ping_count))
                visit_start = ts
                ping_count = 1
            else:
                ping_count += 1
            prev_ts = ts
        segments.append((visit_start, prev_ts, ping_count))

        visits = [(s, e) for s, e, c in segments if c >= min_pings]

        if not visits:
            return {
                "place_id": place_id,
                "total_visits": 0,
                "first_visit": None,
                "last_visit": None,
                "avg_duration_min": None,
                "total_duration_min": None,
                "longest_visit_min": None,
            }

        durations_sec = []
        for start, end in visits:
            try:
                dur = (
                    datetime.fromisoformat(end) - datetime.fromisoformat(start)
                ).total_seconds()
                durations_sec.append(dur)
            except (ValueError, TypeError):
                durations_sec.append(0)

        total_sec = sum(durations_sec)
        avg_sec = total_sec / len(durations_sec) if durations_sec else 0
        longest_sec = max(durations_sec) if durations_sec else 0

        return {
            "place_id": place_id,
            "total_visits": len(visits),
            "first_visit": visits[0][0],
            "last_visit": visits[-1][0],
            "avg_duration_min": round(avg_sec / 60),
            "total_duration_min": round(total_sec / 60),
            "longest_visit_min": round(longest_sec / 60),
        }


def _location_list_dismissed(db_path: str | Path) -> dict:
    with location_db.connect(Path(db_path)) as conn:
        rows = location_db.list_dismissed_clusters(conn)
        return {
            "dismissed": [
                {
                    "id": r.id,
                    "lat": r.lat,
                    "lon": r.lon,
                    "radius_meters": r.radius_meters,
                    "dismissed_at": r.dismissed_at,
                }
                for r in rows
            ]
        }


def _location_dismiss_cluster(db_path: str | Path, data: dict) -> dict:
    radius = int(data.get("radius_meters", 100))
    with location_db.connect(Path(db_path)) as conn:
        cluster_id = location_db.dismiss_cluster(
            conn, float(data["lat"]), float(data["lon"]), radius,
        )
        conn.commit()
        return {
            "id": cluster_id,
            "lat": float(data["lat"]),
            "lon": float(data["lon"]),
            "radius_meters": radius,
        }


def _location_restore_dismissed(db_path: str | Path, cluster_id: int) -> bool:
    with location_db.connect(Path(db_path)) as conn:
        deleted = location_db.restore_dismissed_cluster(conn, cluster_id)
        conn.commit()
        return deleted


# How close a discovered cluster may sit to a place that is already saved
# before `discover` stops surfacing it. Named because `learn
# --from-cluster` resolves through the same filter and has to explain
# the refusal it produces when re-siting an existing place.
CLUSTER_EXCLUSION_METERS = 200


def _location_discover_places(
    db_path: str | Path, min_pings: int = 10,
) -> dict:
    """Find clusters of stationary pings not assigned to any place."""
    with location_db.connect(Path(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT ROUND(lat, 4) as rlat, ROUND(lon, 4) as rlon,
                   AVG(lat) as avg_lat, AVG(lon) as avg_lon,
                   COUNT(*) as cnt,
                   MIN(timestamp) as first_seen, MAX(timestamp) as last_seen
            FROM location_pings
            WHERE place_id IS NULL
              AND (activity_type IS NULL OR activity_type = 'stationary')
            GROUP BY rlat, rlon
            HAVING cnt >= ?
            ORDER BY cnt DESC
            """,
            (max(3, min_pings // 3),),
        ).fetchall()

        points = [
            {"lat": r["avg_lat"], "lon": r["avg_lon"], "count": r["cnt"],
             "first_seen": r["first_seen"], "last_seen": r["last_seen"]}
            for r in rows
        ]

        clusters: list[dict] = []
        used = [False] * len(points)
        for i, p in enumerate(points):
            if used[i]:
                continue
            cluster_lat = p["lat"] * p["count"]
            cluster_lon = p["lon"] * p["count"]
            cluster_count = p["count"]
            first = p["first_seen"]
            last = p["last_seen"]
            members = [(p["lat"], p["lon"])]
            used[i] = True

            for j in range(i + 1, len(points)):
                if used[j]:
                    continue
                if haversine(p["lat"], p["lon"], points[j]["lat"], points[j]["lon"]) <= 200:
                    cluster_lat += points[j]["lat"] * points[j]["count"]
                    cluster_lon += points[j]["lon"] * points[j]["count"]
                    cluster_count += points[j]["count"]
                    members.append((points[j]["lat"], points[j]["lon"]))
                    if points[j]["first_seen"] < first:
                        first = points[j]["first_seen"]
                    if points[j]["last_seen"] > last:
                        last = points[j]["last_seen"]
                    used[j] = True

            if cluster_count >= min_pings:
                center_lat = cluster_lat / cluster_count
                center_lon = cluster_lon / cluster_count
                spread = max(
                    (haversine(center_lat, center_lon, mlat, mlon)
                     for mlat, mlon in members),
                    default=0.0,
                )
                radius_meters = int(min(300, max(50, round(spread + 25))))
                clusters.append({
                    "lat": center_lat,
                    "lon": center_lon,
                    "total_pings": cluster_count,
                    "first_seen": first,
                    "last_seen": last,
                    "radius_meters": radius_meters,
                })

        existing = conn.execute(
            "SELECT lat, lon, radius_meters FROM places"
        ).fetchall()
        dismissed = conn.execute(
            "SELECT lat, lon, radius_meters FROM dismissed_clusters"
        ).fetchall()
        filtered = []
        for c in clusters:
            too_close = False
            for ep in existing:
                dist = haversine(c["lat"], c["lon"], ep["lat"], ep["lon"])
                if dist <= max(ep["radius_meters"], CLUSTER_EXCLUSION_METERS):
                    too_close = True
                    break
            if too_close:
                continue
            for dz in dismissed:
                if haversine(c["lat"], c["lon"], dz["lat"], dz["lon"]) <= dz["radius_meters"]:
                    too_close = True
                    break
            if not too_close:
                filtered.append(c)

        return {"clusters": filtered}


def resolve_cluster_for_point(
    db_path: str | Path,
    lat: float,
    lon: float,
    *,
    min_pings: int = 5,
    max_distance_m: float = 250.0,
) -> dict | None:
    """The discovered cluster nearest ``(lat, lon)``, or ``None``.

    A coordinate read off a day summary is one ping's position, which is
    a worse input than the cluster it belongs to: ``discover`` weights
    the centroid by ping count and fits the radius to the observed
    spread. Clusters carry no id — they are recomputed per run — so the
    coordinate stays the addressing mechanism and this resolves it.

    ``min_pings`` is lower than ``discover``'s own default because the
    caller has already named the stop: it is a point somebody is looking
    at, not a candidate competing for attention in a list.
    """
    clusters = _location_discover_places(db_path, min_pings=min_pings)["clusters"]
    scored = [
        (haversine(lat, lon, c["lat"], c["lon"]), c) for c in clusters
    ]
    scored = [(d, c) for d, c in scored if d <= max_distance_m]
    if not scored:
        return None
    return min(scored, key=lambda pair: pair[0])[1]


def assign_pings_to_place(
    conn,
    place_id: int,
    lat: float,
    lon: float,
    radius_meters: float,
) -> dict:
    """Bring ``location_pings.place_id`` into line with a place's geofence.

    ``place_id`` is resolved at ingest (``webhook_receiver.resolve_place``),
    so a place saved after the fact leaves every historical ping inside it
    at NULL and ``location_day_summary`` — which names a stop by joining
    ``location_pings.place_id`` to ``places.name`` — goes on reporting it
    as an unnamed coordinate. Moving or resizing a place has the mirror
    problem: its pings stay attached to a footprint that no longer
    contains them.

    Two halves, and both surfaces need both. A ping still attached to the
    place but now outside the circle is released; an *unattached* ping
    inside it is adopted. A place that has just been created only ever
    takes the second half, nothing being attached to it yet, which is why
    one helper serves create and update alike.

    Only unattached pings are adopted: the bounding box is centred on this
    place, so a neighbour's pings fall inside it wherever the two circles
    overlap, and reassigning those would silently move history from one
    place to another.

    What it deliberately does not touch is ``location_pings.visit_id``,
    nor the ``visits`` table behind it. A released ping keeps a
    ``visit_id`` naming a visit to the place it just left, and an
    adopted one may carry a visit belonging to somewhere else.
    ``location_db.update_ping_place`` is the writer that keeps the pair
    in step, and only the ingest path uses it; nothing outside
    ``webhook_receiver`` reads the column today, so this is latent
    rather than observable. ``reconcile_visits`` is what re-derives
    ``visits`` from ping ``place_id``, and it runs on a rolling window
    that a backfill of older pings falls outside — so the ping-derived
    surfaces (``location_day_summary``, ``_location_place_stats``) see
    a backfill and the ``visits`` table does not. Lifted verbatim from
    the web route, which has always behaved this way.

    Takes an open connection rather than a path — the caller writes the
    place and this in one transaction, and commits.
    """
    released = 0
    for row in conn.execute(
        "SELECT id, lat, lon FROM location_pings WHERE place_id = ?",
        (place_id,),
    ).fetchall():
        if haversine(lat, lon, row["lat"], row["lon"]) > radius_meters:
            conn.execute(
                "UPDATE location_pings SET place_id = NULL WHERE id = ?",
                (row["id"],),
            )
            released += 1

    # Rough bounding box to keep the haversine pass off the whole table
    # (1 degree of latitude is ~111 km). Two known limits, neither worth
    # carrying machinery for at the scale of one person's GPS history:
    # the box does not wrap the antimeridian, so a ping just across
    # +/-180 from the centre is missed; and the cosine floor caps dlon,
    # which *under*-covers above about 89 degrees of latitude rather
    # than merely avoiding a division by zero. The release half scans
    # the place's own rows and has neither problem.
    dlat = radius_meters / 111_000
    dlon = radius_meters / (
        111_000 * max(0.01, abs(math.cos(math.radians(lat))))
    )
    assigned = 0
    for row in conn.execute(
        """
        SELECT id, lat, lon FROM location_pings
        WHERE place_id IS NULL
          AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
        """,
        (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
    ).fetchall():
        if haversine(lat, lon, row["lat"], row["lon"]) <= radius_meters:
            conn.execute(
                "UPDATE location_pings SET place_id = ? WHERE id = ?",
                (place_id, row["id"]),
            )
            assigned += 1

    return {"assigned": assigned, "released": released}


# ===========================================================================
# The query pipeline both surfaces read (F2)
# ===========================================================================
#
# ``day_summary``, ``current``, ``history`` and ``places`` used to exist
# twice — once in ``skills/location/__init__.py`` for the model and once
# in ``webui/app.py`` for the browser — and the two copies had drifted: only
# the web copy snapped a stop to its saved place's centre, only the skill
# copy carried the address parts and ``duration_minutes``, and an empty
# day came back under two different key sets. Nothing chose any of that.
#
# What stays at each surface is what is genuinely the surface's: the JSON
# envelope it has always printed, its own limit default, and — for
# ``location_history`` — the sort direction, which under a ``LIMIT``
# selects a different set of rows and is therefore a query parameter
# rather than a leftover difference. The reverse-geocode cache lives in
# the framework database, which the two surfaces reach by different
# routes, so it arrives as the ``geocode`` callable.
#
# Pinned by tests/test_location_surface_parity.py.


def resolve_timezone(tz: str | tzinfo | None) -> tuple[tzinfo, str]:
    """Return ``(zone, name)`` for a timezone given as a name or an object.

    An unresolvable name falls back to ``America/Los_Angeles`` — the
    behaviour both copies already had — while the *name* returned is the
    one that was asked for. That asymmetry is deliberate: the payload's
    ``timezone`` field reports the request, so a caller can see that what
    it asked for is not what it got.

    ``""`` is therefore reported as ``""`` rather than as the default it
    resolves to, which is not a nicety: `_get_location_config` hands the
    web route an empty string for a user with no timezone on their
    profile, and the copy this replaced reported it verbatim. Only
    ``None`` — nobody asked — reports the default as the answer.
    """
    if tz is None or isinstance(tz, str):
        name = "America/Los_Angeles" if tz is None else tz
        try:
            return ZoneInfo(name), name
        except Exception:
            return ZoneInfo("America/Los_Angeles"), name
    return tz, getattr(tz, "key", str(tz))


def utc_day_bounds(day: str, zone: tzinfo) -> tuple[str, str]:
    """The UTC half-open bounds ``[since, until)`` of one local calendar day."""
    day_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone)
    day_end = day_start + timedelta(days=1)
    return (
        day_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        day_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def location_current(db_path: str | Path, *, tz: str | tzinfo | None = None) -> dict:
    """The most recent ping, and the visit that is still open.

    ``tz`` is accepted for symmetry with the other three entry points and
    is not read: every timestamp in this payload is the stored UTC string,
    and the one derived value — how long the open visit has been running —
    is a duration, which no timezone changes.
    """
    del tz
    with location_db.connect(Path(db_path)) as conn:
        row = conn.execute(
            """
            SELECT lp.timestamp, lp.lat, lp.lon, lp.altitude, lp.accuracy,
                   lp.activity_type, lp.battery, lp.wifi,
                   p.name as place_name
            FROM location_pings lp
            LEFT JOIN places p ON lp.place_id = p.id
            ORDER BY lp.timestamp DESC LIMIT 1
            """
        ).fetchone()
        if not row:
            return {"last_ping": None, "current_visit": None}

        last_ping = {
            "timestamp": row["timestamp"],
            "lat": row["lat"],
            "lon": row["lon"],
            # Metres as the device reported them; what they are measured
            # against varies by source and is not recorded. Null where the
            # fix was horizontal only, where the device flagged it
            # vertically invalid, and where the point is one the client
            # declared rather than measured (ISSUE-229).
            "altitude": row["altitude"],
            "accuracy": row["accuracy"],
            "activity_type": row["activity_type"],
            "battery": row["battery"],
            "wifi": row["wifi"],
            "place": row["place_name"],
        }

        visit_row = conn.execute(
            """
            SELECT place_name, entered_at, ping_count
            FROM visits
            WHERE exited_at IS NULL
            ORDER BY entered_at DESC LIMIT 1
            """
        ).fetchone()
        current_visit = None
        if visit_row:
            entered = visit_row["entered_at"]
            try:
                entered_dt = datetime.fromisoformat(entered)
                if entered_dt.tzinfo is None:
                    entered_dt = entered_dt.replace(tzinfo=timezone.utc)
                duration_min = int(
                    (datetime.now(timezone.utc) - entered_dt).total_seconds() / 60
                )
            except (ValueError, TypeError):
                duration_min = None
            current_visit = {
                "place_name": visit_row["place_name"],
                "entered_at": entered,
                "duration_minutes": duration_min,
                "ping_count": visit_row["ping_count"],
            }

        return {"last_ping": last_ping, "current_visit": current_visit}


_PING_COLUMNS = """
    SELECT lp.timestamp, lp.lat, lp.lon, lp.altitude, lp.accuracy,
           lp.activity_type, lp.speed, lp.battery, lp.source,
           p.name as place_name
    FROM location_pings lp
    LEFT JOIN places p ON lp.place_id = p.id
"""


def location_history(
    db_path: str | Path,
    *,
    since: str | None,
    until: str | None,
    limit: int,
    order: str = "asc",
    source: str | None = None,
) -> dict:
    """Pings across ``[since, until)``, or the most recent ``limit`` of them.

    ``order`` applies to the bounded query only. Unbounded, the question
    is "the newest ``limit`` pings", and the descending sort is what
    *selects* them rather than how they are presented — so that branch is
    always newest-first, as both copies already were.

    Bounded, the direction is a real parameter: with a ``LIMIT`` it picks
    the start of the window or its end. The map draws a polyline and reads
    the day forwards; the skill reads it newest-first, matching its own
    unbounded branch.

    ``limit`` of 0 means no limit on the bounded query and 100 on the
    unbounded one, which is what each caller relies on today.

    An ``order`` that is neither raises rather than defaulting. Under a
    ``LIMIT`` the direction selects a different set of rows, so a typo
    silently answering with the other end of the day is the one failure
    here that reads as data rather than as a bug.

    ``source`` keeps only pings from that source (``overland``, ``garmin``).
    Each ping carries its source either way: ``activity_type`` alone does not
    tell an imported watch track from the phone, which tags activities too.
    """
    if order not in ("asc", "desc"):
        raise ValueError(f"order must be 'asc' or 'desc', got {order!r}")
    direction = "ASC" if order == "asc" else "DESC"
    with location_db.connect(Path(db_path)) as conn:
        source_clause = " AND lp.source = ?" if source else ""
        source_params = [source] if source else []
        if since and until:
            query = (
                _PING_COLUMNS
                + " WHERE lp.timestamp >= ? AND lp.timestamp < ?"
                + source_clause
                + f" ORDER BY lp.timestamp {direction}"
            )
            params: list = [since, until, *source_params]
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(query, params).fetchall()
        else:
            where = " WHERE lp.source = ?" if source else ""
            rows = conn.execute(
                _PING_COLUMNS + where + " ORDER BY lp.timestamp DESC LIMIT ?",
                (*source_params, limit or 100),
            ).fetchall()

        pings = [
            {
                "timestamp": r["timestamp"],
                "lat": r["lat"],
                "lon": r["lon"],
                # See location_current for the three reasons this is null.
                "altitude": r["altitude"],
                "accuracy": r["accuracy"],
                "place": r["place_name"],
                "activity_type": r["activity_type"],
                "speed": r["speed"],
                "battery": r["battery"],
                "source": r["source"],
            }
            for r in rows
        ]
        return {"pings": pings, "count": len(pings)}


def location_places(db_path: str | Path) -> dict:
    with location_db.connect(Path(db_path)) as conn:
        rows = conn.execute(
            "SELECT id, name, lat, lon, radius_meters, category, notes "
            "FROM places ORDER BY name"
        ).fetchall()
        return {
            "places": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "radius_meters": r["radius_meters"],
                    "category": r["category"],
                    "notes": r["notes"],
                }
                for r in rows
            ]
        }


def location_day_summary(
    db_path: str | Path,
    *,
    day: date | str | None = None,
    tz: str | tzinfo | None = None,
    saved_places: list[dict] | None = None,
    geocode: Callable[[float, float], dict] | None = None,
) -> dict:
    """One local day's stops, named and timed.

    ``day`` defaults to today in ``tz`` and ``saved_places`` to the places
    table in ``db_path``; both are parameters so a caller that has already
    read them need not read them twice.

    ``geocode`` resolves a coordinate to an address dict. It is injected
    because the reverse-geocode cache lives in the framework database,
    which the skill reaches through ``ISTOTA_DB_PATH`` and the web app
    through the loaded config. ``None`` means no reverse geocoding is
    available and an unnamed stop is reported as ``unknown``.
    """
    from istota.geo import (
        ACTIVITY_SOURCES,
        activity_segments,
        dedupe_near_duplicate_pings,
        is_activity,
        merge_consecutive_stops,
    )

    zone, tz_name = resolve_timezone(tz)
    if day is None:
        target_date = datetime.now(zone).strftime("%Y-%m-%d")
    elif isinstance(day, str):
        target_date = day
    else:
        target_date = day.isoformat()

    since_utc, until_utc = utc_day_bounds(target_date, zone)

    with location_db.connect(Path(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT lp.timestamp, lp.lat, lp.lon, lp.activity_type, lp.accuracy, lp.speed,
                   lp.place_id, lp.source, p.name as place_name
            FROM location_pings lp
            LEFT JOIN places p ON lp.place_id = p.id
            WHERE lp.timestamp >= ? AND lp.timestamp < ?
            ORDER BY lp.timestamp ASC
            """,
            (since_utc, until_utc),
        ).fetchall()

        if not rows:
            return {
                "date": target_date,
                "timezone": tz_name,
                "ping_count": 0,
                "transit_pings": 0,
                "stops": [],
                "activities": [],
            }

        if saved_places is None:
            saved_places = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, name, lat, lon, radius_meters FROM places"
                ).fetchall()
            ]

        # A recorded activity is reported as its own segment and kept out of
        # the stop clustering, which would otherwise absorb a loop that starts
        # and ends at one place into the stop around it (ISSUE-558).
        all_pings = [dict(r) for r in rows]
        segments = []
        others = []
        for seg in activity_segments(
            [p for p in all_pings if p["source"] in ACTIVITY_SOURCES]
        ):
            if is_activity(seg):
                segments.append(seg)
            else:
                others.extend(seg["pings"])
        others.extend(p for p in all_pings if p["source"] not in ACTIVITY_SOURCES)
        others.sort(key=lambda p: _parse_ts(p["timestamp"]))
        native = dedupe_near_duplicate_pings(others)

        # Where the day's last observation left the user. After a trailing
        # activity that is where the activity ended, not the stop before it.
        tail_place_id = native[-1]["place_id"] if native else None
        if segments and (
            not native
            or _parse_ts(segments[-1]["last_ts"]) > _parse_ts(native[-1]["timestamp"])
        ):
            end_place = _place_at(
                segments[-1]["last"]["lat"], segments[-1]["last"]["lon"], saved_places,
            )
            tail_place_id = end_place.get("id") if end_place else None

        # A stop still running at local midnight has no departure inside
        # the window; the first later ping somewhere else is what ends it.
        closing_ping = None
        if tail_place_id is not None:
            closing_row = conn.execute(
                """
                SELECT lp.timestamp, lp.lat, lp.lon, lp.activity_type, lp.accuracy, lp.speed,
                       lp.place_id, lp.source, p.name as place_name
                FROM location_pings lp
                LEFT JOIN places p ON lp.place_id = p.id
                WHERE lp.timestamp >= ? AND (lp.place_id IS NULL OR lp.place_id != ?)
                ORDER BY lp.timestamp ASC
                LIMIT 1
                """,
                (until_utc, tail_place_id),
            ).fetchone()
            if closing_row is not None:
                closing_ping = dict(closing_row)

    stops, transit_pings = _stops_around_activities(
        native, segments, saved_places, closing_ping,
    )

    for stop in stops:
        if stop["place_name"]:
            stop["location"] = stop["place_name"]
            stop["location_source"] = "saved_place"
            # Report the place's own centre rather than the centroid of
            # whichever pings landed: two visits to one place otherwise
            # plot a few metres apart.
            for sp in saved_places:
                if sp["name"] == stop["place_name"]:
                    stop["lat"] = sp["lat"]
                    stop["lon"] = sp["lon"]
                    break
        else:
            sp = _saved_place_at(stop["lat"], stop["lon"], saved_places)
            if sp is not None:
                stop["location"] = sp["name"]
                stop["location_source"] = "saved_place_proximity"
                stop["lat"] = sp["lat"]
                stop["lon"] = sp["lon"]
            else:
                geo = geocode(stop["lat"], stop["lon"]) if geocode else {}
                stop["location"] = (
                    geo.get("suburb")
                    or geo.get("neighborhood")
                    or geo.get("road")
                    or geo.get("city")
                    or "unknown"
                )
                stop["location_source"] = geo.get("source", "unknown")
                stop["road"] = geo.get("road")
                stop["neighborhood"] = geo.get("neighborhood")
                stop["suburb"] = geo.get("suburb")

        for key in ("first_ts", "last_ts"):
            stop[key + "_local"] = _local_hhmm(stop[key], zone)

    merged = merge_consecutive_stops(stops)

    for s in merged:
        s["duration_minutes"] = _duration_minutes(s["first_ts"], s["last_ts"])

    activities = []
    for k, seg in enumerate(segments):
        # Only a stop in the stretch just before this activity can be the one
        # it left from; an earlier one is on the far side of another activity.
        stretch_start = _parse_ts(segments[k - 1]["last_ts"]) if k > 0 else None
        before = [
            s for s in merged
            if _parse_ts(s["first_ts"]) < _parse_ts(seg["first_ts"])
            and (stretch_start is None or _parse_ts(s["first_ts"]) >= stretch_start)
        ]
        start_place = _activity_start_place(
            seg["first"], before[-1] if before else None, saved_places,
        )
        # The end takes the ingest rule alone: it is the rule the stop
        # resuming after an activity is opened on, so a named end_place
        # always has that stop behind it.
        end_place = _place_at(seg["last"]["lat"], seg["last"]["lon"], saved_places)
        activities.append({
            "type": "activity",
            "activity": seg["activity"],
            "source": seg["source"],
            "start": _local_hhmm(seg["first_ts"], zone),
            "end": _local_hhmm(seg["last_ts"], zone),
            "duration_minutes": _duration_minutes(seg["first_ts"], seg["last_ts"]),
            "distance_km": round(seg["distance_m"] / 1000, 2),
            "ping_count": seg["ping_count"],
            "start_place": start_place,
            "end_place": end_place["name"] if end_place else None,
            "start_lat": round(seg["first"]["lat"], 5),
            "start_lon": round(seg["first"]["lon"], 5),
            "end_lat": round(seg["last"]["lat"], 5),
            "end_lon": round(seg["last"]["lon"], 5),
        })

    return {
        "date": target_date,
        "timezone": tz_name,
        "ping_count": len(native) + sum(seg["ping_count"] for seg in segments),
        "transit_pings": transit_pings,
        "stops": [
            {
                "type": "stop",
                "location": s["location"],
                "location_source": s.get("location_source"),
                "road": s.get("road"),
                "neighborhood": s.get("neighborhood"),
                "suburb": s.get("suburb"),
                "arrived": s.get("first_ts_local"),
                "departed": s.get("last_ts_local"),
                "duration_minutes": s.get("duration_minutes"),
                "ping_count": s["ping_count"],
                "lat": round(s["lat"], 5),
                "lon": round(s["lon"], 5),
            }
            for s in merged
        ],
        "activities": activities,
    }


def _saved_place_at(lat: float, lon: float, saved_places: list[dict]) -> dict | None:
    """The first saved place whose radius (at least 100 m) holds the point."""
    for sp in saved_places:
        if haversine(lat, lon, sp["lat"], sp["lon"]) <= max(sp["radius_meters"], 100):
            return sp
    return None


def _place_at(lat: float, lon: float, saved_places: list[dict]) -> dict | None:
    """The saved place ingest would have tagged this point with, as a dict.

    Nearest place inside its own radius: the rule that produced every stored
    ``place_id``, so an answer here can be compared against one. The 100 m
    floor in ``_saved_place_at`` is a naming convenience and is not that rule.
    """
    views = [SimpleNamespace(**sp) for sp in saved_places]
    match = resolve_place(lat, lon, views)
    return vars(match) if match is not None else None


def _activity_start_place(
    point: dict, previous_stop: dict | None, saved_places: list[dict],
) -> str | None:
    """The saved place an activity started at, by name.

    The ingest rule first. Failing that, the stop the activity left from,
    when that stop is at a saved place and the point is within the stop
    clustering radius of it: a run from the door can start a few metres
    outside a tight place radius while the stop before it says plainly where
    it was. That stop always closes on the activity's first point, so the
    name and the stop agree.
    """
    place = _place_at(point["lat"], point["lon"], saved_places)
    if place is not None:
        return place["name"]
    if previous_stop is None or previous_stop.get("location_source") not in (
        "saved_place", "saved_place_proximity",
    ):
        return None
    distance = haversine(point["lat"], point["lon"], previous_stop["lat"], previous_stop["lon"])
    if distance > STOP_CLUSTER_RADIUS_M:
        return None
    return previous_stop["location"]


def _local_hhmm(ts: str, zone: tzinfo) -> str:
    try:
        return _parse_ts(ts).astimezone(zone).strftime("%H:%M")
    except Exception:
        return ts


def _duration_minutes(first_ts: str, last_ts: str) -> int | None:
    try:
        return int((_parse_ts(last_ts) - _parse_ts(first_ts)).total_seconds() / 60)
    except (ValueError, TypeError):
        return None


def _stops_around_activities(
    native: list[dict],
    segments: list[dict],
    saved_places: list[dict],
    closing_ping: dict | None,
) -> tuple[list[dict], int]:
    """Cluster the phone's pings into stops, one stretch between activities at a time.

    Each stretch is clustered and transit-filtered on its own, so neither the
    clustering nor the transit filter's absorb-a-nearby-fragment rule can join
    a stop across an activity. A stretch ending in an activity closes on the
    activity's first ping, which is the ISSUE-332 rule: the first observation
    somewhere else is what bounds departure.

    A phone ping falling inside an activity's span is dropped. The watch was
    on the body; a phone left at home keeps declaring home (its wifi-zone
    points) through the whole run, and those would otherwise hold the stop
    open across it.
    """
    from istota.geo import cluster_pings, filter_transit_clusters

    spans = [(_parse_ts(s["first_ts"]), _parse_ts(s["last_ts"])) for s in segments]
    stretches: list[list[dict]] = [[] for _ in range(len(segments) + 1)]
    k = 0
    for ping in native:
        t = _parse_ts(ping["timestamp"])
        while k < len(spans) and t > spans[k][1]:
            k += 1
        if k < len(spans) and t >= spans[k][0]:
            continue
        stretches[k].append(ping)

    stops: list[dict] = []
    transit_pings = 0
    for k, stretch in enumerate(stretches):
        prev_seg = segments[k - 1] if k > 0 else None
        # A watch ping near the door can carry the place's id once the place
        # is backfilled, and a closing ping tagged with the stop's own place is
        # ignored. Leaving on a run is leaving, whatever the ping is tagged.
        if k < len(segments):
            stretch_closing = {**segments[k]["first"], "place_id": None}
        else:
            stretch_closing = closing_ping

        clusters: list[dict] = []
        if prev_seg is not None:
            reopened, stretch = _reopen_after_activity(
                prev_seg, stretch, stretch_closing, saved_places,
            )
            if reopened is not None:
                clusters.append(reopened)
        if stretch:
            clusters.extend(
                cluster_pings(stretch, radius_m=STOP_CLUSTER_RADIUS_M, closing_ping=stretch_closing)
            )

        stretch_stops, stretch_transit = filter_transit_clusters(clusters)
        transit_pings += stretch_transit
        if prev_seg is not None and stretch_stops:
            stretch_stops[0]["_follows_activity"] = True
        stops.extend(stretch_stops)
    return stops, transit_pings


def _reopen_after_activity(
    segment: dict,
    stretch: list[dict],
    closing: dict | None,
    saved_places: list[dict],
) -> tuple[dict | None, list[dict]]:
    """A stop at the place an activity ended, running until the user is seen leaving.

    A phone that stays home through a run is usually silent until the next
    departure, so after a run from the door there may be no ping at home for
    hours. The activity ending inside a saved place is the arrival; pings
    tagged with that place at the head of the next stretch are the stay;
    the first ping anywhere else closes it, with the same travel-time
    estimate a clustered stop gets.

    Returns the stop, or ``None`` when the activity did not end at a saved
    place or nothing after it says the user stayed, and the stretch with the
    pings the stop consumed removed.
    """
    from istota.geo import _estimated_departure_timestamp

    place = _place_at(segment["last"]["lat"], segment["last"]["lon"], saved_places)
    if place is None or place.get("id") is None:
        return None, stretch

    i = 0
    while i < len(stretch) and stretch[i].get("place_id") == place["id"]:
        i += 1
    consumed, rest = stretch[:i], stretch[i:]

    # Bridging the silence back to the activity's end is the ISSUE-332
    # extension run backwards, and it takes the same cap: a phone that next
    # reports home eleven hours later says nothing about the hours between.
    if consumed:
        silence = (
            _parse_ts(consumed[0]["timestamp"]) - _parse_ts(segment["last_ts"])
        ).total_seconds()
        if silence > MAX_STOP_EXTENSION_SECONDS:
            return None, stretch
    if rest:
        closing = rest[0]
    if not consumed and closing is None:
        return None, stretch

    anchor = consumed[-1] if consumed else segment["last"]
    if closing is not None:
        last_ts = _estimated_departure_timestamp(anchor, closing)
    else:
        last_ts = anchor["timestamp"]

    return {
        "lat": place["lat"],
        "lon": place["lon"],
        "ping_count": len(consumed),
        "first_ts": segment["last_ts"],
        "last_ts": last_ts,
        "place_id": place["id"],
        "place_name": place["name"],
    }, rest
