#!/usr/bin/env python3
"""Seed the web dev mock with real, freshly-polled feed data.

Polls a curated set of real RSS/Atom feeds through the *actual*
``istota.feeds.poller.poll_feed`` (so the image-dedup / hero-strip logic runs
exactly as it does in production) and writes ``web/dev-feed-data.json`` in the
shape the Vite mock API expects. Run:

    uv run python scripts/dev/seed_feed_preview.py

then start the frontend against the mock:

    cd web && VITE_MOCK_API=1 npm run dev

Re-run this script to re-poll; refresh the browser to iterate. The JSON file
is gitignored — it's throwaway local data.

The feed list deliberately mixes the known offenders:
  * Guardian    — 3 ``<media:content>`` resolution variants (was 3× hero).
  * PetaPixel   — lead image embedded in the body (was 2× hero).
  * The Verge / Ars — same body-embedded-lead shape, full-ish content.
plus a couple of text-heavy feeds so text cards show up too.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Allow running from the repo root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from istota.feeds.models import FeedRecord
from istota.feeds.poller import poll_feed


# (url, display title, category title)
FEEDS: list[tuple[str, str, str]] = [
    ("https://www.theguardian.com/international/rss", "The Guardian", "News"),
    ("https://petapixel.com/feed/", "PetaPixel", "Photography"),
    ("https://www.theverge.com/rss/index.xml", "The Verge", "Tech"),
    ("https://feeds.arstechnica.com/arstechnica/index", "Ars Technica", "Tech"),
    ("https://www.thisiscolossal.com/feed/", "Colossal", "Art"),
    ("https://daringfireball.net/feeds/main", "Daring Fireball", "Blogs"),
]

MAX_ENTRIES_PER_FEED = 12


def _feed_record(feed_id: int, url: str, title: str, category_id: int) -> FeedRecord:
    return FeedRecord(
        id=feed_id,
        url=url,
        title=title,
        site_url=None,
        category_id=category_id,
        source_type="rss",
        etag=None,
        last_modified=None,
        last_fetched_at=None,
        last_error=None,
        error_count=0,
        poll_interval_minutes=30,
        next_poll_at=None,
    )


def main() -> int:
    categories: dict[str, int] = {}
    feeds_out: list[dict] = []
    entries_out: list[dict] = []
    entry_id = 0

    for feed_id, (url, title, category) in enumerate(FEEDS, start=1):
        cat_id = categories.setdefault(category, len(categories) + 1)
        feed = _feed_record(feed_id, url, title, cat_id)
        print(f"polling {title} … ", end="", flush=True)
        result = poll_feed(feed, http_get=httpx.get)
        if result.error:
            print(f"ERROR: {result.error}")
            continue
        site_url = result.discovered_site_url or url
        feed_source = {
            "id": feed_id,
            "title": result.discovered_title or title,
            "site_url": site_url,
            "category": {"id": cat_id, "title": category},
        }
        feeds_out.append(feed_source)

        items = result.items[:MAX_ENTRIES_PER_FEED]
        print(f"{len(items)} entries")
        for item in items:
            entry_id += 1
            published = item.published_at or datetime.now(timezone.utc).isoformat()
            entries_out.append(
                {
                    "id": entry_id,
                    "title": item.title or "(untitled)",
                    "url": item.url or site_url,
                    "content": item.content_html or "",
                    "images": item.image_urls,
                    "feed": feed_source,
                    # First few unread so the Unseen filter has something to show.
                    "status": "unread" if entry_id <= 8 else "read",
                    "starred": False,
                    "starred_at": "",
                    "published_at": published,
                    "created_at": published,
                }
            )

    out_path = Path(__file__).resolve().parents[2] / "web" / "dev-feed-data.json"
    out_path.write_text(json.dumps({"feeds": feeds_out, "entries": entries_out}, indent=2))
    print(f"\nwrote {len(entries_out)} entries from {len(feeds_out)} feeds → {out_path}")
    print("start the preview:  cd web && VITE_MOCK_API=1 npm run dev")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
