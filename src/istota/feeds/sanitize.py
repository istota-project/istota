"""HTML sanitization for feed entry content.

Prefer ``bleach`` when available; fall back to a minimal regex-based
stripper so the module imports without the extra dep installed (the
``feeds`` extra pulls bleach in).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

try:
    import bleach  # type: ignore
    _HAS_BLEACH = True
except ImportError:
    _HAS_BLEACH = False


# Tags safe to keep in feed content. Tightening over time is fine, but
# loosening warrants a review.
ALLOWED_TAGS = [
    "a", "abbr", "b", "blockquote", "br", "caption", "cite", "code",
    "dd", "del", "details", "div", "dl", "dt", "em", "figcaption",
    "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img",
    "ins", "kbd", "li", "mark", "ol", "p", "picture", "pre", "q", "s",
    "samp", "small", "source", "span", "strong", "sub", "summary", "sup",
    "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "u",
    "ul", "video",
]

ALLOWED_ATTRS = {
    "a": ["href", "title", "rel"],
    "abbr": ["title"],
    "img": ["src", "alt", "title", "loading", "srcset", "sizes"],
    "source": ["src", "srcset", "type", "media"],
    "video": ["src", "controls", "poster", "preload", "playsinline"],
    "time": ["datetime"],
    "th": ["scope"],
}

ALLOWED_PROTOCOLS = ["http", "https", "mailto", "data"]


def sanitize_html(html: str | None) -> str | None:
    """Sanitise HTML for storage. Returns None for empty input."""
    if not html:
        return None
    if _HAS_BLEACH:
        cleaned = bleach.clean(
            html,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRS,
            protocols=ALLOWED_PROTOCOLS,
            strip=True,
        )
        return cleaned
    return _fallback_sanitize(html)


_SCRIPT_RE = re.compile(r"<\s*script\b[^>]*>.*?<\s*/\s*script\s*>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<\s*style\b[^>]*>.*?<\s*/\s*style\s*>", re.IGNORECASE | re.DOTALL)
_ONEVENT_RE = re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_HREF_RE = re.compile(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.IGNORECASE)


def _fallback_sanitize(html: str) -> str:
    """Minimal sanitiser used when bleach isn't installed.

    Strips ``<script>`` / ``<style>``, inline event handlers, and
    ``javascript:`` URIs. Not as thorough as bleach, but adequate for the
    "let me preview the module without the extra installed" case. Production
    deploys install the ``feeds`` extra and get the real thing.
    """
    out = _SCRIPT_RE.sub("", html)
    out = _STYLE_RE.sub("", out)
    out = _ONEVENT_RE.sub("", out)
    out = _JS_HREF_RE.sub(r"\1=\2#\2", out)
    return out


def html_to_text(html: str | None) -> str | None:
    """Crude HTML-to-text extraction for content_text. Used when feed
    sources only give HTML.
    """
    if not html:
        return None
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


_IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


def extract_images(html: str | None) -> list[str]:
    """Pull all ``<img src="...">`` URLs out of HTML in document order."""
    if not html:
        return []
    return _IMG_SRC_RE.findall(html)


# -- image de-duplication -----------------------------------------------------
#
# Feeds routinely reference the same picture more than once: the Guardian
# emits several ``<media:content>`` variants of one photo at different widths
# (``?width=140`` … ``?width=700``), and WordPress feeds (PetaPixel) embed the
# lead image in the article body while also surfacing it as the hero. The
# helpers below let the RSS poller collapse resolution variants and drop the
# hero copy from the body without touching genuine inline images.

# Query params that only vary the rendered size/signature of an image, never
# its identity. Two URLs that share a path and differ only in these are the
# same image.
_RESIZE_QUERY_KEYS = frozenset(
    {
        "width", "w", "height", "h", "quality", "q", "dpr", "fit", "auto",
        "s", "sig", "signature", "crop", "resize", "size", "fm", "format",
    }
)

_IMG_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|avif|bmp|svg|tiff?)$", re.IGNORECASE)


def image_identity(url: str) -> str:
    """A dedup key that treats resolution variants of one image as equal.

    For a URL whose path ends in an image extension, the identity is
    ``netloc + path`` — the query string (width, quality, CDN signature) is
    ignored, so ``…/3049.jpg?width=140`` and ``…/3049.jpg?width=700`` collapse.
    For anything else (e.g. ``image.php?id=1``) the full URL is the identity,
    so images distinguished purely by query are not wrongly merged.
    """
    if not url:
        return url
    parsed = urlparse(url)
    if _IMG_EXT_RE.search(parsed.path):
        return f"{parsed.netloc}{parsed.path}"
    return url


def _url_width(url: str) -> int:
    """Best-effort pixel width from a resize query param (0 if absent)."""
    query = parse_qs(urlparse(url).query)
    for key in ("width", "w"):
        values = query.get(key)
        if values:
            try:
                return int(values[0])
            except (ValueError, TypeError):
                continue
    return 0


def dedupe_image_variants(urls: list[str]) -> list[str]:
    """Collapse resolution variants of the same image, keeping the widest.

    Order is preserved by first occurrence of each distinct image. Within a
    group of variants the widest (largest ``width=``/``w=``) URL wins, so the
    hero renders at the best available resolution.
    """
    best: dict[str, tuple[int, str]] = {}
    order: list[str] = []
    for url in urls:
        ident = image_identity(url)
        width = _url_width(url)
        if ident not in best:
            best[ident] = (width, url)
            order.append(ident)
        elif width > best[ident][0]:
            best[ident] = (width, url)
    return [best[ident][1] for ident in order]


_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC_ATTR_RE = re.compile(r'src\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_EMPTY_ANCHOR_RE = re.compile(r"<a\b[^>]*>\s*</a>", re.IGNORECASE)
_EMPTY_WRAPPER_RE = re.compile(
    r"<(p|figure|picture|div|span)\b[^>]*>\s*</\1>", re.IGNORECASE
)


def remove_images(html: str | None, urls: list[str]) -> str | None:
    """Remove ``<img>`` tags whose src matches any URL in ``urls``.

    Matching is by :func:`image_identity`, so a body image is dropped even if
    it's stored at a different resolution than the hero. Only the listed
    images are removed — every other inline image stays put. Wrapper elements
    (``<a>``, ``<p class="feature-image">``, ``<figure>``) left empty by the
    removal are cleaned up so no dangling link or blank block remains.
    """
    if not html or not urls:
        return html
    targets = {image_identity(u) for u in urls}

    def _strip(match: re.Match) -> str:
        tag = match.group(0)
        src = _SRC_ATTR_RE.search(tag)
        if src and image_identity(src.group(1)) in targets:
            return ""
        return tag

    out = _IMG_TAG_RE.sub(_strip, html)
    # Collapse wrappers our removal emptied; loop for nesting (a → p → figure).
    for _ in range(3):
        collapsed = _EMPTY_ANCHOR_RE.sub("", out)
        collapsed = _EMPTY_WRAPPER_RE.sub("", collapsed)
        if collapsed == out:
            break
        out = collapsed
    return out
