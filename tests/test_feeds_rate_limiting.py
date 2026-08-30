"""Rate limiting on the feeds poller (ISSUE-347).

Are.na returns 429 often enough to be a routine failure mode, and four things
combined to produce it. Each gets its own class here.

The whole file runs against stubs. ``http_get`` is injected for RSS, the two
provider modules are monkeypatched, and ``sleep`` is injected so the pacing
tests assert on the gaps *requested* rather than waiting them out — a pacing
test that really slept would be the slowest thing in the suite and would still
prove less, since what is under test is the arithmetic and not ``time.sleep``.
"""

from datetime import datetime, timezone

import pytest

pytest.importorskip("feedparser", reason="feeds extra not installed")

from istota.feeds import db as feeds_db
from istota.feeds.models import (
    DEFAULT_RATE_LIMIT_BACKOFF_MINUTES,
    MAX_RATE_LIMIT_BACKOFF_MINUTES,
    FeedRateLimited,
    FeedRecord,
    parse_retry_after,
    poll_host,
)
from istota.feeds.poller import poll_due_feeds, poll_feed


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


class _StubResponse:
    def __init__(self, *, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "ignore")
        self.headers = headers or {}


def _stub_get_factory(response):
    def _get(url, **kwargs):
        return response
    return _get


def _feed(url, *, source_type="rss", feed_id=1, error_count=0, last_error=None):
    return FeedRecord(
        id=feed_id, url=url, title=None, site_url=None, category_id=None,
        source_type=source_type, etag=None, last_modified=None,
        last_fetched_at=None, last_error=last_error, error_count=error_count,
        poll_interval_minutes=30, next_poll_at=None,
    )


def _db_with(tmp_path, feeds):
    """Build a feeds DB holding ``[(url, source_type), ...]``, all due now."""
    path = tmp_path / "feeds.db"
    feeds_db.init_db(path)
    with feeds_db.connect(path) as conn:
        for url, source_type in feeds:
            feeds_db.upsert_feed(
                conn, url=url, title=None, site_url=None,
                source_type=source_type, category_id=None,
                poll_interval_minutes=30,
            )
        conn.commit()
    return path


class _RecordingSleep:
    """Stands in for ``time.sleep``: records the gap, never takes it."""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ---------------------------------------------------------------------------
# 1. The pacing key is the host, not the source type
# ---------------------------------------------------------------------------


class TestPollHost:
    """What two feeds have to share before one waits for the other.

    A cadence per feed says nothing about how many requests reach one host at
    once, which is what a per-IP limit counts. Keying on the host is what makes
    one rule cover Are.na, Tumblr and a future provider while leaving RSS —
    dozens of distinct origins — unpaced.
    """

    def test_every_arena_channel_shares_one_host(self):
        assert poll_host("arena:one", "arena") == poll_host("arena:two", "arena")
        assert poll_host("arena:one", "arena") == "api.are.na"

    def test_every_tumblr_blog_shares_one_host(self):
        assert poll_host("tumblr:a", "tumblr") == "api.tumblr.com"

    def test_rss_feeds_on_different_origins_do_not_share_a_host(self):
        a = poll_host("https://a.example.com/feed.xml", "rss")
        b = poll_host("https://b.example.com/feed.xml", "rss")
        assert a != b
        assert a == "a.example.com"

    def test_rss_host_ignores_port_case_and_path(self):
        assert poll_host("https://A.Example.com:443/deep/feed.xml", "rss") == "a.example.com"

    def test_an_unparseable_url_still_yields_a_stable_key(self):
        # Never raise on the pacing path: a feed with a junk URL is about to
        # fail anyway, and it must fail in the fetch rather than here.
        assert poll_host("", "rss")
        assert poll_host("::::", "rss")


