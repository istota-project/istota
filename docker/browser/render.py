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


def _effective_base(soup, base_url):
    """The URL a relative reference in this document resolves against.

    A `<base href>` in the document wins over the page URL, as it does in the
    browser. Shared with the frame splice, which has to read an `<iframe src>`
    exactly the way the browser did when it loaded that frame — two copies of
    this rule would put the splice's matching and the markdown's links on
    different origins.
    """
    if not base_url:
        return base_url
    base_tag = soup.find("base", href=True)
    if not base_tag:
        return base_url
    try:
        return urljoin(base_url, base_tag["href"])
    except Exception:
        return base_url


def _absolutize(soup, base_url):
    """Rewrite relative href/src against the page URL, in place.

    This is what makes the markdown directly actionable: the reader gets a URL
    it can fetch, instead of a path it would have to reassemble by guessing the
    origin (which the skill's own rules forbid). A `<base href>` in the document
    wins over the page URL, as it does in the browser.
    """
    if not base_url:
        return
    base_url = _effective_base(soup, base_url)
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


# How a frame's content is marked in the markdown.
#
# **This is provenance, not a security boundary, and the difference is worth
# being exact about** — an earlier version of this comment claimed the frame
# body "rides inside the caller's existing untrusted-content fence", and that
# was simply false: `istota.untrusted.frame_untrusted` is applied by the
# `nextcloud`, `tasks`, `rooms` and `email` skills, by the native WebFetch tool
# and by image attachments — and by nothing on the browse path, so a rendered
# page, frames or no frames, reaches the model unfenced. Frame
# content is no more dangerous than the page body beside it, which has never
# been fenced either; what it is, is *somebody else's*, and that is the fact the
# marking carries.
#
# Three things make the marking hard to shake off. It is a `blockquote`, so
# markdownify prefixes every line of frame content with `> ` and the provenance
# survives an extractor keeping only part of the frame — a single sibling
# paragraph does not, and that is exactly how `--mode article` used to hand back
# frame content with its marker dropped. It is opened *and* closed, so the
# page's own text visibly resumes. And both spellings are redacted out of the
# frame body first, on the rule `istota.untrusted._redaction_patterns` states:
# a fence the content can close is not a fence.
#
# The stated bound: redaction runs over the frame's text nodes, so a marker
# split across tags (`[fra<span>me]`) is rebuilt by the conversion and survives.
# Closing that needs redaction after conversion, which is a different shape from
# this splice.
FRAME_MARKER = "[frame]"
FRAME_MARKER_END = "[end frame]"
FRAME_WRAPPER_ATTR = "data-frame-src"

# How many frame URLs the census reports back. The list is a diagnosis aid, not
# the payload.
MAX_REPORTED_FRAME_URLS = 20

_FRAME_MARKER_RE = re.compile(r"\[\s*(?:end\s+)?frame\b[^\]]*\]", re.I)
MARKER_REDACTION = "[marker removed]"


def _redact_frame_markers(frame_soup):
    """Take both marker spellings out of the frame's own text, in place."""
    for text in list(frame_soup.find_all(string=True)):
        original = str(text)
        cleaned = _FRAME_MARKER_RE.sub(MARKER_REDACTION, original)
        if cleaned != original:
            text.replace_with(cleaned)


def _origin(url):
    """`scheme://host:port`, or "" for anything without both parts."""
    try:
        parsed = urlparse(url or "")
    except Exception:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def _in_spliced_frame(node):
    """Is this node part of content we spliced in from a frame?

    The article heuristics ask it because both of them pick a *node* out of the
    page, and a frame carrying an `<article>` would otherwise win: it flipped
    `_has_dominant_article`'s veto (so a hub URL stopped being treated as a hub)
    and then `_largest_main_node` returned the frame instead of the page. The
    net effect was a third party's article replacing the page's own, with the
    provenance marker left outside the selected node.
    """
    return node.find_parent(attrs={FRAME_WRAPPER_ATTR: True}) is not None


