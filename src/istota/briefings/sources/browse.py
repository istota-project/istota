"""Browse source resolver — a user-defined URL fetched via the browse skill.

A source references either a bundled preset key (``ap``, ``reuters``, …) or an
arbitrary ``url``. The page is fetched through the headless browser API as
**markdown**, so a headline arrives attached to its URL and under the section
heading it sits beneath; the older flattened-text path dropped every href, which
is why a live frontpage could read as a dead one (ISSUE-192). A browser image
predating ``/render`` answers 404 and we fall back to that text path. Requires
``config.browser.enabled``; off → empty + note.

The gathered text is capped by ``[briefings] max_browse_chars`` (a source's own
``max_chars`` wins), the markdown counterpart of ``max_source_chars``.

The content is an arbitrary web page, so the source is marked ``untrusted`` and
prompt assembly wraps it in the do-not-follow-instructions delimiter. That
delimiter is the whole protection here: the skill selected for a briefing
generation task is ``briefing``, which declares no ``untrusted_input``
companion, so none of that skill's handling rules reach this prompt. Markdown
raised the stakes over the flattened-text path it replaced — the page's own
absolute URLs now arrive intact, and there are more of them.

Source config shape::

    {"url": "https://…", "preset": "ap"|null, "mode": "full"|"article",
     "max_chars": 20000}
"""

from __future__ import annotations

import logging
import re
import threading

import httpx

from istota.briefings.sources import GatheredSource, SourceContext


logger = logging.getLogger(__name__)


# Bundled frontpage presets, keyed by a short slug the settings UI offers as a
# pick-list. Reputable, mostly-text frontpages that render useful headline text
# through the headless browser; grouped by beat for readability (insertion order
# is what the pick-list shows). A user can always point a browse source at an
# arbitrary ``url`` instead.
BROWSE_PRESETS: dict[str, dict] = {
    # Global / general news
    "ap": {"url": "https://apnews.com", "name": "AP News"},
    "reuters": {"url": "https://www.reuters.com", "name": "Reuters"},
    "bbc": {"url": "https://www.bbc.com/news", "name": "BBC News"},
    "guardian": {"url": "https://www.theguardian.com/world", "name": "The Guardian"},
    "npr": {"url": "https://www.npr.org", "name": "NPR"},
    "aljazeera": {"url": "https://www.aljazeera.com", "name": "Al Jazeera"},
    # Europe
    "ft": {"url": "https://www.ft.com", "name": "Financial Times"},
    "lemonde": {"url": "https://www.lemonde.fr/en/", "name": "Le Monde"},
    "spiegel": {"url": "https://www.spiegel.de/international/", "name": "Der Spiegel"},
    "dw": {"url": "https://www.dw.com/en/", "name": "Deutsche Welle"},
    "france24": {"url": "https://www.france24.com/en/", "name": "France 24"},
    # Asia-Pacific
    "japantimes": {"url": "https://www.japantimes.co.jp", "name": "The Japan Times"},
    "scmp": {"url": "https://www.scmp.com", "name": "South China Morning Post"},
    # Business / markets
    "cnbc": {"url": "https://www.cnbc.com", "name": "CNBC"},
    # US politics / policy
    "politico": {"url": "https://www.politico.com", "name": "Politico"},
    "axios": {"url": "https://www.axios.com", "name": "Axios"},
    # Technology
    "techmeme": {"url": "https://www.techmeme.com", "name": "Techmeme"},
    "hackernews": {"url": "https://news.ycombinator.com", "name": "Hacker News"},
}

# Above the browser container's own watchdog deadline (BROWSE_WATCHDOG_DEADLINE_S,
# 90s), so the server's error reaches us instead of the client giving up first
# and leaving the container working on a request nobody is waiting for.
_FETCH_TIMEOUT = 120.0

# The container serves one request at a time (Flask ``threaded=False``), so
# concurrent browse sources queue in the kernel backlog with their client clocks
# already running: source 5 can spend its entire budget waiting to be served and
# then report a live frontpage as unreachable — the exact failure this source
# exists to fix. Serializing costs no wall-clock (the browser was never going to
# work in parallel) and gives each request its full budget from the moment it is
# actually issued. Process-global on purpose: per-user briefings and the shared
# block generator contend for the same single browser.
_BROWSER_LOCK = threading.Lock()

# How long a queued source waits for its turn before giving up, so N browse
# sources in one briefing can't serialize into N × _FETCH_TIMEOUT of generation.
# Skipping is the fail-soft outcome the whole module contracts for.
_QUEUE_WAIT_TIMEOUT = 90.0

