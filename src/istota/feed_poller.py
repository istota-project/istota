"""Feed aggregation poller — RSS, Tumblr, Are.na → static HTML reader."""

import html
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import feedparser
import httpx
import requests

if TYPE_CHECKING:
    from .config import Config

from . import db
from .storage import get_user_feeds_path

logger = logging.getLogger("istota.feed_poller")

# Default poll intervals per feed type (minutes)
_DEFAULT_INTERVALS = {
    "rss": 30,
    "tumblr": 180,
    "arena": 60,
}

# Jitter ranges (fraction of interval)
_JITTER = {
    "rss": 0.10,
    "tumblr": 0.30,
    "arena": 0.10,
}

# Display labels for feed source types
_FEED_SOURCE_LABELS = {"rss": "rss", "tumblr": "tumblr", "arena": "are.na"}


# ============================================================================
# Config parsing
# ============================================================================


@dataclass
class FeedConfig:
    name: str
    type: str       # "rss", "tumblr", "arena"
    url: str
    interval_minutes: int


@dataclass
class FeedsConfig:
    feeds: list[FeedConfig]
    tumblr_api_key: str = ""


def parse_feeds_config(path: Path) -> FeedsConfig | None:
    """Parse a FEEDS.md file (markdown with TOML code block). Returns None if empty/missing."""
    if not path.exists():
        return None

    content = path.read_text()
    if not content.strip():
        return None

    # Extract TOML from fenced code block
    import tomli
    toml_match = re.search(r"```toml\s*\n(.*?)```", content, re.DOTALL)
    if not toml_match:
        return None

    toml_text = toml_match.group(1).strip()
    if not toml_text or all(line.lstrip().startswith("#") for line in toml_text.splitlines() if line.strip()):
        return None

    try:
        data = tomli.loads(toml_text)
    except Exception as e:
        logger.error("Failed to parse FEEDS.md at %s: %s", path, e)
        return None

    tumblr_api_key = ""
    tumblr_section = data.get("tumblr", {})
    if isinstance(tumblr_section, dict):
        tumblr_api_key = tumblr_section.get("api_key", "")

    feeds = []
    for f in data.get("feeds", []):
        feed_type = f.get("type", "rss")
        feeds.append(FeedConfig(
            name=f.get("name", ""),
            type=feed_type,
            url=f.get("url", ""),
            interval_minutes=f.get("interval_minutes", _DEFAULT_INTERVALS.get(feed_type, 30)),
        ))

    if not feeds:
        return None

    return FeedsConfig(feeds=feeds, tumblr_api_key=tumblr_api_key)


# ============================================================================
# Provider fetchers
# ============================================================================


def fetch_rss(
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
) -> tuple[list[dict], str | None, str | None]:
    """Fetch RSS/Atom feed. Returns (items, new_etag, new_last_modified).

    Uses conditional GET when etag/last_modified provided.
    """
    kwargs = {}
    if etag:
        kwargs["etag"] = etag
    if last_modified:
        kwargs["modified"] = last_modified

    parsed = feedparser.parse(url, **kwargs)

    status = getattr(parsed, "status", 200)

    # 304 Not Modified — no new content
    if status == 304:
        logger.debug("RSS %s: 304 Not Modified", url)
        return [], etag, last_modified

    logger.debug("RSS %s: HTTP %d, %d entries", url, status, len(parsed.entries))

    new_etag = getattr(parsed, "etag", None) or etag
    new_modified = None
    if hasattr(parsed, "modified"):
        new_modified = parsed.modified
    elif last_modified:
        new_modified = last_modified

    items = []
    for entry in parsed.entries:
        # Extract image from enclosures or media content
        image_url = None
        for enc in getattr(entry, "enclosures", []):
            if hasattr(enc, "type") and enc.type and enc.type.startswith("image/"):
                image_url = enc.href
                break
        if not image_url:
            for mc in getattr(entry, "media_content", []):
                if mc.get("medium") == "image" or (mc.get("type", "") or "").startswith("image/"):
                    image_url = mc.get("url")
                    break

        # Content
        content_html = None
        content_text = None
        if hasattr(entry, "content") and entry.content:
            content_html = entry.content[0].get("value", "")
        elif hasattr(entry, "summary"):
            content_html = entry.summary

        # Fall back to first inline <img> from content
        if not image_url and content_html:
            img_match = re.search(r'<img[^>]+src="([^"]+)"', content_html)
            if img_match:
                image_url = img_match.group(1)

        # Published date
        published = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
        elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
            try:
                published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass

        if content_html:
            # Convert block/break tags to newlines before stripping
            _text = re.sub(r"<br\s*/?>", "\n", content_html)
            _text = re.sub(r"</p>\s*<p[^>]*>", "\n\n", _text)
            content_text = re.sub(r"<[^>]+>", "", _text).strip()

        items.append({
            "item_id": getattr(entry, "id", None) or getattr(entry, "link", ""),
            "title": getattr(entry, "title", None),
            "url": getattr(entry, "link", None),
            "content_text": content_text,
            "content_html": content_html,
            "image_url": image_url,
            "author": getattr(entry, "author", None),
            "published_at": published,
        })

    return items, new_etag, new_modified


