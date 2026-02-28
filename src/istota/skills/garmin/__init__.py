"""Garmin Connect integration — sync run activities to the Private calendar.

Checks Garmin Connect for running activities on a given date and updates
the Private calendar accordingly:

- RUN event exists + run happened  → add ✅ + stats to event
- RUN event exists + no run        → add ❌ to event
- Run happened, no event           → create new ✅ RUN event with stats
- No run, no event                 → nothing to do

CLI:
    python -m istota.skills.garmin sync [--dry-run] [--date YYYY-MM-DD]

Config:
    Credentials are read from a GARMIN.md file containing a TOML block:

        ```toml
        [garmin]
        email = "your@email.com"
        password = "yourpassword"
        token_dir = "/optional/path/to/token/cache"
        ```

    The config file path is taken from the GARMIN_CONFIG env var,
    defaulting to <NEXTCLOUD_MOUNT>/Users/<user>/zorg/config/GARMIN.md.
"""

import argparse
import json
import os
import re
import sys
import tomllib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import garminconnect

from istota.skills.calendar import (
    CalendarEvent,
    create_event,
    get_caldav_client,
    get_events,
    update_event,
)


DEFAULT_USER_TZ = "America/Los_Angeles"
DEFAULT_PRIVATE_CALENDAR = (
    "https://host.example.com/remote.php/dav/calendars/zorg/"
    "Private%2B%2528Google%2BCalendar%2Bimport%2529_shared_by_alice/"
)


# =============================================================================
# Config
# =============================================================================


def _default_config_path() -> str:
    """Return default GARMIN.md path, using NEXTCLOUD_MOUNT env if available."""
    mount = os.environ.get("NEXTCLOUD_MOUNT", "/srv/mount/nextcloud/content")
    user = os.environ.get("ISTOTA_USER", "alice")
    return os.path.join(mount, "Users", user, "zorg", "config", "GARMIN.md")


def load_config(config_path: str | None = None) -> dict:
    """Parse GARMIN.md and return the [garmin] config block as a dict.

    Raises ValueError if the file has no TOML block or missing email.
    """
    path = config_path or os.environ.get("GARMIN_CONFIG") or _default_config_path()

    with open(path) as f:
        content = f.read()

    match = re.search(r"```toml\n(.*?)```", content, re.DOTALL)
    if not match:
        raise ValueError(f"No TOML block found in {path}")

    cfg = tomllib.loads(match.group(1))
    garmin = cfg.get("garmin", {})

    if not garmin.get("email"):
        raise ValueError(f"Garmin email not configured in {path}")

    return garmin


# =============================================================================
# Garmin Connect
# =============================================================================


def garmin_login(email: str, password: str, token_dir: str) -> garminconnect.Garmin:
    """Authenticate with Garmin Connect, using cached tokens when available."""
    os.makedirs(token_dir, exist_ok=True)

    # Try token-based login first
    try:
        client = garminconnect.Garmin()
        client.login(token_dir)
        return client
    except Exception:
        pass

    # Fall back to email/password
    client = garminconnect.Garmin(email, password)
    client.login()

    try:
        client.garth.dump(token_dir)
    except Exception:
        pass  # Token caching is a best-effort optimisation

    return client


def get_runs(client: garminconnect.Garmin, target_date: date) -> list[dict]:
    """Return running activities from Garmin Connect for the given date."""
    date_str = target_date.strftime("%Y-%m-%d")
    activities = client.get_activities_by_date(date_str, date_str)
    return [
        a for a in activities
        if a.get("activityType", {}).get("typeKey", "").lower() == "running"
    ]


