"""Tests for feed_poller.py module."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config, SiteConfig, UserConfig
from istota.feed_poller import (
    FeedConfig,
    FeedsConfig,
    _build_feed_page_html,
    _build_filter_css,
    _build_status_text,
    _interval_elapsed,
    _poll_single_feed,
    _truncate,
    check_feeds,
    fetch_arena,
    fetch_rss,
    fetch_tumblr,
    generate_static_feed_page,
    parse_feeds_config,
)


SAMPLE_FEEDS_MD = """\
# Feed Subscriptions

```toml
[tumblr]
api_key = "test-tumblr-key"

[[feeds]]
name = "hn-best"
type = "rss"
url = "https://hnrss.org/best"
interval_minutes = 30

[[feeds]]
name = "photoblog"
type = "tumblr"
url = "blogname"
interval_minutes = 180

[[feeds]]
name = "arena-channel"
type = "arena"
url = "channel-slug"
interval_minutes = 60
```
"""

COMMENTED_FEEDS_MD = """\
# Feed Subscriptions

```toml
# [tumblr]
# api_key = "test-key"

# [[feeds]]
# name = "hn-best"
# type = "rss"
# url = "https://hnrss.org/best"
```
"""


# ============================================================================
# Config parsing
# ============================================================================


class TestParseFeedsConfig:
    def test_parses_full_config(self, tmp_path):
        feeds_file = tmp_path / "FEEDS.md"
        feeds_file.write_text(SAMPLE_FEEDS_MD)
        config = parse_feeds_config(feeds_file)
        assert config is not None
        assert config.tumblr_api_key == "test-tumblr-key"
        assert len(config.feeds) == 3
        assert config.feeds[0].name == "hn-best"
        assert config.feeds[0].type == "rss"
        assert config.feeds[0].url == "https://hnrss.org/best"
        assert config.feeds[0].interval_minutes == 30
        assert config.feeds[1].name == "photoblog"
        assert config.feeds[1].type == "tumblr"
        assert config.feeds[1].interval_minutes == 180
        assert config.feeds[2].name == "arena-channel"
        assert config.feeds[2].type == "arena"
        assert config.feeds[2].interval_minutes == 60

    def test_returns_none_for_missing_file(self, tmp_path):
        assert parse_feeds_config(tmp_path / "missing.md") is None

    def test_returns_none_for_empty_file(self, tmp_path):
        f = tmp_path / "FEEDS.md"
        f.write_text("")
        assert parse_feeds_config(f) is None

    def test_returns_none_for_fully_commented_config(self, tmp_path):
        f = tmp_path / "FEEDS.md"
        f.write_text(COMMENTED_FEEDS_MD)
        assert parse_feeds_config(f) is None

    def test_default_intervals(self, tmp_path):
        f = tmp_path / "FEEDS.md"
        f.write_text("""\
# Feeds