class TestPerHostPacing:
    def test_two_arena_channels_are_spaced_apart(self, tmp_path, monkeypatch):
        path = _db_with(tmp_path, [("arena:one", "arena"), ("arena:two", "arena")])
        from istota.feeds.providers import arena as arena_provider
        monkeypatch.setattr(arena_provider, "fetch", lambda ident, **kw: [])

        sleep = _RecordingSleep()
        with feeds_db.connect(path) as conn:
            poll_due_feeds(conn, now=NOW, sleep=sleep, host_gap_seconds=2.0)

        # The first request goes out immediately; the second waits.
        assert len(sleep.calls) == 1
        assert 1.0 < sleep.calls[0] <= 2.0

    def test_feeds_on_different_hosts_never_wait_for_each_other(self, tmp_path):
        path = _db_with(tmp_path, [
            ("https://a.example.com/feed.xml", "rss"),
            ("https://b.example.com/feed.xml", "rss"),
        ])
        resp = _StubResponse(status_code=200, content=b"<rss version='2.0'><channel/></rss>")
        sleep = _RecordingSleep()
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(resp), now=NOW,
                sleep=sleep, host_gap_seconds=2.0,
            )
        assert sleep.calls == []

    def test_two_feeds_on_the_same_rss_origin_are_spaced_apart(self, tmp_path):
        # The host key covers this for free, and it is correct: one origin
        # serving two feeds counts two requests the same way Are.na does.
        path = _db_with(tmp_path, [
            ("https://same.example.com/a.xml", "rss"),
            ("https://same.example.com/b.xml", "rss"),
        ])
        resp = _StubResponse(status_code=200, content=b"<rss version='2.0'><channel/></rss>")
        sleep = _RecordingSleep()
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(resp), now=NOW,
                sleep=sleep, host_gap_seconds=2.0,
            )
        assert len(sleep.calls) == 1

    def test_a_zero_gap_disables_pacing(self, tmp_path, monkeypatch):
        path = _db_with(tmp_path, [("arena:one", "arena"), ("arena:two", "arena")])
        from istota.feeds.providers import arena as arena_provider
        monkeypatch.setattr(arena_provider, "fetch", lambda ident, **kw: [])
        sleep = _RecordingSleep()
        with feeds_db.connect(path) as conn:
            poll_due_feeds(conn, now=NOW, sleep=sleep, host_gap_seconds=0)
        assert sleep.calls == []


# ---------------------------------------------------------------------------
# 2. Retry-After is parsed and honoured
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    """RFC 9110 gives the header two forms and both are seen in the wild."""

    def test_delta_seconds(self):
        assert parse_retry_after("120") == 120

    def test_surrounding_whitespace(self):
        assert parse_retry_after("  120  ") == 120

    def test_http_date_is_resolved_against_now(self):
        # 5 minutes past NOW, in the only date format the header may use.
        assert parse_retry_after("Sat, 29 Aug 2026 12:05:00 GMT", now=NOW) == 300

    def test_a_date_already_past_is_zero_rather_than_negative(self):
        assert parse_retry_after("Sat, 29 Aug 2026 11:00:00 GMT", now=NOW) == 0

    # "²" is the one that bit: `str.isdigit()` is True for it and `int()`
    # then raises, so the first cut of this parser raised out of a 429 and the
    # generic handler recorded it as a feed error — precisely the behaviour
    # ISSUE-347 removed, restored silently for that one header value.
    @pytest.mark.parametrize("raw", [None, "", "soon", "-5", "12.5", "0x10", "²", "١٢٣x"])
    def test_anything_unparseable_is_none(self, raw):
        # None means "the server named no time", which is a different branch
        # from "the server said zero" — the caller falls back to its own
        # default rather than polling again immediately.
        assert parse_retry_after(raw, now=NOW) is None


