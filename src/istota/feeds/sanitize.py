"""HTML sanitization for feed entry content.

Miniflux did this for us before. Prefer ``bleach`` when available; fall
back to a minimal regex-based stripper so the module imports without the
extra dep installed (the ``feeds`` extra pulls bleach in).
"""

from __future__ import annotations

import re

try:
    import bleach  # type: ignore
    _HAS_BLEACH = True
except ImportError:
    _HAS_BLEACH = False


# Tags safe to keep in feed content. Mirrors Miniflux's default policy
# closely; tightening over time is fine, but loosening warrants a review.
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
