"""Tests for the polling engine — RSS conditional GET + error backoff.

Doesn't hit the network. ``http_get`` is stubbed; the Tumblr/Are.na
provider modules are tested separately (Phase 1 keeps them as a vendored
copy of the rss-bridger logic, which already has its own tests).
"""

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("feedparser", reason="feeds extra not installed")

from istota.feeds import db as feeds_db
from istota.feeds.models import (
    MIN_ENTRIES_PER_FEED,
    POLL_CLAIM_SECONDS,
    FeedRecord,
)
from istota.feeds.poller import (
    _backoff_interval,
    poll_due_feeds,
    poll_feed,
)


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

    def test_html_tags_stripped_from_titles(self):
        # The Atlantic's Atom feed ships ``<title type="html">`` with inline
        # markup (``<em>`` around emphasised words). feedparser decodes it to
        # real tags; we store titles as plain text, so the reader shows the
        # words, not literal ``<em>…</em>``.
        atom = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example</title>
  <link href="https://example.com"/>
  <entry>
    <id>atom-1</id>
    <title type="html">The &lt;em&gt;Other&lt;/em&gt; Case for X</title>
    <link href="https://example.com/x"/>
    <summary type="html">&lt;p&gt;Body&lt;/p&gt;</summary>
  </entry>
</feed>
"""
        resp = _StubResponse(status_code=200, content=atom)
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert len(result.items) == 1
        assert result.items[0].title == "The Other Case for X"
        # Plain-text titles never render the body path's HTML.
        assert result.items[0].content_html is not None


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


class TestArenaEmbedPassthrough:
    """The provider→storage seam for playable media.

    ``embed_url`` is the one field the reader needs to build a player, and it
    crosses three boundaries (FetchedItem → EntryRecord → row). A silent drop
    at any of them puts the blank-ish card back.
    """

    def test_embed_url_reaches_storage(self, tmp_path, monkeypatch):
        from istota.feeds import db as feeds_db
        from istota.feeds.models import FetchedItem
        from istota.feeds.providers import arena as arena_provider

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feeds_db.upsert_feed(
                conn, url="arena:c", title="C", site_url=None,
                source_type="arena", category_id=None, poll_interval_minutes=60,
            )
            conn.commit()

        monkeypatch.setattr(arena_provider, "fetch", lambda ident, **kw: [
            FetchedItem(
                guid="1", title="Vid", url="https://www.are.na/block/1",
                image_urls=["https://cdn/thumb.jpg"],
                embed_url="https://www.youtube.com/watch?v=abc",
            ),
        ])

        with feeds_db.connect(path) as conn:
            poll_due_feeds(conn)
            entries = feeds_db.list_entries(conn)

        assert entries[0].embed_url == "https://www.youtube.com/watch?v=abc"


# ---------------------------------------------------------------------------
# Non-image media attachments (ISSUE-356)
# ---------------------------------------------------------------------------


MASTODON_VIDEO_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>M</title><link>https://example.town/@someone</link>
<item>
  <guid isPermaLink="true">https://example.town/@someone/1</guid>
  <link>https://example.town/@someone/1</link>
  <pubDate>Thu, 01 May 2026 12:00:00 GMT</pubDate>
  <description>&lt;p&gt;a clip&lt;/p&gt;</description>
  <media:content url="https://assets.example.town/media/117/original/clip.mp4"
     type="video/mp4" fileSize="1234567" medium="video"/>
</item>
</channel></rss>
"""


MASTODON_IMAGE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>M</title><link>https://example.town/@someone</link>
<item>
  <guid isPermaLink="true">https://example.town/@someone/2</guid>
  <link>https://example.town/@someone/2</link>
  <pubDate>Thu, 01 May 2026 12:00:00 GMT</pubDate>
  <description>&lt;p&gt;a pic&lt;/p&gt;</description>
  <media:content url="https://assets.example.town/media/118/original/pic.png"
     type="image/png" fileSize="999" medium="image"/>