def _frame_nodes(frame_html, frame_url, soup):
    """One frame's content as nodes ready to splice into `soup`, or [].

    The absolutize is per frame and against the frame's own URL: frame content
    carries links relative to the frame's origin, so resolving them against the
    outer page would hand back URLs that look absolute and point nowhere —
    exactly the guessed-URL class the skill's rules forbid.

    `<base>` goes afterwards, and that removal is load-bearing rather than
    tidy: `base` is not in NOISE_TAGS, and `_effective_base` takes the first
    `<base href>` in the document, so a frame's base left in place becomes the
    outer document's base whenever the outer page has none. `head` and `title`
    go with it for a plainer reason: neither is in NOISE_TAGS either, and a
    frame document serialized without a `<body>` — an error page, a non-HTML
    response — otherwise splices its `<title>` into the page as prose.
    """
    try:
        frame_soup = _soup(frame_html)
    except Exception as e:
        log.warning("could not parse frame content from %s: %s", frame_url, e)
        return []
    _absolutize(frame_soup, frame_url)
    _redact_frame_markers(frame_soup)
    for tag in frame_soup.find_all(["base", "head", "title"]):
        tag.decompose()

    body = frame_soup.body or frame_soup
    nodes = [node.extract() for node in list(body.contents)]
    if not any(getattr(n, "name", None) or str(n).strip() for n in nodes):
        return []

    wrapper = soup.new_tag("blockquote", attrs={FRAME_WRAPPER_ATTR: frame_url})
    opener = soup.new_tag("p")
    opener.string = f"{FRAME_MARKER} {frame_url}"
    wrapper.append(opener)
    for node in nodes:
        wrapper.append(node)
    closer = soup.new_tag("p")
    closer.string = FRAME_MARKER_END
    wrapper.append(closer)
    return [wrapper]


def _splice_frames(soup, frames, base_url):
    """Replace each `<iframe>` with its frame's content, in document order.

    Returns how many were placed. Must run before `_strip_noise`, which
    decomposes the `iframe` node — after that there is nowhere to put the
    content. Running first also means the frame's own scripts, styles and
    nested iframes are stripped by that same pass.

    Matching is `<iframe src>`, resolved against the document's effective base,
    against `frame.url`. The alternative — writing a marker attribute through
    `frame_element().evaluate(...)` — is exact and is a DOM write into a page
    this container works hard not to be detected in (BOT_DETECTION.md). This
    way touches nothing, and every failure it has is a *miss* rather than a
    mismatch, which is the property that makes a lossy matcher the safe one:
    a miss is counted and reported, where a mismatch would put one site's text
    under another's heading. The census is what makes it acceptable — the
    caller is told how many frames there were either way.

    Measured against live pages, the miss that actually dominates is not one of
    the ones the design predicted (a redirect, a `srcdoc`, two frames sharing a
    src). It is an `<iframe>` carrying **no `src` attribute at all**: a modern
    ad or player slot is an empty element whose document JavaScript writes, so
    there is nothing in the DOM naming the frame. On a news page essentially
    every unmatched frame is one of these, which is also why it costs little —
    they are ad frames, and FRAME_NOISE_URLS drops most of them before this
    runs. A JS-written *content* frame is the case that is genuinely lost, and
    it is lost visibly.
    """
    unplaced = [r for r in frames if r.get("html")]
    if not unplaced:
        return 0

    effective_base = _effective_base(soup, base_url)
    candidates = []
    for el in soup.find_all("iframe"):
        src = (el.get("src") or "").strip()
        if not src:
            continue
        resolved = src
        if effective_base:
            try:
                resolved = urljoin(effective_base, src)
            except Exception:
                resolved = src
        candidates.append((el, resolved))

    placed = 0

    def place(el, record):
        """Splice one frame's content in, if that node is still in the page.

        The attachment test has to happen *here*, not when `candidates` was
        built: `find_all` is a snapshot, and an iframe nested inside one an
        earlier pass has already replaced left the document with its parent.
        `replace_with` would still succeed on that detached tree — a record
        consumed, `placed` incremented, and the content nowhere in the output.
        Checking at collection time is too early to see it, which is a mistake
        this function made once already.
        """
        nonlocal placed
        if not any(parent is soup for parent in el.parents):
            return False
        nodes = _frame_nodes(record["html"], record.get("url") or "", soup)
        if not nodes:
            return False
        el.replace_with(*nodes)
        placed += 1
        return True

    # Pass 1: the src is exactly the URL the frame is showing.
    for el, resolved in list(candidates):
        match = next((r for r in unplaced if (r.get("url") or "") == resolved), None)
        if match is None:
            continue
        unplaced.remove(match)
        candidates.remove((el, resolved))
        place(el, match)

    # Pass 2: same origin. A framed widget very often navigates away from the
    # `src` it was written with — measured on a live events page whose calendar
    # iframe is written `https://tockify.com/eventbrite` and is showing a
    # different path by the time the DOM is read, which is the single most
    # valuable frame on that page. Requiring the *origin* to match keeps this an
    # identification rather than a guess, and it is applied only when exactly
    # one node and exactly one frame share that origin, so there is no choice to
    # get wrong. Ambiguity falls through and is reported unplaced, which is the
    # rule the whole matcher is built on: a miss is recoverable, a mismatch puts
    # one site's words under another's heading.
    for el, resolved in list(candidates):
        origin = _origin(resolved)
        if not origin:
            continue
        same = [r for r in unplaced if _origin(r.get("url") or "") == origin]
        if len(same) != 1:
            continue
        if sum(1 for _, other in candidates if _origin(other) == origin) != 1:
            continue
        match = same[0]
        unplaced.remove(match)
        candidates.remove((el, resolved))
        place(el, match)

    return placed


