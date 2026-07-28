"""Newsletter HTML → link-preserving markdown.

A newsletter body is the one briefing source whose article URLs live *inside*
the content. The briefing skill's :func:`istota.skills.briefing._strip_html`
flattens it with ``re.sub(r'<[^>]+>', '', text)``, which deletes every
``<a href>`` and keeps only the anchor text — so the article URL was gone before
the model saw it, and a newsletter story could never be cited to its source
article the way an RSS item (which carries a structured ``url``) can.

:func:`html_to_markdown` does the same flattening but rewrites each surviving
anchor as inline ``[anchor](url)``. Inline rather than a trailing reference
block: the story↔link association is what lets the model cite the *right*
article.

Newsletters are link soup, so extraction is best-effort by design:

* anchors with no text (tracking pixels, spacer images) are dropped whole;
* non-``http(s)`` destinations are dropped, keeping the text;
* unsubscribe / view-in-browser / preferences / social-share destinations are
  dropped by URL *and* by anchor text (a tracking-wrapped unsubscribe hides the
  keyword in an opaque redirect, but its label doesn't);
* common redirect wrappers are unwrapped conservatively — only when an
  allowlisted query param holds an absolute ``http(s)`` URL, so an opaque
  wrapper is kept rather than mangled (a working tracked link still lands on the
  article);
* duplicates collapse and the total is capped, so link soup can't flood the
  prompt.

``bleach`` (the ``feeds`` extra) is preferred for the parse because it
normalises attribute quoting and drops junk markup robustly, but the regex
fallback still *preserves links* — the whole point of the module — so the
feature works without the dep, just less robustly on nested markup.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from urllib.parse import parse_qsl, quote, unquote, urlparse

try:
    import bleach  # type: ignore

    _HAS_BLEACH = True
except ImportError:  # pragma: no cover - exercised via monkeypatch
    _HAS_BLEACH = False


logger = logging.getLogger(__name__)

DEFAULT_MAX_LINKS = 20

# How much anchor text may become a link label. A newsletter sometimes wraps a
# whole paragraph in one anchor; the label is there to identify the story, not
# to carry the body.
_MAX_LABEL_CHARS = 220

# Tags kept through the bleach pass. Anchors carry the payload; the block
# elements are kept only so the flattening step below can still turn them into
# newlines (bleach would otherwise collapse the newsletter into one line).
_KEEP_TAGS = [
    "a", "p", "div", "br", "li", "ul", "ol", "tr", "td", "table", "span",
    "strong", "b", "em", "i", "h1", "h2", "h3", "h4", "h5", "h6",
]

_SCRIPT_RE = re.compile(r"<\s*script\b[^>]*>.*?<\s*/\s*script\s*>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<\s*style\b[^>]*>.*?<\s*/\s*style\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Zero/one-pixel tracking images (same shape _strip_html drops).
_PIXEL_RE = re.compile(
    r'<img[^>]*(?:width\s*=\s*["\']?[01]["\']?|height\s*=\s*["\']?[01]["\']?)[^>]*/?\s*>',
    re.IGNORECASE,
)

# `<a ... href=... >inner</a>`. `[^>]*` on the attribute run and a non-greedy
# inner match keep this workable on the malformed markup newsletters ship; the
# bleach pass (when available) normalises the tag first so this is exact.
_ANCHOR_RE = re.compile(
    r"<a\b([^>]*)>(.*?)</a\s*>", re.IGNORECASE | re.DOTALL,
)
_HREF_RE = re.compile(
    r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")

# Destinations that are newsletter chrome rather than content.
_NOISE_URL_RE = re.compile(
    r"unsubscribe|opt[-_]?out|"
    r"(?:email[-_]?)?preferences|manage[-_]?(?:your[-_]?)?(?:subscription|email)|"
    r"view[-_a-z0-9]{0,12}browser|web[-_]?version|"
    r"twitter\.com/intent|x\.com/intent|facebook\.com/sharer|"
    r"linkedin\.com/(?:share|shareArticle)|reddit\.com/submit|"
    r"t\.me/share|api\.whatsapp\.com/send|pinterest\.[a-z]+/pin/create",
    re.IGNORECASE,
)
# The same chrome recognised by its label, for the redirect-wrapped case where
# the URL is opaque.
_NOISE_LABEL_RE = re.compile(
    r"^(?:"
    r"unsubscribe|un-?subscribe.*|view (?:this )?(?:in|on) (?:your )?browser|"
    r"view (?:in|as) web.*|web version|manage (?:your )?(?:preferences|subscription|email).*|"
    r"(?:email |notification )?preferences|update (?:your )?preferences|"
    r"share on \w+|tweet this|forward to a friend|privacy policy|terms(?: of \w+)?|"
    r"advertise|sponsor this newsletter"
    r")$",
    re.IGNORECASE,
)

# Query params that conservatively carry the real destination of a redirect
# wrapper. Only unwrapped when the value is an absolute http(s) URL, which is
# what keeps `?u=<user-id>` from being mistaken for a target.
_REDIRECT_PARAMS = (
    "url", "u", "redirect", "redirect_url", "redirect_uri", "redirecturl",
    "target", "dest", "destination", "link",
)
_MAX_UNWRAP_HOPS = 3

_WS_RE = re.compile(r"\s+")
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Anchors are replaced by a placeholder and spliced back only *after* the
# flattening pass. Necessary because that pass ends in ``html.unescape``, which
# would re-decode entity-shaped runs inside an already-decoded URL — a query
# string carrying `&copy=` or `&reg=` would silently become `©` / `®`. The token
# is control-character-delimited so nothing in the flattening pipeline (tag
# strip, entity decode, invisible-character scrub, whitespace collapse) alters
# it, and no newsletter body can forge one.
_PLACEHOLDER = "\x02istota-link-{}\x02"
_PLACEHOLDER_RE = re.compile(r"\x02istota-link-(\d+)\x02")


def unwrap_redirect(url: str) -> str:
    """Resolve a tracking wrapper to the destination it carries, if it does.

    Conservative on purpose: only an allowlisted query param whose value is an
    absolute ``http(s)`` URL is unwrapped. Anything else — an opaque
    ``/click/<hash>``, a ``u=<user-id>``, a non-http target — is returned
    unchanged, on the reasoning that a working tracked link still lands the
    reader on the article, while a wrong guess sends them somewhere else.
    """
    current = url
    for _ in range(_MAX_UNWRAP_HOPS):
        try:
            query = urlparse(current).query
        except ValueError:
            return current
        if not query:
            return current
        params = dict(parse_qsl(query, keep_blank_values=False))
        target = None
        for key in _REDIRECT_PARAMS:
            raw = params.get(key)
            if not raw:
                continue
            candidate = unquote(raw)
            if _HTTP_URL_RE.match(candidate):
                target = candidate
                break
        if target is None or target == current:
            return current
        current = target
    return current


def _flatten(fragment: str) -> str:
    """Anchor inner HTML → single-line plain text."""
    text = _TAG_RE.sub(" ", fragment)
    text = html_module.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _href_of(attrs: str) -> str:
    match = _HREF_RE.search(attrs or "")
    if not match:
        return ""
    raw = match.group(1) or match.group(2) or match.group(3) or ""
    return html_module.unescape(raw).strip()


def _encode_url(url: str) -> str:
    """Make a URL safe to sit inside ``[label](url)``.

    Whitespace and parentheses would break the markdown the model reads back,
    so they are percent-encoded; everything else is left byte-for-byte (a
    newsletter URL's own encoding must survive).
    """
    return quote(url, safe="/:?#[]@!$&'*+,;=%~._-")


def _safe_label(text: str) -> str:
    """Neutralise a label so it can't break the surrounding link syntax."""
    label = text.replace("[", "(").replace("]", ")")
    if len(label) > _MAX_LABEL_CHARS:
        label = label[:_MAX_LABEL_CHARS].rstrip() + "…"
    return label


def _normalize(html: str) -> str:
    """Pre-clean, then sanitise with bleach when it's installed.

    ``<script>``/``<style>``/comments/pixels go first because bleach strips
    those *tags* but keeps their raw text content, which would dump CSS into
    the prompt.
    """
    out = _COMMENT_RE.sub(" ", html)
    out = _SCRIPT_RE.sub(" ", out)
    out = _STYLE_RE.sub(" ", out)
    out = _PIXEL_RE.sub(" ", out)
    if not _HAS_BLEACH:
        return out
    try:
        return bleach.clean(
            out,
            tags=_KEEP_TAGS,
            attributes={"a": ["href"]},
            protocols=["http", "https"],
            strip=True,
            strip_comments=True,
        )
    except Exception as e:  # noqa: BLE001 - a sanitiser hiccup must not lose the body
        logger.debug("bleach normalisation failed, using raw HTML: %s", e)
        return out


def html_to_markdown(
    html: str | None, *, max_links: int = DEFAULT_MAX_LINKS,
) -> str:
    """Flatten newsletter HTML to text, keeping article links inline.

    ``max_links`` caps the inline links kept per body (``0`` = unlimited);
    over-cap and filtered-out anchors keep their text and lose only the
    destination. Never raises — the caller (a briefing source) is fail-soft.
    """
    if not html:
        return ""

    from ...skills.briefing import _strip_html

    normalized = _normalize(html)

    kept: dict[str, None] = {}  # ordered set of URLs already linked
    links: list[str] = []  # rendered markdown, indexed by placeholder number
    dropped = 0

    def _replace(match: re.Match) -> str:
        nonlocal dropped
        label = _safe_label(_flatten(match.group(2)))
        if not label:
            return " "  # image-only / empty anchor: nothing worth keeping
        url = _href_of(match.group(1))
        if not url or not _HTTP_URL_RE.match(url):
            return label
        if _NOISE_LABEL_RE.match(label) or _NOISE_URL_RE.search(url):
            dropped += 1
            return label
        url = unwrap_redirect(url)
        if not _HTTP_URL_RE.match(url) or _NOISE_URL_RE.search(url):
            dropped += 1
            return label
        if url in kept:
            return label
        if max_links and len(kept) >= max_links:
            dropped += 1
            return label
        kept[url] = None
        links.append(f"[{label}]({_encode_url(url)})")
        return _PLACEHOLDER.format(len(links) - 1)

    try:
        with_tokens = _ANCHOR_RE.sub(_replace, normalized)
        flattened = _strip_html(with_tokens)
        out = _PLACEHOLDER_RE.sub(lambda m: links[int(m.group(1))], flattened)
    except Exception as e:  # noqa: BLE001 - fall back to today's behaviour
        logger.warning("newsletter link extraction failed: %s", e)
        return _strip_html(html)

    if dropped:
        logger.debug(
            "newsletter links: kept %d, dropped %d (noise/dupe/cap)",
            len(kept), dropped,
        )
    return out