def fetch_tumblr(
    blog_name: str,
    api_key: str,
    offset: int = 0,
) -> list[dict]:
    """Fetch recent Tumblr posts via API v2.

    Uses offset-based pagination (since_id is not supported by this endpoint).
    Returns up to 20 posts starting from the given offset.
    """
    url = f"https://api.tumblr.com/v2/blog/{blog_name}/posts"
    params: dict = {"api_key": api_key, "limit": 20, "npf": "true"}
    if offset > 0:
        params["offset"] = offset

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    posts = data.get("response", {}).get("posts", [])
    # Log rate-limit headers when present
    rl_day = resp.headers.get("X-Ratelimit-Perday-Remaining")
    rl_hour = resp.headers.get("X-Ratelimit-Perhour-Remaining")
    if rl_day is not None or rl_hour is not None:
        logger.info(
            "Tumblr %s: HTTP %d, %d posts (offset %d) — rate-limit remaining: %s/day, %s/hour",
            blog_name, resp.status_code, len(posts), offset, rl_day, rl_hour,
        )
    else:
        logger.debug("Tumblr %s: HTTP %d, %d posts (offset %d)", blog_name, resp.status_code, len(posts), offset)

    items = []
    for post in posts:
        post_id = str(post.get("id", ""))
        post_url = post.get("post_url", "")
        title = post.get("summary", "") or post.get("slug", "")
        published = None
        raw_date = post.get("date")
        if raw_date:
            try:
                published = datetime.strptime(
                    raw_date, "%Y-%m-%d %H:%M:%S %Z"
                ).replace(tzinfo=timezone.utc).isoformat()
            except (ValueError, TypeError):
                published = raw_date

        # Extract image and text from NPF content blocks.
        # For reblogs, content lives in trail[].content[] instead.
        all_blocks = list(post.get("content", []))
        for trail_entry in post.get("trail", []):
            all_blocks.extend(trail_entry.get("content", []))

        image_urls = []
        text_parts = []
        for block in all_blocks:
            block_type = block.get("type", "")
            if block_type == "image":
                media = block.get("media", [])
                if media:
                    img = media[0].get("url", "")
                    if img:
                        image_urls.append(img)
            elif block_type == "text":
                text_parts.append(block.get("text", ""))

        content_text = "\n".join(text_parts) if text_parts else None

        # Store as JSON array for multiple images, plain string for single
        if len(image_urls) > 1:
            image_url = json.dumps(image_urls)
        elif image_urls:
            image_url = image_urls[0]
        else:
            image_url = None

        items.append({
            "item_id": post_id,
            "title": title[:200] if title else None,
            "url": post_url,
            "content_text": content_text,
            "content_html": None,
            "image_url": image_url,
            "author": blog_name,
            "published_at": published,
        })

    return items


def _arena_image_url(image_data: dict | None) -> str | None:
    """Get the original image URL from an Are.na image object.

    The ``display`` key returns a base64-encoded transform URL that serves
    resized webp.  The ``original`` key points to the actual file on
    CloudFront.  Strip the ``?bc=0`` cache-buster so browsers negotiate
    format via Accept header (returns JPEG/PNG instead of forced webp).
    """
    if not image_data:
        return None
    url = image_data.get("original", {}).get("url") or image_data.get("display", {}).get("url")
    if not url:
        return None
    return url.split("?")[0]