_FRAME_OPEN_RE = re.compile(rf"^\s*>*\s*{re.escape(FRAME_MARKER)}\s+\S", re.M)


def _count_frame_markers(markdown):
    """How many frames are actually present in the markdown being returned.

    `_splice_frames` reports what it put into the *soup*, and two later stages
    can take it back out again: article extraction selects one node and
    discards the rest of the page, and `_truncate` cuts the tail. Reporting the
    splice count as `included` therefore claimed content the caller could not
    see — the same class of silent short read ISSUE-516 exists to remove, one
    layer further along. Counting the markers in the finished markdown is the
    only answer that describes what was returned.
    """
    return len(_FRAME_OPEN_RE.findall(markdown or ""))


def _frame_notes(found, placed, included, unread, nested, capped, include_frames):
    """What to tell the caller about frames it cannot see in the markdown.

    A capped walk is reported even when it found nothing, which is the case a
    live run turned up: a front page whose frame budget went entirely on ad
    slots answered `found: 0, capped: true` and, under the first version of
    this function, said nothing at all — a correct census of nothing, rendered
    as the silent short read the whole feature exists to remove.

    `unread` and `nested` are counted apart from the unmatched remainder
    because they have different causes and different remedies, and one fixed
    sentence for all three told an operator the wrong one: a frame whose
    `content()` raised did have a src, and a nested frame was never a candidate
    for matching at all.
    """
    notes = []
    if nested:
        notes.append(
            f"{nested} frame{'s' if nested != 1 else ''} nested inside another "
            "frame were not read — the walk goes one level deep, and a nested "
            "frame's <iframe> is not in the document being rendered"
        )
    if not found:
        if capped:
            notes.append(
                "the iframe walk hit its cap before finding a readable frame — "
                "this page carries more frames than the walk covers, so frame "
                "content may be missing and uncounted"
            )
        return notes

    count = f"{found} or more" if capped else str(found)
    plural = "s" if found != 1 else ""
    if not include_frames:
        notes.append(
            f"{count} iframe{plural} on this page were dropped — their content "
            "lives in separate documents this read does not cover. Re-run with "
            "--include-frames to splice it in."
        )
        return notes

    notes.append(f"{included} of {count} iframe{plural} are in the markdown below")
    if unread:
        notes.append(
            f"{unread} frame{'s' if unread != 1 else ''} could not be read — the "
            "frame was detached or navigating when its content was asked for"
        )
    unmatched = found - unread - placed
    if unmatched > 0:
        notes.append(
            f"{unmatched} frame{'s' if unmatched != 1 else ''} could not be "
            "placed — most often an <iframe> with no src attribute, whose "
            "document JavaScript wrote, so nothing in the page names it"
        )
    if included < placed:
        notes.append(
            f"{placed - included} frame{'s' if placed - included != 1 else ''} "
            "were spliced in and then dropped by article extraction or by the "
            "--max-chars cut; re-run with --mode full or a larger budget"
        )
    return notes


def _postprocess(markdown):
    """Tidy the raw conversion without changing what it says.

    News pages ship the same headline twice (a mobile DOM and a desktop DOM),
    which lands as a repeated line; dropping the repeat costs nothing and buys
    a materially shorter page.

    The comparison skips over blank lines rather than resetting on them, so it
    catches the block-level duplicate (two `<h2>`s convert to lines separated by
    a blank) as well as the inline one: a repeat is any line identical to the
    previous *non-empty* line. Table rows are exempt, being the one construct
    where an identical adjacent line is data rather than duplication.
    """
    text = (markdown or "").replace(" ", " ").replace("​", "")
    text = _TRAILING_WS_RE.sub("", text)
    text = _EMPTY_BULLET_RE.sub("", text)

    lines = []
    previous = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped == previous and not stripped.startswith("|"):
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


