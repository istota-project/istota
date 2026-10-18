"""HTML sanitization for feed entry content.

``bleach`` does the sanitising. Without it (an install lacking the ``feeds``
extra) no HTML is kept at all: the body is reduced to escaped text, because
a regex blacklist cannot be made safe (ISSUE-780).
"""

from __future__ import annotations

import html as html_module
import re
from functools import lru_cache
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
    # `loop`/`muted` describe how a clip plays once started and are how a
    # GIF-as-mp4 is authored; they carry no risk. `autoplay` is deliberately
    # absent — a grid of cards that all start playing on scroll is a different
    # thing from letting the reader play one. `width`/`height` stay out too:
    # the card and the reader bound a video with CSS, and a stored width is
    # what made a clip run off the side of its container.
    "video": ["src", "controls", "poster", "preload", "playsinline", "loop", "muted"],
    "time": ["datetime"],
    "th": ["scope"],
}

# Checked by bleach on every URL attribute. `data:` is here for the media
# tags only; `_allow_attribute` keeps it off a link.
ALLOWED_PROTOCOLS = ["http", "https", "mailto", "data"]

LINK_PROTOCOLS = frozenset({"http", "https", "mailto"})

# What a browser ignores inside a URL scheme: C0 controls, space, DEL.
_URL_IGNORED_RE = re.compile(r"[\x00-\x20\x7f]+")


def _link_scheme_allowed(value: str) -> bool:
    """Whether an ``<a href>`` value is relative or uses a link protocol.

    bleach hands the value over with its character references intact, so
    they are decoded first. bleach's own protocol check still runs after.
    """
    normalized = _URL_IGNORED_RE.sub("", html_module.unescape(value)).lower()
    scheme, sep, _ = normalized.partition(":")
    if not sep or any(ch in scheme for ch in "/?#"):
        return True
    return scheme in LINK_PROTOCOLS


def _allow_attribute(tag: str, name: str, value: str) -> bool:
    if name not in ALLOWED_ATTRS.get(tag, ()):
        return False
    if tag == "a" and name == "href":
        return _link_scheme_allowed(value)
    return True


def sanitize_html(html: str | None) -> str | None:
    """Sanitise HTML for storage and for serving. Returns None for empty input.

    Without bleach the result is escaped text with no markup at all.
    """
    if not html:
        return None
    if not _HAS_BLEACH:
        return _text_only(html)
    if len(html) > _CACHED_BODY_MAX_CHARS:
        return _bleach_clean(html)
    return _bleach_clean_cached(html)


# The feeds API re-sanitises every stored body it serves, and the web
# reader's page of entries is mostly the bodies its last request served.
# Large bodies (inline `data:` media) stay out so the cache stays small.
_CACHED_BODY_MAX_CHARS = 64 * 1024


@lru_cache(maxsize=256)
def _bleach_clean_cached(html: str) -> str:
    return _bleach_clean(html)


def _bleach_clean(html: str) -> str:
    cleaned = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=_allow_attribute,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    return _ensure_video_controls(cleaned)


_VIDEO_TAG_RE = re.compile(r"<video\b([^>]*)>", re.IGNORECASE)
_CONTROLS_ATTR_RE = re.compile(r"(?:^|\s)controls(?:[\s=]|$)", re.IGNORECASE)


def _ensure_video_controls(html: str) -> str:
    """Give every ``<video>`` a way to be started.

    A clip is routinely authored as an autoplaying muted loop with no
    controls — a GIF-as-mp4 is not meant to look like a video player. Our
    allowlist drops ``autoplay``, so left alone that markup stores as a tag
    nothing can start: a dead black box in the card and the reader.

    Runs after sanitisation, so it only ever sees tags the allowlist already
    passed — escaped ``&lt;video&gt;`` in prose is text by then and does not
    match.
    """
    def _add(match: re.Match) -> str:
        attrs = match.group(1)
        if _CONTROLS_ATTR_RE.search(attrs):
            return match.group(0)
        return f"<video controls{attrs}>"

    return _VIDEO_TAG_RE.sub(_add, html)


