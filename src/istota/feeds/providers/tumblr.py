"""Tumblr API v2 provider.

Vendored from ``rss-bridger/src/rss_bridger/providers/tumblr.py`` and
adapted to emit :class:`FetchedItem` directly (instead of the bridger's
``FeedItem``). The bridger's job — wrap this output in Atom XML so Miniflux
could subscribe — is no longer needed; the native poller stores these
items straight into SQLite.

Uses ``requests`` rather than ``httpx`` because Tumblr's API edge
disconnects httpx clients without sending a response (likely a TLS / JA3
fingerprint difference). curl, urllib, and requests all work fine. The
upstream rss-bridger made the same choice for the same reason.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import requests

from istota.feeds.models import FetchedItem


PROVIDER_NAME = "tumblr"
TUMBLR_API_BASE = "https://api.tumblr.com/v2/blog"


def fetch(identifier: str, *, api_key: str = "", limit: int = 50) -> list[FetchedItem]:
    """Fetch recent posts from a Tumblr blog.

    Args:
        identifier: Blog name (e.g. ``"nemfrog"`` for ``nemfrog.tumblr.com``).
        api_key: Tumblr API key. Falls back to ``TUMBLR_API_KEY`` env var.
        limit: Max posts to fetch (Tumblr caps at 50 per call).
    """
    key = api_key or os.environ.get("TUMBLR_API_KEY", "")
    if not key:
        raise ValueError("TUMBLR_API_KEY not set")

    limit = min(int(limit), 50)
    url = f"{TUMBLR_API_BASE}/{identifier}/posts"
    params = {"api_key": key, "limit": limit, "npf": "true"}

    resp = requests.get(url, params=params, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()

    posts = data.get("response", {}).get("posts", [])
    items: list[FetchedItem] = []

    for post in posts:
        post_id = str(post.get("id", ""))
        post_url = post.get("post_url") or None
        title = post.get("summary") or post.get("slug") or None

        published_iso: str | None = None
        raw_date = post.get("date")
        if raw_date:
            try:
                published_iso = datetime.strptime(
                    raw_date, "%Y-%m-%d %H:%M:%S %Z"
                ).replace(tzinfo=timezone.utc).isoformat()
            except (ValueError, TypeError):
                published_iso = None

        # NPF content blocks plus reblog trail.
        all_blocks = list(post.get("content", []))
        for trail_entry in post.get("trail", []):
            all_blocks.extend(trail_entry.get("content", []))

        image_urls: list[str] = []
        text_parts: list[str] = []
        for block in all_blocks:
            block_type = block.get("type", "")
            if block_type == "image":
                media = block.get("media") or []
                if media:
                    img = media[0].get("url", "")
                    if img:
                        image_urls.append(img)
            elif block_type == "text":
                text_parts.append(block.get("text", ""))

        items.append(FetchedItem(
            guid=post_id,
            title=(title[:200] if title else None),
            url=post_url,
            content_text=("\n".join(text_parts) if text_parts else None),
            image_urls=image_urls,
            author=identifier,
            published_at=published_iso,
        ))

    return items
