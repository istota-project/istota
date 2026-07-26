"""Browse source resolver — a user-defined URL fetched via the browse skill.

A source references either a bundled preset key (``ap``, ``reuters``, …) or an
arbitrary ``url``. The page is fetched through the headless browser API as
**markdown**, so a headline arrives attached to its URL and under the section
heading it sits beneath; the older flattened-text path dropped every href, which
is why a live frontpage could read as a dead one (ISSUE-192). A browser image
predating ``/render`` answers 404 and we fall back to that text path. Requires
``config.browser.enabled``; off → empty + note. Content is untrusted (the
block's companion ``untrusted_input`` skill carries the handling rules).

Source config shape::

    {"url": "https://…", "preset": "ap"|null, "mode": "full"|"article",
     "max_chars": 20000}
"""

from __future__ import annotations

import logging

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

_FETCH_TIMEOUT = 60.0

# Markdown carries the URLs the flattened text dropped, so it needs a bigger
# budget than ``max_source_chars`` (5000) — a frontpage spends its first couple
# of thousand characters on masthead and subscribe chrome before the headline
# grid starts, and cutting there would land back at "no articles found". A
# source's explicit ``max_chars`` still wins.
_MARKDOWN_MAX_CHARS = 20000

_MODES = ("full", "article")


def _render_markdown(api_url: str, url: str, mode: str, max_chars: int) -> str | None:
    """Page as markdown. ``None`` means the endpoint isn't there (old image)."""
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
        return None
    data = resp.json()
    if data.get("status") != "ok":
        logger.warning(
            "browse source: render returned status %s for %s", data.get("status"), url,
        )
        return ""
    return (data.get("markdown") or "").strip()


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
    markdown_max = int(explicit_max or _MARKDOWN_MAX_CHARS)
    text_max = int(explicit_max or ctx.module_config.max_source_chars or 5000)

    browser = getattr(ctx.app_config, "browser", None)
    if not browser or not getattr(browser, "enabled", False):
        return GatheredSource(
            kind="browse", title=title,
            provenance="(browse unavailable — browser not enabled)", ok=False,
        )

    api_url = browser.api_url
    try:
        body = _render_markdown(api_url, url, mode, markdown_max)
        if body is None:
            body = _browse_text(api_url, url)
            if len(body) > text_max:
                body = body[:text_max] + "\n[truncated]"
    except Exception as e:  # noqa: BLE001
        logger.warning("browse source: fetch failed for %s: %s", url, e)
        return GatheredSource(
            kind="browse", title=title, provenance="(browse fetch failed)", ok=False,
        )

    if not body:
        return GatheredSource(
            kind="browse", title=title, provenance="(browse returned no content)", ok=False,
        )

    return GatheredSource(
        kind="browse", title=title,
        text=f"### {title} ({url})\n{body}",
        provenance=f"frontpage of {title}",
    )
