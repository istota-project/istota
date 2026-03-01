"""FastAPI receiver for Overland GPS location data.

Run as: uvicorn istota.location_receiver:app --host 127.0.0.1 --port 8765

Auth: token in query param (?token=SECRET) or Authorization: Bearer header.
Token-to-user mapping is built from each user's LOCATION.md ingest_token.
"""

import logging
import math
import signal
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import db
from .config import load_config
from .location_loader import (
    LocationAction,
    LocationPlace,
    build_token_user_map,
    load_location_config,
    sync_places_to_db,
)

logger = logging.getLogger("istota.location_receiver")

# Module-level state, populated on startup
_config = None
_token_map: dict[str, str] = {}      # token -> user_id
_places_cache: dict[str, list] = {}   # user_id -> list[Place] (DB objects)
_actions_cache: dict[str, list[LocationAction]] = {}  # user_id -> actions
_lock = threading.Lock()

# Hysteresis threshold: consecutive pings at new place before transition
HYSTERESIS_THRESHOLD = 2


def _get_conn() -> sqlite3.Connection:
    """Get a DB connection using loaded config."""
    conn = sqlite3.connect(_config.db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def reload_config() -> None:
    """Reload config, token map, and places cache."""
    global _config, _token_map, _places_cache, _actions_cache
    _config = load_config()
    with _lock:
        _token_map = build_token_user_map(_config)
        _places_cache.clear()
        _actions_cache.clear()
        conn = _get_conn()
        try:
            for token, user_id in _token_map.items():
                loc_config = load_location_config(_config, user_id)
                if loc_config:
                    sync_places_to_db(conn, user_id, loc_config.places)
                    _places_cache[user_id] = db.get_places(conn, user_id)
                    _actions_cache[user_id] = loc_config.actions
        finally:
            conn.close()
    logger.info(
        "Loaded location config: %d user(s) with tokens", len(_token_map),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    reload_config()
    signal.signal(signal.SIGHUP, lambda *_: reload_config())
    yield


app = FastAPI(title="Istota Location Receiver", lifespan=lifespan)


@app.post("/location")
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
        with _lock:
            places = _places_cache.get(user_id, [])
            actions = _actions_cache.get(user_id, [])

        for feature in locations:
            _process_feature(conn, user_id, feature, places, actions)
        conn.commit()
    except Exception:
        logger.exception("Error processing location batch for %s", user_id)
        conn.rollback()
        return JSONResponse({"error": "processing error"}, status_code=500)
    finally:
        conn.close()

    return JSONResponse({"result": "ok"})


def _process_feature(
    conn: sqlite3.Connection,
    user_id: str,
    feature: dict,
    places: list,
    actions: list[LocationAction],
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

    # Resolve place
    place = resolve_place(lat, lon, places)
    place_id = place.id if place else None

    # Insert ping
    ping_id = db.insert_location_ping(
        conn, user_id, timestamp, lat, lon,
        altitude=props.get("altitude"),
        accuracy=props.get("horizontal_accuracy"),
        speed=speed,
        course=course,
        battery=props.get("battery_level"),
        activity_type=activity_type,
        wifi=props.get("wifi"),
        place_id=place_id,
    )

    # Run state machine
    _update_state_machine(conn, user_id, ping_id, place_id, place, timestamp, actions)


def _update_state_machine(
    conn: sqlite3.Connection,
    user_id: str,
    ping_id: int,
    new_place_id: int | None,
    new_place,
    timestamp: str,
    actions: list[LocationAction],
) -> None:
    """Run the hysteresis state machine for visit tracking."""
    state = db.get_location_state(conn, user_id)

    if state is None:
        # First ping ever — initialize state
        visit_id = None
        if new_place_id is not None:
            visit_id = db.insert_visit(
                conn, user_id, new_place_id, new_place.name, timestamp,
            )
            _fire_actions(conn, user_id, "enter", new_place.name, actions)

        db.set_location_state(
            conn, user_id,
            current_place_id=new_place_id,
            current_visit_id=visit_id,
            consecutive_count=1,
            last_ping_place_id=new_place_id,
        )
        db.update_ping_place(conn, ping_id, new_place_id, visit_id)
        return

    current_place_id = state.current_place_id
    current_visit_id = state.current_visit_id

    if new_place_id == current_place_id:
        # Same place — reset hysteresis, update visit
        if current_visit_id is not None:
            db.increment_visit_ping_count(conn, current_visit_id)
        db.set_location_state(
            conn, user_id,
            current_place_id=current_place_id,
            current_visit_id=current_visit_id,
            consecutive_count=0,
            last_ping_place_id=new_place_id,
        )
        db.update_ping_place(conn, ping_id, new_place_id, current_visit_id)
        return

    # Different place — check hysteresis
    if new_place_id == state.last_ping_place_id:
        consecutive = state.consecutive_count + 1
    else:
        consecutive = 1

    if consecutive >= HYSTERESIS_THRESHOLD:
        # Transition confirmed
        old_place_name = None
        if current_visit_id is not None:
            # Close old visit
            db.close_visit(conn, current_visit_id, timestamp)
            # Get old place name for exit action
            old_visit = db.get_open_visit(conn, user_id)
            if old_visit is None:
                # Visit was just closed, look it up
                old_places = db.get_places(conn, user_id)
                for p in old_places:
                    if p.id == current_place_id:
                        old_place_name = p.name
                        break

        if old_place_name is None and current_place_id is not None:
            places_list = db.get_places(conn, user_id)
            for p in places_list:
                if p.id == current_place_id:
                    old_place_name = p.name
                    break

        # Fire exit action for old place
        if old_place_name:
            _fire_actions(conn, user_id, "exit", old_place_name, actions)

        # Open new visit
        new_visit_id = None
        if new_place_id is not None and new_place is not None:
            new_visit_id = db.insert_visit(
                conn, user_id, new_place_id, new_place.name, timestamp,
            )
            _fire_actions(conn, user_id, "enter", new_place.name, actions)

        db.set_location_state(
            conn, user_id,
            current_place_id=new_place_id,
            current_visit_id=new_visit_id,
            consecutive_count=0,
            last_ping_place_id=new_place_id,
        )
        db.update_ping_place(conn, ping_id, new_place_id, new_visit_id)
    else:
        # Not enough consecutive pings — don't transition yet
        db.set_location_state(
            conn, user_id,
            current_place_id=current_place_id,
            current_visit_id=current_visit_id,
            consecutive_count=consecutive,
            last_ping_place_id=new_place_id,
        )
        # Ping stays associated with current visit
        db.update_ping_place(conn, ping_id, new_place_id, current_visit_id)


def _fire_actions(
    conn: sqlite3.Connection,
    user_id: str,
    trigger: str,
    place_name: str,
    actions: list[LocationAction],
) -> None:
    """Fire matching actions for a place transition."""
    for action in actions:
        if action.trigger != trigger:
            continue
        if action.place != place_name:
            continue

        message = action.message or f"{trigger.capitalize()}: {place_name}"

        if action.surface == "silent":
            logger.info("Silent action: %s %s for %s", trigger, place_name, user_id)
            continue

        if action.surface == "cron_prompt":
            if action.prompt:
                db.create_task(
                    conn, action.prompt, user_id,
                    source_type="scheduled",
                    conversation_token=action.conversation_token or None,
                )
                logger.info(
                    "Created cron_prompt task for %s: %s %s",
                    user_id, trigger, place_name,
                )
            continue

        # ntfy or talk — use notifications module
        try:
            from .notifications import send_notification

            ntfy_priority = None
            if action.priority == "high":
                ntfy_priority = 4
            elif action.priority == "low":
                ntfy_priority = 2

            send_notification(
                _config, user_id, message,
                surface=action.surface,
                conversation_token=action.conversation_token or None,
                priority=ntfy_priority,
                title=f"Location: {place_name}",
            )
        except Exception:
            logger.exception(
                "Failed to send %s notification for %s", action.surface, user_id,
            )


# =============================================================================
# Haversine distance
# =============================================================================

_EARTH_RADIUS_M = 6_371_000  # meters


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in meters between two lat/lon points."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)

    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_M * c


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
