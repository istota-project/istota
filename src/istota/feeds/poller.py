"""Polling engine — RSS/Atom + Tumblr/Are.na providers.

Public surface:

* :func:`poll_feed` — single feed, returns :class:`FetchResult`.
* :func:`poll_due_feeds` — scan SQLite for feeds whose ``next_poll_at`` is in
  the past, poll each, persist entries + fetch state.

Conditional GET (etag / last-modified) is honoured for RSS feeds. Errors
back off by doubling ``poll_interval_minutes`` up to ``backoff_max_minutes``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from istota.feeds import db as feeds_db
from istota.feeds.models import (
    DEFAULT_BACKOFF_MAX_MINUTES,
    DEFAULT_POLL_INTERVAL_MINUTES,
    EntryRecord,
    FeedRecord,
    FetchedItem,
    FetchResult,
    detect_source_type,
    provider_identifier,
)
from istota.feeds.providers import arena as arena_provider
from istota.feeds.providers import tumblr as tumblr_provider
from istota.feeds.sanitize import extract_images, html_to_text, sanitize_html


logger = logging.getLogger("istota.feeds.poller")


# -- single-feed polling ------------------------------------------------------


def poll_feed(
    feed: FeedRecord,
    *,
    tumblr_api_key: str = "",
    http_get: Callable | None = None,
) -> FetchResult:
    """Fetch one feed, dispatching by source_type.

    ``http_get`` is the RSS-fetch hook — defaults to ``httpx.get``. Tests
    inject a stub. Tumblr and Are.na providers manage their own HTTP.
    """
    source = feed.source_type or detect_source_type(feed.url)
    try:
        if source == "tumblr":
            ident = provider_identifier(feed.url)
            items = tumblr_provider.fetch(ident, api_key=tumblr_api_key)
            return FetchResult(feed_url=feed.url, items=items)
        if source == "arena":
            ident = provider_identifier(feed.url)
            items = arena_provider.fetch(ident)
            return FetchResult(feed_url=feed.url, items=items)
        return _poll_rss(feed, http_get=http_get)
    except Exception as exc:  # noqa: BLE001 — captured into FetchResult
        logger.warning("poll_feed failed url=%s err=%s", feed.url, exc)
        return FetchResult(feed_url=feed.url, error=str(exc))


def _poll_rss(feed: FeedRecord, *, http_get: Callable | None) -> FetchResult:
    """RSS/Atom poll via feedparser. Conditional GET honoured."""
    if http_get is None:
        import httpx
        http_get = httpx.get

    headers: dict[str, str] = {
        "User-Agent": "istota-feeds/0.1 (+https://github.com/cynium/istota)",
    }
    if feed.etag:
        headers["If-None-Match"] = feed.etag
    if feed.last_modified:
        headers["If-Modified-Since"] = feed.last_modified

    resp = http_get(feed.url, headers=headers, timeout=30.0, follow_redirects=True)
    status = getattr(resp, "status_code", 0)
    if status == 304:
        return FetchResult(feed_url=feed.url, not_modified=True)
    if status >= 400:
        return FetchResult(
            feed_url=feed.url,
            error=f"HTTP {status} fetching feed",
        )

    raw = getattr(resp, "content", None) or getattr(resp, "text", "")
    parsed = _feedparser_parse(raw)

    items: list[FetchedItem] = []
    for entry in parsed.get("entries", []):
        items.append(_rss_entry_to_item(entry))

    new_etag = None
    new_last_modified = None
    resp_headers = getattr(resp, "headers", {}) or {}
    if isinstance(resp_headers, dict):
        new_etag = resp_headers.get("ETag") or resp_headers.get("etag")
        new_last_modified = (
            resp_headers.get("Last-Modified") or resp_headers.get("last-modified")
        )
    else:
        # httpx Headers — case-insensitive get
        new_etag = resp_headers.get("etag")
        new_last_modified = resp_headers.get("last-modified")

    feed_meta = parsed.get("feed", {}) or {}
    return FetchResult(
        feed_url=feed.url,
        items=items,
        etag=new_etag,
        last_modified=new_last_modified,
        discovered_title=feed_meta.get("title"),
        discovered_site_url=feed_meta.get("link"),
    )


def _feedparser_parse(raw):
    """Wrap feedparser so the import lives at call time (optional dep)."""
    try:
        import feedparser  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "feedparser is required for RSS polling — install the 'feeds' extra"
        ) from e
    return feedparser.parse(raw)


def _rss_entry_to_item(entry) -> FetchedItem:
    """Convert a ``feedparser`` entry dict to :class:`FetchedItem`."""
    guid = (
        entry.get("id")
        or entry.get("guid")
        or entry.get("link")
        or entry.get("title", "")
    )
    title = entry.get("title")
    url = entry.get("link")
    author = entry.get("author")

    content_html = None
    content_list = entry.get("content") or []
    if content_list:
        content_html = content_list[0].get("value")
    if not content_html:
        content_html = entry.get("summary") or entry.get("description")

    cleaned_html = sanitize_html(content_html)
    content_text = html_to_text(cleaned_html)

    image_urls = extract_images(cleaned_html)
    for enc in entry.get("enclosures", []) or []:
        mime = (enc.get("type") or "").lower()
        if mime.startswith("image/") and enc.get("href"):
            image_urls.append(enc["href"])
    if entry.get("media_content"):
        for m in entry["media_content"]:
            if m.get("url"):
                image_urls.append(m["url"])

    published_at = _published_iso(entry)

    return FetchedItem(
        guid=str(guid) if guid else "",
        title=title,
        url=url,
        author=author,
        content_html=cleaned_html,
        content_text=content_text,
        image_urls=_dedupe_preserving_order(image_urls),
        published_at=published_at,
    )


def _published_iso(entry) -> str | None:
    """Pull a UTC ISO 8601 timestamp out of a feedparser entry."""
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                # struct_time has no tz; assume UTC (feedparser normalises).
                dt = datetime(*struct[:6], tzinfo=timezone.utc)
                return dt.isoformat()
            except (TypeError, ValueError):
                continue
    return entry.get("published") or entry.get("updated")


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


# -- batch polling ------------------------------------------------------------


def poll_due_feeds(
    conn: sqlite3.Connection,
    *,
    tumblr_api_key: str = "",
    backoff_max_minutes: int = DEFAULT_BACKOFF_MAX_MINUTES,
    now: datetime | None = None,
    http_get: Callable | None = None,
    limit: int | None = None,
) -> list[tuple[FeedRecord, FetchResult, int]]:
    """Poll every feed whose ``next_poll_at`` is in the past.

    Returns a list of ``(feed, result, new_entry_count)`` tuples for callers
    who want to log or surface progress.
    """
    now = now or datetime.now(timezone.utc)
    feeds = feeds_db.feeds_due_for_poll(conn, now=now)
    if limit is not None:
        feeds = feeds[:limit]

    out: list[tuple[FeedRecord, FetchResult, int]] = []
    for feed in feeds:
        result = poll_feed(feed, tumblr_api_key=tumblr_api_key, http_get=http_get)
        new_count = _persist_poll(conn, feed, result, now=now,
                                  backoff_max_minutes=backoff_max_minutes)
        out.append((feed, result, new_count))
    return out


def _persist_poll(
    conn: sqlite3.Connection,
    feed: FeedRecord,
    result: FetchResult,
    *,
    now: datetime,
    backoff_max_minutes: int,
) -> int:
    """Write fetched entries and update fetch state for one poll outcome."""
    fetched_iso = now.isoformat()
    new_count = 0

    if result.error:
        next_interval = _backoff_interval(
            feed.poll_interval_minutes, feed.error_count + 1, backoff_max_minutes,
        )
        next_poll = (now + timedelta(minutes=next_interval)).isoformat()
        feeds_db.update_feed_fetch_state(
            conn, feed.id,
            etag=feed.etag,
            last_modified=feed.last_modified,
            last_fetched_at=fetched_iso,
            last_error=result.error,
            error_count=feed.error_count + 1,
            next_poll_at=next_poll,
        )
        conn.commit()
        return 0

    if not result.not_modified and result.items:
        records = [
            EntryRecord(
                id=0,
                feed_id=feed.id,
                guid=item.guid,
                title=item.title,
                url=item.url,
                author=item.author,
                content_html=item.content_html,
                content_text=item.content_text,
                image_urls=item.image_urls,
                published_at=item.published_at,
                fetched_at=fetched_iso,
                status="unread",
            )
            for item in result.items
            if item.guid
        ]
        new_count = feeds_db.insert_entries(conn, feed.id, records)

    interval = max(feed.poll_interval_minutes, DEFAULT_POLL_INTERVAL_MINUTES)
    next_poll = (now + timedelta(minutes=interval)).isoformat()
    feeds_db.update_feed_fetch_state(
        conn, feed.id,
        etag=result.etag if not result.not_modified else feed.etag,
        last_modified=result.last_modified if not result.not_modified else feed.last_modified,
        last_fetched_at=fetched_iso,
        last_error=None,
        error_count=0,
        next_poll_at=next_poll,
        discovered_title=result.discovered_title,
        discovered_site_url=result.discovered_site_url,
    )
    conn.commit()
    return new_count


def _backoff_interval(base_minutes: int, error_count: int, cap_minutes: int) -> int:
    """Exponential backoff: double on every consecutive error, capped."""
    base = max(base_minutes, 1)
    interval = base * (2 ** max(error_count - 1, 0))
    return min(interval, cap_minutes)
