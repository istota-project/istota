"""Bounded cross-entry image suppression for the reader (ISSUE-162).

A reblog is a genuinely distinct entry — different post, often a different
blog, sometimes its own commentary — so it always renders. What reads as
noise is painting the *same picture* again a few cards down. This module
decides which image tiles to hide, given the entries about to be rendered
and the recent owners of their image keys.

The rule, stated once:

    An image on entry *E* is suppressed when another entry carries the same
    image (by :func:`~istota.feeds.sanitize.image_identity`) and is **newer**
    than *E* by no more than the look-back window.

Consequences worth keeping in mind:

* The newest carrier always keeps the tile, so scrolling a reverse-chronological
  reader you see each picture once, at the first card that mentions it.
* The decision is a function of the two entries alone — no session state, no
  "already served" ledger — so it is stable across paging and reloads, and a
  given page renders identically however you arrived at it.
* It is bounded: a photo resurfacing after the window renders again. Six-month-old
  images are not silently hidden forever, which is what an all-time seen-index
  would do.
* Entry rows are never mutated. This is a display decision computed at read time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from istota.feeds.sanitize import image_identity


# Look-back window used when the user hasn't set one. Long enough to cover a
# reblog wave working through the feeds you follow, short enough that an image
# genuinely resurfacing later still shows.
DEFAULT_WINDOW_DAYS = 14

_SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class PageEntry:
    """One entry about to be rendered.

    ``seen_ts`` is epoch seconds — ``None`` when the entry carries no parseable
    timestamp, in which case it neither suppresses nor is suppressed (there is
    no way to tell whether it falls inside the window).
    """

    entry_id: int
    seen_ts: int | None
    image_urls: list[str] = field(default_factory=list)


def parse_seen_ts(value: str | None) -> int | None:
    """Parse a stored timestamp to epoch seconds, or ``None``.

    Entry timestamps are normally ISO 8601 UTC written by the poller, but the
    RSS path falls back to whatever string the feed shipped, so an RFC 822 date
    (``Thu, 16 Jul 2026 10:00:00 +0000``) shows up in the wild. A naive value is
    read as UTC. Anything unrecognised is ``None`` rather than a guess.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def entry_seen_ts(published_at: str | None, fetched_at: str | None) -> int | None:
    """The timestamp an entry is placed at: published date, else fetch time."""
    return parse_seen_ts(published_at) or parse_seen_ts(fetched_at)


def plan_suppression(
    entries: list[PageEntry],
    owners: list[tuple[str, int, int]],
    *,
    window_days: int,
) -> dict[int, set[str]]:
    """Return ``{entry_id: {url, …}}`` of image tiles to hide.

    ``owners`` is ``(image_key, entry_id, seen_ts)`` for every entry carrying
    one of the page's image keys inside the window — including the page's own
    entries. Entries absent from the result keep all their images.
    """
    if window_days <= 0 or not entries or not owners:
        return {}

    window = window_days * _SECONDS_PER_DAY
    by_key: dict[str, list[tuple[int, int]]] = {}
    for key, owner_id, owner_ts in owners:
        by_key.setdefault(key, []).append((owner_id, owner_ts))

    plan: dict[int, set[str]] = {}
    for entry in entries:
        if entry.seen_ts is None:
            continue
        for url in entry.image_urls:
            key = image_identity(url)
            for owner_id, owner_ts in by_key.get(key, ()):
                if owner_id == entry.entry_id:
                    continue
                if not _is_newer(owner_id, owner_ts, entry.entry_id, entry.seen_ts):
                    continue
                if owner_ts - entry.seen_ts > window:
                    continue
                plan.setdefault(entry.entry_id, set()).add(url)
                break
    return plan


def _is_newer(owner_id: int, owner_ts: int, entry_id: int, entry_ts: int) -> bool:
    """Whether the owner sorts ahead of the entry, ties broken by id.

    Two entries published at the same instant would otherwise each suppress the
    other (or neither, depending on iteration order); the id tiebreak makes the
    winner deterministic so the same card keeps the tile on every page load.
    """
    if owner_ts != entry_ts:
        return owner_ts > entry_ts
    return owner_id > entry_id