</item>
</channel></rss>
"""


MIXED_MEDIA_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>M</title><link>https://example.town/@someone</link>
<item>
  <guid isPermaLink="true">https://example.town/@someone/3</guid>
  <link>https://example.town/@someone/3</link>
  <pubDate>Thu, 01 May 2026 12:00:00 GMT</pubDate>
  <description>&lt;p&gt;both&lt;/p&gt;</description>
  <media:content url="https://assets.example.town/media/119/original/still.jpg"
     type="image/jpeg" medium="image"/>
  <media:content url="https://assets.example.town/media/120/original/clip.mp4"
     type="video/mp4" medium="video"/>
</item>
</channel></rss>
"""


UNTYPED_VIDEO_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>M</title><link>https://example.com</link>
<item>
  <guid>u-1</guid>
  <link>https://example.com/u1</link>
  <description>&lt;p&gt;no type and no medium&lt;/p&gt;</description>
  <media:content url="https://example.com/media/clip.mp4"/>
</item>
</channel></rss>
"""


UNTYPED_IMAGE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>M</title><link>https://example.com</link>
<item>
  <guid>u-2</guid>
  <link>https://example.com/u2</link>
  <description>&lt;p&gt;no type and no medium&lt;/p&gt;</description>
  <media:content url="https://example.com/media/photo.jpg"/>
</item>
</channel></rss>
"""


PODCAST_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel><title>P</title><link>https://pod.example.com</link>
<item>
  <title>Episode 12</title>
  <guid>pod-12</guid>
  <link>https://pod.example.com/12</link>
  <description>&lt;p&gt;show notes&lt;/p&gt;</description>
  <enclosure url="https://pod.example.com/audio/12.mp3" length="99" type="audio/mpeg"/>