class TestProviderRateLimitBranches:
    """The 429 code *inside* each provider, which nothing else reaches.

    The result-shape tests below monkeypatch `fetch` wholesale and raise
    `FeedRateLimited` by hand, so they never execute the branch that decides to
    raise it. These stub the HTTP client instead, one level lower.
    """

    class _Resp:
        def __init__(self, headers):
            self.status_code = 429
            self.headers = headers

    def test_arena_raises_with_the_named_time_and_host(self, monkeypatch):
        import httpx
        from istota.feeds.providers import arena
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: self._Resp({"Retry-After": "300"}))
        with pytest.raises(FeedRateLimited) as caught:
            arena.fetch("some-channel")
        assert caught.value.retry_after == 300
        assert caught.value.host == "api.are.na"

    def test_tumblr_raises_with_the_named_time_and_host(self, monkeypatch):
        import requests
        from istota.feeds.providers import tumblr
        monkeypatch.setattr(requests, "get", lambda *a, **kw: self._Resp({"Retry-After": "300"}))
        with pytest.raises(FeedRateLimited) as caught:
            tumblr.fetch("someblog", api_key="k")
        assert caught.value.retry_after == 300
        assert caught.value.host == "api.tumblr.com"

    def test_a_lowercase_header_is_still_found(self, monkeypatch):
        # `httpx` and `requests` both hand back a case-insensitive mapping, but
        # a plain dict does not, and a missed lookup silently drops the
        # server's answer and falls back to the default standoff.
        import httpx
        from istota.feeds.providers import arena
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: self._Resp({"retry-after": "300"}))
        with pytest.raises(FeedRateLimited) as caught:
            arena.fetch("some-channel")
        assert caught.value.retry_after == 300

    def test_no_header_raises_with_no_named_time(self, monkeypatch):
        import httpx
        from istota.feeds.providers import arena
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: self._Resp({}))
        with pytest.raises(FeedRateLimited) as caught:
            arena.fetch("some-channel")
        assert caught.value.retry_after is None


class TestRateLimitedResult:
    def test_an_rss_429_becomes_a_rate_limited_result_not_an_error(self):
        resp = _StubResponse(status_code=429, headers={"Retry-After": "120"})
        result = poll_feed(_feed("https://example.com/feed.xml"),
                           http_get=_stub_get_factory(resp))
        assert result.rate_limited is True
        assert result.retry_after_seconds == 120
        # The whole point: a throttle is not a broken feed.
        assert result.error is None

    def test_a_429_with_no_header_carries_no_retry_after(self):
        resp = _StubResponse(status_code=429)
        result = poll_feed(_feed("https://example.com/feed.xml"),
                           http_get=_stub_get_factory(resp))
        assert result.rate_limited is True
        assert result.retry_after_seconds is None
        assert result.error is None

    def test_a_provider_rate_limit_exception_becomes_the_same_result(self, monkeypatch):
        from istota.feeds.providers import arena as arena_provider

        def _boom(ident, **kw):
            raise FeedRateLimited(90, host="api.are.na")

        monkeypatch.setattr(arena_provider, "fetch", _boom)
        result = poll_feed(_feed("arena:slug", source_type="arena"))
        assert result.rate_limited is True
        assert result.retry_after_seconds == 90
        assert result.error is None

    def test_other_http_errors_are_still_errors(self):
        resp = _StubResponse(status_code=503)
        result = poll_feed(_feed("https://example.com/feed.xml"),
                           http_get=_stub_get_factory(resp))
        assert result.rate_limited is False
        assert result.error and "503" in result.error


# ---------------------------------------------------------------------------
# 3. A 429 is not a feed error
# ---------------------------------------------------------------------------


