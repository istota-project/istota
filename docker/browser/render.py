"""Deterministic rendered-DOM to markdown conversion.

Both extraction paths the API had before this module destroy the signal a
link-dense index page carries: `inner_text` flattens the DOM and throws away
every href, and the flat anchor list strips position, so nav chrome and article
links come back indistinguishable. Markdown keeps both at once — a heading
followed by `[Headline](https://...)` preserves the href *and* the positional
cue that says "article, not footer link" — which moves the disambiguation out
of per-site CSS selectors (they rot on every redesign) and into the reader.

Two modes, because hubs and articles want opposite things:

  full     The whole rendered page as markdown. For hub/index pages, where the
           "boilerplate" link grid *is* the content and a readability pass
           would happily discard it.
  article  Main-content isolation first, then convert. For article bodies,
           where nav/ads/related links are noise.

Article mode degrades rather than failing: trafilatura first, then the largest
<article>/<main> node, then the whole page. It never returns empty when the
page had content.

Everything here is pure — HTML string in, markdown out, no browser and no
network. The caller supplies `page.content()` (the serialized post-JS DOM) and
`page.url`.
"""

import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment
from markdownify import MarkdownConverter

log = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 100_000
# Below this an article extraction is treated as a miss and the next fallback
# runs. A real article body is thousands of characters; a few hundred means the
# extractor latched onto a teaser or a cookie banner.
ARTICLE_MIN_CHARS = 500

MODES = ("full", "article")

# Carry no reading signal, but a lot of bytes. Dropped before conversion in
# both modes. Note what is *not* here: nav, header, footer and aside stay,
# because on an index page the navigation-shaped grid is the content.
NOISE_TAGS = (
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "object", "embed", "link", "meta", "source", "input", "select",
    "textarea", "button",
)

_HIDDEN_STYLE_RE = re.compile(r"(display\s*:\s*none|visibility\s*:\s*hidden)", re.I)
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.M)
_EMPTY_BULLET_RE = re.compile(r"^\s*[*+-]\s*$", re.M)


class _Converter(MarkdownConverter):
    """markdownify with the options both modes share."""

    def convert_hN(self, n, el, text, parent_tags):
        # A heading nested inside a link is the standard news-card markup, and
        # the default conversion renders it as "[#### Headline](url)". Keep the
        # link, drop the hashes.
        if "a" in parent_tags:
            return text
        return super().convert_hN(n, el, text, parent_tags)


def _converter(strip_images):
    options = {
        "heading_style": "ATX",
        "autolinks": False,      # keep [text](url), never bare <url>
        "wrap": False,           # never reflow — line breaks carry structure
        "escape_asterisks": False,
        "escape_underscores": False,
        "escape_misc": False,
    }
    if strip_images:
        options["strip"] = ["img"]
    return _Converter(**options)


def _soup(html):
    return BeautifulSoup(html or "", "html.parser")


def _strip_noise(soup):
    """Remove byte-heavy, signal-free nodes in place."""
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup.find_all(attrs={"hidden": True}):
        tag.decompose()
    for tag in soup.find_all(style=_HIDDEN_STYLE_RE):
        tag.decompose()


def _absolutize(soup, base_url):
    """Rewrite relative href/src against the page URL, in place.

    This is what makes the markdown directly actionable: the reader gets a URL
    it can fetch, instead of a path it would have to reassemble by guessing the
    origin (which the skill's own rules forbid). A `<base href>` in the document
    wins over the page URL, as it does in the browser.
    """
    if not base_url:
        return
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = urljoin(base_url, base_tag["href"])
    for tag, attr in (("a", "href"), ("img", "src"), ("area", "href")):
        for el in soup.find_all(tag):
            value = el.get(attr)
            if not value:
                continue
            stripped = value.strip()
            if not stripped or stripped.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                continue
            try:
                el[attr] = urljoin(base_url, stripped)
            except Exception:
                pass


def _postprocess(markdown):
    """Tidy the raw conversion without changing what it says.

    News pages ship the same headline twice (a mobile DOM and a desktop DOM),
    which lands as adjacent identical lines; dropping the repeat costs nothing
    and buys a materially shorter page.
    """
    text = (markdown or "").replace(" ", " ").replace("​", "")
    text = _TRAILING_WS_RE.sub("", text)
    text = _EMPTY_BULLET_RE.sub("", text)

    lines = []
    previous = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped == previous:
            continue
        if stripped:
            previous = stripped
        lines.append(line)

    text = "\n".join(lines)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