```toml
[[feeds]]
name = "rss-feed"
type = "rss"
url = "https://example.com/rss"

[[feeds]]
name = "tumblr-feed"
type = "tumblr"
url = "blogname"

[[feeds]]
name = "arena-feed"
type = "arena"
url = "channel"
```
""")
        config = parse_feeds_config(f)
        assert config is not None
        assert config.feeds[0].interval_minutes == 30   # rss default
        assert config.feeds[1].interval_minutes == 180  # tumblr default
        assert config.feeds[2].interval_minutes == 60   # arena default

    def test_no_toml_block(self, tmp_path):
        f = tmp_path / "FEEDS.md"
        f.write_text("# Feeds\n\nJust some text, no code block.\n")
        assert parse_feeds_config(f) is None


# ============================================================================
# RSS provider
# ============================================================================


class TestFetchRss:
    @patch("istota.feed_poller.feedparser.parse")
    def test_parses_entries(self, mock_parse):
        mock_parse.return_value = SimpleNamespace(
            status=200,
            etag='"new-etag"',
            entries=[
                SimpleNamespace(
                    id="guid-1",
                    title="Test Article",
                    link="https://example.com/1",
                    summary="<p>Summary text</p>",
                    author="Author Name",
                    published_parsed=(2026, 2, 1, 12, 0, 0, 0, 0, 0),
                    enclosures=[],
                ),
            ],
        )
        items, etag, modified = fetch_rss("https://example.com/rss")
        assert len(items) == 1
        assert items[0]["item_id"] == "guid-1"
        assert items[0]["title"] == "Test Article"
        assert items[0]["url"] == "https://example.com/1"
        assert items[0]["content_text"] == "Summary text"
        assert items[0]["author"] == "Author Name"
        assert etag == '"new-etag"'

    @patch("istota.feed_poller.feedparser.parse")
    def test_304_not_modified(self, mock_parse):
        mock_parse.return_value = SimpleNamespace(status=304, entries=[])
        items, etag, modified = fetch_rss("https://example.com/rss", etag='"old"')
        assert items == []
        assert etag == '"old"'

    @patch("istota.feed_poller.feedparser.parse")
    def test_image_from_enclosure(self, mock_parse):
        mock_parse.return_value = SimpleNamespace(
            status=200,
            entries=[
                SimpleNamespace(
                    id="guid-img",
                    title="Image Post",
                    link="https://example.com/img",
                    author=None,
                    enclosures=[SimpleNamespace(type="image/jpeg", href="https://example.com/photo.jpg")],
                    published_parsed=None,
                    updated_parsed=None,
                ),
            ],
        )
        items, _, _ = fetch_rss("https://example.com/rss")
        assert items[0]["image_url"] == "https://example.com/photo.jpg"

    @patch("istota.feed_poller.feedparser.parse")
    def test_image_from_media_content(self, mock_parse):
        mock_parse.return_value = SimpleNamespace(
            status=200,
            entries=[
                SimpleNamespace(
                    id="guid-mc",
                    title="Media Post",
                    link="https://example.com/mc",
                    author=None,
                    enclosures=[],
                    media_content=[{"medium": "image", "url": "https://example.com/media.jpg"}],
                    published_parsed=None,
                    updated_parsed=None,
                ),
            ],
        )
        items, _, _ = fetch_rss("https://example.com/rss")
        assert items[0]["image_url"] == "https://example.com/media.jpg"


# ============================================================================
# Tumblr provider
# ============================================================================


class TestFetchTumblr:
    @patch("istota.feed_poller.requests.get")
    def test_parses_posts(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "response": {
                    "posts": [
                        {
                            "id": 12345,
                            "post_url": "https://blog.tumblr.com/post/12345",
                            "summary": "Test post",
                            "date": "2026-02-01 12:00:00 GMT",
                            "content": [
                                {"type": "text", "text": "Hello world"},
                                {"type": "image", "media": [{"url": "https://img.tumblr.com/photo.jpg"}]},
                            ],
                        },
                    ],
                },
            },
        )
        items = fetch_tumblr("blogname", "api-key")
        assert len(items) == 1
        assert items[0]["item_id"] == "12345"
        assert items[0]["title"] == "Test post"
        assert items[0]["image_url"] == "https://img.tumblr.com/photo.jpg"
        assert items[0]["content_text"] == "Hello world"
        assert items[0]["author"] == "blogname"

    @patch("istota.feed_poller.requests.get")
    def test_extracts_images_from_reblog_trail(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "response": {
                    "posts": [
                        {
                            "id": 99999,
                            "post_url": "https://blog.tumblr.com/post/99999",
                            "summary": "Reblogged post",
                            "date": "2026-02-01 12:00:00 GMT",
                            "content": [],
                            "trail": [
                                {
                                    "content": [
                                        {"type": "image", "media": [{"url": "https://img.tumblr.com/trail.jpg"}]},
                                        {"type": "text", "text": "Original caption"},
                                    ],
                                },
                            ],
                        },
                    ],
                },
            },
        )
        items = fetch_tumblr("reblogger", "api-key")
        assert len(items) == 1
        assert items[0]["image_url"] == "https://img.tumblr.com/trail.jpg"
        assert items[0]["content_text"] == "Original caption"

    @patch("istota.feed_poller.requests.get")
    def test_collects_multiple_images(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "response": {
                    "posts": [
                        {
                            "id": 55555,
                            "post_url": "https://blog.tumblr.com/post/55555",
                            "summary": "Photoset",
                            "date": "2026-02-01 12:00:00 GMT",
                            "content": [
                                {"type": "image", "media": [{"url": "https://img.tumblr.com/1.jpg"}]},
                                {"type": "image", "media": [{"url": "https://img.tumblr.com/2.jpg"}]},
                                {"type": "image", "media": [{"url": "https://img.tumblr.com/3.jpg"}]},
                            ],
                        },
                    ],
                },
            },
        )
        items = fetch_tumblr("photoblog", "api-key")
        assert len(items) == 1
        import json
        urls = json.loads(items[0]["image_url"])
        assert len(urls) == 3
        assert urls[0] == "https://img.tumblr.com/1.jpg"


# ============================================================================
# Are.na provider
# ============================================================================


class TestFetchArena:
    @patch("istota.feed_poller.httpx.get")
    def test_parses_blocks(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "contents": [
                    {
                        "id": 999,
                        "class": "Image",
                        "title": "Cool image",
                        "image": {
                            "original": {"url": "https://cdn.are.na/img.jpg?bc=0"},
                            "display": {"url": "https://images.are.na/base64encoded"},
                        },
                        "source": None,
                        "connected_at": "2026-02-01T12:00:00Z",
                        "user": {"full_name": "Arena User", "slug": "arenauser"},
                    },
                    {
                        "id": 998,
                        "class": "Text",
                        "title": "Note",
                        "content": "Some text content",
                        "source": None,
                        "connected_at": "2026-02-01T11:00:00Z",
                        "user": {"full_name": "Arena User", "slug": "arenauser"},
                    },
                ],
            },
        )
        items = fetch_arena("channel-slug")
        assert len(items) == 2
        assert items[0]["item_id"] == "999"
        assert items[0]["image_url"] == "https://cdn.are.na/img.jpg"
        assert items[0]["author"] == "Arena User"
        assert items[1]["item_id"] == "998"
        assert items[1]["content_text"] == "Some text content"

    @patch("istota.feed_poller.httpx.get")
    def test_skips_seen_items(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "contents": [
                    {"id": 100, "class": "Text", "title": "New", "content": "new",
                     "source": None, "connected_at": "2026-02-01T12:00:00Z", "user": None},
                    {"id": 50, "class": "Text", "title": "Old", "content": "old",
                     "source": None, "connected_at": "2026-01-01T12:00:00Z", "user": None},
                ],
            },
        )
        items = fetch_arena("channel-slug", since_id="75")
        assert len(items) == 1
        assert items[0]["item_id"] == "100"


# ============================================================================
# DB operations
# ============================================================================


class TestFeedDBOperations:
    def test_feed_state_crud(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            # Initially no state
            assert db.get_feed_state(conn, "alice", "hn") is None

            # Create state
            db.update_feed_state(conn, "alice", "hn",
                                 last_poll_at="2026-02-01T12:00:00",
                                 last_item_id="guid-1",
                                 consecutive_errors=0)
            state = db.get_feed_state(conn, "alice", "hn")
            assert state is not None
            assert state.last_poll_at == "2026-02-01T12:00:00"
            assert state.last_item_id == "guid-1"

            # Update state
            db.update_feed_state(conn, "alice", "hn",
                                 last_item_id="guid-2",
                                 etag='"etag-val"')
            state = db.get_feed_state(conn, "alice", "hn")
            assert state.last_item_id == "guid-2"
            assert state.etag == '"etag-val"'

    def test_feed_item_insert_and_dedup(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            assert db.insert_feed_item(conn, "alice", "hn", "id-1", title="Post 1")
            assert db.insert_feed_item(conn, "alice", "hn", "id-2", title="Post 2")
            # Duplicate
            assert not db.insert_feed_item(conn, "alice", "hn", "id-1", title="Post 1 dup")

            items = db.get_feed_items(conn, "alice")
            assert len(items) == 2

    def test_feed_item_ordering(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.insert_feed_item(conn, "alice", "hn", "id-old",
                                title="Old", published_at="2026-01-01T00:00:00")
            db.insert_feed_item(conn, "alice", "hn", "id-new",
                                title="New", published_at="2026-02-01T00:00:00")
            items = db.get_feed_items(conn, "alice")
            assert items[0].title == "New"
            assert items[1].title == "Old"

    def test_cleanup_old_feed_items(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            # Insert item with old fetched_at
            conn.execute(
                """INSERT INTO feed_items (user_id, feed_name, item_id, title, fetched_at)
                   VALUES (?, ?, ?, ?, ?)""",
                ("alice", "hn", "old-1", "Old", "2025-01-01T00:00:00"),
            )
            conn.execute(
                """INSERT INTO feed_items (user_id, feed_name, item_id, title, fetched_at)
                   VALUES (?, ?, ?, ?, ?)""",
                ("alice", "hn", "new-1", "New", datetime.now(timezone.utc).isoformat()),
            )
            deleted = db.cleanup_old_feed_items(conn, retention_days=30)
            assert deleted == 1
            items = db.get_feed_items(conn, "alice")
            assert len(items) == 1
            assert items[0].title == "New"


# ============================================================================
# Interval / jitter logic
# ============================================================================


class TestIntervalElapsed:
    def test_no_state_means_elapsed(self):
        feed = FeedConfig(name="test", type="rss", url="http://x", interval_minutes=30)
        assert _interval_elapsed(None, feed) is True

    def test_no_last_poll_means_elapsed(self):
        feed = FeedConfig(name="test", type="rss", url="http://x", interval_minutes=30)
        state = db.FeedState(
            user_id="alice", feed_name="test",
            last_poll_at=None, last_item_id=None,
            etag=None, last_modified=None,
            consecutive_errors=0, last_error=None,
        )
        assert _interval_elapsed(state, feed) is True

    def test_recent_poll_not_elapsed(self):
        feed = FeedConfig(name="test", type="rss", url="http://x", interval_minutes=30)
        recent = datetime.now(timezone.utc).isoformat()
        state = db.FeedState(
            user_id="alice", feed_name="test",
            last_poll_at=recent, last_item_id=None,
            etag=None, last_modified=None,
            consecutive_errors=0, last_error=None,
        )
        assert _interval_elapsed(state, feed) is False

    def test_old_poll_elapsed(self):
        feed = FeedConfig(name="test", type="rss", url="http://x", interval_minutes=30)
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        state = db.FeedState(
            user_id="alice", feed_name="test",
            last_poll_at=old, last_item_id=None,
            etag=None, last_modified=None,
            consecutive_errors=0, last_error=None,
        )
        assert _interval_elapsed(state, feed) is True


# ============================================================================
# Poll single feed
# ============================================================================


class TestPollSingleFeed:
    @patch("istota.feed_poller.fetch_rss")
    def test_rss_inserts_items(self, mock_fetch, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        mock_fetch.return_value = (
            [
                {"item_id": "g1", "title": "A", "url": "http://a",
                 "content_text": "text", "content_html": None,
                 "image_url": None, "author": None, "published_at": None},
            ],
            '"etag"',
            None,
        )
        feed = FeedConfig(name="test-rss", type="rss", url="http://rss", interval_minutes=30)
        feeds_config = FeedsConfig(feeds=[feed])

        with db.get_db(db_path) as conn:
            count = _poll_single_feed(conn, "alice", feed, feeds_config)
        assert count == 1

        with db.get_db(db_path) as conn:
            items = db.get_feed_items(conn, "alice")
            assert len(items) == 1
            assert items[0].title == "A"

            state = db.get_feed_state(conn, "alice", "test-rss")
            assert state is not None
            assert state.consecutive_errors == 0
            assert state.etag == '"etag"'

    @patch("istota.feed_poller.fetch_rss")
    def test_rss_error_increments_errors(self, mock_fetch, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        mock_fetch.side_effect = Exception("Network error")
        feed = FeedConfig(name="err-feed", type="rss", url="http://rss", interval_minutes=30)
        feeds_config = FeedsConfig(feeds=[feed])

        with db.get_db(db_path) as conn:
            count = _poll_single_feed(conn, "alice", feed, feeds_config)
        assert count == 0

        with db.get_db(db_path) as conn:
            state = db.get_feed_state(conn, "alice", "err-feed")
            assert state.consecutive_errors == 1
            assert "Network error" in state.last_error

    def test_tumblr_requires_api_key(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        feed = FeedConfig(name="tumblr-feed", type="tumblr", url="blogname", interval_minutes=180)
        feeds_config = FeedsConfig(feeds=[feed], tumblr_api_key="")

        with db.get_db(db_path) as conn:
            count = _poll_single_feed(conn, "alice", feed, feeds_config)
        assert count == 0

    @patch("istota.feed_poller.fetch_tumblr")
    def test_tumblr_single_page(self, mock_fetch, tmp_path):
        """Tumblr feed with fewer than 20 posts fetches one page."""
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        mock_fetch.return_value = [
            {"item_id": "100", "title": "Post", "url": "http://t/100",
             "content_text": "text", "content_html": None,
             "image_url": None, "author": "blog", "published_at": None},
        ]
        feed = FeedConfig(name="tumblr-feed", type="tumblr", url="blogname", interval_minutes=180)
        feeds_config = FeedsConfig(feeds=[feed], tumblr_api_key="test-key")

        with db.get_db(db_path) as conn:
            count = _poll_single_feed(conn, "alice", feed, feeds_config)
        assert count == 1
        # Only one page fetched (returned < 20 items, so second page not attempted)
        mock_fetch.assert_called_once_with("blogname", "test-key", offset=0)

    @patch("istota.feed_poller.fetch_tumblr")
    def test_tumblr_paginates_when_all_new(self, mock_fetch, tmp_path):
        """Tumblr pagination fetches additional pages when all items are new."""
        db_path = tmp_path / "test.db"
        db.init_db(db_path)

        def make_items(start_id, count):
            return [
                {"item_id": str(start_id - i), "title": f"Post {start_id - i}",
                 "url": f"http://t/{start_id - i}",
                 "content_text": "text", "content_html": None,
                 "image_url": None, "author": "blog", "published_at": None}
                for i in range(count)
            ]

        # Page 0: 20 new items, page 1: 5 new items (< 20, stops)
        mock_fetch.side_effect = [
            make_items(120, 20),  # IDs 120..101
            make_items(100, 5),   # IDs 100..96
        ]

        feed = FeedConfig(name="tumblr-feed", type="tumblr", url="blogname", interval_minutes=180)
        feeds_config = FeedsConfig(feeds=[feed], tumblr_api_key="test-key")

        with db.get_db(db_path) as conn:
            count = _poll_single_feed(conn, "alice", feed, feeds_config)
        assert count == 25
        assert mock_fetch.call_count == 2
        mock_fetch.assert_any_call("blogname", "test-key", offset=0)
        mock_fetch.assert_any_call("blogname", "test-key", offset=20)

    @patch("istota.feed_poller.fetch_tumblr")
    def test_tumblr_stops_paginating_when_mostly_dupes(self, mock_fetch, tmp_path):
        """Tumblr pagination stops when most items on a page are duplicates."""
        db_path = tmp_path / "test.db"
        db.init_db(db_path)

        # Pre-insert some items so page 1 will be mostly dupes
        with db.get_db(db_path) as conn:
            for i in range(15):
                db.insert_feed_item(conn, "alice", "tumblr-feed", str(100 - i))

        def make_items(start_id, count):
            return [
                {"item_id": str(start_id - i), "title": f"Post {start_id - i}",
                 "url": f"http://t/{start_id - i}",
                 "content_text": "text", "content_html": None,
                 "image_url": None, "author": "blog", "published_at": None}
                for i in range(count)
            ]

        # Page 0: 20 items, IDs 120..101, all new
        # Page 1: 20 items, IDs 100..81, 15 are dupes (100..86) → <50% new → stop
        mock_fetch.side_effect = [
            make_items(120, 20),  # all new
            make_items(100, 20),  # 15 dupes, 5 new → stops here
        ]

        feed = FeedConfig(name="tumblr-feed", type="tumblr", url="blogname", interval_minutes=180)
        feeds_config = FeedsConfig(feeds=[feed], tumblr_api_key="test-key")

        with db.get_db(db_path) as conn:
            count = _poll_single_feed(conn, "alice", feed, feeds_config)
        assert count == 25  # 20 from page 0 + 5 new from page 1
        assert mock_fetch.call_count == 2  # Stopped after page 1

    @patch("istota.feed_poller.fetch_tumblr")
    def test_tumblr_respects_max_pages_cap(self, mock_fetch, tmp_path):
        """Tumblr pagination respects the max_pages=5 cap."""
        db_path = tmp_path / "test.db"
        db.init_db(db_path)

        call_count = 0
        def make_page(*args, **kwargs):
            nonlocal call_count
            start = 1000 - call_count * 20
            call_count += 1
            return [
                {"item_id": str(start - i), "title": f"Post",
                 "url": f"http://t/{start - i}",
                 "content_text": "text", "content_html": None,
                 "image_url": None, "author": "blog", "published_at": None}
                for i in range(20)
            ]

        mock_fetch.side_effect = make_page

        feed = FeedConfig(name="tumblr-feed", type="tumblr", url="blogname", interval_minutes=180)
        feeds_config = FeedsConfig(feeds=[feed], tumblr_api_key="test-key")

        with db.get_db(db_path) as conn:
            count = _poll_single_feed(conn, "alice", feed, feeds_config)
        assert count == 100  # 5 pages × 20
        assert mock_fetch.call_count == 5


# ============================================================================
# Static page generation
# ============================================================================


class TestStaticPageGeneration:
    def test_build_feed_page_html_contains_items(self):
        items = [
            db.FeedItem(
                id=1, user_id="alice", feed_name="hn",
                item_id="g1", title="Test Article",
                url="https://example.com/1",
                content_text="Some text content",
                content_html=None, image_url=None,
                author="Author", published_at="2026-02-01T12:00:00",
                fetched_at="2026-02-01T12:00:00",
            ),
            db.FeedItem(
                id=2, user_id="alice", feed_name="photos",
                item_id="g2", title="Photo",
                url="https://example.com/2",
                content_text=None, content_html=None,
                image_url="https://example.com/photo.jpg",
                author=None, published_at="2026-02-01T11:00:00",
                fetched_at="2026-02-01T11:00:00",
            ),
        ]
        html = _build_feed_page_html(items, ["hn", "photos"])
        assert "Test Article" in html
        assert "https://example.com/photo.jpg" in html
        assert 'class="card text feed-hn"' in html
        assert 'class="card image feed-photos"' in html
        assert "data-type" in html

    def test_build_feed_page_html_has_css_features(self):
        items = [
            db.FeedItem(
                id=1, user_id="alice", feed_name="hn",
                item_id="g1", title="Test",
                url=None, content_text="Text", content_html=None,
                image_url=None, author=None,
                published_at=None, fetched_at="2026-02-01T12:00:00",
            ),
        ]
        html_content = _build_feed_page_html(items, ["hn"])
        # Check for modern CSS features
        assert "animation-timeline:view()" in html_content
        assert ":has(" in html_content

    def test_build_filter_css(self):
        css = _build_filter_css(["hn", "photos"])
        assert 'data-type="image"' in css
        assert 'data-type="text"' in css

    def test_lightbox_markup_for_images(self):
        items = [
            db.FeedItem(
                id=1, user_id="alice", feed_name="photos",
                item_id="g1", title="Photo",
                url=None, content_text=None, content_html=None,
                image_url="https://example.com/photo.jpg",
                author=None, published_at=None, fetched_at="2026-02-01T12:00:00",
            ),
        ]
        html_content = _build_feed_page_html(items, ["photos"])
        assert 'data-full="https://example.com/photo.jpg"' in html_content
        assert "lightbox" in html_content
        assert "zoom-in" in html_content

    def test_multi_image_gallery(self):
        import json
        urls = ["https://example.com/a.jpg", "https://example.com/b.jpg"]
        items = [
            db.FeedItem(
                id=10, user_id="alice", feed_name="tumblr",
                item_id="m1", title="Photoset",
                url=None, content_text=None, content_html=None,
                image_url=json.dumps(urls),
                author=None, published_at=None, fetched_at="2026-02-01T12:00:00",
            ),
        ]
        html_content = _build_feed_page_html(items, ["tumblr"])
        assert "card-gallery" in html_content
        assert 'data-full="https://example.com/a.jpg"' in html_content
        assert 'data-full="https://example.com/b.jpg"' in html_content
        assert "a.jpg" in html_content
        assert "b.jpg" in html_content

    def test_status_notice_in_page(self):
        items = [
            db.FeedItem(
                id=1, user_id="alice", feed_name="hn",
                item_id="g1", title="Test",
                url=None, content_text="Text", content_html=None,
                image_url=None, author=None,
                published_at=None, fetched_at="2026-02-01T12:00:00",
            ),
        ]
        html_content = _build_feed_page_html(
            items, ["hn"], generated_at="Feb 09, 14:30", new_item_count=5,
        )
        assert "status-notice" in html_content
        assert "Feb 09, 14:30" in html_content
        assert "+5 new" in html_content
        assert "1 items" in html_content

    def test_status_notice_zero_new_items(self):
        items = [
            db.FeedItem(
                id=1, user_id="alice", feed_name="hn",
                item_id="g1", title="Test",
                url=None, content_text="Text", content_html=None,
                image_url=None, author=None,
                published_at=None, fetched_at="2026-02-01T12:00:00",
            ),
        ]
        html_content = _build_feed_page_html(
            items, ["hn"], generated_at="Feb 09, 14:30", new_item_count=0,
        )
        assert "+0 new" not in html_content
        assert "Feb 09, 14:30" in html_content

    def test_generate_static_feed_page(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        mount_path = tmp_path / "mount"
        mount_path.mkdir()

        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount_path,
            site=SiteConfig(enabled=True, hostname="test.com"),
        )

        with db.get_db(db_path) as conn:
            db.insert_feed_item(conn, "alice", "hn", "id-1",
                                title="Test", published_at="2026-02-01T00:00:00")

        result = generate_static_feed_page(config, "alice")
        assert result is True
        output = mount_path / "Users" / "alice" / "istota" / "html" / "feeds" / "index.html"
        assert output.exists()
        content = output.read_text()
        assert "Test" in content
        assert "<!doctype html>" in content

    def test_generate_static_feed_page_uses_user_timezone(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        mount_path = tmp_path / "mount"
        mount_path.mkdir()

        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount_path,
            site=SiteConfig(enabled=True, hostname="test.com"),
            users={"alice": UserConfig(timezone="US/Eastern")},
        )

        with db.get_db(db_path) as conn:
            db.insert_feed_item(conn, "alice", "hn", "id-1",
                                title="Test", published_at="2026-02-01T00:00:00")

        from unittest.mock import patch as _patch
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Fix time to 2026-02-09 20:00 UTC = 15:00 Eastern
        fixed_utc = datetime(2026, 2, 9, 20, 0, tzinfo=ZoneInfo("UTC"))
        with _patch("istota.feed_poller.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_utc.astimezone(ZoneInfo("US/Eastern"))
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = generate_static_feed_page(config, "alice")

        assert result is True
        output = mount_path / "Users" / "alice" / "istota" / "html" / "feeds" / "index.html"
        content = output.read_text()
        assert "15:00" in content

    def test_generate_static_feed_page_no_items(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        mount_path = tmp_path / "mount"
        mount_path.mkdir()
        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount_path,
            site=SiteConfig(enabled=True, hostname="test.com"),
        )
        result = generate_static_feed_page(config, "alice")
        assert result is False


# ============================================================================
# check_feeds orchestration
# ============================================================================


class TestCheckFeeds:
    def test_skips_when_site_disabled(self, tmp_path):
        config = Config(site=SiteConfig(enabled=False))
        assert check_feeds(config) == 0

    def test_skips_when_no_mount(self, tmp_path):
        config = Config(
            site=SiteConfig(enabled=True),
            nextcloud_mount_path=None,
        )
        assert check_feeds(config) == 0

    @patch("istota.feed_poller._poll_single_feed")
    @patch("istota.feed_poller.generate_static_feed_page")
    def test_polls_feeds_for_enabled_users(self, mock_gen, mock_poll, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        mount = tmp_path / "mount"
        mount.mkdir()

        # Create FEEDS.md
        workspace = mount / "Users" / "alice" / "istota" / "config"
        workspace.mkdir(parents=True)
        (workspace / "FEEDS.md").write_text(SAMPLE_FEEDS_MD)

        site_dir = tmp_path / "site"
        site_dir.mkdir()

        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            site=SiteConfig(enabled=True, hostname="test.com", base_path=str(site_dir)),
            users={"alice": UserConfig(site_enabled=True)},
        )

        mock_poll.return_value = 1  # 1 new item per feed
        result = check_feeds(config)
        assert result == 3  # 3 feeds * 1 item each
        mock_gen.assert_called_once()


# ============================================================================
# Helper functions
# ============================================================================


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello") == "hello"

    def test_long_text_truncated(self):
        long = "word " * 100
        result = _truncate(long, max_len=50)
        assert len(result) <= 55  # some slack for word boundary + ellipsis
        assert result.endswith("…")

    def test_none_returns_empty(self):
        assert _truncate(None) == ""

    def test_empty_returns_empty(self):
        assert _truncate("") == ""


class TestBuildStatusText:
    def test_all_parts(self):
        result = _build_status_text("Feb 09, 14:30", 5, 100)
        assert result == "Feb 09, 14:30 · +5 new · 100 items"

    def test_zero_new_items_omitted(self):
        result = _build_status_text("Feb 09, 14:30", 0, 50)
        assert result == "Feb 09, 14:30 · 50 items"
        assert "new" not in result

    def test_no_timestamp(self):
        result = _build_status_text("", 3, 10)
        assert result == "+3 new · 10 items"

    def test_items_only(self):
        result = _build_status_text("", 0, 42)
        assert result == "42 items"