class TestThrottledFeedIsNotBroken:
    def _poll_once(self, tmp_path, resp, *, error_count=0, last_error=None):
        path = _db_with(tmp_path, [("https://example.com/feed.xml", "rss")])
        if error_count or last_error:
            with feeds_db.connect(path) as conn:
                conn.execute(
                    "UPDATE feeds SET error_count = ?, last_error = ?",
                    (error_count, last_error),
                )
                conn.commit()
        with feeds_db.connect(path) as conn:
            outcomes = poll_due_feeds(
                conn, http_get=_stub_get_factory(resp), now=NOW, jitter_fraction=0,
            )
            feed = feeds_db.list_feeds(conn)[0]
        return outcomes, feed

    def test_a_429_does_not_increment_error_count_or_write_last_error(self, tmp_path):
        resp = _StubResponse(status_code=429, headers={"Retry-After": "600"})
        _, feed = self._poll_once(tmp_path, resp)
        assert feed.error_count == 0
        assert feed.last_error is None

    def test_a_429_leaves_an_existing_error_record_alone(self, tmp_path):
        # It says nothing about whether the previous failure is fixed, so it
        # must neither clear the record nor add to it.
        resp = _StubResponse(status_code=429)
        _, feed = self._poll_once(tmp_path, resp, error_count=3, last_error="HTTP 500")
        assert feed.error_count == 3
        assert feed.last_error == "HTTP 500"

    def test_a_named_time_longer_than_the_cadence_is_honoured(self, tmp_path):
        """The case Retry-After exists for.

        A server naming two hours is naming something the generic doubling
        would not have reached, and it is the half of the header that carries
        information the poller does not already have. The feed's cadence is 30
        minutes here, so this is the header winning.
        """
        resp = _StubResponse(status_code=429, headers={"Retry-After": str(2 * 3600)})
        _, feed = self._poll_once(tmp_path, resp)
        minutes = (datetime.fromisoformat(feed.next_poll_at) - NOW).total_seconds() / 60
        assert abs(minutes - 120) < 1

    def test_no_named_time_stands_off_the_module_default(self, tmp_path):
        """With no header, the standoff is the rate-limit default.

        Above the ordinary cadence on purpose: being turned away has to cost
        more than a normal poll, or the standoff means nothing.
        """
        resp = _StubResponse(status_code=429)
        _, feed = self._poll_once(tmp_path, resp)
        minutes = (datetime.fromisoformat(feed.next_poll_at) - NOW).total_seconds() / 60
        assert abs(minutes - DEFAULT_RATE_LIMIT_BACKOFF_MINUTES) < 1

    def test_an_absurd_retry_after_is_capped(self, tmp_path):
        # A server naming a week would otherwise take the channel off the air
        # for a week, on one header we did not verify.
        resp = _StubResponse(status_code=429, headers={"Retry-After": str(7 * 24 * 3600)})
        _, feed = self._poll_once(tmp_path, resp)
        delta = (datetime.fromisoformat(feed.next_poll_at) - NOW).total_seconds() / 60
        assert abs(delta - MAX_RATE_LIMIT_BACKOFF_MINUTES) < 1

    def test_a_retry_after_below_the_feeds_own_cadence_still_waits_the_cadence(self, tmp_path):
        # The floor, and the direction it runs in: Retry-After extends the
        # feed's own cadence and never shortens it. Coming back in 5s because
        # the server permits it would re-enter the burst this issue is about,
        # and polling more often than the cadence is not something the header
        # can ask for — waiting longer than asked is always compliant.
        resp = _StubResponse(status_code=429, headers={"Retry-After": "5"})
        _, feed = self._poll_once(tmp_path, resp)
        delta = (datetime.fromisoformat(feed.next_poll_at) - NOW).total_seconds() / 60
        assert delta >= 30

    def test_a_429_never_schedules_sooner_than_a_success_would(self, tmp_path):
        """The invariant, on the feed shape that used to break it.

        The success path schedules `max(poll_interval_minutes, 30)`, so a
        rate-limit floor of the raw `poll_interval_minutes` let a 429 come back
        *sooner* than a healthy poll on any feed configured under 30 minutes —
        more pressure on the host that had just turned us away. Both floors are
        now the same quantity. A 5-minute cadence is reachable from the
        settings page, which bounds the value at neither end.
        """
        path = _db_with(tmp_path, [("https://example.com/feed.xml", "rss")])
        with feeds_db.connect(path) as conn:
            conn.execute("UPDATE feeds SET poll_interval_minutes = 5")
            conn.commit()

        def _minutes_after(resp):
            with feeds_db.connect(path) as conn:
                conn.execute("UPDATE feeds SET next_poll_at = NULL")
                conn.commit()
                poll_due_feeds(conn, http_get=_stub_get_factory(resp), now=NOW,
                               jitter_fraction=0)
                feed = feeds_db.list_feeds(conn)[0]
            return (datetime.fromisoformat(feed.next_poll_at) - NOW).total_seconds() / 60

        success = _minutes_after(
            _StubResponse(status_code=200, content=b"<rss version='2.0'><channel/></rss>"))
        throttled_no_header = _minutes_after(_StubResponse(status_code=429))
        throttled_short = _minutes_after(
            _StubResponse(status_code=429, headers={"Retry-After": "60"}))

        assert throttled_no_header >= success
        assert throttled_short >= success

    def test_the_outcome_is_reported_as_throttled_not_as_an_error(self, tmp_path):
        resp = _StubResponse(status_code=429)
        outcomes, _ = self._poll_once(tmp_path, resp)
        _, result, new_count = outcomes[0]
        assert result.rate_limited is True
        assert result.error is None
        assert new_count == 0


