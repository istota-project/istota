"""Garmin Connect data access layer — general-purpose JSON output.

Exposes Garmin Connect API surface as structured JSON output:

CLI:
    python -m istota.skills.garmin connect
    python -m istota.skills.garmin user
    python -m istota.skills.garmin activities [--date YYYY-MM-DD] [--limit N] [--type TYPE]
    python -m istota.skills.garmin stats [--date YYYY-MM-DD]
    python -m istota.skills.garmin health [--date YYYY-MM-DD]

Config:
    Credentials are read from a GARMIN.md file containing a TOML block:

        ```toml
        [garmin]
        email = "your@email.com"
        password = "yourpassword"
        token_dir = "/optional/path/to/token/cache"
        ```

    The config file path is taken from the GARMIN_CONFIG env var
    (set by executor.py) or the --config CLI flag.
"""

import argparse
import json
import os
import re
import sys
import tomllib
from datetime import date, datetime

import garminconnect


# =============================================================================
# Config
# =============================================================================


def load_config(config_path: str | None = None) -> dict:
    """Parse GARMIN.md and return the [garmin] config block as a dict.

    Raises ValueError if the file has no TOML block or missing email.
    """
    path = config_path or os.environ.get("GARMIN_CONFIG", "")
    if not path:
        raise ValueError("GARMIN_CONFIG env var not set and no --config provided")

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
# Garmin Connect auth
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


def _get_client(args) -> tuple[garminconnect.Garmin, dict]:
    """Load config and return authenticated Garmin client + config dict."""
    garmin_cfg = load_config(getattr(args, "config", None))
    deferred_dir = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    if deferred_dir:
        default_token_dir = os.path.join(deferred_dir, "garmin_tokens")
    else:
        default_token_dir = "/tmp/istota/garmin_tokens"
    token_dir = garmin_cfg.get("token_dir", default_token_dir)
    client = garmin_login(garmin_cfg["email"], garmin_cfg["password"], token_dir)
    return client, garmin_cfg


# =============================================================================
# Subcommand handlers
# =============================================================================


def cmd_connect(args) -> dict:
    """Test authentication and return success/failure + user display name."""
    client, garmin_cfg = _get_client(args)

    try:
        display_name = client.get_full_name()
    except Exception:
        display_name = None

    return {
        "status": "ok",
        "email": garmin_cfg["email"],
        "display_name": display_name,
    }


def cmd_user(args) -> dict:
    """Return user profile and device info from garminconnect."""
    client, _ = _get_client(args)

    profile = {}
    try:
        profile = client.get_user_profile() or {}
    except Exception:
        pass

    devices = []
    try:
        devices = client.get_devices() or []
    except Exception:
        pass

    return {
        "profile": profile,
        "devices": devices,
    }


def cmd_activities(args) -> list:
    """Return list of activities as JSON array."""
    client, _ = _get_client(args)

    if args.date:
        target_date = args.date
    else:
        target_date = date.today().strftime("%Y-%m-%d")

    limit = args.limit if args.limit else 10

    raw = client.get_activities_by_date(target_date, target_date) or []

    # Filter by activity type if requested
    if args.type:
        filter_type = args.type.lower()
        raw = [
            a for a in raw
            if a.get("activityType", {}).get("typeKey", "").lower() == filter_type
        ]

    # Apply limit
    raw = raw[:limit]

    result = []
    for a in raw:
        result.append({
            "activityId": a.get("activityId"),
            "activityName": a.get("activityName"),
            "activityType": a.get("activityType", {}).get("typeKey"),
            "startTimeLocal": a.get("startTimeLocal"),
            "duration": a.get("duration"),
            "distance": a.get("distance"),
            "averageHR": a.get("averageHR"),
            "maxHR": a.get("maxHR"),
            "calories": a.get("calories"),
            "averageSpeed": a.get("averageSpeed"),
        })

    return result


