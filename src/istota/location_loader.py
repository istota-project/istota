"""Load location config from LOCATION.md files and sync places to DB."""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import tomli

from . import db
from .storage import get_user_location_path

logger = logging.getLogger("istota.location_loader")

_TOML_BLOCK_RE = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)


@dataclass
class LocationSettings:
    ingest_token: str = ""
    default_radius: int = 100


@dataclass
class LocationPlace:
    name: str
    lat: float
    lon: float
    radius_meters: int = 100
    category: str = "other"


@dataclass
class LocationAction:
    trigger: str          # "enter", "exit", "dwell"
    place: str
    message: str = ""
    surface: str = "ntfy"  # "ntfy", "talk", "silent", "cron_prompt"
    priority: str = "default"
    prompt: str = ""       # for cron_prompt
    conversation_token: str = ""
    dwell_minutes: int = 0


@dataclass
class LocationConfig:
    settings: LocationSettings = field(default_factory=LocationSettings)
    places: list[LocationPlace] = field(default_factory=list)
    actions: list[LocationAction] = field(default_factory=list)


def load_location_config(config, user_id: str) -> LocationConfig | None:
    """Load location config from a user's LOCATION.md file.

    Returns LocationConfig, or None if file doesn't exist or mount not configured.
    """
    if not config.use_mount:
        return None

    loc_path = config.nextcloud_mount_path / get_user_location_path(
        user_id, config.bot_dir_name
    ).lstrip("/")
    if not loc_path.exists():
        return None

    try:
        content = loc_path.read_text()
        match = _TOML_BLOCK_RE.search(content)
        if not match:
            return LocationConfig()
        toml_str = match.group(1)
        data = tomli.loads(toml_str)
    except Exception as e:
        logger.warning("Failed to parse LOCATION.md for %s: %s", user_id, e)
        return None

    return parse_location_data(data)


def parse_location_data(data: dict) -> LocationConfig:
    """Parse a TOML data dict into a LocationConfig."""
    settings_data = data.get("settings", {})
    settings = LocationSettings(
        ingest_token=settings_data.get("ingest_token", ""),
        default_radius=settings_data.get("default_radius", 100),
    )

    places = []
    for p in data.get("places", []):
        name = p.get("name", "").strip()
        if not name:
            continue
        places.append(LocationPlace(
            name=name,
            lat=p.get("lat", 0.0),
            lon=p.get("lon", 0.0),
            radius_meters=p.get("radius_meters", settings.default_radius),
            category=p.get("category", "other"),
        ))

    actions = []
    for a in data.get("actions", []):
        trigger = a.get("trigger", "").strip()
        place = a.get("place", "").strip()
        if not trigger or not place:
            continue
        actions.append(LocationAction(
            trigger=trigger,
            place=place,
            message=a.get("message", ""),
            surface=a.get("surface", "ntfy"),
            priority=a.get("priority", "default"),
            prompt=a.get("prompt", ""),
            conversation_token=a.get("conversation_token", ""),
            dwell_minutes=a.get("dwell_minutes", 0),
        ))

    return LocationConfig(settings=settings, places=places, actions=actions)


def sync_places_to_db(conn, user_id: str, places: list[LocationPlace]) -> None:
    """Sync places from LOCATION.md into the DB.

    - New places are inserted
    - Existing places are updated
    - Orphaned DB places (not in file) are deleted
    """
    db_places = db.get_places(conn, user_id)
    db_by_name = {p.name: p for p in db_places}
    file_names = {p.name for p in places}

    for fp in places:
        db.upsert_place(
            conn, user_id, fp.name, fp.lat, fp.lon,
            radius_meters=fp.radius_meters,
            category=fp.category,
        )

    for dbp in db_places:
        if dbp.name not in file_names:
            db.delete_place(conn, user_id, dbp.name)
            logger.info(
                "Removed orphaned place '%s' for user %s", dbp.name, user_id,
            )

    conn.commit()


def build_token_user_map(config) -> dict[str, str]:
    """Build a mapping from ingest tokens to user IDs.

    Loads LOCATION.md for every configured user and extracts their ingest_token.
    """
    token_map = {}
    for user_id in config.users:
        loc_config = load_location_config(config, user_id)
        if loc_config and loc_config.settings.ingest_token:
            token_map[loc_config.settings.ingest_token] = user_id
    return token_map