# ---------------------------------------------------------------------------
# 4. Jitter disperses a synchronised set
# ---------------------------------------------------------------------------


class TestJitter:
    """A set that burst together must not reschedule to the same instant.

    Without this the herd re-forms every round: same due time, same burst, one
    doubling later.

    **The assertion is the size of the spread, not its existence**, and that is
    the whole design of this class. `poll_due_feeds` schedules each feed
    against the clock as it is when that feed is persisted, so eight feeds
    land on eight distinct `next_poll_at` values with jitter switched off —
    about 85 microseconds apart. `len(set(polls)) > 1` was therefore true of a
    build with the feature removed, which is the failure `.claude/rules/testbed.md`
    calls a probe indistinguishable from a no-op. A spread of half a minute is
    something only jitter can produce; loop overhead cannot reach it.
    """

    #: Loop overhead is microseconds; jitter on a 30-minute base is minutes.
    #: Anything between the two separates them, and this is deliberately far
    #: above the first rather than just below the second.
    MIN_SPREAD_MINUTES = 0.5

    def _offsets(self, tmp_path, count, resp, *, jitter_fraction=0.1):
        """Each feed's next poll, in minutes after *its own* recorded fetch.

        Measured against the feed's own base rather than against `NOW`: the
        schedule is computed per feed against the live clock, so measuring from
        `NOW` folds the loop's elapsed wall time into every offset and makes the
        bounds a race against how loaded the machine is. Under `-n auto` on a
        busy box that is a real flake, not a theoretical one.
        """
        path = _db_with(tmp_path, [
            (f"https://host{i}.example.com/feed.xml", "rss") for i in range(count)
        ])
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(resp), now=NOW,
                jitter_fraction=jitter_fraction,
            )
            feeds = feeds_db.list_feeds(conn)
        out = []
        for f in feeds:
            base = datetime.fromisoformat(f.last_fetched_at)
            out.append((datetime.fromisoformat(f.next_poll_at) - base).total_seconds() / 60)
        return out

    def test_successful_polls_are_spread_by_more_than_loop_overhead(self, tmp_path):
        resp = _StubResponse(status_code=200, content=b"<rss version='2.0'><channel/></rss>")
        offsets = self._offsets(tmp_path, 8, resp)
        assert max(offsets) - min(offsets) > self.MIN_SPREAD_MINUTES

    def test_errored_polls_are_spread_by_more_than_loop_overhead(self, tmp_path):
        # The path that matters most — a set that failed together is exactly a
        # set that was throttled together.
        resp = _StubResponse(status_code=503)
        offsets = self._offsets(tmp_path, 8, resp)
        assert max(offsets) - min(offsets) > self.MIN_SPREAD_MINUTES

    def test_the_control_no_jitter_produces_no_spread(self, tmp_path):
        """The control the three assertions above are worth nothing without.

        With jitter off the spread must be microseconds, which is what proves
        the bound they use is measuring jitter and not the loop.
        """
        resp = _StubResponse(status_code=200, content=b"<rss version='2.0'><channel/></rss>")
        offsets = self._offsets(tmp_path, 8, resp, jitter_fraction=0)
        assert max(offsets) - min(offsets) < 0.001
        for minutes in offsets:
            assert minutes == 30

    def test_jitter_stays_inside_its_fraction(self, tmp_path):
        resp = _StubResponse(status_code=200, content=b"<rss version='2.0'><channel/></rss>")
        offsets = self._offsets(tmp_path, 12, resp)
        for minutes in offsets:
            assert 27 <= minutes <= 33

    def test_a_fraction_at_or_above_one_cannot_schedule_a_feed_in_the_past(self, tmp_path):
        # Unreachable from production, which passes the 0.1 constant, but the
        # parameter is public: at 1.5 the low end of the range is negative, and
        # a next_poll_at before now leaves the feed permanently due.
        resp = _StubResponse(status_code=200, content=b"<rss version='2.0'><channel/></rss>")
        offsets = self._offsets(tmp_path, 12, resp, jitter_fraction=1.5)
        assert min(offsets) > 0