def format_run_stats(activity: dict) -> str:
    """Format a Garmin activity as a human-readable stats string."""
    distance_m = activity.get("distance") or 0
    distance_km = distance_m / 1000

    duration_s = activity.get("duration") or 0
    duration_min = int(duration_s // 60)
    duration_sec = int(duration_s % 60)

    avg_hr = activity.get("averageHR") or 0

    if distance_km > 0:
        pace_s_per_km = duration_s / distance_km
        pace_min = int(pace_s_per_km // 60)
        pace_sec = int(pace_s_per_km % 60)
        pace_str = f"{pace_min}:{pace_sec:02d} /km"
    else:
        pace_str = "N/A"

    lines = [
        f"Distance: {distance_km:.2f} km",
        f"Duration: {duration_min}:{duration_sec:02d}",
        f"Pace: {pace_str}",
    ]
    if avg_hr:
        lines.append(f"Avg HR: {int(avg_hr)} bpm")

    return "\n".join(lines)


# =============================================================================
# Sync logic
# =============================================================================


def _strip_status(summary: str) -> str:
    """Remove leading ✅ or ❌ from an event summary."""
    return re.sub(r"^[✅❌]\s*", "", summary).strip()


def sync(
    target_date: date,
    calendar_url: str,
    garmin_cfg: dict,
    cal_client,
    dry_run: bool = False,
) -> dict:
    """Perform the Garmin → calendar sync for a single date.

    Returns a result dict with keys: date, action, summary.
    """
    tz_name = DEFAULT_USER_TZ
    tz = ZoneInfo(tz_name)

    day_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    # Fetch calendar events
    cal_events = get_events(cal_client, calendar_url, day_start, day_end)
    run_events = [
        e for e in cal_events
        if _strip_status(e.summary).upper() == "RUN"
    ]

    # Fetch Garmin runs
    token_dir = garmin_cfg.get("token_dir", "/srv/app/istota/data/garmin_tokens")
    garmin_client = garmin_login(
        garmin_cfg["email"],
        garmin_cfg["password"],
        token_dir,
    )
    runs = get_runs(garmin_client, target_date)

    # Determine action
    if not runs and not run_events:
        return {
            "date": target_date.isoformat(),
            "action": "nothing",
            "summary": "No runs and no RUN events",
        }

    if run_events:
        event = run_events[0]
        base_summary = _strip_status(event.summary)

        if runs:
            # Case 1: planned run, completed
            stats = format_run_stats(runs[0])
            new_summary = f"✅ {base_summary}"
            if not dry_run:
                update_event(cal_client, calendar_url, event.uid,
                             summary=new_summary, description=stats)
            return {
                "date": target_date.isoformat(),
                "action": "updated_completed",
                "summary": new_summary,
                "stats": stats,
                "dry_run": dry_run,
            }
        else:
            # Case 2: planned run, missed
            new_summary = f"❌ {base_summary}"
            if not dry_run:
                update_event(cal_client, calendar_url, event.uid, summary=new_summary)
            return {
                "date": target_date.isoformat(),
                "action": "updated_missed",
                "summary": new_summary,
                "dry_run": dry_run,
            }

    # Case 3: unplanned run — create event
    run = runs[0]
    stats = format_run_stats(run)
    summary = "✅ RUN"

    start_local_str = run.get("startTimeLocal", "")
    try:
        run_start = datetime.fromisoformat(start_local_str).replace(tzinfo=tz)
        duration_s = run.get("duration") or 3600
        run_end = run_start + timedelta(seconds=int(duration_s))
    except (ValueError, TypeError):
        run_start = day_start
        run_end = day_end

    uid = None
    if not dry_run:
        uid = create_event(cal_client, calendar_url, summary,
                           run_start, run_end, description=stats)

    return {
        "date": target_date.isoformat(),
        "action": "created",
        "summary": summary,
        "stats": stats,
        "uid": uid,
        "dry_run": dry_run,
    }


# =============================================================================
# CLI
# =============================================================================


def cmd_sync(args) -> dict:
    """Run the Garmin → calendar sync."""
    tz = ZoneInfo(DEFAULT_USER_TZ)

    if args.date:
        target_date = date.fromisoformat(args.date)
    else:
        target_date = datetime.now(tz).date()

    garmin_cfg = load_config(args.config)

    caldav_url = os.environ.get("CALDAV_URL", "https://host.example.com/remote.php/dav")
    caldav_user = os.environ.get("CALDAV_USERNAME", "zorg")
    caldav_pass = os.environ.get("CALDAV_PASSWORD", "")
    cal_client = get_caldav_client(caldav_url, caldav_user, caldav_pass)

    calendar_url = args.calendar or DEFAULT_PRIVATE_CALENDAR

    return sync(target_date, calendar_url, garmin_cfg, cal_client, dry_run=args.dry_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.garmin",
        description="Garmin Connect → CalDAV sync",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Sync Garmin runs to the Private calendar")
    p_sync.add_argument(
        "--date",
        help="Target date (YYYY-MM-DD); defaults to today in LA timezone",
    )
    p_sync.add_argument(
        "--calendar",
        help="CalDAV calendar URL (defaults to the Private calendar)",
    )
    p_sync.add_argument(
        "--config",
        help="Path to GARMIN.md config file (overrides GARMIN_CONFIG env var)",
    )
    p_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {"sync": cmd_sync}

    try:
        result = commands[args.command](args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result.get("status") == "error":
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)
