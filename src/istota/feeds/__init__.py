"""Native feeds module — RSS/Atom/Tumblr/Are.na in-process.

Per-user workspace under ``{workspace}/feeds/data/feeds.db`` — single
SQLite holds subscriptions, categories, entries, and read state. The
legacy ``feeds.toml`` is auto-imported on first touch by
:mod:`istota.feeds._migrate` for users upgrading from the TOML era.
"""

from istota.feeds._loader import (
    FeedsContext,
    UserNotFoundError,
    list_users,
    resolve_for_user,
)
from istota.feeds._migrate import ensure_initialised, migrate_legacy_toml


__version__ = "0.1.0"
__all__ = [
    "FeedsContext",
    "UserNotFoundError",
    "ensure_initialised",
    "list_users",
    "migrate_legacy_toml",
    "resolve_for_user",
]
