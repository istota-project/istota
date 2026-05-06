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


__version__ = "0.1.0"
__all__ = [
    "FeedsContext",
    "UserNotFoundError",
    "list_users",
    "resolve_for_user",
]