def _url_looks_like_index(url):
    """Does this URL *shape* suggest a section front rather than one article?

    Article mode on an index page is the dangerous case: readability keeps a few
    hundred characters of teaser prose, clears any content-length floor, and
    silently discards the headline grid that was the whole point of the fetch.
    Measured on live front pages, none of the *aggregate* content signals
    separate the two — link retention, paragraph length and link density all
    overlap between real hubs and real articles in both directions. URL shape
    does separate most of them, and needs no per-site knowledge: an article ends
    in a slug that is long, or dated, or sits several levels deep, while a
    section front is a short word or two.

    It is a guess, not a classification, and a deliberately biased one: it errs
    toward `index`, because that answer costs noise (the full page contains the
    article) while the other costs the entire page. The families it gets wrong
    are the short, undated, shallow slug — `/wiki/Poland`, `/p/some-title`,
    `/blog/why-i-left` — which is why `_has_dominant_article` gets a veto over
    it in `to_markdown`.
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


# A page is treated as a real article, whatever its URL looks like, when one
# article node holds at least this much of the page's text.
ARTICLE_DOMINANCE_RATIO = 0.4


def _has_dominant_article(soup):
    """Is there one <article> node holding most of this page's text?

    The narrow content signal the aggregate ones lacked, and the veto over a
    URL-shape guess. Deliberately excludes `main` / `[role=main]`, which
    `_largest_main_node` accepts: on a hub, `main` wraps the entire headline
    grid and would dominate every time, turning the veto into exactly the
    silent-grid-discard this module guards against. A hub's `<article>` nodes
    are cards — many of them, each a small fraction of the page — so requiring a
    single dominant one separates the two shapes without per-site knowledge.
    """
    nodes = [n for n in soup.select("article, [itemprop=articleBody]")
             if not _in_spliced_frame(n)]
    if not nodes:
        return False
    body = soup.body or soup
    total = len(body.get_text(strip=True))
    if total <= 0:
        return False
    largest = max(len(n.get_text(strip=True)) for n in nodes)
    if largest < ARTICLE_MIN_CHARS:
        return False
    return largest / total >= ARTICLE_DOMINANCE_RATIO


def _largest_main_node(soup):
    """The most content-bearing <article>/<main>/[role=main], or None."""
    candidates = [n for n in soup.select("article, main, [role=main]")
                  if not _in_spliced_frame(n)]
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


def to_markdown(html, base_url="", mode="full", max_chars=DEFAULT_MAX_CHARS,
                frames=None, include_frames=False, frames_capped=False):
    """Convert rendered HTML to markdown.

    Returns a dict with `markdown`, the `mode` actually used (which may differ
    from `requested_mode` when article extraction found nothing), `chars`,
    `truncated`, a `frames` census, and human-readable `notes` explaining any
    degradation.

    `frames` is the child frames the caller surveyed, as records shaped
    `{"url": str, "skip": str | None, "html": str | None}`. A record with
    `skip` set was classified out by the walk — `nested` is reported, the rest
    (ads, consent banners, blank frames) are not content and are silently not
    counted. `html` is None for a content-bearing frame whose content was not
    asked for or could not be read. Frames are *counted* whatever
    `include_frames` says, because a page that is mostly iframe otherwise
    renders short and silent (ISSUE-516); they are spliced into the markdown
    only when it is true. `frames_capped` says the caller's walk hit its own
    bound, so the count reads "N or more".

    Frame content counts against `max_chars` rather than being added on top of
    it — truncation runs post-conversion over the whole markdown, so this falls
    out rather than needing a second budget.
    """
    requested = mode if mode in MODES else "full"
    notes = []
    if mode not in MODES:
        notes.append(f"unknown mode {mode!r} — used full")

    all_records = list(frames or ())
    frame_records = [r for r in all_records if not r.get("skip")]
    nested = sum(1 for r in all_records if r.get("skip") == "nested")
    unread = sum(1 for r in frame_records if not r.get("html")) if include_frames else 0

    soup = _soup(html)
    placed = 0
    if frame_records and include_frames:
        placed = _splice_frames(soup, frame_records, base_url)
    _strip_noise(soup)
    _absolutize(soup, base_url)
    normalized_html = str(soup)

    used = requested
    markdown = None
    if requested == "article":
        if _url_looks_like_index(base_url) and not _has_dominant_article(soup):
            used = "full"
            notes.append(
                "URL looks like a section front and no single article node "
                "dominates the page — rendered in full so a headline grid isn't "
                "discarded"
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

    # After truncation, deliberately: `included` is what the caller can read,
    # not what was put into the soup.
    included = _count_frame_markers(markdown) if placed else 0
    notes.extend(_frame_notes(
        len(frame_records), placed, included, unread, nested,
        frames_capped, include_frames,
    ))

    return {
        "markdown": markdown,
        "mode": used,
        "requested_mode": requested,
        "chars": len(markdown),
        "truncated": truncated,
        "frames": {
            "found": len(frame_records),
            "included": included,
            "capped": bool(frames_capped),
            # Which frames, not just how many. A bare count says something is
            # missing; the URL says what, which is the difference between "this
            # page is short" and "the events calendar is on tockify.com" — and
            # it is what lets a caller go and fetch the frame directly when the
            # splice could not place it. Bounded, so a page of frames cannot
            # turn the census into the payload.
            "urls": [
                r.get("url") or "" for r in frame_records
            ][:MAX_REPORTED_FRAME_URLS],
        },
        "notes": notes,
    }
