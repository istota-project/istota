"""Browse source resolver — a user-defined URL fetched via the browse skill.

A source references either a bundled preset key (``ap``, ``reuters``, …) or an
arbitrary ``url``. The page text is fetched through the headless browser API and
truncated. Requires ``config.browser.enabled``; off → empty + note. Content is
untrusted (the block's companion ``untrusted_input`` skill carries the handling
rules).

Source config shape::

    {"url": "https://…", "preset": "ap"|null, "max_chars": 5000}
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
    max_chars = int(config.get("max_chars", ctx.module_config.max_source_chars) or 5000)

    browser = getattr(ctx.app_config, "browser", None)
    if not browser or not getattr(browser, "enabled", False):
        return GatheredSource(
            kind="browse", title=title,
            provenance="(browse unavailable — browser not enabled)", ok=False,
        )

    api_url = browser.api_url
    try:
        resp = httpx.post(
            f"{api_url}/browse",
            json={"url": url, "timeout": 30, "keep_session": False},
            timeout=_FETCH_TIMEOUT,
        )
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("browse source: fetch failed for %s: %s", url, e)
        return GatheredSource(
            kind="browse", title=title, provenance="(browse fetch failed)", ok=False,
        )

    if data.get("status") != "ok":
        return GatheredSource(
            kind="browse", title=title,
            provenance=f"(browse returned status {data.get('status')})", ok=False,
        )

    text = data.get("text", "") or ""
    if not text.strip():
        return GatheredSource(
            kind="browse", title=title, provenance="(browse returned no text)", ok=False,
        )
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[truncated]"

    return GatheredSource(
        kind="browse", title=title,
        text=f"### {title} ({url})\n{text}",
        provenance=f"frontpage of {title}",
    )
