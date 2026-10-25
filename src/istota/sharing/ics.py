"""ICS feeds: a calendar a user shares with Istota by its secret address.

The address is a bearer secret. It is entered in the web UI only, stored under
the daemon-owned `secrets` service `sources.SECRET_SERVICE` (key: the source
id) and read only here, by the add route's one validating fetch and by the
scheduler's poll. No verb takes it, no log line names more than its host, and
no `FeedError` carries any part of it.

Every fetch goes through `sandbox.guarded_http.guarded_send`: up to three
redirects, each re-checked, the body capped at `[sharing] ics_max_bytes`, plain
http and private addresses only for a `[sharing] private_hosts` host. The poll
(`poll_due_feeds`, the `sharing-ics-poll` gate) sends conditional GETs and
backs off with the feeds poller's own schedule; the last good body sits in
`shared_feed_cache`, and a task reads that, never the network.

`expand` turns a cached body into the occurrences in a window, recurrences
included and capped, for the calendar skill, which fences every text field.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from istota.sandbox import guarded_http, net_guard
from istota.sharing import sources

logger = logging.getLogger("istota.sharing.ics")

KIND = "ics_feed"
MAX_URL_CHARS = 2048
NAME_MAX_CHARS = 120
MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 30.0
FETCH_DEADLINE_SECONDS = 90.0
MAX_OCCURRENCES = 2000
# The longest an erroring feed waits between tries: a provider that is down
# for a week is asked once a day, not every quarter hour.
MAX_BACKOFF_MINUTES = 24 * 60
POLL_BATCH = 200
_MAX_VALIDATOR_CHARS = 256
_HEADERS = {"Accept": "text/calendar, text/plain;q=0.5, */*;q=0.1", "User-Agent": "Istota calendar feed"}


class FeedError(Exception):
    """A feed that could not be added or read, as a fixed reason code and nothing else."""

    def __init__(self, code: str, *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class FeedInfo:
    name: str | None


@dataclass(frozen=True)
class FeedEvent:
    """One occurrence, shaped like the CalDAV path's: naive wall clock plus its zone."""

    uid: str
    summary: str
    start: datetime
    end: datetime
    location: str | None
    description: str | None
    all_day: bool
    timezone: str | None


@dataclass
class PollReport:
    polled: int = 0
    changed: int = 0
    unchanged: int = 0
    failed: int = 0


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ addresses

def normalize_feed_url(raw: object, *, private_hosts) -> str:
    """The address as stored and fetched: ``webcal`` read as https, no fragment.

    Scheme and host are lowercased; path and query are kept as given, since a
    provider's secret is often in either. ``invalid_url`` for anything that
    is not an http(s) address with a host and no userinfo, ``host_refused``
    for plain http to a host not in ``private_hosts``.
    """
    if not isinstance(raw, str):
        raise FeedError("invalid_url")
    text = raw.strip()
    if not text or len(text) > MAX_URL_CHARS:
        raise FeedError("invalid_url")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise FeedError("invalid_url")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise FeedError("invalid_url") from None
    scheme = parts.scheme.lower()
    if scheme in ("webcal", "webcals"):
        scheme = "https"
    if scheme not in ("http", "https") or not parts.hostname:
        raise FeedError("invalid_url")
    if parts.username is not None or parts.password is not None:
        raise FeedError("invalid_url")
    host = net_guard.normal_host(parts.hostname)
    if not host:
        raise FeedError("invalid_url")
    if scheme == "http" and host not in net_guard.host_set(private_hosts):
        raise FeedError("host_refused")
    name = f"[{host}]" if ":" in host else host
    netloc = name if port is None or port == guarded_http.default_port(scheme) else f"{name}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def remote_key(normalized_url: str) -> str:
    """The row's identity for a feed: the SHA-256 of its normalized address."""
    return hashlib.sha256(normalized_url.encode()).hexdigest()


def remote_host(normalized_url: str) -> str:
    """``host[:port]``, the only part of the address anything shows."""
    parts = urlsplit(normalized_url)
    scheme = parts.scheme
    return guarded_http.host_header(parts.hostname or "", parts.port or guarded_http.default_port(scheme), scheme)


# ----------------------------------------------------------------- validation

def _parse(raw: bytes):
    from icalendar import Calendar

    text = raw.decode("utf-8", errors="replace").lstrip("﻿").lstrip()
    if not text[:15].upper().startswith("BEGIN:VCALENDAR"):
        raise ValueError("not a calendar")
    calendar = Calendar.from_ical(text)
    if getattr(calendar, "name", "") != "VCALENDAR":
        raise ValueError("not a calendar")
    return calendar