# Markdown carries the URLs the flattened text dropped, so it needs a bigger
# budget than ``max_source_chars`` (5000) — a frontpage spends its first couple
# of thousand characters on masthead and subscribe chrome before the headline
# grid starts, and cutting there would land back at "no articles found". The
# operator knob is ``[briefings] max_browse_chars``; this is its code floor for
# a config that predates the field. A source's explicit ``max_chars`` wins over
# both.
_MARKDOWN_MAX_CHARS = 20000

_MODES = ("full", "article")

# ``/render`` appends its own truncation footer to the markdown, worded for the
# interactive skill ("raise --max-chars or switch to --mode article"). Those
# flags mean nothing here, and the footer would be spliced into the synthesis
# prompt as an instruction the model can surface in the delivered briefing. The
# response carries a ``truncated`` flag, so drop the prose and report the fact
# through provenance instead.
_TRUNCATION_FOOTER_RE = re.compile(r"\n*\[Markdown truncated at [^\]]*\]\s*\Z")


def _render_markdown(
    api_url: str, url: str, mode: str, max_chars: int,
) -> tuple[str | None, bool]:
    """Page as markdown + whether it was cut.

    A ``None`` body means the endpoint isn't there (old image).
    """
    resp = httpx.post(
        f"{api_url}/render",
        json={
            "url": url,
            "mode": mode,
            "timeout": 30,
            "keep_session": False,
            "max_chars": max_chars,
        },
        timeout=_FETCH_TIMEOUT,
    )
    if resp.status_code == 404:
        logger.info("browse source: browser image has no /render — using text path")
        return None, False
    data = resp.json()
    if data.get("status") != "ok":
        logger.warning(
            "browse source: render returned status %s for %s", data.get("status"), url,
        )
        return "", False
    markdown = data.get("markdown") or ""
    body = _TRUNCATION_FOOTER_RE.sub("", markdown).strip()
    return body, bool(data.get("truncated"))


def _browse_text(api_url: str, url: str) -> str:
    """Legacy flattened-text path, for a browser image predating /render."""
    resp = httpx.post(
        f"{api_url}/browse",
        json={"url": url, "timeout": 30, "keep_session": False},
        timeout=_FETCH_TIMEOUT,
    )
    data = resp.json()
    if data.get("status") != "ok":
        logger.warning(
            "browse source: browse returned status %s for %s", data.get("status"), url,
        )
        return ""
    return (data.get("text") or "").strip()


def resolve(config: dict, ctx: SourceContext) -> GatheredSource:
    preset_key = config.get("preset")
    url = config.get("url")
    name = None
    if preset_key:
        preset = BROWSE_PRESETS.get(preset_key)
        if not preset:
            return GatheredSource(
                kind="browse", title=str(preset_key),
                provenance=f"(unknown browse preset '{preset_key}')", ok=False,
            )
        url = preset["url"]
        name = preset["name"]
    if not url:
        return GatheredSource(
            kind="browse", title="Browse",
            provenance="(browse source has no url or preset)", ok=False,
        )
    title = name or url
    mode = config.get("mode") or "full"
    if mode not in _MODES:
        mode = "full"
    explicit_max = config.get("max_chars")
    markdown_max = int(
        explicit_max
        or getattr(ctx.module_config, "max_browse_chars", 0)
        or _MARKDOWN_MAX_CHARS
    )
    text_max = int(explicit_max or ctx.module_config.max_source_chars or 5000)

    browser = getattr(ctx.app_config, "browser", None)
    if not browser or not getattr(browser, "enabled", False):
        return GatheredSource(
            kind="browse", title=title,
            provenance="(browse unavailable — browser not enabled)", ok=False,
        )

    api_url = browser.api_url
    if not _BROWSER_LOCK.acquire(timeout=_QUEUE_WAIT_TIMEOUT):
        logger.warning("browse source: browser busy, skipped %s", url)
        return GatheredSource(
            kind="browse", title=title,
            provenance="(browse skipped — browser busy)", ok=False,
        )
    try:
        body, truncated = _render_markdown(api_url, url, mode, markdown_max)
        if body is None:
            body = _browse_text(api_url, url)
            if len(body) > text_max:
                body = body[:text_max]
                truncated = True
    except Exception as e:  # noqa: BLE001
        logger.warning("browse source: fetch failed for %s: %s", url, e)
        return GatheredSource(
            kind="browse", title=title, provenance="(browse fetch failed)", ok=False,
        )
    finally:
        _BROWSER_LOCK.release()

    if not body:
        return GatheredSource(
            kind="browse", title=title, provenance="(browse returned no content)", ok=False,
        )

    provenance = f"frontpage of {title}"
    if truncated:
        provenance += " — truncated"

    return GatheredSource(
        kind="browse", title=title,
        text=f"### {title} ({url})\n{body}",
        provenance=provenance,
        untrusted=True,
    )
