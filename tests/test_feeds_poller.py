"""Tests for the polling engine — RSS conditional GET + error backoff.

Doesn't hit the network. ``http_get`` is stubbed; the Tumblr/Are.na
provider modules are tested separately (Phase 1 keeps them as a vendored
copy of the rss-bridger logic, which already has its own tests).
"""

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("feedparser", reason="feeds extra not installed")

from istota.feeds import db as feeds_db
from istota.feeds.models import POLL_CLAIM_SECONDS, FeedRecord
from istota.feeds.poller import (
    _backoff_interval,
    _parse_is_truncated,
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
# membership snapshots + poll claims (ISSUE-388)
# ---------------------------------------------------------------------------


EMPTY_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example</title></channel></rss>
"""

# The two documents the completeness gate has to tell apart, and each other's
# control (ISSUE-388). Both are bozo with the same recognised `version`; the
# only thing separating them is expat's message, which is why they are literal
# bodies through the real feedparser rather than a stubbed parse result. A
# wording change in expat then fails a test instead of silently switching
# retention off for every imperfect feed, or on for truncated ones.
#
# Whole document, one undeclared entity. Every entry is recovered and absence
# from it is meaningful, so it must count as a complete snapshot. This is the
# most common defect in feeds in the wild.
ENTITY_RSS = b"""<?xml version="1.0"?>
<rss version="2.0">
<channel>
<title>Example</title>
<item><guid>hello-1</guid><title>Hello &nbsp; there</title></item>
<item><guid>hello-2</guid><title>Second</title></item>
<item><guid>hello-3</guid><title>Third</title></item>
</channel>
</rss>
"""

# The same feed cut off after its second item. `version` is intact because the
# root element is at the head; the tail is simply gone. Treating it as complete
# would make the missing entries historical and age them out.
TRUNCATED_RSS = b"""<?xml version="1.0"?>
<rss version="2.0">
<channel>
<title>Example</title>
<item><guid>hello-1</guid><title>Hello</title></item>
<item><guid>hello-2</guid><title>Second</title></item>
"""

# The same pair in Atom. The discriminator is a message from the XML parser
# rather than anything format-specific, but that is a claim worth holding
# rather than assuming: without this pair, a wording change reaching only one
# format would move retention for half the feeds in the reader silently.
ENTITY_ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Example</title>
<entry><id>urn:1</id><title>First &nbsp; one</title></entry>
<entry><id>urn:2</id><title>Second</title></entry>
</feed>
"""

TRUNCATED_ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Example</title>
<entry><id>urn:1</id><title>First</title></entry>
"""

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


class TestMembershipComplete:
    def test_a_well_formed_feed_is_a_complete_snapshot(self):
        resp = _StubResponse(status_code=200, content=SAMPLE_RSS)
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.membership_complete is True

    def test_a_well_formed_empty_feed_is_still_complete(self):
        resp = _StubResponse(status_code=200, content=EMPTY_RSS)
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.items == []
        assert result.membership_complete is True

    def test_an_html_error_page_is_not_complete(self):
        resp = _StubResponse(status_code=200, content=HTML_ERROR_PAGE)
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.membership_complete is False

    def test_an_empty_body_is_not_complete(self):
        resp = _StubResponse(status_code=200, content=b"")
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.membership_complete is False

    def test_a_truncated_document_is_not_complete_but_keeps_its_items(self):
        """The tail is gone and nothing in the document says so.

        Its entries are still identifiable and still worth refreshing; what it
        may not do is make the entries it lost look like entries the source
        dropped.
        """
        resp = _StubResponse(status_code=200, content=TRUNCATED_RSS)
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.membership_complete is False
        assert [i.guid for i in result.items] == ["hello-1", "hello-2"]

    def test_an_undeclared_entity_is_still_a_complete_snapshot(self):
        """The control for the test above, and the reason ``bozo`` alone is not
        the gate.

        Both documents are bozo with the same recognised version. This one
        arrived whole with every entry recovered, and it is the commonest
        defect in real feeds — gating on the raw flag would leave a large,
        unknowable subset of feeds never establishing a snapshot, and so never
        pruned by either pass.
        """
        resp = _StubResponse(status_code=200, content=ENTITY_RSS)
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.membership_complete is True
        assert [i.guid for i in result.items] == ["hello-1", "hello-2", "hello-3"]

    def test_both_documents_really_are_bozo_with_the_same_version(self):
        """What the pair above is worth depends on this being true.

        If a feedparser upgrade stopped flagging one of them, or recognised
        one's version and not the other's, the two tests would still pass while
        testing nothing about the truncation predicate.
        """
        import feedparser

        entity = feedparser.parse(ENTITY_RSS)
        truncated = feedparser.parse(TRUNCATED_RSS)
        assert entity.get("version") == truncated.get("version") == "rss20"
        assert bool(entity.get("bozo")) and bool(truncated.get("bozo"))
        assert _parse_is_truncated(truncated) is True
        assert _parse_is_truncated(entity) is False

    def test_a_clean_parse_is_never_truncated(self):
        import feedparser

        assert _parse_is_truncated(feedparser.parse(SAMPLE_RSS)) is False

    def test_a_flagged_parse_with_nothing_to_inspect_reads_as_truncated(self):
        """Fails in the safe direction: not advancing a snapshot costs a poll
        cycle, advancing one wrongly deletes entries."""
        assert _parse_is_truncated({"bozo": 1}) is True
        assert _parse_is_truncated({"bozo": 1, "bozo_exception": None}) is True

    def test_a_structurally_invalid_empty_response_stores_no_validators(self):
        """An error page's ETag must never be stored.

        Storing it turns every later request into a 304 against the error
        page, so the feed can never recover on its own.
        """
        resp = _StubResponse(
            status_code=200, content=HTML_ERROR_PAGE,
            headers={"ETag": '"error-page"',
                     "Last-Modified": "Thu, 01 May 2026 12:00:00 GMT"},
        )
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.etag is None
        assert result.last_modified is None

    def test_a_truncated_response_stores_no_validators_either(self):
        """The same rule, for a document that did yield items.

        Truncation happens in transit, so the ETag is the validator for the
        *full* body: storing it pins the feed at 304 while its snapshot marker
        never advances, and a feed with no marker is exempt from both retention
        passes. One extra full fetch is the cost of refusing it.
        """
        resp = _StubResponse(
            status_code=200, content=TRUNCATED_RSS,
            headers={"ETag": '"partial"',
                     "Last-Modified": "Thu, 01 May 2026 12:00:00 GMT"},
        )
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.items  # the head was readable and is still worth storing
        assert result.etag is None
        assert result.last_modified is None

    def test_a_complete_response_does_store_its_validators(self):
        """The control for the two above: the guard is scoped to a document we
        could not read, not applied to every response."""
        resp = _StubResponse(
            status_code=200, content=ENTITY_RSS,
            headers={"ETag": '"good"',
                     "Last-Modified": "Thu, 01 May 2026 12:00:00 GMT"},
        )
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.etag == '"good"'
        assert result.last_modified == "Thu, 01 May 2026 12:00:00 GMT"

    def test_a_truncated_atom_document_is_not_complete_either(self):
        """The predicate is pinned on Atom as well as RSS.

        Same pair, same reasoning: both documents are bozo with the same
        recognised version, and only the expat message separates them. Without
        an Atom pair a format-specific change in the message would move
        retention for half the feeds in the reader with nothing failing.
        """
        entity = poll_feed(
            _rss_feed(),
            http_get=_stub_get_factory(
                _StubResponse(status_code=200, content=ENTITY_ATOM)
            ),
        )
        truncated = poll_feed(
            _rss_feed(),
            http_get=_stub_get_factory(
                _StubResponse(status_code=200, content=TRUNCATED_ATOM)
            ),
        )
        assert entity.membership_complete is True
        assert [i.guid for i in entity.items] == ["urn:1", "urn:2"]
        assert truncated.membership_complete is False
        assert [i.guid for i in truncated.items] == ["urn:1"]

    def test_both_atom_documents_are_bozo_with_the_same_version(self):
        import feedparser

        entity = feedparser.parse(ENTITY_ATOM)
        truncated = feedparser.parse(TRUNCATED_ATOM)
        assert entity.get("version") == truncated.get("version") == "atom10"
        assert bool(entity.get("bozo")) and bool(truncated.get("bozo"))

    def test_a_304_is_not_complete(self):
        resp = _StubResponse(status_code=304)
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.not_modified is True
        assert result.membership_complete is False

    def test_an_http_error_is_not_complete(self):
        resp = _StubResponse(status_code=503)
        result = poll_feed(_rss_feed(), http_get=_stub_get_factory(resp))
        assert result.error is not None
        assert result.membership_complete is False

    def test_a_provider_that_returns_normally_is_complete(self, monkeypatch):
        from istota.feeds.models import FetchedItem
        from istota.feeds.providers import arena as arena_provider

        monkeypatch.setattr(
            arena_provider, "fetch", lambda ident, **kw: [FetchedItem(guid="b1")],
        )
        feed = FeedRecord(
            id=2, url="arena:chan", title=None, site_url=None, category_id=None,
            source_type="arena", etag=None, last_modified=None,
            last_fetched_at=None, last_error=None, error_count=0,
            poll_interval_minutes=60, next_poll_at=None,
        )
        result = poll_feed(feed)
        assert result.membership_complete is True

    def test_a_provider_that_rejects_its_payload_is_not_complete(self, monkeypatch):
        from istota.feeds.providers import tumblr as tumblr_provider

        def _boom(ident, **kw):
            raise ValueError("tumblr response.posts missing or not a list")

        monkeypatch.setattr(tumblr_provider, "fetch", _boom)
        feed = FeedRecord(
            id=3, url="tumblr:blog", title=None, site_url=None, category_id=None,
            source_type="tumblr", etag=None, last_modified=None,
            last_fetched_at=None, last_error=None, error_count=0,
            poll_interval_minutes=60, next_poll_at=None,
        )
        result = poll_feed(feed, tumblr_api_key="k")
        assert result.error is not None
        assert result.membership_complete is False


class TestSnapshotPersistence:
    def test_a_complete_response_stamps_the_marker_the_entries_and_the_ranks(
        self, tmp_path,
    ):
        path, feed_id = _seed_rss_feed(tmp_path)
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        resp = _StubResponse(status_code=200, content=TWO_ITEM_RSS)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(conn, http_get=_stub_get_factory(resp), now=now)
            feed = feeds_db.list_feeds(conn)[0]
            rows = {
                r["guid"]: r
                for r in conn.execute(
                    "SELECT guid, last_seen_at, document_rank FROM feed_entries"
                )
            }
        assert feed.current_document_at is not None
        assert {g: r["document_rank"] for g, r in rows.items()} == {"one": 0, "two": 1}
        # Entries admitted from the response carry exactly the feed's marker.
        assert {r["last_seen_at"] for r in rows.values()} == {
            feed.current_document_at
        }

    def test_a_second_complete_response_advances_the_marker(self, tmp_path):
        path, _ = _seed_rss_feed(tmp_path)
        first = _StubResponse(status_code=200, content=TWO_ITEM_RSS)
        second = _StubResponse(status_code=200, content=SAMPLE_RSS)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(first),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            first_marker = feeds_db.list_feeds(conn)[0].current_document_at
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
        assert feed.current_document_at > first_marker
        # Only the guid the second document returned is stamped with it; the
        # two it dropped keep the older observation and become history.
        assert rows["hello-1"] == feed.current_document_at
        assert rows["one"] == first_marker
        assert rows["two"] == first_marker

    def test_a_valid_empty_response_advances_the_marker_and_touches_no_entry(
        self, tmp_path,
    ):
        path, _ = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=SAMPLE_RSS)
                ),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            first_marker = feeds_db.list_feeds(conn)[0].current_document_at
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
        assert feed.current_document_at > first_marker
        assert row["last_seen_at"] == first_marker

    def test_an_incomplete_response_refreshes_without_advancing_the_marker(
        self, tmp_path,
    ):
        path, _ = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=SAMPLE_RSS)
                ),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            first_marker = feeds_db.list_feeds(conn)[0].current_document_at
        _make_due(path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=TRUNCATED_RSS)
                ),
                now=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
            row = conn.execute(
                "SELECT last_seen_at, document_rank FROM feed_entries "
                "WHERE guid='hello-1'"
            ).fetchone()
        assert feed.current_document_at == first_marker
        # The entry was observed, so it is not a deletion candidate — but it
        # is now ahead of the marker rather than part of it.
        assert row["last_seen_at"] > first_marker
        assert row["document_rank"] == 0

    def test_an_undeclared_entity_response_still_advances_the_marker(
        self, tmp_path,
    ):
        """The consequence the truncation predicate exists for.

        Under a plain ``bozo`` gate this feed would never establish a snapshot,
        and since the count pass is gated on the same marker, neither retention
        pass would ever touch it — the growth this change bounds, unbounded,
        for the commonest defect there is.
        """
        path, _ = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=ENTITY_RSS)
                ),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
            ranks = {
                r["guid"]: r["document_rank"]
                for r in conn.execute("SELECT guid, document_rank FROM feed_entries")
            }
        assert feed.current_document_at is not None
        assert ranks == {"hello-1": 0, "hello-2": 1, "hello-3": 2}

    def test_a_304_preserves_the_marker_and_clears_the_claim(self, tmp_path):
        path, feed_id = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=SAMPLE_RSS)
                ),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            first_marker = feeds_db.list_feeds(conn)[0].current_document_at
        _make_due(path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(_StubResponse(status_code=304)),
                now=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.current_document_at == first_marker
        assert feed.poll_claimed_until is None

    def test_an_error_preserves_the_marker_and_clears_the_claim(self, tmp_path):
        path, _ = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=SAMPLE_RSS)
                ),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            first_marker = feeds_db.list_feeds(conn)[0].current_document_at
        _make_due(path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(_StubResponse(status_code=503)),
                now=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
            )
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.current_document_at == first_marker
        assert feed.poll_claimed_until is None

    def test_a_rate_limit_preserves_the_marker_and_clears_the_claim(self, tmp_path):
        path, _ = _seed_rss_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            poll_due_feeds(
                conn, http_get=_stub_get_factory(
                    _StubResponse(status_code=200, content=SAMPLE_RSS)
                ),
                now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            )
            first_marker = feeds_db.list_feeds(conn)[0].current_document_at
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
        assert feed.current_document_at == first_marker
        assert feed.poll_claimed_until is None
        assert feed.last_throttled_at is not None

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
