"""Miniflux API client for istota.

Provides functions to fetch entries and feeds from a Miniflux instance.
Used by the briefing skill for news summaries. The web UI (`web_app.py`)
has its own async client that calls Miniflux directly.
"""

import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger("istota.feeds")


# ============================================================================
# Data types
# ============================================================================


@dataclass
class FeedItem:
    """A feed item from Miniflux."""
    id: int
    user_id: str
    feed_name: str
    item_id: str
    title: str | None
    url: str | None
    content_text: str | None
    content_html: str | None
    image_url: str | None
    author: str | None
    published_at: str | None
    fetched_at: str | None


# ============================================================================
# Miniflux API
# ============================================================================


def _get_miniflux_client(base_url: str, api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"X-Auth-Token": api_key},
        timeout=30.0,
    )


def _extract_image_from_enclosures(enclosures: list[dict] | None) -> str | None:
    """Extract first image URL from Miniflux enclosures."""
    if not enclosures:
        return None
    for enc in enclosures:
        mime = enc.get("mime_type", "")
        if mime.startswith("image/"):
            return enc.get("url")
    return None


def _extract_image_from_content(content_html: str | None) -> str | None:
    """Extract first <img> src from HTML content as fallback."""
    if not content_html:
        return None
    match = re.search(r'<img[^>]+src="([^"]+)"', content_html)
    if match:
        return match.group(1)
    return None


def _map_entries(entries: list[dict], user_id: str) -> list[FeedItem]:
    """Map Miniflux API entries to FeedItem objects."""
    items = []
    for e in entries:
        feed_title = e.get("feed", {}).get("title", "")
        image_url = _extract_image_from_enclosures(e.get("enclosures"))
        if not image_url:
            image_url = _extract_image_from_content(e.get("content"))

        items.append(FeedItem(
            id=e["id"],
            user_id=user_id,
            feed_name=feed_title,
            item_id=str(e["id"]),
            title=e.get("title"),
            url=e.get("url"),
            content_text=None,
            content_html=e.get("content"),
            image_url=image_url,
            author=e.get("author"),
            published_at=e.get("published_at"),
            fetched_at=e.get("created_at"),
        ))
    return items


def fetch_miniflux_entries(
    base_url: str,
    api_key: str,
    limit: int = 500,
) -> list[dict]:
    """Fetch recent entries from Miniflux API."""
    with _get_miniflux_client(base_url, api_key) as client:
        resp = client.get(
            "/v1/entries",
            params={
                "limit": limit,
                "order": "published_at",
                "direction": "desc",
            },
        )
        resp.raise_for_status()
        return resp.json().get("entries", [])