def cmd_stats(args) -> dict:
    """Return daily stats: steps, calories, stress, body battery."""
    client, _ = _get_client(args)

    if args.date:
        target_date = args.date
    else:
        target_date = date.today().strftime("%Y-%m-%d")

    summary = {}
    try:
        summary = client.get_user_summary(target_date) or {}
    except Exception:
        pass

    body_battery = None
    try:
        bb_data = client.get_body_battery(target_date) or []
        if bb_data:
            # Body battery data is a list of readings; take the last charged value
            charged = [
                entry.get("charged")
                for entry in bb_data
                if entry.get("charged") is not None
            ]
            if charged:
                body_battery = charged[-1]
    except Exception:
        pass

    return {
        "date": target_date,
        "steps": summary.get("totalSteps"),
        "totalKilocalories": summary.get("totalKilocalories"),
        "activeKilocalories": summary.get("activeKilocalories"),
        "floorsAscended": summary.get("floorsAscended"),
        "floorsDescended": summary.get("floorsDescended"),
        "stressAvg": summary.get("averageStressLevel"),
        "bodyBattery": body_battery,
    }


def cmd_health(args) -> dict:
    """Return health metrics: resting HR, sleep, HRV."""
    client, _ = _get_client(args)

    if args.date:
        target_date = args.date
    else:
        target_date = date.today().strftime("%Y-%m-%d")

    resting_hr = None
    try:
        hr_data = client.get_rhr_day(target_date) or {}
        resting_hr = hr_data.get("allMetrics", {}).get("metricsMap", {}).get(
            "WELLNESS_RESTING_HEART_RATE", [{}]
        )
        if isinstance(resting_hr, list) and resting_hr:
            resting_hr = resting_hr[0].get("value")
        else:
            resting_hr = None
    except Exception:
        pass

    # Fallback: resting HR from daily summary
    if resting_hr is None:
        try:
            summary = client.get_user_summary(target_date) or {}
            resting_hr = summary.get("restingHeartRate")
        except Exception:
            pass

    sleep_duration = None
    avg_sleep_stress = None
    try:
        sleep_data = client.get_sleep_data(target_date) or {}
        daily = sleep_data.get("dailySleepDTO", {})
        sleep_duration = daily.get("sleepTimeSeconds")
        avg_sleep_stress = daily.get("avgSleepStress")
    except Exception:
        pass

    hrv_weekly = None
    try:
        hrv_data = client.get_hrv_data(target_date) or {}
        hrv_weekly = hrv_data.get("hrvSummary", {}).get("weeklyAvg")
    except Exception:
        pass

    return {
        "date": target_date,
        "restingHeartRate": resting_hr,
        "avgSleepStress": avg_sleep_stress,
        "sleepDuration": sleep_duration,
        "hrvWeeklyAverage": hrv_weekly,
    }


# =============================================================================
# CLI
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.garmin",
        description="Garmin Connect data access layer — outputs JSON",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # connect
    p_connect = sub.add_parser("connect", help="Test authentication and return user info")
    p_connect.add_argument(
        "--config",
        help="Path to GARMIN.md config file (overrides GARMIN_CONFIG env var)",
    )

    # user
    p_user = sub.add_parser("user", help="Return user profile and device info")
    p_user.add_argument(
        "--config",
        help="Path to GARMIN.md config file (overrides GARMIN_CONFIG env var)",
    )

    # activities
    p_activities = sub.add_parser("activities", help="Fetch activity list with filters")
    p_activities.add_argument(
        "--date",
        help="Date to fetch activities for (YYYY-MM-DD); defaults to today",
    )
    p_activities.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of activities to return (default: 10)",
    )
    p_activities.add_argument(
        "--type",
        help="Filter by activity type key (e.g. running, cycling)",
    )
    p_activities.add_argument(
        "--config",
        help="Path to GARMIN.md config file (overrides GARMIN_CONFIG env var)",
    )

    # stats
    p_stats = sub.add_parser("stats", help="Return daily stats (steps, calories, stress, body battery)")
    p_stats.add_argument(
        "--date",
        help="Date to fetch stats for (YYYY-MM-DD); defaults to today",
    )
    p_stats.add_argument(
        "--config",
        help="Path to GARMIN.md config file (overrides GARMIN_CONFIG env var)",
    )

    # health
    p_health = sub.add_parser("health", help="Return health metrics (HR, sleep, HRV)")
    p_health.add_argument(
        "--date",
        help="Date to fetch health metrics for (YYYY-MM-DD); defaults to today",
    )
    p_health.add_argument(
        "--config",
        help="Path to GARMIN.md config file (overrides GARMIN_CONFIG env var)",
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "connect": cmd_connect,
        "user": cmd_user,
        "activities": cmd_activities,
        "stats": cmd_stats,
        "health": cmd_health,
    }

    try:
        result = commands[args.command](args)
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