def validate_feed(raw: bytes) -> FeedInfo:
    """The feed's name if ``raw`` parses as a VCALENDAR; ``not_a_calendar`` otherwise."""
    if not raw:
        raise FeedError("not_a_calendar")
    try:
        calendar = _parse(raw)
    except Exception:
        raise FeedError("not_a_calendar") from None
    name = calendar.get("X-WR-CALNAME")
    name = " ".join(str(name).split())[:NAME_MAX_CHARS] if name is not None else ""
    return FeedInfo(name=name or None)


# ------------------------------------------------------------------- fetching

def _retry_after(value: str | None) -> int | None:
    if value and value.strip().isdigit():
        return int(value.strip())
    return None


def _fetch(config, url: str, *, etag: str | None = None, last_modified: str | None = None):
    """One guarded GET; the answer for a 200 or 304, `FeedError` for anything else."""
    headers = dict(_HEADERS)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    with guarded_http.new_client(timeout=FETCH_TIMEOUT_SECONDS) as client:
        try:
            response = guarded_http.guarded_send(
                client, "GET", url,
                private_hosts=config.sharing.private_hosts,
                max_bytes=config.sharing.ics_max_bytes,
                timeout=FETCH_TIMEOUT_SECONDS,
                deadline_seconds=FETCH_DEADLINE_SECONDS,
                max_redirects=MAX_REDIRECTS,
                headers=headers,
            )
        except guarded_http.GuardRefused as exc:
            raise FeedError(exc.code) from None
    if response.status_code in (200, 304):
        return response
    raise FeedError(
        f"http_{response.status_code}",
        retry_after=_retry_after(response.headers.get("retry-after")),
    )


def _validator(value: str | None) -> str | None:
    if value and len(value) <= _MAX_VALIDATOR_CHARS:
        return value
    return None


def _store_cache(conn: sqlite3.Connection, source_id: int, response, fetched_at: str) -> None:
    conn.execute(
        "INSERT INTO shared_feed_cache (source_id, body, etag, last_modified, fetched_at)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET body = excluded.body,"
        " etag = excluded.etag, last_modified = excluded.last_modified,"
        " fetched_at = excluded.fetched_at",
        (source_id, response.content, _validator(response.headers.get("etag")),
         _validator(response.headers.get("last-modified")), fetched_at),
    )


def read_cached(conn: sqlite3.Connection, source_id: int) -> bytes | None:
    """The feed's last good body, or None. The caller has the source from `sources_for_task`."""
    row = conn.execute(
        "SELECT body FROM shared_feed_cache WHERE source_id = ?", (source_id,),
    ).fetchone()
    return bytes(row[0]) if row is not None else None


def cache_times(conn: sqlite3.Connection, source_id: int) -> str | None:
    """When the cached body was fetched, or None."""
    row = conn.execute(
        "SELECT fetched_at FROM shared_feed_cache WHERE source_id = ?", (source_id,),
    ).fetchone()
    return row[0] if row is not None else None


# --------------------------------------------------------------------- adding

def add_feed(conn: sqlite3.Connection, config, user_id: str, url: str) -> int:
    """Subscribe ``user_id`` to the feed at ``url``; its source id.

    Fetches once and validates before anything is written, then writes the
    row, the address and the cache in one transaction. A feed the user
    already has raises `sources.SourceExists` with its id; one they removed is
    reactivated under its old id with the address given now.
    """
    settings = config.sharing
    normalized = normalize_feed_url(url, private_hosts=settings.private_hosts)
    key = remote_key(normalized)
    existing = sources.find_source(conn, user_id, KIND, key)
    if existing is not None and existing.state in sources.REACHABLE_STATES:
        raise sources.SourceExists(existing.id)
    if len(sources.sources_for_user(conn, user_id, kinds=(KIND,))) >= settings.ics_max_feeds_per_user:
        raise FeedError("too_many_feeds")

    response = _fetch(config, normalized)
    if response.status_code != 200:
        # A 304 to a request that named no validator is not an answer.
        raise FeedError(f"http_{response.status_code}")
    info = validate_feed(response.content)
    host = remote_host(normalized)
    extras = {"name": info.name} if info.name else {}
    now = datetime.now(timezone.utc)

    from istota.credentials import store as secrets_store

    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Read again under the write lock: a second add may have landed during the fetch.
        existing = sources.find_source(conn, user_id, KIND, key)
        if existing is not None and existing.state in sources.REACHABLE_STATES:
            raise sources.SourceExists(existing.id)
        if len(sources.sources_for_user(conn, user_id, kinds=(KIND,))) >= settings.ics_max_feeds_per_user:
            raise FeedError("too_many_feeds")
        if existing is not None:
            source_id = existing.id
            sources.set_state(conn, source_id, user_id, "active")
            sources.set_details(conn, source_id, user_id, remote_host=host, extras=extras)
        else:
            source_id = sources.create_source(
                conn, user_id=user_id, kind=KIND, state="active", remote_host=host,
                remote_key=key, access="read", extras=extras,
            )
        secrets_store.set_secret(
            None, user_id, sources.SECRET_SERVICE, str(source_id), normalized, connection=conn,
        )
        _store_cache(conn, source_id, response, _iso(now))
        sources.set_poll_times(
            conn, source_id, user_id, last_ok_at=_iso(now),
            next_poll_at=_iso(now + timedelta(seconds=settings.ics_poll_interval)),
        )
    except BaseException:
        conn.rollback()
        raise
    logger.info("ics_feed_added source=%s user=%s host=%s", source_id, user_id, host)
    return source_id


