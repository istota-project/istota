"""Tests for the polling engine — RSS conditional GET + error backoff.

Doesn't hit the network. ``http_get`` is stubbed; the Tumblr/Are.na
provider modules are tested separately (Phase 1 keeps them as a vendored
copy of the rss-bridger logic, which already has its own tests).
"""

from datetime import datetime, timezone

import pytest

pytest.importorskip("feedparser", reason="feeds extra not installed")

from istota.feeds import db as feeds_db
from istota.feeds.models import FeedRecord
from istota.feeds.poller import _backoff_interval, poll_due_feeds, poll_feed


# ---------------------------------------------------------------------------
# stubbed http_get fixtures
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, *, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "ignore")
        self.headers = headers or {}


def _stub_get_factory(response: _StubResponse):
    def _get(url, **kwargs):
        return response
    return _get


SAMPLE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0">
<channel>
<title>Example</title>
<link>https://example.com</link>
<item>
  <title>Hello</title>
  <link>https://example.com/hello</link>
  <guid>hello-1</guid>
  <pubDate>Thu, 01 May 2026 12:00:00 GMT</pubDate>
  <description>&lt;p&gt;Hello world&lt;/p&gt;</description>
</item>
</channel>
</rss>
"""


# ---------------------------------------------------------------------------
# _backoff_interval — pure function
# ---------------------------------------------------------------------------


class TestBackoffInterval:
    def test_first_error_doubles_once(self):
        # error_count = 1 → 2 ** 0 = 1× base
        assert _backoff_interval(30, 1, 24 * 60) == 30

    def test_doubles_each_consecutive_error(self):
        assert _backoff_interval(30, 2, 24 * 60) == 60
        assert _backoff_interval(30, 3, 24 * 60) == 120
        assert _backoff_interval(30, 4, 24 * 60) == 240

    def test_caps_at_max(self):
        assert _backoff_interval(30, 20, 24 * 60) == 24 * 60


# ---------------------------------------------------------------------------
# poll_feed — RSS happy path + 304 + 5xx
# ---------------------------------------------------------------------------


def _rss_feed() -> FeedRecord:
    return FeedRecord(
        id=1, url="https://example.com/feed.xml",
        title=None, site_url=None, category_id=None,
        source_type="rss", etag=None, last_modified=None,
        last_fetched_at=None, last_error=None, error_count=0,
        poll_interval_minutes=30, next_poll_at=None,
    )


class TestPollFeedRss:
    def test_happy_path_parses_entries(self):
        resp = _StubResponse(
            status_code=200,
            content=SAMPLE_RSS,
            headers={"ETag": '"abc"', "Last-Modified": "Thu, 01 May 2026 12:00:00 GMT"},
        )
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.error is None
        assert result.not_modified is False
        assert result.etag == '"abc"'
        assert result.last_modified == "Thu, 01 May 2026 12:00:00 GMT"
        assert result.discovered_title == "Example"
        assert len(result.items) == 1
        assert result.items[0].guid == "hello-1"
        assert result.items[0].title == "Hello"

    def test_304_returns_not_modified(self):
        resp = _StubResponse(status_code=304, content=b"", headers={})
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.not_modified is True
        assert result.items == []
        assert result.error is None

    def test_5xx_records_error(self):
        resp = _StubResponse(status_code=503, content=b"", headers={})
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.not_modified is False
        assert result.error is not None
        assert "503" in result.error

    def test_conditional_headers_sent(self):
        captured = {}

        def _get(url, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            return _StubResponse(status_code=304)

        feed = _rss_feed()
        feed.etag = '"prev"'
        feed.last_modified = "Wed, 30 Apr 2026 12:00:00 GMT"
        poll_feed(feed, http_get=_get)
        assert captured["headers"].get("If-None-Match") == '"prev"'
        assert captured["headers"].get("If-Modified-Since") == feed.last_modified


# ---------------------------------------------------------------------------
# poll_due_feeds — persists state, applies backoff, dedupes entries
# ---------------------------------------------------------------------------


class TestPollDueFeeds:
    def test_persists_entries_and_clears_error(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feeds_db.upsert_feed(
                conn,
                url="https://example.com/feed.xml",
                title=None, site_url=None,
                source_type="rss", category_id=None,
                poll_interval_minutes=30,
            )
            conn.commit()

        resp = _StubResponse(
            status_code=200,
            content=SAMPLE_RSS,
            headers={"ETag": '"v1"'},
        )
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        with feeds_db.connect(path) as conn:
            outcomes = poll_due_feeds(
                conn, http_get=_stub_get_factory(resp), now=now,
            )
            entries = feeds_db.list_entries(conn)
            feed = feeds_db.list_feeds(conn)[0]

        assert len(outcomes) == 1
        feed_record, result, new_count = outcomes[0]
        assert new_count == 1
        assert result.error is None
        assert len(entries) == 1
        assert entries[0].guid == "hello-1"
        assert feed.etag == '"v1"'
        assert feed.error_count == 0
        assert feed.next_poll_at is not None

    def test_5xx_increments_error_count_and_backs_off(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feeds_db.upsert_feed(
                conn, url="https://example.com/feed.xml",
                title=None, site_url=None,
                source_type="rss", category_id=None,
                poll_interval_minutes=30,
            )
            conn.commit()
        resp = _StubResponse(status_code=503)
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(conn, http_get=_stub_get_factory(resp), now=now)
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.error_count == 1
        assert feed.last_error and "503" in feed.last_error
        assert feed.next_poll_at is not None
        # Next poll should be ~30 min out (one backoff doubling = 1× base).
        next_dt = datetime.fromisoformat(feed.next_poll_at)
        delta = (next_dt - now).total_seconds() / 60
        assert 25 <= delta <= 35

    def test_repeat_polls_dedupe_by_guid(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feeds_db.upsert_feed(
                conn, url="https://example.com/feed.xml",
                title=None, site_url=None,
                source_type="rss", category_id=None,
                poll_interval_minutes=30,
            )
            conn.commit()
        resp = _StubResponse(status_code=200, content=SAMPLE_RSS)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(conn, http_get=_stub_get_factory(resp),
                           now=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc))
            # Reset next_poll_at so the second pass picks up the feed.
            conn.execute("UPDATE feeds SET next_poll_at = NULL")
            conn.commit()
            poll_due_feeds(conn, http_get=_stub_get_factory(resp),
                           now=datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc))
            entries = feeds_db.list_entries(conn)
        assert len(entries) == 1  # second poll didn't double-insert
