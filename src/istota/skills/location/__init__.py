"""Location tracking skill — GPS data from Overland iOS app.

CLI:
    python -m istota.skills.location current
    python -m istota.skills.location history [--limit N] [--date YYYY-MM-DD] [--tz TZ]
    python -m istota.skills.location places
    python -m istota.skills.location learn NAME [--category CAT] [--radius N] [--notes TXT]
    python -m istota.skills.location update (--name NAME | --id ID) [--rename NEW] [--category CAT] [--radius N] [--notes TXT] [--lat N] [--lon N]
    python -m istota.skills.location delete (--name NAME | --id ID)
    python -m istota.skills.location reverse-geocode --lat N --lon N
    python -m istota.skills.location day-summary --date YYYY-MM-DD [--tz TZ]
    python -m istota.skills.location discover [--min-pings N]
    python -m istota.skills.location dismiss-cluster --lat N --lon N [--radius M]
    python -m istota.skills.location list-dismissed
    python -m istota.skills.location restore-dismissed CLUSTER_ID
    python -m istota.skills.location place-stats (--name NAME | --id ID)

Per-user split: per-user GPS data lives in ``location.db`` resolved
via ``LOCATION_DB_PATH`` (set by the ``setup_env`` hook below). Two
subcommands also need the framework-side geocode caches and read
``ISTOTA_DB_PATH`` for those.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def setup_env(ctx) -> dict[str, str]:
    """Inject LOCATION_DB_PATH for the per-user location.db.

    Self-gates on ``Config.is_module_enabled(user_id, "location")``.
    Returns ``{}`` (no env contribution) when the module is disabled
    for the user, when nextcloud_mount_path is unset, or when any
    other resolution gate fails.
    """
    from istota import location as _location  # noqa: PLC0415

    config = ctx.config
    user_id = ctx.task.user_id
    try:
        loc_ctx = _location.resolve_for_user(user_id, config)
    except _location.UserNotFoundError:
        return {}
    return {"LOCATION_DB_PATH": str(loc_ctx.db_path)}


def _get_location_db_path() -> str:
    db_path = os.environ.get("LOCATION_DB_PATH", "")
    if not db_path:
        print(json.dumps({
            "status": "error", "error": "LOCATION_DB_PATH not set",
        }))
        sys.exit(1)
    return db_path


def _get_framework_db_path() -> str:
    """Path to framework istota.db — only needed for geocode caches."""
    db_path = os.environ.get("ISTOTA_DB_PATH", "")
    if not db_path:
        print(json.dumps({
            "status": "error", "error": "ISTOTA_DB_PATH not set",
        }))
        sys.exit(1)
    return db_path


def _connect_location() -> sqlite3.Connection:
    """Open a raw connection to per-user location.db.

    Subcommands use raw connections (rather than the package's
    contextmanager) because they read+commit+close inline; the manager
    pattern is awkward when the conn outlives a single ``with`` block.
    """
    conn = sqlite3.connect(_get_location_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def cmd_current(args):
    conn = _connect_location()

    cursor = conn.execute(
        """
        SELECT lp.timestamp, lp.lat, lp.lon, lp.accuracy,
               lp.activity_type, lp.battery, lp.wifi,
               p.name as place_name
        FROM location_pings lp
        LEFT JOIN places p ON lp.place_id = p.id
        ORDER BY lp.timestamp DESC LIMIT 1
        """
    )
    row = cursor.fetchone()
    if not row:
        print(json.dumps({"last_ping": None, "current_visit": None}))
        conn.close()
        return

    last_ping = {
        "timestamp": row["timestamp"],
        "lat": row["lat"],
        "lon": row["lon"],
        "accuracy": row["accuracy"],
        "activity_type": row["activity_type"],
        "battery": row["battery"],
        "wifi": row["wifi"],
        "place": row["place_name"],
    }

    cursor = conn.execute(
        """
        SELECT place_name, entered_at, ping_count
        FROM visits
        WHERE exited_at IS NULL
        ORDER BY entered_at DESC LIMIT 1
        """
    )
    visit_row = cursor.fetchone()
    current_visit = None
    if visit_row:
        entered = visit_row["entered_at"]
        try:
            entered_dt = datetime.fromisoformat(entered)
            now = datetime.now(timezone.utc)
            if entered_dt.tzinfo is None:
                entered_dt = entered_dt.replace(tzinfo=timezone.utc)
            duration_min = int((now - entered_dt).total_seconds() / 60)
        except (ValueError, TypeError):
            duration_min = None

        current_visit = {
            "place_name": visit_row["place_name"],
            "entered_at": entered,
            "duration_minutes": duration_min,
            "ping_count": visit_row["ping_count"],
        }

    print(json.dumps({"last_ping": last_ping, "current_visit": current_visit}))
    conn.close()


def cmd_history(args):
    conn = _connect_location()

    if args.date:
        tz_name = getattr(args, "tz", None) or os.environ.get("TZ", "America/Los_Angeles")
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("America/Los_Angeles")

        day_start_local = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=tz)
        day_end_local = day_start_local + timedelta(days=1)
        since = day_start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        until = day_end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        limit = args.limit or 0
        query = """
            SELECT lp.timestamp, lp.lat, lp.lon, lp.accuracy,
                   lp.activity_type, lp.speed, lp.battery,
                   p.name as place_name
            FROM location_pings lp
            LEFT JOIN places p ON lp.place_id = p.id
            WHERE lp.timestamp >= ? AND lp.timestamp < ?
            ORDER BY lp.timestamp DESC
        """
        params: list = [since, until]
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        cursor = conn.execute(query, params)
    else:
        limit = args.limit or 20
        cursor = conn.execute(
            """
            SELECT lp.timestamp, lp.lat, lp.lon, lp.accuracy,
                   lp.activity_type, lp.speed, lp.battery,
                   p.name as place_name
            FROM location_pings lp
            LEFT JOIN places p ON lp.place_id = p.id
            ORDER BY lp.timestamp DESC LIMIT ?
            """,
            (limit,),
        )

    rows = cursor.fetchall()
    results = [
        {
            "timestamp": r["timestamp"],
            "lat": r["lat"],
            "lon": r["lon"],
            "accuracy": r["accuracy"],
            "place": r["place_name"],
            "activity_type": r["activity_type"],
            "speed": r["speed"],
            "battery": r["battery"],
        }
        for r in rows
    ]
    print(json.dumps(results))
    conn.close()


def cmd_places(args):
    conn = _connect_location()
    cursor = conn.execute(
        """
        SELECT id, name, lat, lon, radius_meters, category, notes
        FROM places ORDER BY name
        """
    )
    rows = cursor.fetchall()
    results = [
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
    print(json.dumps(results))
    conn.close()


def cmd_learn(args):
    from istota.location import db as location_db

    conn = _connect_location()

    name = args.name
    radius = args.radius or 100
    category = args.category or "other"
    notes = (getattr(args, "notes", None) or "").strip() or None

    cursor = conn.execute(
        "SELECT lat, lon, accuracy, timestamp FROM location_pings "
        "ORDER BY timestamp DESC LIMIT 1"
    )
    row = cursor.fetchone()
    if not row:
        print(json.dumps({"status": "error", "error": "No location pings found"}))
        conn.close()
        sys.exit(1)

    lat, lon = row["lat"], row["lon"]
    location_db.upsert_place(
        conn, name, lat, lon,
        radius_meters=radius, category=category, notes=notes,
    )
    conn.commit()
    conn.close()

    print(json.dumps({
        "status": "ok",
        "place": name,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "radius_meters": radius,
        "notes": notes,
        "message": f"Saved '{name}' at {lat:.4f}, {lon:.4f}.",
    }))


def _resolve_place(conn, name=None, place_id=None):
    """Find a place by name or ID. Returns (place, error_msg)."""
    from istota.location import db as location_db

    if place_id is not None:
        place = location_db.get_place_by_id(conn, place_id)
        if not place:
            return None, f"No place found with ID {place_id}"
        return place, None
    if name:
        place = location_db.get_place_by_name(conn, name)
        if not place:
            return None, f"No place found with name '{name}'"
        return place, None
    return None, "Specify --name or --id"


def cmd_update(args):
    from istota.location import db as location_db

    conn = _connect_location()
    place, err = _resolve_place(conn, name=args.name, place_id=args.id)
    if err:
        print(json.dumps({"status": "error", "error": err}))
        conn.close()
        sys.exit(1)

    updates: dict = {}
    clear_notes = False
    if args.rename is not None:
        updates["name"] = args.rename
    if args.category is not None:
        updates["category"] = args.category
    if args.radius is not None:
        updates["radius_meters"] = args.radius
    if args.notes is not None:
        n = args.notes.strip()
        if n:
            updates["notes"] = n
        else:
            clear_notes = True
    if args.lat is not None:
        updates["lat"] = args.lat
    if args.lon is not None:
        updates["lon"] = args.lon

    if not updates and not clear_notes:
        print(json.dumps({"status": "error", "error": "No changes specified"}))
        conn.close()
        sys.exit(1)

    if updates:
        location_db.update_place(conn, place.id, **updates)
    if clear_notes:
        conn.execute("UPDATE places SET notes = NULL WHERE id = ?", (place.id,))
    conn.commit()

    updated = location_db.get_place_by_id(conn, place.id)
    conn.close()

    print(json.dumps({
        "status": "ok",
        "place": {
            "id": updated.id,
            "name": updated.name,
            "lat": updated.lat,
            "lon": updated.lon,
            "radius_meters": updated.radius_meters,
            "category": updated.category,
            "notes": updated.notes,
        },
    }))


def cmd_delete(args):
    from istota.location import db as location_db

    conn = _connect_location()
    place, err = _resolve_place(conn, name=args.name, place_id=args.id)
    if err:
        print(json.dumps({"status": "error", "error": err}))
        conn.close()
        sys.exit(1)

    place_name = place.name
    location_db.nullify_place_on_pings(conn, place.id)
    location_db.delete_place_by_id(conn, place.id)
    conn.commit()
    conn.close()

    print(json.dumps({"status": "ok", "deleted": place_name}))


_VIRTUAL_LOCATION_PATTERNS = [
    "zoom.us", "zoom", "meet.google", "teams.microsoft",
    "teams", "webex", "skype", "hangouts", "facetime",
    "google meet", "microsoft teams",
]


def _is_virtual_location(location: str) -> bool:
    loc_lower = location.lower()
    return any(p in loc_lower for p in _VIRTUAL_LOCATION_PATTERNS)


def _match_place(location_text: str, places):
    loc_lower = location_text.lower()
    for place in places:
        if place["name"].lower() in loc_lower or loc_lower in place["name"].lower():
            return {
                "name": place["name"],
                "lat": place["lat"],
                "lon": place["lon"],
                "radius_meters": place["radius_meters"],
            }
    return None


def _geocode_location(location_text: str, framework_conn):
    """Resolve location text to lat/lon via cache or Nominatim.

    Cache reads/writes go to framework istota.db (cross-user dedup).
    """
    from istota.db import get_cached_geocode, cache_geocode

    cached = get_cached_geocode(framework_conn, location_text)
    if cached:
        return cached

    try:
        from geopy.geocoders import Nominatim
        geolocator = Nominatim(user_agent="istota")
        result = geolocator.geocode(location_text, timeout=10)
        if result:
            cache_geocode(
                framework_conn, location_text,
                result.latitude, result.longitude,
            )
            framework_conn.commit()
            return (result.latitude, result.longitude)
    except Exception:
        pass

    return None


def cmd_attendance(args):
    """Cross-reference calendar events with GPS pings.

    Triple-DB: per-user location.db for pings/places, framework
    istota.db for the geocode cache, CalDAV for events.
    """
    from istota.geo import haversine
    from istota.skills.calendar import (
        CalendarEvent,
        get_caldav_client,
        list_calendars,
        get_events,
    )
    from istota.location import db as location_db
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    conn = _connect_location()
    framework_conn = sqlite3.connect(_get_framework_db_path())
    framework_conn.row_factory = sqlite3.Row

    tz_name = os.environ.get("TZ", "America/Los_Angeles")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target_date = datetime.now(tz).date()

    day_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    caldav_url = os.environ.get("CALDAV_URL", "")
    caldav_user = os.environ.get("CALDAV_USERNAME", "")
    caldav_pass = os.environ.get("CALDAV_PASSWORD", "")

    if not all([caldav_url, caldav_user, caldav_pass]):
        print(json.dumps({"status": "error", "error": "CalDAV credentials not set (CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD)"}))
        conn.close()
        framework_conn.close()
        sys.exit(1)

    client = get_caldav_client(caldav_url, caldav_user, caldav_pass)

    try:
        calendars = list_calendars(client)
    except Exception as e:
        print(json.dumps({"status": "error", "error": f"Failed to list calendars: {e}"}))
        conn.close()
        framework_conn.close()
        sys.exit(1)

    all_events: list[CalendarEvent] = []
    for cal_name, cal_url in calendars:
        try:
            events = get_events(client, cal_url, day_start, day_end)
            all_events.extend(events)
        except Exception:
            continue

    filtered = []
    for ev in all_events:
        if ev.all_day:
            continue
        if not ev.location:
            continue
        if _is_virtual_location(ev.location):
            continue
        if args.event:
            query = args.event.lower()
            if query != ev.uid.lower() and query not in ev.summary.lower():
                continue
        filtered.append(ev)

    if not filtered:
        print(json.dumps({"date": str(target_date), "events": []}))
        conn.close()
        framework_conn.close()
        return

    places_rows = conn.execute(
        "SELECT name, lat, lon, radius_meters FROM places"
    ).fetchall()
    places = [dict(r) for r in places_rows]

    default_radius = 200

    results = []
    for ev in filtered:
        event_lat, event_lon, radius = None, None, default_radius
        source = None

        place_match = _match_place(ev.location, places)
        if place_match:
            event_lat = place_match["lat"]
            event_lon = place_match["lon"]
            radius = place_match["radius_meters"]
            source = "place"
        else:
            coords = _geocode_location(ev.location, framework_conn)
            if coords:
                event_lat, event_lon = coords
                source = "geocode"

        entry = {
            "summary": ev.summary,
            "uid": ev.uid,
            "start": ev.start.isoformat(),
            "end": ev.end.isoformat(),
            "location": ev.location,
            "location_resolved": source is not None,
            "resolution_source": source,
        }

        if event_lat is None:
            entry["attended"] = None
            results.append(entry)
            continue

        entry["event_lat"] = round(event_lat, 6)
        entry["event_lon"] = round(event_lon, 6)
        entry["radius_meters"] = radius

        ev_start = ev.start
        ev_end = ev.end
        if ev_start.tzinfo is None:
            ev_start = ev_start.replace(tzinfo=tz)
        if ev_end.tzinfo is None:
            ev_end = ev_end.replace(tzinfo=tz)

        window_start = (ev_start - timedelta(minutes=30)).astimezone(timezone.utc)
        window_end = (ev_end + timedelta(minutes=30)).astimezone(timezone.utc)
        ping_since = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        ping_until = window_end.strftime("%Y-%m-%dT%H:%M:%SZ")

        pings = location_db.get_pings(
            conn, since=ping_since, until=ping_until, limit=1000,
        )

        nearby_pings = []
        for ping in pings:
            dist = haversine(event_lat, event_lon, ping.lat, ping.lon)
            if dist <= radius:
                nearby_pings.append(ping)

        if nearby_pings:
            entry["attended"] = True
            entry["first_nearby_ping"] = nearby_pings[-1].timestamp
            entry["last_nearby_ping"] = nearby_pings[0].timestamp
            entry["nearby_ping_count"] = len(nearby_pings)
        else:
            entry["attended"] = None

        results.append(entry)

    print(json.dumps({"date": str(target_date), "events": results}))
    conn.close()
    framework_conn.close()


def cmd_reverse_geocode(args):
    """Reverse-geocode a lat/lon.

    Cache lookup goes to framework istota.db; this subcommand doesn't
    touch the per-user location.db at all.
    """
    from istota.geo import reverse_geocode

    framework_conn = sqlite3.connect(_get_framework_db_path())
    framework_conn.row_factory = sqlite3.Row
    try:
        result = reverse_geocode(args.lat, args.lon, framework_conn)
        print(json.dumps(result, indent=2))
    finally:
        framework_conn.close()


def cmd_day_summary(args):
    """Day summary with reverse-geocoded place names.

    Reads pings from per-user location.db; resolves ``unknown`` stops
    via reverse_geocode against the framework istota.db cache.
    """
    from istota.geo import (
        reverse_geocode, cluster_pings, dedupe_near_duplicate_pings, haversine,
        filter_transit_clusters, merge_consecutive_stops,
    )

    conn = _connect_location()
    framework_conn = sqlite3.connect(_get_framework_db_path())
    framework_conn.row_factory = sqlite3.Row

    tz_name = getattr(args, "tz", None) or os.environ.get("TZ", "America/Los_Angeles")
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")

    target_date = args.date or datetime.now(tz).strftime("%Y-%m-%d")

    day_start_local = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=tz)
    day_end_local = day_start_local + timedelta(days=1)
    since_utc = day_start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    until_utc = day_end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute(
        """
        SELECT lp.timestamp, lp.lat, lp.lon, lp.activity_type, lp.accuracy,
               lp.place_id, p.name as place_name
        FROM location_pings lp
        LEFT JOIN places p ON lp.place_id = p.id
        WHERE lp.timestamp >= ? AND lp.timestamp < ?
        ORDER BY lp.timestamp ASC
        """,
        (since_utc, until_utc),
    ).fetchall()

    if not rows:
        print(json.dumps({"date": target_date, "stops": [], "ping_count": 0}))
        conn.close()
        framework_conn.close()
        return

    pings = [dict(r) for r in rows]
    pings = dedupe_near_duplicate_pings(pings)
    clusters = cluster_pings(pings, radius_m=250)

    saved_places = conn.execute(
        "SELECT id, name, lat, lon, radius_meters FROM places"
    ).fetchall()
    saved_places = [dict(r) for r in saved_places]

    stops, transit_pings = filter_transit_clusters(clusters)

    for stop in stops:
        if stop["place_name"]:
            stop["location"] = stop["place_name"]
            stop["location_source"] = "saved_place"
        else:
            matched = False
            for sp in saved_places:
                dist = haversine(stop["lat"], stop["lon"], sp["lat"], sp["lon"])
                if dist <= max(sp["radius_meters"], 100):
                    stop["location"] = sp["name"]
                    stop["location_source"] = "saved_place_proximity"
                    matched = True
                    break

            if not matched:
                geo = reverse_geocode(stop["lat"], stop["lon"], framework_conn)
                name = (
                    geo.get("suburb")
                    or geo.get("neighborhood")
                    or geo.get("road")
                    or geo.get("city")
                    or "unknown"
                )
                stop["location"] = name
                stop["location_source"] = geo.get("source", "unknown")
                stop["road"] = geo.get("road")
                stop["neighborhood"] = geo.get("neighborhood")
                stop["suburb"] = geo.get("suburb")

        for key in ("first_ts", "last_ts"):
            try:
                utc_dt = datetime.fromisoformat(stop[key]).replace(tzinfo=timezone.utc)
                stop[key + "_local"] = utc_dt.astimezone(tz).strftime("%H:%M")
            except Exception:
                stop[key + "_local"] = stop[key]

    merged = merge_consecutive_stops(stops)

    for s in merged:
        try:
            first = datetime.fromisoformat(s["first_ts"]).replace(tzinfo=timezone.utc)
            last = datetime.fromisoformat(s["last_ts"]).replace(tzinfo=timezone.utc)
            s["duration_minutes"] = int((last - first).total_seconds() / 60)
        except (ValueError, TypeError):
            s["duration_minutes"] = None

    result = {
        "date": target_date,
        "timezone": tz_name,
        "ping_count": len(pings),
        "transit_pings": transit_pings,
        "stops": [
            {
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
    }

    print(json.dumps(result, indent=2))
    conn.close()
    framework_conn.close()


def cmd_discover(args):
    """Find clusters of stationary pings not assigned to any place."""
    from istota.location_logic import _location_discover_places

    db_path = _get_location_db_path()
    min_pings = getattr(args, "min_pings", None) or 10
    result = _location_discover_places(db_path, min_pings=min_pings)
    print(json.dumps(result, indent=2))


def cmd_dismiss_cluster(args):
    """Mark a cluster zone as dismissed so it stops surfacing in discover."""
    from istota.location_logic import _location_dismiss_cluster

    db_path = _get_location_db_path()
    radius = getattr(args, "radius", None) or 100
    result = _location_dismiss_cluster(
        db_path,
        {"lat": args.lat, "lon": args.lon, "radius_meters": radius},
    )
    print(json.dumps({"status": "ok", **result}, indent=2))


def cmd_list_dismissed(args):
    """List all dismissed cluster zones."""
    from istota.location_logic import _location_list_dismissed

    db_path = _get_location_db_path()
    result = _location_list_dismissed(db_path)
    print(json.dumps(result, indent=2))


def cmd_restore_dismissed(args):
    """Un-dismiss a cluster zone by id."""
    from istota.location_logic import _location_restore_dismissed

    db_path = _get_location_db_path()
    deleted = _location_restore_dismissed(db_path, args.cluster_id)
    if not deleted:
        print(json.dumps({"status": "error", "error": "dismissed cluster not found"}))
        return
    print(json.dumps({"status": "ok", "id": args.cluster_id}, indent=2))


def cmd_place_stats(args):
    """Visit statistics for a place."""
    from istota.location_logic import _location_place_stats
    from istota.location import db as location_db

    db_path = _get_location_db_path()

    place_id = getattr(args, "id", None)
    if place_id is None:
        conn = _connect_location()
        try:
            place = location_db.get_place_by_name(conn, args.name)
        finally:
            conn.close()
        if not place:
            print(json.dumps({
                "status": "error", "error": f"place '{args.name}' not found",
            }))
            return
        place_id = place.id

    result = _location_place_stats(db_path, place_id)
    if result is None:
        print(json.dumps({
            "status": "error", "error": "place not found",
        }))
        return
    print(json.dumps(result, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(description="Location tracking CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("current", help="Current location and visit")

    hist = sub.add_parser("history", help="Recent location pings")
    hist.add_argument("--limit", type=int, default=0)
    hist.add_argument("--date", help="Filter by date (YYYY-MM-DD)")
    hist.add_argument("--tz", help="Timezone (default: TZ env var or America/Los_Angeles)")

    sub.add_parser("places", help="List known places")

    learn = sub.add_parser("learn", help="Save current location as a named place")
    learn.add_argument("name", help="Place name")
    learn.add_argument("--category", default="other", help="Place category")
    learn.add_argument("--radius", type=int, default=100, help="Geofence radius in meters")
    learn.add_argument("--notes", help="Optional free-text notes")

    update = sub.add_parser("update", help="Update an existing place")
    update_target = update.add_mutually_exclusive_group(required=True)
    update_target.add_argument("--name", help="Place name to update")
    update_target.add_argument("--id", type=int, help="Place ID to update")
    update.add_argument("--rename", help="New name")
    update.add_argument("--category", help="New category")
    update.add_argument("--radius", type=int, help="New radius in meters")
    update.add_argument("--notes", help="New notes")
    update.add_argument("--lat", type=float, help="New latitude")
    update.add_argument("--lon", type=float, help="New longitude")

    delete = sub.add_parser("delete", help="Delete a place")
    delete_target = delete.add_mutually_exclusive_group(required=True)
    delete_target.add_argument("--name", help="Place name to delete")
    delete_target.add_argument("--id", type=int, help="Place ID to delete")

    attend = sub.add_parser("attendance", help="Check calendar attendance via GPS")
    attend.add_argument("--date", help="Date to check (YYYY-MM-DD, default: today)")
    attend.add_argument("--event", help="Filter by event UID or title substring")

    rgeo = sub.add_parser("reverse-geocode", help="Reverse geocode a lat/lon pair")
    rgeo.add_argument("--lat", type=float, required=True)
    rgeo.add_argument("--lon", type=float, required=True)

    dsum = sub.add_parser("day-summary", help="Day summary with reverse-geocoded locations")
    dsum.add_argument("--date", help="Date (YYYY-MM-DD, default: today)")
    dsum.add_argument("--tz", help="Timezone (default: TZ env var or America/Los_Angeles)")

    disc = sub.add_parser("discover", help="Find unknown clusters of stationary pings")
    disc.add_argument("--min-pings", dest="min_pings", type=int, default=10,
                      help="Minimum pings for a cluster to surface (default 10)")

    dismiss = sub.add_parser("dismiss-cluster", help="Dismiss a cluster zone so it stops appearing in discover")
    dismiss.add_argument("--lat", type=float, required=True)
    dismiss.add_argument("--lon", type=float, required=True)
    dismiss.add_argument("--radius", type=int, default=100, help="Dismissal radius in meters (default 100)")

    sub.add_parser("list-dismissed", help="List dismissed cluster zones")

    restore = sub.add_parser("restore-dismissed", help="Un-dismiss a cluster zone by id")
    restore.add_argument("cluster_id", type=int, help="Dismissed cluster id")

    pstats = sub.add_parser("place-stats", help="Visit statistics for a place")
    pstats_target = pstats.add_mutually_exclusive_group(required=True)
    pstats_target.add_argument("--name", help="Place name")
    pstats_target.add_argument("--id", type=int, help="Place ID")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "current": cmd_current,
        "history": cmd_history,
        "places": cmd_places,
        "learn": cmd_learn,
        "update": cmd_update,
        "delete": cmd_delete,
        "attendance": cmd_attendance,
        "reverse-geocode": cmd_reverse_geocode,
        "day-summary": cmd_day_summary,
        "discover": cmd_discover,
        "dismiss-cluster": cmd_dismiss_cluster,
        "list-dismissed": cmd_list_dismissed,
        "restore-dismissed": cmd_restore_dismissed,
        "place-stats": cmd_place_stats,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)