def fetch_arena(
    channel_slug: str,
    since_id: str | None = None,
) -> list[dict]:
    """Fetch recent Are.na channel contents."""
    url = f"https://api.are.na/v2/channels/{channel_slug}/contents"
    params = {"per": 20, "sort": "position", "direction": "desc"}

    resp = httpx.get(url, params=params, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()

    contents = data.get("contents", [])
    logger.debug("Are.na %s: HTTP %d, %d blocks", channel_slug, resp.status_code, len(contents))

    items = []
    for block in contents:
        block_id = str(block.get("id", ""))

        # Skip items we've already seen (compare as integers for Are.na IDs)
        if since_id:
            try:
                if int(block_id) <= int(since_id):
                    continue
            except (ValueError, TypeError):
                if block_id <= since_id:
                    continue

        block_class = block.get("class", "")
        title = block.get("title", None)
        source_url = block.get("source", {}).get("url") if block.get("source") else None

        image_url = None
        content_text = None

        if block_class == "Image":
            image_url = _arena_image_url(block.get("image"))
        elif block_class == "Text":
            content_text = block.get("content", "")
        elif block_class == "Link":
            image_url = _arena_image_url(block.get("image"))
            content_text = block.get("description", "")

        published = block.get("connected_at") or block.get("created_at")
        author = None
        if block.get("user"):
            author = block["user"].get("full_name") or block["user"].get("slug")

        items.append({
            "item_id": block_id,
            "title": title,
            "url": source_url,
            "content_text": content_text,
            "content_html": None,
            "image_url": image_url,
            "author": author,
            "published_at": published,
        })

    return items


# ============================================================================
# Polling orchestration
# ============================================================================


def _interval_elapsed(state: db.FeedState | None, feed: FeedConfig) -> bool:
    """Check if enough time has passed since last poll, with jitter."""
    if state is None or state.last_poll_at is None:
        return True

    try:
        last = datetime.fromisoformat(state.last_poll_at)
    except (ValueError, TypeError):
        return True

    now = datetime.now(timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    elapsed_minutes = (now - last).total_seconds() / 60.0
    jitter_frac = _JITTER.get(feed.type, 0.10)
    jitter = feed.interval_minutes * random.uniform(-jitter_frac, jitter_frac)
    threshold = feed.interval_minutes + jitter

    return elapsed_minutes >= threshold


def _poll_single_feed(
    conn, user_id: str, feed: FeedConfig, feeds_config: FeedsConfig,
) -> int:
    """Poll one feed and insert new items. Returns count of new items."""
    state = db.get_feed_state(conn, user_id, feed.name)

    if not _interval_elapsed(state, feed):
        logger.debug("Feed %s/%s: skipped, interval not elapsed", user_id, feed.name)
        return 0

    try:
        if feed.type == "rss":
            items, new_etag, new_modified = fetch_rss(
                feed.url,
                etag=state.etag if state else None,
                last_modified=state.last_modified if state else None,
            )
            extra_state = {}
            if new_etag:
                extra_state["etag"] = new_etag
            if new_modified:
                extra_state["last_modified"] = new_modified
        elif feed.type == "tumblr":
            if not feeds_config.tumblr_api_key:
                logger.warning("Tumblr feed %s requires api_key in [tumblr] section", feed.name)
                return 0
            # Paginate: fetch pages of 20 until we stop finding new items.
            # The Tumblr posts endpoint does NOT support since_id — only
            # offset-based pagination — so we fetch successive pages and
            # stop once most of a page is already in the DB.
            items = []
            seen_ids: set[str] = set()  # Track IDs from earlier pages
            max_pages = 5  # Cap at 100 posts per poll cycle
            for page in range(max_pages):
                page_items = fetch_tumblr(
                    feed.url,
                    feeds_config.tumblr_api_key,
                    offset=page * 20,
                )
                if not page_items:
                    break
                items.extend(page_items)
                # Check how many from this page are actually new
                page_new = 0
                for item in page_items:
                    iid = item["item_id"]
                    if iid not in seen_ids and not db.feed_item_exists(
                        conn, user_id, feed.name, iid,
                    ):
                        page_new += 1
                    seen_ids.add(iid)
                # Stop if this was a partial page (end of blog)
                if len(page_items) < 20:
                    break
                # Stop if less than half the page was new — we've caught up
                if page_new < len(page_items) // 2:
                    break
            extra_state = {}
        elif feed.type == "arena":
            items = fetch_arena(
                feed.url,
                since_id=state.last_item_id if state else None,
            )
            extra_state = {}
        else:
            logger.warning("Unknown feed type %r for feed %s", feed.type, feed.name)
            return 0

    except Exception as e:
        errors = (state.consecutive_errors + 1) if state else 1
        db.update_feed_state(
            conn, user_id, feed.name,
            last_poll_at=datetime.now(timezone.utc).isoformat(),
            consecutive_errors=errors,
            last_error=str(e)[:500],
        )
        logger.warning(
            "Feed %s/%s: fetch error (consecutive: %d): %s",
            user_id, feed.name, errors, e,
        )
        return 0

    new_count = 0
    latest_id = state.last_item_id if state else None
    for item in items:
        inserted = db.insert_feed_item(
            conn, user_id, feed.name,
            item_id=item["item_id"],
            title=item.get("title"),
            url=item.get("url"),
            content_text=item.get("content_text"),
            content_html=item.get("content_html"),
            image_url=item.get("image_url"),
            author=item.get("author"),
            published_at=item.get("published_at"),
        )
        if inserted:
            new_count += 1
            # Track newest item ID (first new item seen, since APIs return newest first)
            if latest_id is None:
                latest_id = item["item_id"]

    # Update state
    state_update = {
        "last_poll_at": datetime.now(timezone.utc).isoformat(),
        "consecutive_errors": 0,
        "last_error": None,
    }
    if latest_id:
        state_update["last_item_id"] = latest_id
    if feed.type == "rss":
        state_update.update(extra_state)
    db.update_feed_state(conn, user_id, feed.name, **state_update)

    # Clear previous error state
    if state and state.consecutive_errors > 0:
        logger.info(
            "Feed %s/%s: recovered after %d consecutive errors",
            user_id, feed.name, state.consecutive_errors,
        )

    fetched = len(items)
    if new_count > 0:
        logger.info("Feed %s/%s: %d new item(s) (fetched %d)", user_id, feed.name, new_count, fetched)
    else:
        logger.debug("Feed %s/%s: 0 new items (fetched %d)", user_id, feed.name, fetched)

    return new_count


def check_feeds(config: "Config") -> int:
    """Poll all feeds for all users. Returns total new item count."""
    if not config.site.enabled:
        return 0
    if not config.use_mount:
        return 0

    total_new = 0
    users_polled = 0

    for user_id, user_config in config.users.items():
        if not user_config.site_enabled:
            continue

        feeds_path = config.nextcloud_mount_path / get_user_feeds_path(user_id, config.bot_dir_name).lstrip("/")
        feeds_config = parse_feeds_config(feeds_path)
        if not feeds_config:
            continue

        users_polled += 1
        user_new = 0
        with db.get_db(config.db_path) as conn:
            for feed in feeds_config.feeds:
                user_new += _poll_single_feed(conn, user_id, feed, feeds_config)

        if user_new > 0:
            try:
                generate_static_feed_page(config, user_id, new_item_count=user_new)
            except Exception as e:
                logger.error("Error generating feed page for %s: %s", user_id, e)

        total_new += user_new

    logger.debug("Feed poll cycle: %d user(s), %d new item(s) total", users_polled, total_new)
    return total_new


# ============================================================================
# Static page generation
# ============================================================================


def _escape(text: str | None) -> str:
    """HTML-escape text, return empty string for None."""
    if text is None:
        return ""
    return html.escape(text, quote=True)


def _truncate(text: str | None, max_len: int = 300) -> str:
    """Truncate text and add ellipsis if needed."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "…"


# Tags allowed in feed card excerpts (safe inline/block elements)
_ALLOWED_TAGS = {"a", "b", "strong", "i", "em", "br", "p", "ul", "ol", "li", "blockquote", "code", "pre", "img"}


def _sanitize_html(content: str, max_len: int = 0) -> str:
    """Sanitize HTML to allowed tags only, optionally truncate by text length.

    Preserves safe tags (links, emphasis, line breaks, lists) while
    stripping everything else. Set max_len=0 for no truncation.
    """
    if not content:
        return ""

    # Unescape any double-encoded entities first
    content = html.unescape(content)

    result = []
    text_len = 0
    truncated = False
    i = 0

    while i < len(content) and not truncated:
        if content[i] == "<":
            # Find end of tag
            end = content.find(">", i)
            if end == -1:
                break
            tag_str = content[i:end + 1]
            # Extract tag name (handle closing tags and attributes)
            tag_match = re.match(r"</?(\w+)", tag_str)
            if tag_match and tag_match.group(1).lower() in _ALLOWED_TAGS:
                # For <a> tags, keep only href attribute
                if tag_match.group(1).lower() == "a" and not tag_str.startswith("</"):
                    href_match = re.search(r'href="([^"]*)"', tag_str)
                    if href_match:
                        tag_str = f'<a href="{_escape(html.unescape(href_match.group(1)))}">'
                    else:
                        tag_str = "<a>"
                elif tag_match.group(1).lower() == "img":
                    src_match = re.search(r'src="([^"]*)"', tag_str)
                    alt_match = re.search(r'alt="([^"]*)"', tag_str)
                    src = _escape(html.unescape(src_match.group(1))) if src_match else ""
                    alt = _escape(html.unescape(alt_match.group(1))) if alt_match else ""
                    tag_str = f'<img src="{src}" alt="{alt}" loading="lazy">'
                result.append(tag_str)
            i = end + 1
        else:
            # Text content
            if max_len and text_len >= max_len:
                truncated = True
                break
            result.append(_escape(content[i]))
            text_len += 1
            i += 1

    text = "".join(result)
    if truncated:
        text = text.rsplit(" ", 1)[0] + "…"
    return text.strip()


def _parse_image_urls(image_url: str | None) -> list[str]:
    """Parse image_url field — may be a plain URL or a JSON array of URLs."""
    if not image_url:
        return []
    if image_url.startswith("["):
        try:
            urls = json.loads(image_url)
            return [u for u in urls if isinstance(u, str) and u]
        except (json.JSONDecodeError, TypeError):
            pass
    return [image_url]


def _format_excerpt(item) -> str:
    """Format item content for card display.

    Uses content_html when available (sanitized), otherwise falls back
    to content_text with newlines converted to <br>. No truncation —
    CSS handles overflow in grid view.
    """
    if item.content_html:
        return _sanitize_html(item.content_html, max_len=0)

    if item.content_text:
        text = html.unescape(item.content_text)
        text = _escape(text)
        # Preserve newlines
        text = text.replace("\n", "<br>")
        return text

    return ""


def _build_status_text(generated_at: str, new_item_count: int, total_items: int) -> str:
    """Build the status notice text for the feed page footer."""
    parts = []
    if generated_at:
        parts.append(generated_at)
    if new_item_count > 0:
        parts.append(f"+{new_item_count} new")
    parts.append(f"{total_items} items")
    return " · ".join(parts)


def _build_feed_page_html(
    items: list[db.FeedItem],
    feed_names: list[str],
    generated_at: str = "",
    new_item_count: int = 0,
    feed_types: dict[str, str] | None = None,
) -> str:
    """Build the complete HTML for the feed reader page."""
    # Group items by type for filtering
    items_html_parts = []
    for item in items:
        images = _parse_image_urls(item.image_url)
        has_image = bool(images)
        item_type = "image" if has_image else "text"
        feed_class = f"feed-{re.sub(r'[^a-z0-9-]', '-', item.feed_name.lower())}"

        # Build card content
        card_parts = []

        if has_image:
            alt = _escape(item.title) if item.title else ""
            multi = len(images) > 1
            max_grid = 4  # Show 4 thumbnails in grid view; rest hidden until list view
            hidden_count = max(0, len(images) - max_grid) if multi else 0

            img_parts = []
            for idx, img_url in enumerate(images):
                # Mark images beyond the grid cap
                extra_cls = ""
                overlay = ""
                if multi and idx >= max_grid:
                    extra_cls = " gallery-extra"
                elif multi and idx == max_grid - 1 and hidden_count > 0:
                    extra_cls = " gallery-more"
                    overlay = f'<span class="gallery-count">+{hidden_count + 1}</span>'
                img_parts.append(
                    f'<button class="card-image{extra_cls}" data-full="{_escape(img_url)}">'
                    f'<img src="{_escape(img_url)}" alt="{alt}" loading="lazy">'
                    f'{overlay}'
                    f'</button>'
                )

            if multi:
                card_parts.append(f'<div class="card-gallery">{"".join(img_parts)}</div>')
            else:
                card_parts.append(img_parts[0])

        # Card body wraps title + excerpt so it clips independently of meta
        body_parts = []
        title_text = _escape(item.title) if item.title else ""
        if title_text:
            if item.url:
                body_parts.append(f'<h3><a href="{_escape(item.url)}">{title_text}</a></h3>')
            else:
                body_parts.append(f'<h3>{title_text}</h3>')

        excerpt = _format_excerpt(item)
        if excerpt:
            body_parts.append(f'<div class="excerpt">{excerpt}</div>')

        if body_parts:
            card_parts.append(f'<div class="card-body">{"".join(body_parts)}</div>')

        # Metadata line
        meta_parts = []
        meta_parts.append(f'<span class="feed-name">{_escape(item.feed_name)}</span>')
        source_type = (feed_types or {}).get(item.feed_name, "rss")
        meta_parts.append(f'<span class="feed-source">{_FEED_SOURCE_LABELS.get(source_type, source_type)}</span>')
        date_str = item.published_at or item.fetched_at or ""
        if date_str:
            try:
                dt = datetime.fromisoformat(date_str)
                meta_parts.append(f'<time datetime="{date_str}">{dt.strftime("%b %d")}</time>')
            except (ValueError, TypeError):
                pass
        card_parts.append(f'<div class="meta">{"".join(meta_parts)}</div>')

        # Data attributes for client-side sorting
        published_ts = item.published_at or item.fetched_at or ""
        added_ts = item.fetched_at or ""

        items_html_parts.append(
            f'<article class="card {item_type} {feed_class}"'
            f' data-published="{_escape(published_ts)}"'
            f' data-added="{_escape(added_ts)}">'
            + "".join(card_parts)
            + '</article>'
        )

    # Build filter checkboxes (type only — images/text)
    filter_parts = [
        '<label class="filter-chip">'
        '<input type="checkbox" checked data-type="image">'
        '<span>images</span>'
        '</label>',
        '<label class="filter-chip">'
        '<input type="checkbox" checked data-type="text">'
        '<span>text</span>'
        '</label>',
    ]

    filters_html = "".join(filter_parts)
    items_html = "".join(items_html_parts)

    # Sort toggle
    sort_toggle_html = (
        '<div class="sort-toggle">'
        '<label class="filter-chip" title="Sort by original publish date">'
        '<input type="radio" name="sort" value="published" checked>'
        '<span>published</span>'
        '</label>'
        '<label class="filter-chip" title="Sort by date added to feed">'
        '<input type="radio" name="sort" value="added">'
        '<span>added</span>'
        '</label>'
        '</div>'
    )

    # View toggle (right-aligned)
    view_toggle_html = (
        '<div class="view-toggle">'
        '<label class="filter-chip" title="Masonry view">'
        '<input type="radio" name="view" value="masonry" checked>'
        '<span>grid</span>'
        '</label>'
        '<label class="filter-chip" title="Single column view">'
        '<input type="radio" name="view" value="column">'
        '<span>list</span>'
        '</label>'
        '</div>'
    )

    return f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feeds</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}
body{{
  margin:0;padding:1.5rem;
  font-family:system-ui,-apple-system,sans-serif;
  background:#111;color:#e0e0e0;
  line-height:1.5;
}}

/* Filter bar */
.filters{{
  display:flex;flex-wrap:wrap;gap:.5rem;
  margin-bottom:1.5rem;
  position:sticky;top:0;z-index:10;
  background:#111;padding:.75rem 0;
  align-items:center;
}}
.filter-feeds{{
  display:flex;flex-wrap:wrap;gap:.5rem;
  flex:1;
}}
.sort-toggle,.view-toggle{{
  display:flex;gap:.25rem;
}}
.view-toggle{{
  margin-left:auto;
}}
.filter-chip{{
  cursor:pointer;
  display:inline-flex;align-items:center;
  padding:.25rem .75rem;
  border:1px solid #333;border-radius:999px;
  font-size:.8rem;
  transition:all .15s;
  user-select:none;
}}
.filter-chip input{{display:none}}
.filter-chip:has(input:checked){{
  background:#e0e0e0;color:#111;
  border-color:#e0e0e0;
}}

/* Grid */
.feed-grid{{
  display:grid;
  grid-template-columns:repeat(auto-fill, minmax(320px, 1fr));
  gap:1rem;
}}
.card{{
  background:#1a1a1a;
  border-radius:.5rem;
  overflow:hidden;
  max-height:420px;
  display:flex;flex-direction:column;
  animation:fade-in linear both;
  animation-timeline:view();
  animation-range:entry 0% entry 30%;
}}
@keyframes fade-in{{
  from{{opacity:0;transform:translateY(1rem)}}
  to{{opacity:1;transform:translateY(0)}}
}}
.card-image{{
  display:flex;justify-content:center;
  cursor:zoom-in;
  border:none;padding:0;background:#0e0e0e;width:100%;
}}
.card-image img{{
  width:100%;display:block;
  max-height:360px;
  object-fit:contain;
  border-radius:.5rem .5rem 0 0;
}}
.card-body{{
  flex:1;min-height:0;overflow:hidden;
}}
.card-gallery{{
  display:grid;
  grid-template-columns:repeat(2,1fr);
  gap:2px;
}}
.card-gallery .card-image img{{
  border-radius:0;
  aspect-ratio:1;
  object-fit:cover;
  max-height:none;
}}
.card-gallery .card-image:first-child img{{
  border-radius:.5rem 0 0 0;
}}
.card-gallery .card-image:nth-child(2) img{{
  border-radius:0 .5rem 0 0;
}}
.card-gallery .card-image:only-child img{{
  border-radius:.5rem .5rem 0 0;
  grid-column:span 2;
  aspect-ratio:auto;
  object-fit:initial;
}}
.gallery-extra{{
  display:none;
}}
.gallery-more{{
  position:relative;
}}
.gallery-count{{
  position:absolute;inset:0;
  display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.55);
  color:#fff;font-size:1.2rem;font-weight:600;
  pointer-events:none;
}}
.card-body h3{{
  margin:0;padding:.75rem .75rem .25rem;
  font-size:.95rem;font-weight:600;
}}
.card-body h3 a{{color:#e0e0e0;text-decoration:none}}
.card-body h3 a:hover{{text-decoration:underline}}
.card-body .excerpt{{
  margin:0;padding:.5rem .75rem;
  font-size:.85rem;color:#bbb;
}}
.card-body .excerpt a{{color:#aaa;text-decoration:underline}}
.card-body .excerpt a:hover{{color:#e0e0e0}}
.card-body .excerpt p{{margin:.5em 0}}
.card-body .excerpt img{{max-width:100%;height:auto;border-radius:.25rem;margin:.5em 0;display:block}}
.card-body .excerpt strong,.card-body .excerpt b{{color:#ddd}}
.card-body .excerpt em,.card-body .excerpt i{{color:#ccc}}
.card .meta{{
  display:flex;gap:.5rem;align-items:center;
  padding:.5rem .75rem;
  font-size:.75rem;color:#666;
  border-top:1px solid #222;
  margin-top:auto;
}}
.card .meta .feed-name{{
  background:#252525;padding:.1rem .4rem;border-radius:.2rem;
}}

/* Lightbox */
.lightbox{{
  display:none;position:fixed;inset:0;z-index:100;
  background:rgba(0,0,0,.9);
  justify-content:center;align-items:center;
  cursor:zoom-out;
}}
.lightbox.open{{display:flex}}
.lightbox img{{
  max-width:90vw;max-height:90vh;
  object-fit:contain;
}}

/* CSS-only filtering via :has() */
{_build_filter_css(feed_names)}

/* List view */
.filters:has([value="column"]:checked) ~ .feed-grid{{
  grid-template-columns:1fr;
  max-width:640px;
  margin:0 auto;
}}
.filters:has([value="column"]:checked) ~ .feed-grid .card{{
  max-height:none;
  overflow:hidden;
}}
.filters:has([value="column"]:checked) ~ .feed-grid .gallery-extra{{
  display:block;
}}
.filters:has([value="column"]:checked) ~ .feed-grid .gallery-count{{
  display:none;
}}
.filters:has([value="column"]:checked) ~ .feed-grid .card-gallery{{
  grid-template-columns:1fr;
}}
.filters:has([value="column"]:checked) ~ .feed-grid .card-image img{{
  max-height:none;
  object-fit:cover;
  border-radius:0;
}}
.filters:has([value="column"]:checked) ~ .feed-grid .card-gallery .card-image img{{
  aspect-ratio:auto;
}}

/* Status notice */
.status-notice{{
  position:fixed;bottom:.75rem;right:.75rem;
  font-size:.7rem;color:#555;
  background:#161616;padding:.3rem .6rem;
  border-radius:.25rem;
  z-index:5;
  pointer-events:none;
}}

@media (max-width:640px){{
  body{{padding:1rem .75rem}}
  .filters{{gap:.35rem;padding:.5rem 0}}
  .filter-feeds{{gap:.35rem}}
  .sort-toggle,.view-toggle{{gap:.15rem}}
  .filter-chip{{font-size:.65rem;padding:.15rem .5rem}}
}}

</style>
</head>
<body>
<div class="status-notice">{_build_status_text(generated_at, new_item_count, len(items))}</div>
<nav class="filters">
<div class="filter-feeds">{filters_html}</div>
{sort_toggle_html}
{view_toggle_html}
</nav>
<main class="feed-grid">
{items_html}
</main>
<div class="lightbox" id="lb"><img></div>
<script>
const lb=document.getElementById('lb'),lbi=lb.querySelector('img');
document.addEventListener('click',e=>{{
  const b=e.target.closest('[data-full]');
  if(b){{lbi.src=b.dataset.full;lb.classList.add('open')}}
}});
lb.addEventListener('click',()=>{{lb.classList.remove('open');lbi.src=''}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape')lb.classList.remove('open')}});
</script>
<script>
(function(){{
  const grid=document.querySelector('.feed-grid');
  document.querySelectorAll('input[name="sort"]').forEach(r=>{{
    r.addEventListener('change',function(){{
      const key='data-'+this.value;
      const cards=[...grid.children];
      cards.sort((a,b)=>(b.getAttribute(key)||'').localeCompare(a.getAttribute(key)||''));
      cards.forEach(c=>grid.appendChild(c));
    }});
  }});
}})();
</script>
</body>
</html>"""


def _build_filter_css(feed_names: list[str]) -> str:
    """Build CSS rules that hide/show cards based on checkbox state."""
    rules = []

    # Type toggles
    rules.append(
        '.filters:has([data-type="image"]:not(:checked)) ~ .feed-grid .image'
        '{display:none}'
    )
    rules.append(
        '.filters:has([data-type="text"]:not(:checked)) ~ .feed-grid .text'
        '{display:none}'
    )

    return "\n".join(rules)


def generate_static_feed_page(
    config: "Config", user_id: str, new_item_count: int = 0,
) -> bool:
    """Generate the static feed reader HTML page. Returns True on success."""
    if not config.site.enabled or not config.nextcloud_mount_path:
        return False

    site_dir = config.nextcloud_mount_path / "Users" / user_id / config.bot_dir_name / "html"
    feeds_dir = site_dir / "feeds"
    feeds_dir.mkdir(parents=True, exist_ok=True)

    with db.get_db(config.db_path) as conn:
        items = db.get_feed_items(
            conn, user_id, limit=1000,
            max_age_days=config.scheduler.feed_item_retention_days,
        )

    if not items:
        logger.debug("No feed items for %s, skipping page generation", user_id)
        return False

    # Collect unique feed names in order of first appearance
    seen = set()
    feed_names = []
    for item in items:
        if item.feed_name not in seen:
            feed_names.append(item.feed_name)
            seen.add(item.feed_name)

    # Build feed_name → type mapping from FEEDS.md
    feed_types = {}
    feeds_path = config.nextcloud_mount_path / get_user_feeds_path(user_id, config.bot_dir_name).lstrip("/")
    feeds_config = parse_feeds_config(feeds_path)
    if feeds_config:
        for feed in feeds_config.feeds:
            feed_types[feed.name] = feed.type

    user_config = config.get_user(user_id)
    tz_str = user_config.timezone if user_config else "UTC"
    try:
        user_tz = ZoneInfo(tz_str)
    except (KeyError, ValueError):
        user_tz = ZoneInfo("UTC")
    generated_at = datetime.now(tz=user_tz).strftime("%b %d, %H:%M")
    page_html = _build_feed_page_html(
        items, feed_names,
        generated_at=generated_at,
        new_item_count=new_item_count,
        feed_types=feed_types,
    )

    output_path = feeds_dir / "index.html"
    output_path.write_text(page_html)

    # Set permissions: group-readable for www-data
    try:
        os.chmod(str(output_path), 0o644)
    except OSError:
        pass

    logger.info("Generated feed page for %s: %d items", user_id, len(items))
    return True
