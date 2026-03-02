"""Location tracking skill — GPS data from Overland iOS app.

CLI:
    python -m istota.skills.location current
    python -m istota.skills.location history [--limit N] [--date YYYY-MM-DD]
    python -m istota.skills.location places
    python -m istota.skills.location learn NAME [--category CAT] [--radius N]
"""

import argparse
import json
import os
import re
import sys
import sqlite3
from datetime import datetime, timezone


def _get_conn() -> sqlite3.Connection:
    db_path = os.environ.get("ISTOTA_DB_PATH", "")
    if not db_path:
        print(json.dumps({"error": "ISTOTA_DB_PATH not set"}))
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _get_user_id() -> str:
    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        print(json.dumps({"error": "ISTOTA_USER_ID not set"}))
        sys.exit(1)
    return user_id


def cmd_current(args):
    conn = _get_conn()
    user_id = _get_user_id()

    # Latest ping
    cursor = conn.execute(
        """
        SELECT lp.timestamp, lp.lat, lp.lon, lp.accuracy,
               lp.activity_type, lp.battery, lp.wifi,
               p.name as place_name
        FROM location_pings lp
        LEFT JOIN places p ON lp.place_id = p.id
        WHERE lp.user_id = ?
        ORDER BY lp.timestamp DESC LIMIT 1
        """,
        (user_id,),
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

    # Current visit (open)
    cursor = conn.execute(
        """
        SELECT place_name, entered_at, ping_count
        FROM visits
        WHERE user_id = ? AND exited_at IS NULL
        ORDER BY entered_at DESC LIMIT 1
        """,
        (user_id,),
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
    conn = _get_conn()
    user_id = _get_user_id()
    limit = args.limit or 20

    if args.date:
        since = f"{args.date}T00:00:00"
        until = f"{args.date}T23:59:59"
        cursor = conn.execute(
            """
            SELECT lp.timestamp, lp.lat, lp.lon, lp.accuracy,
                   lp.activity_type, lp.speed, lp.battery,
                   p.name as place_name
            FROM location_pings lp
            LEFT JOIN places p ON lp.place_id = p.id
            WHERE lp.user_id = ? AND lp.timestamp >= ? AND lp.timestamp <= ?
            ORDER BY lp.timestamp DESC LIMIT ?
            """,
            (user_id, since, until, limit),
        )
    else:
        cursor = conn.execute(
            """
            SELECT lp.timestamp, lp.lat, lp.lon, lp.accuracy,
                   lp.activity_type, lp.speed, lp.battery,
                   p.name as place_name
            FROM location_pings lp
            LEFT JOIN places p ON lp.place_id = p.id
            WHERE lp.user_id = ?
            ORDER BY lp.timestamp DESC LIMIT ?
            """,
            (user_id, limit),
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
    conn = _get_conn()
    user_id = _get_user_id()

    cursor = conn.execute(
        """
        SELECT name, lat, lon, radius_meters, category, notes
        FROM places WHERE user_id = ? ORDER BY name
        """,
        (user_id,),
    )
    rows = cursor.fetchall()
    results = [
        {
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


_TOML_BLOCK_RE = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)


def cmd_learn(args):
    conn = _get_conn()
    user_id = _get_user_id()

    name = args.name
    radius = args.radius or 100
    category = args.category or "other"

    # Get latest ping
    cursor = conn.execute(
        """
        SELECT lat, lon, accuracy, timestamp
        FROM location_pings WHERE user_id = ?
        ORDER BY timestamp DESC LIMIT 1
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        print(json.dumps({"error": "No location pings found"}))
        conn.close()
        sys.exit(1)

    lat, lon = row["lat"], row["lon"]
    conn.close()

    # Write to LOCATION.md (source of truth — DB synced on webhook reload)
    mount_path = os.environ.get("NEXTCLOUD_MOUNT_PATH", "")
    if not mount_path:
        print(json.dumps({"error": "NEXTCLOUD_MOUNT_PATH not set, cannot write LOCATION.md"}))
        sys.exit(1)

    _append_place_to_location_md(mount_path, user_id, name, lat, lon, radius, category)

    print(json.dumps({
        "status": "ok",
        "place": name,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "radius_meters": radius,
        "message": f"Saved '{name}' at {lat:.4f}, {lon:.4f}. Restart webhooks service to sync.",
    }))


def _append_place_to_location_md(
    mount_path: str, user_id: str, name: str,
    lat: float, lon: float, radius: int, category: str,
) -> None:
    """Append a [[places]] entry to the user's LOCATION.md file."""
    # Find the bot dir by looking for LOCATION.md in any config subdir
    from pathlib import Path
    user_base = Path(mount_path) / "Users" / user_id
    location_files = list(user_base.glob("*/config/LOCATION.md"))
    if not location_files:
        return

    loc_file = location_files[0]
    content = loc_file.read_text()

    new_place = (
        f'\n[[places]]\n'
        f'name = "{name}"\n'
        f'lat = {lat:.6f}\n'
        f'lon = {lon:.6f}\n'
        f'radius_meters = {radius}\n'
        f'category = "{category}"\n'
    )

    # Insert before the closing ``` of the TOML block
    match = _TOML_BLOCK_RE.search(content)
    if match:
        insert_pos = match.end() - 3  # before closing ```
        content = content[:insert_pos] + new_place + content[insert_pos:]
    else:
        # No TOML block — create one
        content += f"\n```toml{new_place}```\n"

    loc_file.write_text(content)


_VIRTUAL_LOCATION_PATTERNS = [
    "zoom.us", "zoom", "meet.google", "teams.microsoft",
    "teams", "webex", "skype", "hangouts", "facetime",
    "google meet", "microsoft teams",
]


def _is_virtual_location(location: str) -> bool:
    """Check if a location string indicates a virtual meeting."""
    loc_lower = location.lower()
    return any(p in loc_lower for p in _VIRTUAL_LOCATION_PATTERNS)


def _match_place(location_text: str, places):
    """Fuzzy-match a location string against known places (case-insensitive substring).

    Returns (place_name, lat, lon, radius_meters) or None.
    """
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


def _geocode_location(location_text: str, conn):
    """Resolve location text to lat/lon via cache or Nominatim.

    Returns (lat, lon) or None.
    """
    from istota.db import get_cached_geocode, cache_geocode

    cached = get_cached_geocode(conn, location_text)
    if cached:
        return cached

    try:
        from geopy.geocoders import Nominatim
        geolocator = Nominatim(user_agent="istota")
        result = geolocator.geocode(location_text, timeout=10)
        if result:
            cache_geocode(conn, location_text, result.latitude, result.longitude)
            conn.commit()
            return (result.latitude, result.longitude)
    except Exception:
        pass

    return None


def cmd_attendance(args):
    from istota.geo import haversine
    from istota.skills.calendar import (
        CalendarEvent,
        get_caldav_client,
        list_calendars,
        get_events,
    )
    from istota import db
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    conn = _get_conn()
    user_id = _get_user_id()

    # Determine timezone
    tz_name = os.environ.get("TZ", "America/Los_Angeles")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")

    # Determine date
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target_date = datetime.now(tz).date()

    # Build day boundaries in local timezone
    day_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    # Connect to CalDAV
    caldav_url = os.environ.get("CALDAV_URL", "")
    caldav_user = os.environ.get("CALDAV_USERNAME", "")
    caldav_pass = os.environ.get("CALDAV_PASSWORD", "")

    if not all([caldav_url, caldav_user, caldav_pass]):
        print(json.dumps({"error": "CalDAV credentials not set (CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD)"}))
        conn.close()
        sys.exit(1)

    client = get_caldav_client(caldav_url, caldav_user, caldav_pass)

    # Fetch events from all calendars visible to this client (including shared calendars)
    try:
        calendars = list_calendars(client)
    except Exception as e:
        print(json.dumps({"error": f"Failed to list calendars: {e}"}))
        conn.close()
        sys.exit(1)

    all_events: list[CalendarEvent] = []
    for cal_name, cal_url in calendars:
        try:
            events = get_events(client, cal_url, day_start, day_end)
            all_events.extend(events)
        except Exception:
            continue

    # Filter events
    filtered = []
    for ev in all_events:
        if ev.all_day:
            continue
        if not ev.location:
            continue
        if _is_virtual_location(ev.location):
            continue
        # Optional --event filter
        if args.event:
            query = args.event.lower()
            if query != ev.uid.lower() and query not in ev.summary.lower():
                continue
        filtered.append(ev)

    if not filtered:
        print(json.dumps({"date": str(target_date), "events": []}))
        conn.close()
        return

    # Load known places from DB
    places_rows = conn.execute(
        "SELECT name, lat, lon, radius_meters FROM places WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    places = [dict(r) for r in places_rows]

    default_radius = 200

    results = []
    for ev in filtered:
        # Resolve location to coordinates
        event_lat, event_lon, radius = None, None, default_radius
        source = None

        # Try place match first
        place_match = _match_place(ev.location, places)
        if place_match:
            event_lat = place_match["lat"]
            event_lon = place_match["lon"]
            radius = place_match["radius_meters"]
            source = "place"
        else:
            # Try geocode
            coords = _geocode_location(ev.location, conn)
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

        # Query pings for event time window (with 30min buffer)
        ev_start = ev.start
        ev_end = ev.end
        # Ensure timezone-aware for comparison
        if ev_start.tzinfo is None:
            ev_start = ev_start.replace(tzinfo=tz)
        if ev_end.tzinfo is None:
            ev_end = ev_end.replace(tzinfo=tz)

        # Convert to UTC for DB query (pings stored as UTC ISO strings)
        window_start = (ev_start - timedelta(minutes=30)).astimezone(timezone.utc)
        window_end = (ev_end + timedelta(minutes=30)).astimezone(timezone.utc)
        ping_since = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        ping_until = window_end.strftime("%Y-%m-%dT%H:%M:%SZ")

        pings = db.get_pings(conn, user_id, since=ping_since, until=ping_until, limit=1000)

        # Check proximity
        nearby_pings = []
        for ping in pings:
            dist = haversine(event_lat, event_lon, ping.lat, ping.lon)
            if dist <= radius:
                nearby_pings.append(ping)

        if nearby_pings:
            entry["attended"] = True
            entry["first_nearby_ping"] = nearby_pings[-1].timestamp  # pings are newest-first
            entry["last_nearby_ping"] = nearby_pings[0].timestamp
            entry["nearby_ping_count"] = len(nearby_pings)
        else:
            entry["attended"] = None

        results.append(entry)

    print(json.dumps({"date": str(target_date), "events": results}))
    conn.close()


def build_parser():
    parser = argparse.ArgumentParser(description="Location tracking CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("current", help="Current location and visit")

    hist = sub.add_parser("history", help="Recent location pings")
    hist.add_argument("--limit", type=int, default=20)
    hist.add_argument("--date", help="Filter by date (YYYY-MM-DD)")

    sub.add_parser("places", help="List known places")

    learn = sub.add_parser("learn", help="Save current location as a named place")
    learn.add_argument("name", help="Place name")
    learn.add_argument("--category", default="other", help="Place category")
    learn.add_argument("--radius", type=int, default=100, help="Geofence radius in meters")

    attend = sub.add_parser("attendance", help="Check calendar attendance via GPS")
    attend.add_argument("--date", help="Date to check (YYYY-MM-DD, default: today)")
    attend.add_argument("--event", help="Filter by event UID or title substring")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "current": cmd_current,
        "history": cmd_history,
        "places": cmd_places,
        "learn": cmd_learn,
        "attendance": cmd_attendance,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)