def _truncate(markdown, max_chars):
    """Cut at a line boundary and say so. Returns (text, truncated)."""
    if max_chars <= 0 or len(markdown) <= max_chars:
        return markdown, False
    cut = markdown[:max_chars]
    newline = cut.rfind("\n")
    if newline > max_chars * 0.8:
        cut = cut[:newline]
    return (
        cut.rstrip()
        + f"\n\n[Markdown truncated at {max_chars} characters — "
        "raise --max-chars or switch to --mode article]",
        True,
    )


def _looks_like_index(url):
    """Does this URL address a section front rather than one article?

    Article mode on an index page is the dangerous case: readability keeps a few
    hundred characters of teaser prose, clears any content-length floor, and
    silently discards the headline grid that was the whole point of the fetch.
    Measured on live front pages, no *content* signal separates the two — link
    retention, paragraph length and link density all overlap between real hubs
    and real articles in both directions. The URL does separate them, and needs
    no per-site knowledge: an article ends in a slug that is long, or dated, or
    sits several levels deep, while a section front is a short word or two.

    Errs toward `index`, because that answer costs noise (the full page contains
    the article) while the other costs the entire page.
    """
    path = urlparse(url or "").path.strip("/")
    if not path:
        return True
    segments = [s for s in path.split("/") if s]
    last = segments[-1]
    if len(segments) >= 4:
        return False
    if re.search(r"\d", last):
        return False
    return len(last) < 25


def _largest_main_node(soup):
    """The most content-bearing <article>/<main>/[role=main], or None."""
    candidates = soup.select("article, main, [role=main]")
    if not candidates:
        return None
    return max(candidates, key=lambda node: len(node.get_text(strip=True)))


def _trafilatura_markdown(html, base_url):
    try:
        import trafilatura
    except ImportError:
        log.warning("trafilatura not installed — article mode using fallbacks only")
        return None
    try:
        return trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_images=True,
            include_tables=True,
            include_formatting=True,
            favor_recall=True,
            url=base_url or None,
        )
    except Exception as e:
        log.warning("trafilatura extraction failed: %s", e)
        return None


def _article_markdown(html, soup, base_url, notes):
    """Article body as markdown, or None if nothing article-shaped was found."""
    extracted = _trafilatura_markdown(html, base_url)
    if extracted and len(extracted.strip()) >= ARTICLE_MIN_CHARS:
        return extracted

    node = _largest_main_node(soup)
    if node is not None:
        converted = _converter(strip_images=False).convert_soup(node)
        if len(converted.strip()) >= ARTICLE_MIN_CHARS:
            notes.append(
                "readability extraction was thin — used the page's main content node"
            )
            return converted

    # Deliberately not "return whatever the extractor found". On a hub page
    # readability returns a handful of nav words with the hrefs stripped —
    # precisely the useless output this module exists to replace. The full page
    # is a superset of the article, so falling through to it never loses
    # content; keeping a sub-floor extraction would.
    return None


def to_markdown(html, base_url="", mode="full", max_chars=DEFAULT_MAX_CHARS):
    """Convert rendered HTML to markdown.

    Returns a dict with `markdown`, the `mode` actually used (which may differ
    from `requested_mode` when article extraction found nothing), `chars`,
    `truncated`, and human-readable `notes` explaining any degradation.
    """
    requested = mode if mode in MODES else "full"
    notes = []
    if mode not in MODES:
        notes.append(f"unknown mode {mode!r} — used full")

    soup = _soup(html)
    _strip_noise(soup)
    _absolutize(soup, base_url)
    normalized_html = str(soup)

    used = requested
    markdown = None
    if requested == "article":
        if _looks_like_index(base_url):
            used = "full"
            notes.append(
                "URL addresses a section front, not an article — rendered in full "
                "so the headline grid isn't discarded"
            )
        else:
            markdown = _article_markdown(normalized_html, soup, base_url, notes)
            if markdown is None:
                used = "full"
                notes.append(
                    "no article content found — fell back to full page"
                )

    if markdown is None:
        body = soup.body or soup
        markdown = _converter(strip_images=True).convert_soup(body)

    markdown = _postprocess(markdown)
    markdown, truncated = _truncate(markdown, max_chars)

    return {
        "markdown": markdown,
        "mode": used,
        "requested_mode": requested,
        "chars": len(markdown),
        "truncated": truncated,
        "notes": notes,
    }
