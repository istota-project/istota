"""Per-user location module — GPS pings, places, visits, and the
state-machine tables that back the bot's location features.

Per-user SQLite at ``{workspace}/location/data/location.db``. The
framework ``istota.db`` keeps only the global geocode caches
(``geocode_cache``, ``reverse_geocode_cache``); the five user-scoped
location tables live here. See ``.claude/rules/`` and the spec for
the dual-DB read pattern (skill CLI / web routes that need both).
"""

from istota.location._loader import (
    UserNotFoundError,
    list_users,
    resolve_for_user,
)
from istota.location._migrate import migrate_legacy_data
from istota.location.db import connect, init_db, with_geocode_conn
from istota.location.models import LocationContext


__version__ = "0.1.0"
__all__ = [
    "LocationContext",
    "UserNotFoundError",
    "connect",
    "init_db",
    "list_users",
    "migrate_legacy_data",
    "resolve_for_user",
    "with_geocode_conn",
]