</item>
</channel></rss>
"""


class TestNonImageMediaAttachments:
    """A ``media:content`` video is not a hero image (ISSUE-356).

    The enclosure loop always gated on ``image/``; the ``media:content`` loop
    did not, so Mastodon's video attachment — which arrives only as
    ``media:content`` — landed in ``image_urls`` and the reader painted it
    into an ``<img src>`` that never decodes.
    """

    def test_a_video_attachment_is_not_stored_as_an_image(self):
        item = _first_item(MASTODON_VIDEO_RSS)
        assert item.image_urls == []

    def test_a_video_attachment_is_stored_as_playable_media(self):
        item = _first_item(MASTODON_VIDEO_RSS)
        assert item.media_url == "https://assets.example.town/media/117/original/clip.mp4"
        assert item.media_type == "video/mp4"

    def test_an_image_attachment_is_still_a_hero(self):
        item = _first_item(MASTODON_IMAGE_RSS)
        assert item.image_urls == ["https://assets.example.town/media/118/original/pic.png"]
        assert item.media_url is None

    def test_a_post_carrying_both_keeps_the_image_and_the_video(self):
        item = _first_item(MIXED_MEDIA_RSS)
        assert item.image_urls == ["https://assets.example.town/media/119/original/still.jpg"]
        assert item.media_url == "https://assets.example.town/media/120/original/clip.mp4"

    def test_an_untyped_attachment_falls_back_to_its_extension(self):
        # No type and no medium: the extension is the only evidence there is,
        # and a bare .mp4 is exactly the shape that broke.
        item = _first_item(UNTYPED_VIDEO_RSS)
        assert item.image_urls == []
        assert item.media_url == "https://example.com/media/clip.mp4"
        assert item.media_type == "video/mp4"

    def test_an_untyped_image_still_reads_as_an_image(self):
        # Unchanged for everything the extension does not name as playable, so
        # no feed loses a hero to this fix.
        item = _first_item(UNTYPED_IMAGE_RSS)
        assert item.image_urls == ["https://example.com/media/photo.jpg"]
        assert item.media_url is None

    def test_a_video_enclosure_becomes_playable_media(self):
        # PetaPixel's mp4 enclosure was already kept out of image_urls; it was
        # simply dropped on the floor. It has somewhere to go now.
        item = _first_item(PETAPIXEL_RSS)
        assert item.image_urls == ["https://p.com/uploads/cover.jpg"]
        assert item.media_url == "https://p.com/uploads/clip.mp4"
        assert item.media_type == "video/mp4"

    def test_an_audio_enclosure_becomes_playable_media(self):
        item = _first_item(PODCAST_RSS)
        assert item.media_url == "https://pod.example.com/audio/12.mp3"
        assert item.media_type == "audio/mpeg"

    @pytest.mark.parametrize("attrs", [
        # An unrecognised MIME type must not cost the entry its hero. The old
        # media:content loop took every URL whatever its type said, and
        # application/octet-stream is a common CDN default.
        'type="application/octet-stream"',
        'type="application/octet-stream" medium="image"',
        # A MIME type that is not a type at all.
        'type="image"',
        # MRSS's medium legitimately takes values outside our three.
        'medium="document"',
    ])
    def test_an_oddly_typed_image_attachment_keeps_its_hero(self, attrs):
        rss = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">'
            "<channel><title>M</title><link>https://example.com</link><item>"
            "<guid>odd-1</guid><link>https://example.com/odd</link>"
            "<description>&lt;p&gt;hi&lt;/p&gt;</description>"
            f'<media:content url="https://example.com/media/photo.jpg" {attrs}/>'
            "</item></channel></rss>"
        ).encode()
        item = _first_item(rss)
        assert item.image_urls == ["https://example.com/media/photo.jpg"]
        assert item.media_url is None

    def test_an_oddly_typed_video_attachment_still_plays(self):
        # Same fall-through, landing on the extension rather than the default.
        rss = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">'
            "<channel><title>M</title><link>https://example.com</link><item>"
            "<guid>odd-2</guid><link>https://example.com/odd2</link>"
            "<description>&lt;p&gt;hi&lt;/p&gt;</description>"
            '<media:content url="https://example.com/media/clip.mp4"'
            ' type="application/octet-stream"/>'
            "</item></channel></rss>"
        ).encode()
        item = _first_item(rss)
        assert item.image_urls == []
        assert item.media_url == "https://example.com/media/clip.mp4"

    def test_an_untyped_enclosure_is_still_dropped(self):
        # The `untyped` kwarg's whole reason for existing: an <enclosure> with
        # no evidence was always discarded, where a media:content was kept as
        # an image. Unifying the two would change what a feed shows.
        rss = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0">'
            "<channel><title>E</title><link>https://example.com</link><item>"
            "<guid>enc-1</guid><link>https://example.com/e1</link>"
            "<description>&lt;p&gt;hi&lt;/p&gt;</description>"
            '<enclosure url="https://example.com/media/thing" length="1"/>'
            "</item></channel></rss>"
        ).encode()
        item = _first_item(rss)
        assert item.image_urls == []
        assert item.media_url is None

    def test_a_media_url_is_stored_stripped(self):
        # The scheme check runs on a stripped copy, so the stripped copy is
        # what gets stored — otherwise the value checked is not the value used.
        from istota.feeds.poller import _rss_entry_to_item

        item = _rss_entry_to_item({
            "id": "s-1",
            "link": "https://example.com/s1",
            "summary": "<p>hi</p>",
            "media_content": [
                {"url": "  https://example.com/clip.mp4  ", "type": "video/mp4"},
            ],
        })
        assert item.media_url == "https://example.com/clip.mp4"

    def test_a_non_http_media_url_is_refused(self):
        # The URL is remote input and ends up in a `src`, so the sanitizer's
        # bar — http/https only — applies on the way in, not on the way out.
        from istota.feeds.poller import _rss_entry_to_item

        item = _rss_entry_to_item({
            "id": "x-1",
            "link": "https://example.com/x1",
            "summary": "<p>hi</p>",
            "media_content": [{"url": "javascript:alert(1)", "type": "video/mp4"}],
        })
        assert item.media_url is None
        assert item.image_urls == []

    def test_media_reaches_storage(self, tmp_path):
        """FetchedItem → EntryRecord → row, the seam ``embed_url`` has a test
        for. A silent drop at any of the three puts the broken hero back."""
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feeds_db.upsert_feed(
                conn, url="https://example.town/@someone.rss", title="M",
                site_url=None, source_type="rss", category_id=None,
                poll_interval_minutes=60,
            )
            conn.commit()

        resp = _StubResponse(status_code=200, content=MASTODON_VIDEO_RSS)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(conn, http_get=_stub_get_factory(resp))
            entries = feeds_db.list_entries(conn)

        assert entries[0].media_url == "https://assets.example.town/media/117/original/clip.mp4"
        assert entries[0].media_type == "video/mp4"
        assert entries[0].image_urls == []


# ---------------------------------------------------------------------------
# observation marker + poll claims (ISSUE-388)
# ---------------------------------------------------------------------------


# A well-formed feed that legitimately holds nothing. It returns no item, so
# it does not advance the marker — the same answer the error page below gets,
# and deliberately so: the two are indistinguishable without the completeness
# reasoning this design removed, and keeping history is the safe reading.
EMPTY_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example</title></channel></rss>
"""

