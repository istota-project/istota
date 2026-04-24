"""FastAPI webhook receiver for istota.

Run as: uvicorn istota.webhook_receiver:app --host 127.0.0.1 --port 8765

Currently handles:
- /webhooks/location — Overland GPS location data
"""

import logging
import signal
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import db
from .config import load_config

logger = logging.getLogger("istota.webhook_receiver")

# Module-level state, populated on startup
_config = None
_token_map: dict[str, str] = {}      # token -> user_id
_places_cache: dict[str, list] = {}   # user_id -> list[Place] (DB objects)
_lock = threading.Lock()

# Hysteresis threshold: consecutive pings at new place before opening a visit
HYSTERESIS_THRESHOLD = 2

# Fallbacks when config hasn't been loaded (e.g., tests that call state-machine
# helpers directly). The webhook path always uses config values.
DEFAULT_ACCURACY_THRESHOLD_M = 100.0
DEFAULT_VISIT_EXIT_MINUTES = 5.0


def _parse_ts(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _get_conn() -> sqlite3.Connection:
    """Get a DB connection using loaded config."""
    conn = sqlite3.connect(_config.db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _get_overland_resource(user_config):
    """Find the first overland resource for a user config."""
    for rc in user_config.resources:
        if rc.type == "overland" and rc.extra.get("ingest_token"):
            return rc
    return None


def reload_config() -> None:
    """Reload config, token map, and places cache."""
    global _config, _token_map, _places_cache
    _config = load_config()
    with _lock:
        token_map = {}
        places_cache = {}
        conn = _get_conn()
        try:
            for user_id, uc in _config.users.items():
                rc = _get_overland_resource(uc)
                if rc:
                    token_map[rc.extra["ingest_token"]] = user_id
                    places_cache[user_id] = db.get_places(conn, user_id)
        finally:
            conn.close()
        _token_map = token_map
        _places_cache = places_cache
    logger.info(
        "Loaded location config: %d user(s) with tokens", len(_token_map),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    reload_config()
    signal.signal(signal.SIGHUP, lambda *_: reload_config())
    yield


app = FastAPI(title="Istota Webhook Receiver", lifespan=lifespan)

location_router = APIRouter(prefix="/webhooks/location")


@location_router.post("")
async def receive_location(
    request: Request,
    token: str = Query(default=""),
):
    """Receive Overland GPS batch payload."""
    # Resolve token from query param or Authorization header
    auth_token = token
    if not auth_token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            auth_token = auth_header[7:].strip()

    if not auth_token:
        return JSONResponse({"error": "missing token"}, status_code=401)

    with _lock:
        user_id = _token_map.get(auth_token)

    if not user_id:
        return JSONResponse({"error": "invalid token"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    locations = body.get("locations", [])
    if not locations:
        return JSONResponse({"result": "ok"})

    conn = _get_conn()
    try:
        # Refresh places from DB (picks up web UI changes without restart)
        places = db.get_places(conn, user_id)
        with _lock:
            _places_cache[user_id] = places

        for feature in locations:
            _process_feature(conn, user_id, feature, places)
        conn.commit()
    except Exception:
        logger.exception("Error processing location batch for %s", user_id)
        conn.rollback()
        return JSONResponse({"error": "processing error"}, status_code=500)
    finally:
        conn.close()

    return JSONResponse({"result": "ok"})


app.include_router(location_router)


def _process_feature(
    conn: sqlite3.Connection,
    user_id: str,
    feature: dict,
    places: list,
) -> None:
    """Process a single GeoJSON Feature from Overland."""
    geom = feature.get("geometry", {})
    coords = geom.get("coordinates", [])
    if len(coords) < 2:
        return

    lon, lat = coords[0], coords[1]
    props = feature.get("properties", {})

    timestamp = props.get("timestamp", datetime.now(timezone.utc).isoformat())

    # Extract motion/activity — Overland uses "motion" array and/or "activity" string
    motion = props.get("motion", [])
    activity = props.get("activity", "")
    if motion and isinstance(motion, list):
        activity_type = motion[0]  # primary motion state
    elif activity:
        activity_type = activity
    else:
        activity_type = None

    speed = props.get("speed")
    if speed is not None and speed < 0:
        speed = None

    course = props.get("course")
    if course is not None and course < 0:
        course = None

    accuracy = props.get("horizontal_accuracy")

    # Accuracy gate: only use good pings for place matching and state updates.
    # Low-accuracy pings are still stored for history so the map isn't empty.
    threshold = (
        _config.location.accuracy_threshold_m
        if _config is not None else DEFAULT_ACCURACY_THRESHOLD_M
    )
    low_accuracy = accuracy is not None and accuracy > threshold

    if low_accuracy:
        place = None
        place_id = None
    else:
        place = resolve_place(lat, lon, places)
        place_id = place.id if place else None

    ping_id = db.insert_location_ping(
        conn, user_id, timestamp, lat, lon,
        altitude=props.get("altitude"),
        accuracy=accuracy,
        speed=speed,
        course=course,
        battery=props.get("battery_level"),
        activity_type=activity_type,
        wifi=props.get("wifi"),
        place_id=place_id,
    )

    if low_accuracy:
        # Don't let a jittery ping move the state machine. The ping keeps its
        # place_id=NULL for history and stats.
        return

    _update_state_machine(conn, user_id, ping_id, place_id, place, timestamp)


def _update_state_machine(
    conn: sqlite3.Connection,
    user_id: str,
    ping_id: int,
    new_place_id: int | None,
    new_place,
    timestamp: str,
) -> None:
    """Run the state machine for visit tracking.

    Uses two asymmetric thresholds:
    - opening a visit: ``HYSTERESIS_THRESHOLD`` consecutive pings at the new
      place (filters walk-bys and single-ping GPS spikes).
    - closing an open visit: continuous "away" time must reach
      ``visit_exit_minutes`` (filters GPS drift while stationary). A single
      ping back at the place resets the away clock.
    """
    state = db.get_location_state(conn, user_id)
    exit_minutes = (
        _config.location.visit_exit_minutes
        if _config is not None else DEFAULT_VISIT_EXIT_MINUTES
    )

    if state is None:
        visit_id = None
        if new_place_id is not None:
            visit_id = db.insert_visit(
                conn, user_id, new_place_id, new_place.name, timestamp,
            )

        db.set_location_state(
            conn, user_id,
            current_place_id=new_place_id,
            current_visit_id=visit_id,
            consecutive_count=1,
            last_ping_place_id=new_place_id,
            exit_started_at=None,
        )
        db.update_ping_place(conn, ping_id, new_place_id, visit_id)
        return

    current_place_id = state.current_place_id
    current_visit_id = state.current_visit_id

    if current_place_id is not None and new_place_id == current_place_id:
        # Back at (or still at) the current place — clear exit timer.
        if current_visit_id is not None:
            db.increment_visit_ping_count(conn, current_visit_id)
        db.set_location_state(
            conn, user_id,
            current_place_id=current_place_id,
            current_visit_id=current_visit_id,
            consecutive_count=0,
            last_ping_place_id=new_place_id,
            exit_started_at=None,
        )
        db.update_ping_place(conn, ping_id, new_place_id, current_visit_id)
        return

    # This ping is away from (or different from) the current place.
    # 1) Check if the current visit should close based on dwell exit.
    # 2) Independently, build up hysteresis for opening a new visit.

    exit_started_at = state.exit_started_at
    should_close = False
    close_exit_ts = timestamp

    if current_visit_id is not None:
        if exit_started_at is None:
            exit_started_at = timestamp
        away_sec = (_parse_ts(timestamp) - _parse_ts(exit_started_at)).total_seconds()
        if away_sec >= exit_minutes * 60:
            should_close = True
            close_exit_ts = exit_started_at

    if new_place_id == state.last_ping_place_id:
        consecutive = state.consecutive_count + 1
    else:
        consecutive = 1

    open_new = (
        new_place_id is not None
        and new_place is not None
        and consecutive >= HYSTERESIS_THRESHOLD
    )

    # Opening at a *different* named place always closes the old visit, even
    # if the dwell threshold isn't met yet — the user clearly moved.
    if open_new and current_visit_id is not None:
        should_close = True
        # If we never recorded an exit start (user teleported directly from
        # place A to place B), fall back to this ping's timestamp.
        close_exit_ts = exit_started_at or timestamp

    if should_close:
        db.close_visit(conn, current_visit_id, close_exit_ts)
        current_place_id = None
        current_visit_id = None
        exit_started_at = None

    if open_new:
        new_visit_id = db.insert_visit(
            conn, user_id, new_place_id, new_place.name, timestamp,
        )
        db.set_location_state(
            conn, user_id,
            current_place_id=new_place_id,
            current_visit_id=new_visit_id,
            consecutive_count=0,
            last_ping_place_id=new_place_id,
            exit_started_at=None,
        )
        db.update_ping_place(conn, ping_id, new_place_id, new_visit_id)
        return

    db.set_location_state(
        conn, user_id,
        current_place_id=current_place_id,
        current_visit_id=current_visit_id,
        consecutive_count=consecutive,
        last_ping_place_id=new_place_id,
        exit_started_at=exit_started_at,
    )
    # Ping keeps its observed place_id; visit_id follows the open visit if any.
    db.update_ping_place(conn, ping_id, new_place_id, current_visit_id)


# =============================================================================
# Haversine distance
# =============================================================================

from istota.geo import haversine  # noqa: E402


def resolve_place(lat: float, lon: float, places: list) -> object | None:
    """Find the nearest place within its radius. Returns Place or None."""
    best = None
    best_dist = float("inf")

    for place in places:
        dist = haversine(lat, lon, place.lat, place.lon)
        if dist <= place.radius_meters and dist < best_dist:
            best = place
            best_dist = dist

    return best
