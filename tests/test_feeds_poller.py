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


# ---------------------------------------------------------------------------
# RSS image dedup + hero-strip (Guardian 3×, PetaPixel 2×)
# ---------------------------------------------------------------------------


def _first_item(rss_bytes):
    """Poll a stubbed RSS body and return the single FetchedItem."""
    resp = _StubResponse(status_code=200, content=rss_bytes)
    result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
    assert result.error is None
    assert len(result.items) == 1
    return result.items[0]


GUARDIAN_RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:media="http://search.yahoo.com/mrss/" version="2.0">
<channel><title>G</title><link>https://g.com</link>
<item>
  <title>Match</title>
  <link>https://g.com/a</link>
  <guid>g-1</guid>
  <pubDate>Thu, 01 May 2026 12:00:00 GMT</pubDate>
  <description>&lt;p&gt;Kick off soon.&lt;/p&gt;</description>
  <media:content width="140" url="https://i.guim.co.uk/img/media/abc/master/3049.jpg?width=140&amp;s=aaa"/>
  <media:content width="460" url="https://i.guim.co.uk/img/media/abc/master/3049.jpg?width=460&amp;s=bbb"/>
  <media:content width="700" url="https://i.guim.co.uk/img/media/abc/master/3049.jpg?width=700&amp;s=ccc"/>
</item>
</channel></rss>
"""


PETAPIXEL_RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
<channel><title>P</title><link>https://p.com</link>
<item>
  <title>Deals</title>
  <link>https://p.com/a</link>
  <guid>p-1</guid>
  <pubDate>Thu, 01 May 2026 12:00:00 GMT</pubDate>
  <description><![CDATA[<p class="feature-image"><a href="https://p.com/a"><img width="1600" src="https://p.com/uploads/cover.jpg" class="wp-post-image" /></a></p><p>Looking to save on new photography gear this Fourth of July?</p>]]></description>
  <enclosure url="https://p.com/uploads/clip.mp4" length="1234" type="video/mp4" />
</item>
</channel></rss>
"""


MULTI_INLINE_RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
<channel><title>X</title><link>https://x.com</link>
<item>
  <title>Gallery piece</title>
  <link>https://x.com/a</link>
  <guid>x-1</guid>
  <pubDate>Thu, 01 May 2026 12:00:00 GMT</pubDate>
  <description><![CDATA[<p><img src="https://x.com/lead.jpg" /></p><p>intro text</p><figure><img src="https://x.com/mid.jpg" /></figure><p>more text</p>]]></description>
</item>
</channel></rss>
"""


class TestRssImageDedup:
    def test_guardian_resolution_variants_collapse_to_one_hero(self):
        item = _first_item(GUARDIAN_RSS)
        assert item.image_urls == [
            "https://i.guim.co.uk/img/media/abc/master/3049.jpg?width=700&s=ccc"
        ]

    def test_petapixel_lead_image_becomes_hero_and_leaves_body(self):
        item = _first_item(PETAPIXEL_RSS)
        # Hero is the in-body lead image; the video enclosure is not an image.
        assert item.image_urls == ["https://p.com/uploads/cover.jpg"]
        # The lead image no longer sits in the body (no hero+body dup)...
        assert "cover.jpg" not in (item.content_html or "")
        # ...but the article text survives untouched.
        assert "Looking to save" in (item.content_html or "")

    def test_inline_images_past_the_lead_are_preserved(self):
        item = _first_item(MULTI_INLINE_RSS)
        # Only the lead is promoted to the hero.
        assert item.image_urls == ["https://x.com/lead.jpg"]
        # The lead is stripped from the body, the mid-article image stays.
        assert "lead.jpg" not in (item.content_html or "")
        assert "mid.jpg" in (item.content_html or "")