# Served at HTTP 200 with `bozo` False, parsing cleanly into zero entries. If
# a response like this advanced the marker, one proxy error would make a
# whole feed's history age-eligible at once.
HTML_ERROR_PAGE = b"<html><body><h1>404 Not Found</h1></body></html>"

TWO_ITEM_RSS = b"""<?xml version="1.0"?>
<rss version="2.0">
<channel>
<title>Example</title>
<item><guid>one</guid><title>One</title></item>
<item><guid>two</guid><title>Two</title></item>
</channel>
</rss>
"""


def _seed_rss_feed(tmp_path, *, url="https://example.com/feed.xml"):
    path = tmp_path / "feeds.db"
    feeds_db.init_db(path)
    with feeds_db.connect(path) as conn:
        feed_id = feeds_db.upsert_feed(
            conn, url=url, title=None, site_url=None,
            source_type="rss", category_id=None, poll_interval_minutes=30,
        )
        conn.commit()
    return path, feed_id


def _make_due(path):
    with feeds_db.connect(path) as conn:
        conn.execute(
            "UPDATE feeds SET next_poll_at = NULL, poll_claimed_until = NULL"
        )
        conn.commit()


class TestTheObservationMarker:
    """``feeds.last_items_seen_at`` advances when, and only when, a response
    returned at least one identifiable item."""

    def test_a_response_carrying_items_advances_it_and_stamps_each_entry(
        self, tmp_path,
    ):
        path, _ = _seed_rss_feed(tmp_path)
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        resp = _StubResponse(status_code=200, content=TWO_ITEM_RSS)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(conn, http_get=_stub_get_factory(resp), now=now)
            feed = feeds_db.list_feeds(conn)[0]
            rows = {
                r["guid"]: r["last_seen_at"]
                for r in conn.execute("SELECT guid, last_seen_at FROM feed_entries")
            }
        assert feed.last_items_seen_at is not None
        # Every entry the response returned carries exactly the feed's marker,
        # which is what protects it from the age pass.
        assert set(rows) == {"one", "two"}
        assert set(rows.values()) == {feed.last_items_seen_at}

    def test_a_second_response_advances_it_and_stamps_only_what_it_returned(
        self, tmp_path,
    ):
        path, _ = _seed_rss_feed(tmp_path)
        first = _StubResponse(status_code=200, content=TWO_ITEM_RSS)
        second = _StubResponse(status_code=200, content=SAMPLE_RSS)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(first),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            first_marker = feeds_db.list_feeds(conn)[0].last_items_seen_at
        _make_due(path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(second),
                now=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
            rows = {
                r["guid"]: r["last_seen_at"]
                for r in conn.execute("SELECT guid, last_seen_at FROM feed_entries")
            }
        assert feed.last_items_seen_at > first_marker
        assert rows["hello-1"] == feed.last_items_seen_at
        # The two the second response did not return keep the older stamp, and
        # are the only rows the age pass can reach.
        assert rows["one"] == first_marker
        assert rows["two"] == first_marker

    def test_a_valid_empty_response_does_not_advance_it(self, tmp_path):
        """A genuinely empty feed keeps its history.

        The cost is stated in the spec: such a feed is never age-pruned and is
        bounded by the maximum alone. That is the safe direction.
        """
        path, _ = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=SAMPLE_RSS)
                ),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            first_marker = feeds_db.list_feeds(conn)[0].last_items_seen_at
        _make_due(path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=EMPTY_RSS)
                ),
                now=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
            row = conn.execute(
                "SELECT last_seen_at FROM feed_entries WHERE guid='hello-1'"
            ).fetchone()
        assert feed.last_items_seen_at == first_marker
        assert row["last_seen_at"] == first_marker

    def test_an_html_error_page_does_not_advance_it(self, tmp_path):
        """The control the whole rule rests on.

        The assertion is on the marker itself, not on "no entry was inserted"
        — that is equally true of a legitimately empty feed, so it cannot tell
        the two apart, and only the marker says whether a feed's stored history
        just became age-eligible on the strength of a proxy error.
        """
        path, _ = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=SAMPLE_RSS)
                ),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            first_marker = feeds_db.list_feeds(conn)[0].last_items_seen_at
        _make_due(path)
        with feeds_db.connect(path) as conn:
            outcomes = poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=HTML_ERROR_PAGE)
                ),
                now=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
            row = conn.execute(
                "SELECT last_seen_at FROM feed_entries WHERE guid='hello-1'"
            ).fetchone()
        # The response was a 200 that parsed without complaint, so nothing
        # upstream of the marker rule refused it.
        assert outcomes[0][1].error is None
        assert outcomes[0][1].items == []
        assert feed.last_items_seen_at == first_marker
        assert row["last_seen_at"] == first_marker

    def test_a_304_preserves_it_and_clears_the_claim(self, tmp_path):
        path, _ = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=SAMPLE_RSS)
                ),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            first_marker = feeds_db.list_feeds(conn)[0].last_items_seen_at
        _make_due(path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(_StubResponse(status_code=304)),
                now=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.last_items_seen_at == first_marker
        assert feed.poll_claimed_until is None

    def test_an_error_preserves_it_and_clears_the_claim(self, tmp_path):
        path, _ = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=SAMPLE_RSS)
                ),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            first_marker = feeds_db.list_feeds(conn)[0].last_items_seen_at
        _make_due(path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(_StubResponse(status_code=503)),
                now=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.last_items_seen_at == first_marker
        assert feed.poll_claimed_until is None

    def test_a_rate_limit_preserves_it_and_clears_the_claim(self, tmp_path):
        path, _ = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=SAMPLE_RSS)
                ),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            first_marker = feeds_db.list_feeds(conn)[0].last_items_seen_at
        _make_due(path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn,
                http_get=_stub_get_factory(
                    _StubResponse(status_code=429, headers={"Retry-After": "60"})
                ),
                now=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.last_items_seen_at == first_marker
        assert feed.poll_claimed_until is None
        assert feed.last_throttled_at is not None

    def test_a_provider_response_advances_it(self, tmp_path, monkeypatch):
        """A provider that returned normally validated its own payload, and a
        non-empty list is an ordinary response carrying items."""
        from istota.feeds.models import FetchedItem
        from istota.feeds.providers import arena as arena_provider

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feeds_db.upsert_feed(
                conn, url="arena:chan", title=None, site_url=None,
                source_type="arena", category_id=None, poll_interval_minutes=60,
            )
            conn.commit()
        monkeypatch.setattr(
            arena_provider, "fetch", lambda ident, **kw: [FetchedItem(guid="b1")],
        )
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
            row = conn.execute(
                "SELECT last_seen_at FROM feed_entries WHERE guid='b1'"
            ).fetchone()
        assert feed.last_items_seen_at is not None
        assert row["last_seen_at"] == feed.last_items_seen_at

    def test_a_provider_that_rejects_its_payload_does_not_advance_it(
        self, tmp_path, monkeypatch,
    ):
        from istota.feeds.providers import arena as arena_provider

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="arena:chan", title=None, site_url=None,
                source_type="arena", category_id=None, poll_interval_minutes=60,
            )
            conn.execute(
                "UPDATE feeds SET last_items_seen_at = ? WHERE id = ?",
                ("2026-08-01T00:00:00+00:00", feed_id),
            )
            conn.commit()

        def _boom(ident, **kw):
            raise ValueError("arena data missing or not a list")

        monkeypatch.setattr(arena_provider, "fetch", _boom)
        with feeds_db.connect(path) as conn:
            outcomes = poll_due_feeds(
                conn, now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
        assert outcomes[0][1].error is not None
        assert feed.last_items_seen_at == "2026-08-01T00:00:00+00:00"
        assert feed.poll_claimed_until is None

    def test_a_successful_poll_clears_the_claim(self, tmp_path):
        path, _ = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=SAMPLE_RSS)
                ),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.poll_claimed_until is None