_RAW_TEXT_OPEN_RE = re.compile(r"<\s*(script|style)\b", re.IGNORECASE)


def _drop_raw_text_elements(html: str) -> str:
    """Remove ``<script>`` and ``<style>`` elements, bodies included.

    A linear scan: a lazy regex rescans to the end of the input for every
    unclosed opening tag, which is quadratic on hostile input. An unclosed
    element runs to the end of the input, as it does in a browser.
    """
    lower = html.lower()
    parts = []
    pos = 0
    while True:
        match = _RAW_TEXT_OPEN_RE.search(html, pos)
        if match is None:
            parts.append(html[pos:])
            break
        parts.append(html[pos:match.start()])
        close = lower.find("</" + match.group(1).lower(), match.end())
        if close == -1:
            break
        end = lower.find(">", close)
        if end == -1:
            break
        parts.append(" ")
        pos = end + 1
    return "".join(parts)


def _text_only(html: str) -> str | None:
    """The body as escaped text, for an install without bleach."""
    text = html_to_text(_drop_raw_text_elements(html))
    if not text:
        return None
    return html_module.escape(html_module.unescape(text), quote=False)


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

# Tumblr's NPF media URLs put the rendered size in the *path*, not the query,
# and serve the same file from numbered CDN shards:
#
#   https://64.media.tumblr.com/<media-key>/<post-key>-NN/s500x750/<hash>.jpg
#          └ shard                                        └ size    └ content hash
#
# Neither the shard nor the size changes which picture it is, so both are
# normalised away. The filename is a content hash, which is what actually
# identifies the image (ISSUE-162).
_TUMBLR_MEDIA_HOST_RE = re.compile(r"^(?:\d+\.)?media\.tumblr\.com$", re.IGNORECASE)
_TUMBLR_SIZE_SEGMENT_RE = re.compile(r"^s(\d+)x(\d+)$", re.IGNORECASE)


def _tumblr_identity(netloc: str, path: str) -> str:
    """Canonical ``media.tumblr.com`` path with the size segment removed."""
    segments = [
        seg for seg in path.split("/") if not _TUMBLR_SIZE_SEGMENT_RE.match(seg)
    ]
    return "media.tumblr.com" + "/".join(segments)


def image_identity(url: str) -> str:
    """A dedup key that treats resolution variants of one image as equal.

    For a URL whose path ends in an image extension, the identity is
    ``netloc + path`` — the query string (width, quality, CDN signature) is
    ignored, so ``…/3049.jpg?width=140`` and ``…/3049.jpg?width=700`` collapse.
    Tumblr media URLs additionally drop the CDN shard and the ``/sWxH/`` size
    segment, which are the two axes a reblogged photo varies on.
    For anything else (e.g. ``image.php?id=1``) the full URL is the identity,
    so images distinguished purely by query are not wrongly merged.
    """
    if not url:
        return url
    parsed = urlparse(url)
    if _TUMBLR_MEDIA_HOST_RE.match(parsed.netloc):
        return _tumblr_identity(parsed.netloc, parsed.path)
    if _IMG_EXT_RE.search(parsed.path):
        return f"{parsed.netloc}{parsed.path}"
    return url


def _url_width(url: str) -> int:
    """Best-effort pixel width for an image URL (0 when unknown).

    Reads a resize query param (``width=`` / ``w=``) or, for tumblr media
    URLs, the ``/sWxH/`` path segment — so the largest rendition wins when
    variants of one photo collapse.
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("width", "w"):
        values = query.get(key)
        if values:
            try:
                return int(values[0])
            except (ValueError, TypeError):
                continue
    if _TUMBLR_MEDIA_HOST_RE.match(parsed.netloc):
        for seg in parsed.path.split("/"):
            match = _TUMBLR_SIZE_SEGMENT_RE.match(seg)
            if match:
                return int(match.group(1))
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