class TestThrottleIsVisible:
    """A 429 must be reportable somewhere.

    Making it not-an-error was the point of the fix, but the first cut left it
    recorded on no surface at all: `error_count` and `last_error` untouched by
    design, and nothing else carrying it — so a run turned away on every feed
    was byte-identical to a successful poll that found nothing, and reported
    `errors=0`. That is a worse silence than the wrong label it replaced.
    """

    def _poll(self, tmp_path, resp):
        path = _db_with(tmp_path, [("https://example.com/feed.xml", "rss")])
        with feeds_db.connect(path) as conn:
            poll_due_feeds(conn, http_get=_stub_get_factory(resp), now=NOW)
            return path, feeds_db.list_feeds(conn)[0]

    def test_a_throttle_is_recorded_on_the_feed(self, tmp_path):
        _, feed = self._poll(tmp_path, _StubResponse(status_code=429))
        assert feed.last_throttled_at
        # And still not as an error, which is the other half of the rule.
        assert feed.error_count == 0
        assert feed.last_error is None

    def test_a_throttle_does_not_claim_a_fetch_that_did_not_happen(self, tmp_path):
        _, feed = self._poll(tmp_path, _StubResponse(status_code=429))
        assert feed.last_fetched_at is None

    def test_a_later_success_clears_the_throttle_mark(self, tmp_path):
        # The column means "throttled now", not "throttled once" — otherwise
        # one 429 marks a feed for the life of the database.
        path, feed = self._poll(tmp_path, _StubResponse(status_code=429))
        assert feed.last_throttled_at
        ok = _StubResponse(status_code=200, content=b"<rss version='2.0'><channel/></rss>")
        with feeds_db.connect(path) as conn:
            conn.execute("UPDATE feeds SET next_poll_at = NULL")
            conn.commit()
            poll_due_feeds(conn, http_get=_stub_get_factory(ok), now=NOW)
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.last_throttled_at is None
        assert feed.last_fetched_at

    def test_an_error_after_a_throttle_preserves_the_mark(self, tmp_path):
        # An error says nothing about whether the throttle has cleared.
        path, _ = self._poll(tmp_path, _StubResponse(status_code=429))
        with feeds_db.connect(path) as conn:
            conn.execute("UPDATE feeds SET next_poll_at = NULL")
            conn.commit()
            poll_due_feeds(conn, http_get=_stub_get_factory(_StubResponse(status_code=503)),
                           now=NOW)
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.last_throttled_at
        assert feed.error_count == 1


# ---------------------------------------------------------------------------
# 5. The scheduled run caps its burst
# ---------------------------------------------------------------------------


class TestScheduledBurstCap:
    def test_run_scheduled_polls_at_most_the_default_limit(self, tmp_path, monkeypatch):
        from istota.feeds.models import DEFAULT_SCHEDULED_POLL_LIMIT

        over = DEFAULT_SCHEDULED_POLL_LIMIT + 5
        path = _db_with(tmp_path, [
            (f"https://host{i}.example.com/feed.xml", "rss") for i in range(over)
        ])
        resp = _StubResponse(status_code=200, content=b"<rss version='2.0'><channel/></rss>")
        with feeds_db.connect(path) as conn:
            outcomes = poll_due_feeds(
                conn, http_get=_stub_get_factory(resp), now=NOW,
                limit=DEFAULT_SCHEDULED_POLL_LIMIT,
            )
        assert len(outcomes) == DEFAULT_SCHEDULED_POLL_LIMIT
