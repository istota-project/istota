"""Polling engine — RSS/Atom + Tumblr/Are.na providers.

Public surface:

* :func:`poll_feed` — single feed, returns :class:`FetchResult`.
* :func:`poll_due_feeds` — scan SQLite for feeds whose ``next_poll_at`` is in
  the past, poll each, persist entries + fetch state.

Conditional GET (etag / last-modified) is honoured for RSS feeds. Errors
back off by doubling ``poll_interval_minutes`` up to ``backoff_max_minutes``.

Rate limiting (ISSUE-347) is four separate things, and they are only useful
together:

* **Pacing.** :func:`poll_due_feeds` holds a minimum gap between two requests
  to the same host. It lives here rather than in a provider so one rule covers
  Are.na, Tumblr and whatever comes next; the key is the host, so RSS pays
  nothing for it.
* **``Retry-After``.** A 429 is answered on the server's own terms instead of
  the generic doubling, clamped at both ends.
* **A 429 is not an error.** It neither increments ``error_count`` nor writes
  ``last_error``, so a throttled channel stops reading as a broken one.
* **Jitter.** Every ``next_poll_at`` is spread, so a set that burst together
  disperses instead of re-forming one doubling later.

**Known gap: this budget does not span users.** The poll runs as a per-user
skill subprocess (``_module.feeds.run_scheduled``, one job per user), so on a
multi-user deployment two users' polls are two processes reaching one host from
one IP with no shared budget. Within-process pacing is still the large win —
the observed bursts are one user's channel set — but nothing here bounds the
cross-user case, and a limiter that did would need to live outside the process.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from istota.feeds import db as feeds_db
from istota.feeds.models import (
    DEFAULT_BACKOFF_MAX_MINUTES,
    DEFAULT_HOST_GAP_SECONDS,
    DEFAULT_JITTER_FRACTION,
    DEFAULT_MAX_PACING_SECONDS,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DEFAULT_RATE_LIMIT_BACKOFF_MINUTES,
    MAX_RATE_LIMIT_BACKOFF_MINUTES,
    EntryRecord,
    FeedRateLimited,
    FeedRecord,
    FetchedItem,
    FetchResult,
    detect_source_type,
    is_http_url,
    media_type_for_url,
    poll_host,
    retry_after_from_headers,
    provider_identifier,
)
from istota.feeds.providers import arena as arena_provider
from istota.feeds.providers import tumblr as tumblr_provider
from istota.feeds.sanitize import (
    dedupe_image_variants,
    extract_images,
    html_to_text,
    remove_images,
    sanitize_html,
)


logger = logging.getLogger("istota.feeds.poller")


# -- single-feed polling ------------------------------------------------------


def poll_feed(
    feed: FeedRecord,
    *,
    tumblr_api_key: str = "",
    http_get: Callable | None = None,
) -> FetchResult:
    """Fetch one feed, dispatching by source_type.

    ``http_get`` is the RSS-fetch hook — defaults to ``httpx.get``. Tests
    inject a stub. Tumblr and Are.na providers manage their own HTTP.
    """
    source = feed.source_type or detect_source_type(feed.url)
    try:
        if source == "tumblr":
            ident = provider_identifier(feed.url)
            items = tumblr_provider.fetch(ident, api_key=tumblr_api_key)
            # A provider that returned normally validated its own collection
            # first, so the list it hands back is the whole window the API was
            # asked for — a complete membership snapshot (ISSUE-388). A
            # malformed payload raises out of `fetch` and lands in the generic
            # handler below, which carries no completeness.
            return FetchResult(
                feed_url=feed.url, items=items, membership_complete=True,
            )
        if source == "arena":
            ident = provider_identifier(feed.url)
            items = arena_provider.fetch(ident)
            return FetchResult(
                feed_url=feed.url, items=items, membership_complete=True,
            )
        return _poll_rss(feed, http_get=http_get)
    except FeedRateLimited as exc:
        # Ahead of the generic handler below, and deliberately not folded into
        # it: a throttle is not a failure of the feed, so it carries no
        # ``error`` and the persist step leaves the error record alone.
        logger.info(
            "poll_feed rate_limited url=%s host=%s retry_after=%s",
            feed.url, exc.host, exc.retry_after,
        )
        return FetchResult(
            feed_url=feed.url,
            rate_limited=True,
            retry_after_seconds=exc.retry_after,
        )
    except Exception as exc:  # noqa: BLE001 — captured into FetchResult
        logger.warning("poll_feed failed url=%s err=%s", feed.url, exc)
        return FetchResult(feed_url=feed.url, error=str(exc))


def _poll_rss(feed: FeedRecord, *, http_get: Callable | None) -> FetchResult:
    """RSS/Atom poll via feedparser. Conditional GET honoured."""
    if http_get is None:
        import httpx
        http_get = httpx.get

    headers: dict[str, str] = {
        "User-Agent": "istota-feeds/0.1 (+https://github.com/istota-project/istota)",
    }
    if feed.etag:
        headers["If-None-Match"] = feed.etag
    if feed.last_modified:
        headers["If-Modified-Since"] = feed.last_modified

    resp = http_get(feed.url, headers=headers, timeout=30.0, follow_redirects=True)
    status = getattr(resp, "status_code", 0)
    if status == 304:
        return FetchResult(feed_url=feed.url, not_modified=True)
    if status == 429:
        # Raised rather than returned so the 429 shape is built in exactly one
        # place, shared with the two providers that raise it out of their own
        # HTTP client.
        raise FeedRateLimited(
            retry_after_from_headers(getattr(resp, "headers", {}) or {}),
            host=poll_host(feed.url, feed.source_type or ""),
        )
    if status >= 400:
        return FetchResult(
            feed_url=feed.url,
            error=f"HTTP {status} fetching feed",
        )

    raw = getattr(resp, "content", None) or getattr(resp, "text", "")
    parsed = _feedparser_parse(raw)

    items: list[FetchedItem] = []
    for entry in parsed.get("entries", []):
        items.append(_rss_entry_to_item(entry))

    # Whether absence from this response means the source dropped an entry
    # (ISSUE-388). Two conditions, catching two different failures, neither
    # substituting for the other.
    #
    # `version` rejects a document that is not a feed at all. An HTML error
    # page served at HTTP 200 parses *cleanly* — `bozo` is False — and yields
    # zero entries, which is byte for byte what a feed that legitimately
    # emptied looks like; `version` is the whole of what tells them apart.
    #
    # Truncation is the other failure, and `version` cannot see it: a response
    # cut off in transit still carries the root element at its head and
    # silently loses its tail. Treating that as complete makes the missing
    # tail historical, ages it out, and destroys read state.
    version = parsed.get("version") or ""
    truncated = _parse_is_truncated(parsed)
    membership_complete = bool(version) and not truncated
    if not membership_complete:
        # No body content and no exception text in the log line: both can carry
        # the response itself, and this runs for every error page a feed
        # serves.
        logger.warning(
            "poll_rss incomplete document url=%s version=%r truncated=%s items=%d",
            feed.url, version, truncated, len(items),
        )

    new_etag = None
    new_last_modified = None
    resp_headers = getattr(resp, "headers", {}) or {}
    if isinstance(resp_headers, dict):
        new_etag = resp_headers.get("ETag") or resp_headers.get("etag")
        new_last_modified = (
            resp_headers.get("Last-Modified") or resp_headers.get("last-modified")
        )
    else:
        # httpx Headers — case-insensitive get
        new_etag = resp_headers.get("etag")
        new_last_modified = resp_headers.get("last-modified")

    if not membership_complete:
        # Never store a validator taken off a document we could not read
        # whole. An ETag answers every later request with a 304, and a 304
        # carries no entries — so the feed could never establish a snapshot
        # again and could not recover until the publisher happened to change
        # something.
        #
        # This covers a truncated response that *did* yield items, not only
        # the empty error page the spec names. A truncation happens in
        # transit, so the ETag is the validator for the full body: storing it
        # pins the feed at 304 while its stored marker never advances, and a
        # feed with no marker is exempt from both retention passes — the
        # unbounded growth this change exists to close. One extra full fetch
        # is the whole cost of refusing it.
        new_etag = None
        new_last_modified = None

    feed_meta = parsed.get("feed", {}) or {}
    return FetchResult(
        feed_url=feed.url,
        items=items,
        etag=new_etag,
        last_modified=new_last_modified,
        discovered_title=feed_meta.get("title"),
        discovered_site_url=feed_meta.get("link"),
        membership_complete=membership_complete,
    )


# Expat's EOF-class parse errors — the messages it produces when the document
# simply stopped. Every other well-formedness complaint describes a defect
# *within* a document that arrived whole.
_EOF_PARSE_ERRORS = (
    "no element found",
    "unclosed token",
    "unclosed cdata section",
    "partial character",
    "unexpected end of file",
)


def _parse_is_truncated(parsed) -> bool:
    """Whether a bozo parse looks like a response that was cut off.

    ``bozo`` alone is far too blunt to gate membership on, and the reason is
    the whole point of this predicate. An undeclared entity and a truncation
    raise the same flag and mean opposite things: the first document arrived
    whole with every entry recovered, and an undeclared entity is the most
    common defect in feeds in the wild. Gating on the raw flag would leave a
    large, unknowable subset of feeds never establishing ``current_document_at``
    — and since the count pass is gated on that too, *neither* retention pass
    would ever touch them. The growth this exists to bound would stay unbounded
    for exactly those feeds, silently.

    So only an EOF-class exception counts. The discriminator is expat's message
    text, which is not an API and can move across Python or expat versions —
    that fragility is why the judgement lives in one place, and why
    ``tests/test_feeds_poller.py`` pins both sides of it with real documents
    through the real ``feedparser`` rather than a stub. A wording change fails
    a test instead of silently switching retention off for every imperfect
    feed, or on for truncated ones.

    A flagged parse it cannot read a message out of at all counts as
    truncated. That is the safe direction: refusing to advance a snapshot
    costs one poll cycle, advancing one wrongly deletes entries.
    """
    if not parsed.get("bozo"):
        return False
    exc = parsed.get("bozo_exception")
    message = ""
    if exc is not None:
        get_message = getattr(exc, "getMessage", None)
        if callable(get_message):
            try:
                message = str(get_message() or "")
            except Exception:  # noqa: BLE001 — a parse error must not fail a poll
                message = ""
        if not message:
            message = str(exc)
    if not message.strip():
        # Flagged, with nothing to inspect. Nothing here establishes that the
        # document arrived whole, so it is not treated as one.
        return True
    lowered = message.lower()
    return any(marker in lowered for marker in _EOF_PARSE_ERRORS)


def _feedparser_parse(raw):
    """Wrap feedparser so the import lives at call time (optional dep)."""
    try:
        import feedparser  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "feedparser is required for RSS polling — install the 'feeds' extra"
        ) from e
    return feedparser.parse(raw)


def _classify_attachment(
    url: str | None,
    mime: str | None,
    medium: str | None,
    images: list[str],
    playable: list[tuple[str, str]],
    *,
    untyped: str,
) -> None:
    """File one attachment as a hero image or as playable media (ISSUE-356).

    ``<enclosure>`` and ``<media:content>`` describe the same thing in two
    vocabularies, and this is the one place that reads either. Three pieces of
    evidence, consulted in order and **falling through** rather than
    short-circuiting: the MIME type, then ``medium``, then the file extension.

    Falling through is the part that has to be exact. An attachment carrying a
    type we do not recognise — ``application/octet-stream`` is a common CDN
    default, and MRSS's ``medium`` legitimately takes values like ``document``
    — still gets the later questions asked of it, and if none of them answers,
    lands on ``untyped``. Short-circuiting the drop there is a hero silently
    lost to a fix about videos, since the old ``media:content`` loop took every
    URL it saw whatever its type said.

    ``untyped`` is what an attachment no evidence classified falls back to, and
    it differs per element because the historical defaults differed:
    ``media:content`` kept such a thing as an image, an ``<enclosure>`` was
    dropped unless it was typed ``image/``. Preserved rather than unified, so
    the fix neither gains nor loses a feed a hero.

    Appends in place. The caller's order is feed order, and the first playable
    attachment is the one the reader gets.
    """
    if not url:
        return
    url = url.strip()
    if not is_http_url(url):
        # The URL ends up in a `src` attribute, so the sanitizer's bar — http
        # and https only — applies on the way in. The stripped value is the one
        # that was checked, so it is also the one that gets stored.
        return

    mime = (mime or "").lower().strip()
    medium = (medium or "").lower().strip()

    # 1. The MIME type, where it says something we understand.
    if mime.startswith("image/"):
        images.append(url)
        return
    if mime.startswith(("video/", "audio/")):
        playable.append((url, mime))
        return

    # 2. `medium`, which an untyped or oddly-typed attachment may still carry.
    if medium == "image":
        images.append(url)
        return
    if medium in ("video", "audio"):
        # Name the concrete format from the extension where we can, since a
        # bare `video/*` is not a usable `type` hint.
        playable.append((url, media_type_for_url(url) or f"{medium}/*"))
        return

    # 3. The extension, then the per-element default.
    guessed = media_type_for_url(url)
    if guessed:
        playable.append((url, guessed))
    elif untyped == "image":
        images.append(url)


def _rss_entry_to_item(entry) -> FetchedItem:
    """Convert a ``feedparser`` entry dict to :class:`FetchedItem`."""
    guid = (
        entry.get("id")
        or entry.get("guid")
        or entry.get("link")
        or entry.get("title", "")
    )
    # Store titles as plain text. Atom feeds (e.g. The Atlantic) ship
    # ``<title type="html">`` with inline markup like ``<em>``; feedparser
    # decodes it to real tags, but the reader renders titles with escaping
    # interpolation, so any tags would show literally. Strip them here.
    title = html_to_text(entry.get("title"))
    url = entry.get("link")
    author = entry.get("author")

    content_html = None
    content_list = entry.get("content") or []
    if content_list:
        content_html = content_list[0].get("value")
    if not content_html:
        content_html = entry.get("summary") or entry.get("description")

    cleaned_html = sanitize_html(content_html)

    # Hero/gallery images = the article's lead image (the first one embedded in
    # the body) plus any enclosure / media:content images. We deliberately take
    # only the *lead* body image, not every inline image, so images the author
    # placed mid-article stay in the body. Resolution variants of one image
    # (e.g. the Guardian's ?width=140/460/700) collapse to the widest.
    body_images = extract_images(cleaned_html)
    media_images: list[str] = []
    playable: list[tuple[str, str]] = []
    for enc in entry.get("enclosures", []) or []:
        _classify_attachment(
            enc.get("href"), enc.get("type"), None, media_images, playable,
            untyped="drop",
        )
    for m in entry.get("media_content") or []:
        _classify_attachment(
            m.get("url"), m.get("type"), m.get("medium"), media_images, playable,
            untyped="image",
        )

    lead_body_image = body_images[:1]  # 0 or 1 element
    image_urls = dedupe_image_variants(lead_body_image + media_images)

    # Drop the hero image(s) from the body so the reader doesn't paint it twice
    # (once as the hero, once at the top of the excerpt). Only images promoted
    # to image_urls are removed; every other inline image is preserved.
    cleaned_html = remove_images(cleaned_html, image_urls)
    content_text = html_to_text(cleaned_html)

    published_at = _published_iso(entry)

    media_url, media_type = playable[0] if playable else (None, None)

    return FetchedItem(
        guid=str(guid) if guid else "",
        title=title,
        url=url,
        author=author,
        content_html=cleaned_html,
        content_text=content_text,
        image_urls=image_urls,
        media_url=media_url,
        media_type=media_type,
        published_at=published_at,
    )


def _published_iso(entry) -> str | None:
    """Pull a UTC ISO 8601 timestamp out of a feedparser entry."""
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                # struct_time has no tz; assume UTC (feedparser normalises).
                dt = datetime(*struct[:6], tzinfo=timezone.utc)
                return dt.isoformat()
            except (TypeError, ValueError):
                continue
    return entry.get("published") or entry.get("updated")


# -- batch polling ------------------------------------------------------------


def poll_due_feeds(
    conn: sqlite3.Connection,
    *,
    tumblr_api_key: str = "",
    backoff_max_minutes: int = DEFAULT_BACKOFF_MAX_MINUTES,
    now: datetime | None = None,
    http_get: Callable | None = None,
    limit: int | None = None,
    host_gap_seconds: float = DEFAULT_HOST_GAP_SECONDS,
    max_pacing_seconds: float = DEFAULT_MAX_PACING_SECONDS,
    jitter_fraction: float = DEFAULT_JITTER_FRACTION,
    rng: random.Random | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[tuple[FeedRecord, FetchResult, int]]:
    """Poll every feed whose ``next_poll_at`` is in the past.

    Returns a list of ``(feed, result, new_entry_count)`` tuples for callers
    who want to log or surface progress.

    ``host_gap_seconds`` is the minimum spacing between two requests to one
    host; ``0`` disables pacing. ``max_pacing_seconds`` bounds the total time
    one run may spend asleep — when it is exhausted the run *stops*, leaving
    the feeds it did not reach still due for the next tick, rather than
    carrying on unpaced. ``sleep`` and ``rng`` are injection points for the
    tests — a pacing test that really slept would be the slowest thing in the
    suite and would prove less than asserting on the gaps requested.
    """
    now = now or datetime.now(timezone.utc)
    # Every timestamp this run writes is compared lexically against another ISO
    # string — the poll claim across processes, and `last_seen_at` against the
    # feed's snapshot marker — so a caller's non-UTC clock is converted once
    # here rather than at each of the four writes.
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    sleep = sleep or time.sleep
    rng = rng or random.Random()
    feeds = feeds_db.feeds_due_for_poll(conn, now=now)
    if limit is not None:
        feeds = feeds[:limit]

    started = time.monotonic()
    last_request_at: dict[str, float] = {}
    paced_seconds = 0.0

    out: list[tuple[FeedRecord, FetchResult, int]] = []
    for feed in feeds:
        host = poll_host(feed.url, feed.source_type or "")
        if host_gap_seconds > 0:
            previous = last_request_at.get(host)
            if previous is not None:
                wait = host_gap_seconds - (time.monotonic() - previous)
                if wait > 0:
                    if paced_seconds + wait > max_pacing_seconds:
                        logger.info(
                            "poll_due_feeds pacing budget spent after %d feed(s); "
                            "%d left due for the next run",
                            len(out), len(feeds) - len(out),
                        )
                        break
                    paced_seconds += wait
                    sleep(wait)

        # Claim the feed immediately before its fetch and after any pacing
        # sleep — a claim taken before the sleep, or before the budget break
        # above, would hold a feed nobody went on to fetch (ISSUE-388). The
        # due list already excluded a claimed feed; this closes the interval
        # between that SELECT and this request, which is where a manual poll
        # and the scheduled one actually collide. A refusal is an ordinary
        # skip: the other process is fetching it, and its result is the one
        # that should decide the feed's membership state.
        claim_now = now + timedelta(seconds=time.monotonic() - started)
        if not feeds_db.claim_feed_for_poll(conn, feed.id, now=claim_now):
            logger.info(
                "poll_due_feeds feed_id=%s skipped: claimed by another poll",
                feed.id,
            )
            continue
        # Recorded after the claim, so a skipped feed does not pace the run
        # against a request that was never issued.
        last_request_at[host] = time.monotonic()

        result = poll_feed(feed, tumblr_api_key=tumblr_api_key, http_get=http_get)
        # Pacing means the loop can run for minutes, so the schedule is
        # computed against the clock as it is now rather than against the
        # instant the batch started — otherwise the tail of a paced run is
        # scheduled from a base already well in the past.
        poll_now = now + timedelta(seconds=time.monotonic() - started)
        new_count = _persist_poll(conn, feed, result, now=poll_now,
                                  backoff_max_minutes=backoff_max_minutes,
                                  jitter_fraction=jitter_fraction, rng=rng)
        out.append((feed, result, new_count))
    return out


def _persist_poll(
    conn: sqlite3.Connection,
    feed: FeedRecord,
    result: FetchResult,
    *,
    now: datetime,
    backoff_max_minutes: int,
    jitter_fraction: float = DEFAULT_JITTER_FRACTION,
    rng: random.Random | None = None,
) -> int:
    """Write fetched entries and update fetch state for one poll outcome."""
    rng = rng or random.Random()
    fetched_iso = now.isoformat()
    new_count = 0

    if result.rate_limited:
        # A throttled feed is healthy, so `error_count` and `last_error` are
        # written back **unchanged** rather than cleared: the 429 says nothing
        # about whether a previous real failure is fixed. Not incrementing is
        # also what stops a throttle carrying a doubled interval forward once
        # it clears — the doubling reads `error_count`, which never moved.
        next_interval = _rate_limit_interval(
            feed.poll_interval_minutes, result.retry_after_seconds,
        )
        next_poll = _schedule(now, next_interval, jitter_fraction, rng)
        feeds_db.update_feed_fetch_state(
            conn, feed.id,
            etag=feed.etag,
            last_modified=feed.last_modified,
            # `last_fetched_at` is written back unchanged too, for the same
            # reason as the three above it: nothing was fetched. Advancing it
            # would have the feed assert a successful fetch that did not
            # happen, which is the failure this branch exists to avoid, merely
            # pointed the other way.
            last_fetched_at=feed.last_fetched_at,
            last_error=feed.last_error,
            error_count=feed.error_count,
            next_poll_at=next_poll,
            # The one place a throttle is recorded. `last_error` deliberately
            # does not carry it — a throttled channel is healthy — but that
            # left it recorded nowhere, so a run turned away on every feed
            # reported a clean poll that happened to find nothing.
            last_throttled_at=fetched_iso,
            # `current_document_at` is deliberately not named: a throttle
            # returned no entry list, so the last complete snapshot stands.
            poll_claimed_until=None,
        )
        conn.commit()
        return 0

    if result.error:
        next_interval = _backoff_interval(
            feed.poll_interval_minutes, feed.error_count + 1, backoff_max_minutes,
        )
        next_poll = _schedule(now, next_interval, jitter_fraction, rng)
        feeds_db.update_feed_fetch_state(
            conn, feed.id,
            etag=feed.etag,
            last_modified=feed.last_modified,
            last_fetched_at=fetched_iso,
            last_error=result.error,
            error_count=feed.error_count + 1,
            next_poll_at=next_poll,
            # Preserved: an error says nothing about whether the throttle that
            # preceded it has cleared.
            last_throttled_at=feed.last_throttled_at,
            # Same as above: an error established nothing about membership, so
            # the snapshot marker is left alone and only the claim is released.
            poll_claimed_until=None,
        )
        conn.commit()
        return 0

    document_ranks: dict[str, int] | None = None
    if not result.not_modified:
        items = result.items
        if result.membership_complete:
            # A complete response is the feed's window, so its source order is
            # the rank. Duplicate guids take their first occurrence — later
            # ones are the same entry, and giving one two ranks would make the
            # window depend on which copy was written last.
            #
            # Stage 2 narrows this to the admitted window; today every
            # identifiable item is admitted, which is the same thing whenever
            # the response fits under the per-feed count.
            deduped: list[FetchedItem] = []
            document_ranks = {}
            for item in result.items:
                if not item.guid or item.guid in document_ranks:
                    continue
                document_ranks[item.guid] = len(deduped)
                deduped.append(item)
            items = deduped
    else:
        items = []

    if items:
        records = [
            EntryRecord(
                id=0,
                feed_id=feed.id,
                guid=item.guid,
                title=item.title,
                url=item.url,
                author=item.author,
                content_html=item.content_html,
                content_text=item.content_text,
                image_urls=item.image_urls,
                embed_url=item.embed_url,
                file_url=item.file_url,
                media_url=item.media_url,
                media_type=item.media_type,
                published_at=item.published_at,
                fetched_at=fetched_iso,
                status="unread",
            )
            for item in items
            if item.guid
        ]
        new_count = feeds_db.insert_entries(
            conn, feed.id, records, document_ranks=document_ranks,
        )

    interval = max(feed.poll_interval_minutes, DEFAULT_POLL_INTERVAL_MINUTES)
    next_poll = _schedule(now, interval, jitter_fraction, rng)
    # The marker moves only for a response we trusted as a complete snapshot,
    # and it is exactly the timestamp the admitted entries were stamped with —
    # that identity is what later tells a current entry from history. A 304 or
    # an incomplete parse says nothing, so it leaves the last one standing.
    snapshot_at: object = feeds_db.UNCHANGED
    if result.membership_complete and not result.not_modified:
        snapshot_at = fetched_iso
    feeds_db.update_feed_fetch_state(
        conn, feed.id,
        etag=result.etag if not result.not_modified else feed.etag,
        last_modified=result.last_modified if not result.not_modified else feed.last_modified,
        last_fetched_at=fetched_iso,
        last_error=None,
        error_count=0,
        next_poll_at=next_poll,
        discovered_title=result.discovered_title,
        discovered_site_url=result.discovered_site_url,
        # Cleared: a fetch that got through is the throttle having lifted, so
        # the column means "throttled now" rather than "throttled once".
        last_throttled_at=None,
        current_document_at=snapshot_at,
        poll_claimed_until=None,
    )
    conn.commit()
    return new_count


def _backoff_interval(base_minutes: int, error_count: int, cap_minutes: int) -> int:
    """Exponential backoff: double on every consecutive error, capped."""
    base = max(base_minutes, 1)
    interval = base * (2 ** max(error_count - 1, 0))
    return min(interval, cap_minutes)


def _rate_limit_interval(base_minutes: int, retry_after_seconds: int | None) -> float:
    """How long to stand off after a 429, in minutes.

    Clamped at both ends, for different reasons.

    The floor is **the same quantity the success path uses**, and that identity
    is the point rather than a coincidence: the success path schedules
    ``max(poll_interval_minutes, DEFAULT_POLL_INTERVAL_MINUTES)``, so a floor of
    the raw ``poll_interval_minutes`` would let a 429 come back *sooner* than a
    successful poll on any feed configured under 30 minutes — increasing
    pressure on the host that just turned us away, which is the opposite of
    what this function is for. `DEFAULT_RATE_LIMIT_BACKOFF_MINUTES` is folded in
    on top so being throttled costs at least as much as an ordinary cadence and
    usually more. The invariant, stated so a future edit can be checked against
    it: a 429 never schedules earlier than a success would.

    The ceiling stops one unverified header taking a channel off the air for a
    week.

    ``None`` means the server named no time, which is why it is a separate
    branch from a small number rather than being defaulted earlier.
    """
    floor = float(max(
        base_minutes,
        DEFAULT_POLL_INTERVAL_MINUTES,
        DEFAULT_RATE_LIMIT_BACKOFF_MINUTES,
    ))
    if retry_after_seconds is None:
        return floor
    named = retry_after_seconds / 60.0
    return min(max(named, floor), float(MAX_RATE_LIMIT_BACKOFF_MINUTES))


def _schedule(
    now: datetime, minutes: float, jitter_fraction: float, rng: random.Random,
) -> str:
    """``now`` plus ``minutes``, spread by ``jitter_fraction``, as ISO 8601.

    Applied on the success path as well as the two failure paths: a set that is
    synchronised for any reason drifts apart from the next poll onwards rather
    than staying in lockstep for good. ``0`` is exact, which is what the tests
    asserting a particular interval depend on.
    """
    # Clamped below 1.0: at or above it the low end of the range is zero or
    # negative, so `next_poll_at` lands at or before `now` and the feed is
    # permanently due on every tick. Production only ever passes the 0.1
    # constant, but the parameter is public and documented as an override.
    fraction = min(max(jitter_fraction, 0.0), 0.99)
    spread = 1.0
    if fraction > 0:
        spread = rng.uniform(1.0 - fraction, 1.0 + fraction)
    return (now + timedelta(minutes=minutes * spread)).isoformat()