class TestPollClaimsInTheBatch:
    def test_a_claimed_feed_is_not_fetched(self, tmp_path):
        path, feed_id = _seed_rss_feed(tmp_path)
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        calls: list[str] = []

        def _get(url, **kwargs):
            calls.append(url)
            return _StubResponse(status_code=200, content=SAMPLE_RSS)

        with feeds_db.connect(path) as claimer:
            feeds_db.claim_feed_for_poll(claimer, feed_id, now=now)

        with feeds_db.connect(path) as conn:
            outcomes = poll_due_feeds(
                conn, http_get=_get, now=now + timedelta(seconds=5),
            )
            entries = feeds_db.list_entries(conn)
        assert calls == []
        assert outcomes == []
        assert entries == []

    def test_a_poll_claims_the_feed_before_fetching_it(self, tmp_path):
        """The claim is committed before the network call, not after it.

        A second process reading the table mid-fetch is exactly the race this
        exists for, so the assertion is made from inside the stub.
        """
        path, feed_id = _seed_rss_feed(tmp_path)
        observed: list[str | None] = []

        def _get(url, **kwargs):
            with feeds_db.connect(path) as other:
                row = other.execute(
                    "SELECT poll_claimed_until FROM feeds WHERE id = ?", (feed_id,),
                ).fetchone()
            observed.append(row["poll_claimed_until"])
            return _StubResponse(status_code=200, content=SAMPLE_RSS)

        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_get,
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
        assert observed and observed[0] is not None

    def test_a_claim_taken_after_the_due_list_still_skips_the_feed(
        self, tmp_path, monkeypatch,
    ):
        """The interval the in-loop claim exists for, and nothing else covers.

        The test above claims the feed before the run starts, so
        ``feeds_due_for_poll``'s own filter is what stops it and the
        ``claim_feed_for_poll`` refusal in the loop could be deleted with the
        suite still green. Here a rival takes the claim *after* the due SELECT
        returned the feed — the race between that SELECT and the request, which
        is the whole reason the second check is there.
        """
        path, feed_id = _seed_rss_feed(tmp_path)
        calls: list[str] = []

        def _get(url, **kwargs):
            calls.append(url)
            return _StubResponse(status_code=200, content=SAMPLE_RSS)

        real_due = feeds_db.feeds_due_for_poll

        def _due_then_stolen(conn, now=None):
            feeds = real_due(conn, now=now)
            with feeds_db.connect(path) as rival:
                feeds_db.claim_feed_for_poll(rival, feed_id, now=now)
            return feeds

        monkeypatch.setattr(feeds_db, "feeds_due_for_poll", _due_then_stolen)

        with feeds_db.connect(path) as conn:
            outcomes = poll_due_feeds(
                conn, http_get=_get,
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            entries = feeds_db.list_entries(conn)
            feed = feeds_db.list_feeds(conn)[0]
        assert calls == []
        assert outcomes == []
        assert entries == []
        # The rival's lease is intact — the skip released nothing it did not
        # take, which is what makes it safe to run two polls at once.
        assert feed.poll_claimed_until is not None
        assert feed.last_fetched_at is None

    def test_an_expired_claim_does_not_block_the_next_run(self, tmp_path):
        path, feed_id = _seed_rss_feed(tmp_path)
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with feeds_db.connect(path) as claimer:
            feeds_db.claim_feed_for_poll(claimer, feed_id, now=now)
        resp = _StubResponse(status_code=200, content=SAMPLE_RSS)
        with feeds_db.connect(path) as conn:
            outcomes = poll_due_feeds(
                conn, http_get=_stub_get_factory(resp),
                now=now + timedelta(seconds=POLL_CLAIM_SECONDS + 1),
            )
        assert len(outcomes) == 1


class TestConditionalValidators:
    """An ETag off a document that is not a feed must not be stored.

    A liveness rule rather than a retention one, and it is scoped as narrowly
    as the marker rule beside it: nothing here judges whether a response was
    complete. A 200 that yielded no items *and* that the parser did not
    recognise as a feed at all is the case — an HTML error page — and storing
    its validator answers every later request with a 304, so the feed stops
    updating with `last_error` clear and nothing saying why.
    """

    def test_an_html_error_page_stores_no_validators(self):
        resp = _StubResponse(
            status_code=200, content=HTML_ERROR_PAGE,
            headers={"ETag": '"error-page"',
                     "Last-Modified": "Thu, 01 May 2026 12:00:00 GMT"},
        )
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.items == []
        assert result.etag is None
        assert result.last_modified is None

    def test_a_genuinely_empty_feed_keeps_its_conditional_get(self):
        """The control, and the reason the guard is not "no items".

        An empty feed is a feed. Refusing its validator would cost it a full
        body fetch on every poll, for ever, to no purpose.
        """
        resp = _StubResponse(
            status_code=200, content=EMPTY_RSS,
            headers={"ETag": '"empty-but-real"',
                     "Last-Modified": "Thu, 01 May 2026 12:00:00 GMT"},
        )
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.items == []
        assert result.etag == '"empty-but-real"'
        assert result.last_modified == "Thu, 01 May 2026 12:00:00 GMT"

    def test_an_ordinary_response_stores_its_validators(self):
        resp = _StubResponse(
            status_code=200, content=SAMPLE_RSS,
            headers={"ETag": '"good"'},
        )
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.etag == '"good"'


class TestPollDueFeedsClock:
    def test_a_naive_clock_is_refused(self, tmp_path):
        """A naive stamp sorts below every `+00:00` one in the same column, so
        a poll would write entries whose observation reads as older than the
        marker written in the same transaction."""
        path, _ = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            with pytest.raises(ValueError):
                poll_due_feeds(
                    conn,
                    http_get=_stub_get_factory(
                        _StubResponse(status_code=200, content=SAMPLE_RSS)
                    ),
                    now=datetime(2026, 9, 1, 12, 0),
                )
            assert feeds_db.list_entries(conn) == []


class TestAdmission:
    """``plan_admission`` caps one response at the same budget the count pass
    enforces, so a response larger than the maximum has no tail to churn."""

    def _feed(self, tmp_path):
        path, feed_id = _seed_rss_feed(tmp_path, url="arena:chan")
        with feeds_db.connect(path) as conn:
            conn.execute(
                "UPDATE feeds SET source_type = 'arena' WHERE id = ?", (feed_id,),
            )
            conn.commit()
        return path, feed_id

    def _poll_with(self, path, guids, *, when, monkeypatch):
        from istota.feeds.models import FetchedItem
        from istota.feeds.providers import arena as arena_provider

        monkeypatch.setattr(
            arena_provider, "fetch",
            lambda ident, **kw: [FetchedItem(guid=g, title=g) for g in guids],
        )
        _make_due(path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(conn, now=when)

    def test_a_response_larger_than_the_maximum_admits_only_the_budget(
        self, tmp_path, monkeypatch,
    ):
        path, _ = self._feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.set_max_entries_per_feed(conn, 5)
            conn.commit()
        self._poll_with(
            path, [f"b{i:02d}" for i in range(12)],
            when=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            monkeypatch=monkeypatch,
        )
        with feeds_db.connect(path) as conn:
            guids = [
                r["guid"]
                for r in conn.execute("SELECT guid FROM feed_entries ORDER BY guid")
            ]
        # Source order, so the first five blocks of the page.
        assert guids == ["b00", "b01", "b02", "b03", "b04"]

    def test_repeating_that_response_inserts_nothing_and_resurrects_nothing(
        self, tmp_path, monkeypatch,
    ):
        path, _ = self._feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.set_max_entries_per_feed(conn, 5)
            conn.commit()
        page = [f"b{i:02d}" for i in range(12)]
        self._poll_with(
            path, page, when=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            monkeypatch=monkeypatch,
        )
        with feeds_db.connect(path) as conn:
            conn.execute("UPDATE feed_entries SET status = 'read'")
            conn.commit()
        self._poll_with(
            path, page, when=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
            monkeypatch=monkeypatch,
        )
        with feeds_db.connect(path) as conn:
            rows = conn.execute(
                "SELECT guid, status FROM feed_entries ORDER BY guid"
            ).fetchall()
        assert [r["guid"] for r in rows] == ["b00", "b01", "b02", "b03", "b04"]
        assert {r["status"] for r in rows} == {"read"}

    def _star(self, path, feed_id, guid):
        with feeds_db.connect(path) as conn:
            conn.execute(
                "INSERT INTO feed_entries(feed_id, guid, title, fetched_at, "
                "last_seen_at, status, starred) "
                "VALUES (?, ?, ?, ?, ?, 'read', 1)",
                (feed_id, guid, guid, "2026-08-01T00:00:00+00:00",
                 "2026-08-01T00:00:00+00:00"),
            )
            conn.commit()

    def test_a_returned_star_is_always_admitted_and_the_budget_is_floored(
        self, tmp_path, monkeypatch,
    ):
        """At or below ``MIN_ENTRIES_PER_FEED`` the floor *is* the maximum.

        So a star takes nothing off the budget and the feed goes over the
        maximum by its stars instead — which is where stars already sat by
        design, and is what stops a feed whose stars fill its maximum from
        going permanently inert.
        """
        path, feed_id = self._feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.set_max_entries_per_feed(conn, 3)
            conn.commit()
        self._star(path, feed_id, "b09")
        self._poll_with(
            path, [f"b{i:02d}" for i in range(12)],
            when=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            monkeypatch=monkeypatch,
        )
        with feeds_db.connect(path) as conn:
            rows = conn.execute(
                "SELECT guid, last_seen_at FROM feed_entries ORDER BY guid"
            ).fetchall()
            feed = feeds_db.list_feeds(conn)[0]
        # The star is refreshed whatever its position in the page, and the
        # unstarred budget is the floored 3 rather than 3 - 1.
        assert [r["guid"] for r in rows] == ["b00", "b01", "b02", "b09"]
        assert {r["last_seen_at"] for r in rows} == {feed.last_items_seen_at}

    def test_stars_consume_the_total_above_the_floor(
        self, tmp_path, monkeypatch,
    ):
        """Where the maximum is above the floor, stars do come off it."""
        cap = MIN_ENTRIES_PER_FEED + 5
        path, feed_id = self._feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.set_max_entries_per_feed(conn, cap)
            conn.commit()
        for guid in ("b57", "b58", "b59"):
            self._star(path, feed_id, guid)
        self._poll_with(
            path, [f"b{i:02d}" for i in range(60)],
            when=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            monkeypatch=monkeypatch,
        )
        with feeds_db.connect(path) as conn:
            guids = [
                r["guid"]
                for r in conn.execute("SELECT guid FROM feed_entries ORDER BY guid")
            ]
        # 55 - 3 stars = 52 unstarred items in source order, plus the three
        # stars the page returned.
        assert guids == [f"b{i:02d}" for i in range(52)] + ["b57", "b58", "b59"]

    def test_a_feed_whose_stars_fill_its_maximum_still_admits_the_floor(
        self, tmp_path, monkeypatch,
    ):
        """Otherwise the budget is zero for good and the feed stores nothing.

        Nothing surfaces that state, so the feed looks like a source that has
        stopped publishing rather than a setting that has stopped working.
        """
        path, feed_id = self._feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.set_max_entries_per_feed(conn, 3)
            conn.commit()
        for i in range(4):
            self._star(path, feed_id, f"kept{i}")
        self._poll_with(
            path, [f"b{i:02d}" for i in range(12)],
            when=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            monkeypatch=monkeypatch,
        )
        with feeds_db.connect(path) as conn:
            guids = [
                r["guid"]
                for r in conn.execute(
                    "SELECT guid FROM feed_entries WHERE starred = 0 ORDER BY guid"
                )
            ]
        assert guids == ["b00", "b01", "b02"]

    def test_a_maximum_of_zero_admits_every_identifiable_item(
        self, tmp_path, monkeypatch,
    ):
        path, _ = self._feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.set_max_entries_per_feed(conn, 0)
            conn.commit()
        self._poll_with(
            path, [f"b{i:02d}" for i in range(12)],
            when=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            monkeypatch=monkeypatch,
        )
        with feeds_db.connect(path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) c FROM feed_entries"
            ).fetchone()["c"]
        assert count == 12

    def test_plan_admission_drops_unidentifiable_and_duplicate_items(
        self, tmp_path,
    ):
        from istota.feeds import retention
        from istota.feeds.models import FetchedItem

        path, feed_id = self._feed(tmp_path)
        items = [
            FetchedItem(guid=""),
            FetchedItem(guid="a"),
            FetchedItem(guid="a", title="second copy"),
            FetchedItem(guid="b"),
        ]
        with feeds_db.connect(path) as conn:
            admitted = retention.plan_admission(
                conn, feed_id, items, max_entries_per_feed=10,
            )
        assert [i.guid for i in admitted] == ["a", "b"]
        # The first occurrence wins: a later copy is the same entry, and
        # taking it would make the window depend on write order.
        assert admitted[0].title is None