def remove_feed(conn: sqlite3.Connection, user_id: str, source_id: int) -> bool:
    """Unsubscribe: the row goes to ``revoked`` and takes its address and cache with it."""
    source = sources.get_source(conn, source_id, user_id)
    if source is None or source.kind != KIND or source.state == "revoked":
        return False
    removed = sources.revoke(conn, source_id, user_id, reason="user_removed")
    if removed:
        logger.info("ics_feed_removed source=%s user=%s", source_id, user_id)
    return removed


# -------------------------------------------------------------------- polling

def _due_feeds(conn: sqlite3.Connection, now: datetime) -> list[sources.SharedSource]:
    return sources.due_sources(conn, KIND, _iso(now), limit=POLL_BATCH)


def _base_minutes(config) -> int:
    return max(1, -(-int(config.sharing.ics_poll_interval) // 60))


def _next_after_failure(config, error_count: int, code: str, retry_after: int | None,
                        now: datetime) -> datetime:
    from istota.feeds.poller import _backoff_interval, _rate_limit_interval

    base = _base_minutes(config)
    if code == "http_429":
        minutes = _rate_limit_interval(base, retry_after)
    else:
        minutes = _backoff_interval(base, error_count, MAX_BACKOFF_MINUTES)
    return now + timedelta(minutes=minutes)


def _poll_one(config, source: sources.SharedSource, now: datetime) -> str:
    """Fetch one feed and record the result; ``changed``, ``unchanged`` or ``failed``."""
    from istota import db
    from istota.credentials import store as secrets_store

    with db.get_db(config.db_path) as conn:
        try:
            url = secrets_store.get_secret(
                None, source.user_id, sources.SECRET_SERVICE, str(source.id),
                connection=conn, touch=False, strict=True,
            )
        except Exception:
            url = False
        if url is None:
            sources.revoke(conn, source.id, source.user_id, reason="secret_missing")
            conn.commit()
            logger.warning("ics_poll source=%s reason=secret_missing", source.id)
            return "failed"
        validators = conn.execute(
            "SELECT etag, last_modified FROM shared_feed_cache WHERE source_id = ?", (source.id,),
        ).fetchone()

    code, retry_after, response, info = None, None, None, None
    if url is False:
        code = "secret_unreadable"
    else:
        try:
            response = _fetch(
                config, url,
                etag=validators[0] if validators else None,
                last_modified=validators[1] if validators else None,
            )
            if response.status_code == 200:
                info = validate_feed(response.content)
        except FeedError as exc:
            code, retry_after = exc.code, exc.retry_after

    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = sources.get_source(conn, source.id, source.user_id)
        if current is None or current.state not in sources.REACHABLE_STATES:
            conn.commit()
            return "failed"
        if code is not None:
            sources.set_state(conn, source.id, source.user_id, "error", reason=code)
            nxt = _next_after_failure(config, current.error_count + 1, code, retry_after, now)
            sources.set_poll_times(conn, source.id, source.user_id, next_poll_at=_iso(nxt))
            conn.commit()
            logger.info("ics_poll source=%s host=%s reason=%s", source.id, source.remote_host, code)
            return "failed"
        if current.state == "error":
            sources.set_state(conn, source.id, source.user_id, "active")
        outcome = "unchanged"
        if response.status_code == 200:
            _store_cache(conn, source.id, response, _iso(now))
            extras = dict(current.extras)
            if info.name:
                extras["name"] = info.name
            else:
                extras.pop("name", None)
            sources.set_details(conn, source.id, source.user_id, extras=extras)
            outcome = "changed"
        sources.set_poll_times(
            conn, source.id, source.user_id, last_ok_at=_iso(now),
            next_poll_at=_iso(now + timedelta(seconds=config.sharing.ics_poll_interval)),
        )
        conn.commit()
        return outcome


def poll_due_feeds(config, now: datetime | None = None) -> PollReport:
    """Fetch every feed that is due, one at a time; never raises.

    One feed's failure is recorded on its row and the loop goes on. Nothing is
    polled without the master key, since every address is encrypted with it
    and reading none would look like every secret gone.
    """
    from istota import db
    from istota.credentials import store as secrets_store

    report = PollReport()
    now = now or datetime.now(timezone.utc)
    try:
        if not secrets_store.secret_key_available():
            return report
        with db.get_db(config.db_path) as conn:
            due = _due_feeds(conn, now)
    except Exception as exc:  # noqa: BLE001 - the gate never raises
        logger.warning("ics_poll_scan_failed class=%s", type(exc).__name__)
        return report
    for source in due:
        try:
            outcome = _poll_one(config, source, now)
        except Exception as exc:  # noqa: BLE001 - one feed never stops the others
            logger.warning("ics_poll_failed source=%s class=%s", source.id, type(exc).__name__)
            outcome = "failed"
        report.polled += 1
        setattr(report, outcome, getattr(report, outcome) + 1)
    return report


# ------------------------------------------------------------------ expansion

def _as_reference(value, reference: datetime) -> datetime:
    """``value`` (a date or datetime) in a form comparable with ``reference``."""
    if not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if reference.tzinfo is None:
        return value.replace(tzinfo=None)
    if value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


def _zone_name(prop, value: datetime) -> str | None:
    tzid = prop.params.get("TZID") if hasattr(prop, "params") else None
    if tzid:
        return str(tzid)
    if value.tzinfo is None:
        return None
    key = getattr(value.tzinfo, "key", None)
    if key:
        return key
    name = str(value.tzinfo)
    return "UTC" if name.upper() == "UTC" else name


def _text(component, name: str) -> str | None:
    value = component.get(name)
    return str(value) if value is not None and str(value) else None


def _event(component) -> FeedEvent:
    dtstart = component.get("DTSTART")
    dtend = component.get("DTEND")
    start = dtstart.dt
    all_day = not isinstance(start, datetime)
    end = dtend.dt if dtend is not None else (start + timedelta(days=1) if all_day else start)
    zone = None
    if all_day:
        start = datetime.combine(start, time.min)
        end = datetime.combine(end if not isinstance(end, datetime) else end.date(), time.min)
    else:
        zone = _zone_name(dtstart, start)
        start = start.replace(tzinfo=None)
        if isinstance(end, datetime):
            end = end.astimezone(dtstart.dt.tzinfo).replace(tzinfo=None) if (
                end.tzinfo is not None and dtstart.dt.tzinfo is not None) else end.replace(tzinfo=None)
        else:
            end = datetime.combine(end, time.min)
    return FeedEvent(
        uid=str(component.get("UID", "")),
        summary=_text(component, "SUMMARY") or "Untitled",
        start=start, end=end,
        location=_text(component, "LOCATION"),
        description=_text(component, "DESCRIPTION"),
        all_day=all_day, timezone=zone,
    )


def expand(raw: bytes, start: datetime, end: datetime, *,
           limit: int = MAX_OCCURRENCES) -> tuple[list[FeedEvent], bool]:
    """The occurrences starting before ``end`` that are under way at or after ``start``.

    Recurrences are expanded lazily, so a ``FREQ=SECONDLY`` rule costs
    ``limit`` occurrences and no more; the second value says the cut was
    reached. A body that no longer parses reads as no events. An all-day
    event is kept only on the dates the window covers, as the CalDAV path
    does, so a UTC offset cannot pull in the next day's.
    """
    try:
        calendar = _parse(raw)
        import recurring_ical_events
        query = recurring_ical_events.of(calendar, components=["VEVENT"])
    except Exception:
        return [], False
    first_day = start.date()
    last_day = end.date()
    events: list[FeedEvent] = []
    truncated = False
    try:
        for component in query.after(start):
            dtstart = component.get("DTSTART")
            if dtstart is None:
                continue
            value = dtstart.dt
            if _as_reference(value, end) >= end:
                break
            if not isinstance(value, datetime):
                dtend = component.get("DTEND")
                last = dtend.dt if dtend is not None else value + timedelta(days=1)
                last = last.date() if isinstance(last, datetime) else last
                if value >= last_day or last <= first_day:
                    continue
            if len(events) >= limit:
                truncated = True
                break
            events.append(_event(component))
    except Exception as exc:  # noqa: BLE001 - a malformed rule ends the listing, not the read
        logger.info("ics_expand_stopped class=%s", type(exc).__name__)
    return events, truncated
