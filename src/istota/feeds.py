"""Feed page generation via Miniflux API.

Replaces the old feed_poller.py — Miniflux handles RSS aggregation,
this module fetches entries from its API and generates static HTML pages.
"""

import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("istota.feeds")


# ============================================================================
# Data types
# ============================================================================


@dataclass
class FeedItem:
    """A feed item for HTML page generation."""
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


# ============================================================================
# Page generation orchestration
# ============================================================================


def regenerate_feed_page(
    config: "Config",
    user_id: str,
    base_url: str,
    api_key: str,
) -> bool:
    """Regenerate the static feed page for a user from Miniflux entries."""
    if not config.site.enabled or not config.nextcloud_mount_path:
        return False

    try:
        raw_entries = fetch_miniflux_entries(base_url, api_key)
    except Exception as e:
        logger.error("Error fetching Miniflux entries for %s: %s", user_id, e)
        return False

    items = _map_entries(raw_entries, user_id)
    if not items:
        logger.debug("No Miniflux entries for %s, skipping page generation", user_id)
        return False

    # Collect unique feed names in order of first appearance
    seen = set()
    feed_names = []
    for item in items:
        if item.feed_name not in seen:
            feed_names.append(item.feed_name)
            seen.add(item.feed_name)

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
        new_item_count=0,
    )

    site_dir = config.nextcloud_mount_path / "Users" / user_id / config.bot_dir_name / "html"
    feeds_dir = site_dir / "feeds"
    feeds_dir.mkdir(parents=True, exist_ok=True)

    output_path = feeds_dir / "index.html"
    output_path.write_text(page_html)

    try:
        os.chmod(str(output_path), 0o644)
    except OSError:
        pass

    logger.info("Generated feed page for %s: %d items from Miniflux", user_id, len(items))
    return True


def regenerate_feed_pages(config: "Config") -> int:
    """Regenerate static feed pages for all users with Miniflux resources.

    Returns the number of pages regenerated.
    """
    if not config.site.enabled:
        return 0
    if not config.use_mount:
        return 0

    count = 0
    for user_id, user_config in config.users.items():
        if not user_config.site_enabled:
            continue

        miniflux_resources = [
            rc for rc in user_config.resources
            if rc.type == "miniflux" and rc.base_url and rc.api_key
        ]
        if not miniflux_resources:
            continue

        rc = miniflux_resources[0]
        try:
            if regenerate_feed_page(config, user_id, rc.base_url, rc.api_key):
                count += 1
        except Exception as e:
            logger.error("Error regenerating feed page for %s: %s", user_id, e)

    return count


# ============================================================================
# Static page HTML generation (preserved from feed_poller.py)
# ============================================================================

# Display labels for feed source types
_FEED_SOURCE_LABELS = {"rss": "rss", "tumblr": "tumblr", "arena": "are.na"}


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
    return text[:max_len].rsplit(" ", 1)[0] + "\u2026"


# Tags allowed in feed card excerpts (safe inline/block elements)
_ALLOWED_TAGS = {"a", "b", "strong", "i", "em", "br", "p", "ul", "ol", "li", "blockquote", "code", "pre", "img"}


