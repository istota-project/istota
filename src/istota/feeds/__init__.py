"""Native feeds module — RSS/Atom/Tumblr/Are.na in-process.

Replaces the previous Miniflux + PostgreSQL + rss-bridger stack. Mirrors the
``istota.money`` package layout: per-user workspace under
``{workspace}/feeds/`` with ``config/FEEDS.toml`` (subscriptions) and
``data/feeds.db`` (entries + read state).

The ``_miniflux`` submodule keeps the legacy briefing client around until the
``[feeds] backend`` flag flips to ``"native"`` for everyone.
"""

from istota.feeds._loader import (
    FeedsContext,
    UserNotFoundError,
    list_users,
    resolve_for_user,
)

# Backwards-compat re-exports — the legacy Miniflux client used to live at
# ``istota.feeds``. Tests and the briefing skill still import from here.
from istota.feeds._miniflux import (
    FeedItem,
    _extract_image_from_content,
    _extract_image_from_enclosures,
    _map_entries,
    fetch_miniflux_entries,
)
from istota.feeds._native_briefing import fetch_native_entries


def fetch_briefing_entries(
    user_id: str, istota_config, limit: int = 500,
) -> list[FeedItem]:
    """Fetch recent feed entries for a briefing, dispatching on the configured backend.

    ``[feeds] backend = "miniflux"`` (default) reads through the legacy HTTP
    client against a Miniflux instance configured on the user's resources.
    ``"native"`` reads directly from the workspace SQLite populated by
    :mod:`istota.feeds.poller`.
    """
    backend = getattr(getattr(istota_config, "feeds", None), "backend", "miniflux")
    if backend == "native":
        return fetch_native_entries(user_id, istota_config, limit=limit)

    uc = istota_config.get_user(user_id) if istota_config else None
    if uc is None:
        return []
    for r in uc.resources:
        if r.type == "miniflux" and r.base_url and r.api_key:
            raw = fetch_miniflux_entries(r.base_url, r.api_key, limit=limit)
            return _map_entries(raw, user_id)
    return []


__version__ = "0.1.0"
__all__ = [
    "FeedItem",
    "FeedsContext",
    "UserNotFoundError",
    "_extract_image_from_content",
    "_extract_image_from_enclosures",
    "_map_entries",
    "fetch_briefing_entries",
    "fetch_miniflux_entries",
    "fetch_native_entries",
    "list_users",
    "resolve_for_user",
]
