"""Are.na API provider.

Vendored from ``rss-bridger/src/rss_bridger/providers/arena.py`` and
adapted to emit :class:`FetchedItem` directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from istota.feeds.models import FetchedItem


PROVIDER_NAME = "arena"
ARENA_API_BASE = "https://api.are.na/v2/channels"


def fetch(identifier: str, *, limit: int = 50) -> list[FetchedItem]:
    """Fetch recent blocks from an Are.na channel.

    Args:
        identifier: Channel slug (e.g. ``"my-channel"``).
        limit: Max blocks to fetch (Are.na caps at 100 per call).
    """
    limit = min(int(limit), 100)
    url = f"{ARENA_API_BASE}/{identifier}/contents"
    params = {"per": limit, "sort": "position", "direction": "desc"}

    resp = httpx.get(url, params=params, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()

    contents = data.get("contents", [])
    items: list[FetchedItem] = []

    for block in contents:
        block_id = str(block.get("id", ""))
        block_class = block.get("class", "")
        title = block.get("title")
        source = block.get("source") or {}
        source_url = source.get("url")
        arena_url = f"https://www.are.na/block/{block_id}"

        image_urls: list[str] = []
        content_text: str | None = None
        content_html: str | None = None

        if block_class == "Image":
            img = _arena_image_url(block.get("image"))
            if img:
                image_urls.append(img)
        elif block_class == "Text":
            content_text = block.get("content", "")
        elif block_class == "Link":
            img = _arena_image_url(block.get("image"))
            if img:
                image_urls.append(img)
            content_text = block.get("description", "")
            if source_url:
                content_html = f'<p><a href="{source_url}">Source: {source_url}</a></p>'

        published_iso = _parse_datetime(
            block.get("connected_at") or block.get("created_at")
        )
        author = None
        if block.get("user"):
            author = block["user"].get("full_name") or block["user"].get("slug")

        items.append(FetchedItem(
            guid=block_id,
            title=title,
            url=arena_url,
            content_text=content_text,
            content_html=content_html,
            image_urls=image_urls,
            author=author,
            published_at=published_iso,
        ))

    return items


def _arena_image_url(image_data: dict | None) -> str | None:
    """Get the original image URL from an Are.na image object.

    Prefers the original CloudFront URL over the display (resized webp) URL.
    Strips the ``?bc=0`` cache-buster so browsers negotiate format via
    Accept header.
    """
    if not image_data:
        return None
    url = (
        image_data.get("original", {}).get("url")
        or image_data.get("display", {}).get("url")
    )
    if not url:
        return None
    return url.split("?")[0]


def _parse_datetime(value: str | None) -> str | None:
    """Parse ISO 8601 / Are.na datetime to UTC ISO 8601 string."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None