def _sanitize_html(content: str, max_len: int = 0) -> str:
    """Sanitize HTML to allowed tags only, optionally truncate by text length."""
    if not content:
        return ""

    content = html.unescape(content)

    result = []
    text_len = 0
    truncated = False
    i = 0

    while i < len(content) and not truncated:
        if content[i] == "<":
            end = content.find(">", i)
            if end == -1:
                break
            tag_str = content[i:end + 1]
            tag_match = re.match(r"</?(\w+)", tag_str)
            if tag_match and tag_match.group(1).lower() in _ALLOWED_TAGS:
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
                    alt_text = _escape(html.unescape(alt_match.group(1))) if alt_match else ""
                    tag_str = f'<img src="{src}" alt="{alt_text}" loading="lazy">'
                result.append(tag_str)
            i = end + 1
        else:
            if max_len and text_len >= max_len:
                truncated = True
                break
            result.append(_escape(content[i]))
            text_len += 1
            i += 1

    text = "".join(result)
    if truncated:
        text = text.rsplit(" ", 1)[0] + "\u2026"
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

    If the item has a top-level image_url, strip matching <img> tags
    from the excerpt to avoid showing the same image twice.
    """
    if item.content_html:
        content = item.content_html
        if item.image_url:
            for img_url in _parse_image_urls(item.image_url):
                # Remove <img> tags (possibly wrapped in <p>) that match the card image
                content = re.sub(
                    r'<p>\s*<img[^>]+src="' + re.escape(img_url) + r'"[^>]*>\s*</p>',
                    '', content,
                )
                content = re.sub(
                    r'<img[^>]+src="' + re.escape(img_url) + r'"[^>]*>',
                    '', content,
                )
        content = content.strip()
        if not content:
            return ""
        return _sanitize_html(content, max_len=0)

    if item.content_text:
        text = html.unescape(item.content_text)
        text = _escape(text)
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
    return " \u00b7 ".join(parts)


def _build_feed_page_html(
    items: list[FeedItem],
    feed_names: list[str],
    generated_at: str = "",
    new_item_count: int = 0,
    feed_types: dict[str, str] | None = None,
) -> str:
    """Build the complete HTML for the feed reader page."""
    items_html_parts = []
    for item in items:
        images = _parse_image_urls(item.image_url)
        has_image = bool(images)
        item_type = "image" if has_image else "text"
        feed_class = f"feed-{re.sub(r'[^a-z0-9-]', '-', item.feed_name.lower())}"

        card_parts = []

        title_text = _escape(item.title) if item.title else ""

        if has_image:
            alt = _escape(item.title) if item.title else ""
            multi = len(images) > 1
            max_grid = 4
            hidden_count = max(0, len(images) - max_grid) if multi else 0

            img_parts = []
            for idx, img_url in enumerate(images):
                extra_cls = ""
                overlay = ""
                if multi and idx >= max_grid:
                    extra_cls = " gallery-extra"
                elif multi and idx == max_grid - 1 and hidden_count > 0:
                    extra_cls = " gallery-more"
                    overlay = f'<span class="gallery-count">+{hidden_count + 1}</span>'
                img_parts.append(
                    f'<button type="button" class="card-image{extra_cls}" data-full="{_escape(img_url)}">'
                    f'<img src="{_escape(img_url)}" alt="{alt}" loading="lazy">'
                    f'{overlay}'
                    f'</button>'
                )

            # Title overlay on the image area
            title_overlay = ""
            if title_text:
                if item.url:
                    title_overlay = f'<div class="card-title-overlay"><a href="{_escape(item.url)}">{title_text}</a></div>'
                else:
                    title_overlay = f'<div class="card-title-overlay">{title_text}</div>'

            if multi:
                card_parts.append(f'<div class="card-gallery">{"".join(img_parts)}</div>')
            else:
                card_parts.append(img_parts[0])
            if title_overlay:
                card_parts.append(title_overlay)

            # Excerpt below image (no title in card-body for image cards)
            excerpt = _format_excerpt(item)
            if excerpt:
                card_parts.append(f'<div class="card-body"><div class="excerpt">{excerpt}</div></div>')
        else:
            # Text-only cards: title + excerpt in card-body as before
            body_parts = []
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

        meta_parts = []
        meta_parts.append(f'<span class="feed-name">{_escape(item.feed_name)}</span>')
        source_type = (feed_types or {}).get(item.feed_name, "rss")
        meta_parts.append(f'<span class="feed-source">{_FEED_SOURCE_LABELS.get(source_type, source_type)}</span>')
        date_str = item.published_at or item.fetched_at or ""
        if date_str:
            try:
                dt = datetime.fromisoformat(date_str)
                if item.url:
                    meta_parts.append(f'<a href="{_escape(item.url)}" class="meta-link"><time datetime="{date_str}">{dt.strftime("%b %d")}</time></a>')
                else:
                    meta_parts.append(f'<time datetime="{date_str}">{dt.strftime("%b %d")}</time>')
            except (ValueError, TypeError):
                pass
        card_parts.append(f'<div class="meta">{"".join(meta_parts)}</div>')

        published_ts = item.published_at or item.fetched_at or ""
        added_ts = item.fetched_at or ""

        items_html_parts.append(
            f'<article class="card {item_type} {feed_class}"'
            f' data-published="{_escape(published_ts)}"'
            f' data-added="{_escape(added_ts)}">'
            + "".join(card_parts)
            + '</article>'
        )

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
.card-title-overlay{{
  padding:.25rem .6rem;
  background:#161616;
  font-size:.7rem;color:#888;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}}
.card-title-overlay a{{color:#888;text-decoration:none}}
.card-title-overlay a:hover{{color:#ccc}}
.card-body h3{{
  margin:0;padding:.5rem .75rem .25rem;
  font-size:.8rem;font-weight:600;
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
.card .meta .meta-link{{
  color:#666;text-decoration:none;margin-left:auto;
}}
.card .meta .meta-link:hover{{color:#aaa}}

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
  if(b){{e.preventDefault();lbi.src=b.dataset.full;lb.classList.add('open')}}
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
    rules.append(
        '.filters:has([data-type="image"]:not(:checked)) ~ .feed-grid .image'
        '{display:none}'
    )
    rules.append(
        '.filters:has([data-type="text"]:not(:checked)) ~ .feed-grid .text'
        '{display:none}'
    )
    return "\n".join(rules)
