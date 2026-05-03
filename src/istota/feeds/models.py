"""Data types for the feeds module.

The ``FeedsContext`` is the per-user runtime handle (paths + parsed config).
Built by :func:`istota.feeds.workspace.synthesize_feeds_context` or the legacy
:func:`istota.feeds._loader.resolve_for_user`. Everything else takes a context
and operates on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# URL-scheme prefixes that bypass the RSS poller and route to native API
# providers instead. These are the same identifiers rss-bridger used; on
# OPML import we rewrite ``http://127.0.0.1:8900/{provider}/{id}/feed.xml``
# to ``{provider}:{id}`` so the same feeds.toml works locally and on
# fresh machines.
PROVIDER_SCHEMES = ("tumblr:", "arena:")

# Default poll cadence and error-backoff cap (minutes).
DEFAULT_POLL_INTERVAL_MINUTES = 30
DEFAULT_BACKOFF_MAX_MINUTES = 24 * 60

# Per-source-type default poll cadence (minutes). Tumblr and Are.na have
# tight per-key / per-IP rate limits, so we poll them less aggressively when
# feeds.toml doesn't override. RSS / Atom go through dozens of separate
# origins so the global default applies.
_SOURCE_TYPE_POLL_DEFAULTS: dict[str, int] = {
    "tumblr": 60,
    "arena": 60,
}


def default_poll_interval_for(source_type: str) -> int:
    """Return the default ``poll_interval_minutes`` for a feed source type."""
    return _SOURCE_TYPE_POLL_DEFAULTS.get(source_type, DEFAULT_POLL_INTERVAL_MINUTES)


@dataclass
class FeedsContext:
    """Per-user runtime handle for the feeds module.

    All paths are absolute. The data dir / config dir / db path are
    materialised by the workspace loader; ``tumblr_api_key`` and other
    credentials come from the user's istota resource extras.
    """
    user_id: str
    data_dir: Path
    config_dir: Path
    config_path: Path        # feeds.toml
    db_path: Path
    tumblr_api_key: str = ""

    def ensure_dirs(self) -> None:
        """Create data/config dirs if they don't exist yet."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class FeedRecord:
    """A row from the ``feeds`` table."""
    id: int
    url: str
    title: str | None
    site_url: str | None
    category_id: int | None
    source_type: str            # 'rss' | 'tumblr' | 'arena'
    etag: str | None
    last_modified: str | None
    last_fetched_at: str | None
    last_error: str | None
    error_count: int
    poll_interval_minutes: int
    next_poll_at: str | None


@dataclass
class CategoryRecord:
    """A row from the ``feed_categories`` table."""
    id: int
    slug: str
    title: str


@dataclass
class EntryRecord:
    """A row from the ``feed_entries`` table."""
    id: int
    feed_id: int
    guid: str
    title: str | None
    url: str | None
    author: str | None
    content_html: str | None
    content_text: str | None
    image_urls: list[str] = field(default_factory=list)
    published_at: str | None = None
    fetched_at: str = ""
    status: str = "unread"      # 'unread' | 'read' | 'removed'
    starred: bool = False
    starred_at: str | None = None


@dataclass
class FetchedItem:
    """A polled item, pre-storage. Producers (RSS poller, Tumblr provider,
    Are.na provider) emit these; the storage layer turns them into
    :class:`EntryRecord` rows.
    """
    guid: str
    title: str | None = None
    url: str | None = None
    author: str | None = None
    content_html: str | None = None
    content_text: str | None = None
    image_urls: list[str] = field(default_factory=list)
    published_at: str | None = None     # ISO 8601 UTC


@dataclass
class FetchResult:
    """Outcome of polling one feed."""
    feed_url: str
    items: list[FetchedItem] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    error: str | None = None
    discovered_title: str | None = None
    discovered_site_url: str | None = None


def detect_source_type(url: str) -> str:
    """Classify a feed URL by how the poller should fetch it."""
    lo = url.lower()
    if lo.startswith("tumblr:"):
        return "tumblr"
    if lo.startswith("arena:"):
        return "arena"
    return "rss"


def provider_identifier(url: str) -> str:
    """Strip the ``provider:`` scheme to get the bare identifier."""
    for scheme in PROVIDER_SCHEMES:
        if url.lower().startswith(scheme):
            return url[len(scheme):]
    return url


def parse_image_urls(raw: Any) -> list[str]:
    """Coerce the DB ``image_urls`` column (JSON or empty) into a list."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        import json
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return [raw] if raw else []
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
        return [str(parsed)]
    return []
