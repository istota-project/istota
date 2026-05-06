"""Native feeds module — RSS/Atom/Tumblr/Are.na in-process.

Mirrors the ``istota.money`` package layout: per-user workspace under
``{workspace}/feeds/`` with ``config/feeds.toml`` (subscriptions) and
``data/feeds.db`` (entries + read state).
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
