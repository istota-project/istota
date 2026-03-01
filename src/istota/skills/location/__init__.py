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

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "current": cmd_current,
        "history": cmd_history,
        "places": cmd_places,
        "learn": cmd_learn,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)
