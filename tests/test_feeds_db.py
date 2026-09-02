"""Tests for the native feeds SQLite layer."""

import sqlite3

from datetime import datetime, timedelta, timezone

import pytest

from istota.feeds import db as feeds_db
from istota.feeds.models import (
    DEFAULT_ENTRY_RETENTION_DAYS,
    DEFAULT_MAX_ENTRIES_PER_FEED,
    MIN_ENTRIES_PER_FEED,
    POLL_CLAIM_SECONDS,
    EntryRecord,
    FeedsContext,
    FetchedItem,
    FetchResult,
)
from istota.feeds import retention


class TestInitDb:
    def test_creates_tables(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        assert path.exists()
        with feeds_db.connect(path) as conn:
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"feed_categories", "feeds", "feed_entries", "schema_meta"} <= tables

    def test_idempotent(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        feeds_db.init_db(path)  # second call must not raise

    def test_records_schema_version(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
        assert row["value"] == str(feeds_db.SCHEMA_VERSION)
        assert int(row["value"]) >= 2

    def test_v1_to_v2_migration_idempotent(self, tmp_path):
        """Simulate an old DB (v1 schema) and confirm the v2 migration runs."""
        import sqlite3

        path = tmp_path / "feeds.db"
        # Hand-build a v1 schema: feed_entries without `starred` / `starred_at`.
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE feed_categories (
                    id INTEGER PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL
                );
                CREATE TABLE feeds (
                    id INTEGER PRIMARY KEY,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT, site_url TEXT,
                    category_id INTEGER REFERENCES feed_categories(id),
                    source_type TEXT NOT NULL,
                    etag TEXT, last_modified TEXT, last_fetched_at TEXT,
                    last_error TEXT,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    poll_interval_minutes INTEGER NOT NULL DEFAULT 30,
                    next_poll_at TEXT
                );
                CREATE TABLE feed_entries (
                    id INTEGER PRIMARY KEY,
                    feed_id INTEGER NOT NULL REFERENCES feeds(id),
                    guid TEXT NOT NULL,
                    title TEXT, url TEXT, author TEXT,
                    content_html TEXT, content_text TEXT,
                    image_urls TEXT, published_at TEXT,
                    fetched_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unread',
                    UNIQUE(feed_id, guid)
                );
                CREATE TABLE schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                INSERT INTO schema_meta(key, value) VALUES ('version', '1');
                """
            )
            conn.commit()
        finally:
            conn.close()

        feeds_db.init_db(path)  # should add starred / starred_at + bump version
        with feeds_db.connect(path) as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
            assert "starred" in cols
            assert "starred_at" in cols
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
            assert row["value"] == str(feeds_db.SCHEMA_VERSION)

        feeds_db.init_db(path)  # second run is a no-op

    def test_v3_to_v4_adds_embed_url_preserving_entries(self, tmp_path):
        """A v3 DB gains ``feed_entries.embed_url`` without losing rows."""
        import sqlite3

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="arena:c", title="C", site_url=None,
                source_type="arena", category_id=None,
                poll_interval_minutes=60,
            )
            feeds_db.insert_entries(conn, feed_id, [
                EntryRecord(
                    id=0, feed_id=feed_id, guid="old", title="Old", url=None,
                    author=None, content_html=None, content_text=None,
                    image_urls=[], published_at=None,
                    fetched_at="2026-05-01T00:00:00+00:00",
                ),
            ])
            conn.commit()

        # Rewind to v3: drop the column and the recorded version.
        conn = sqlite3.connect(path)
        try:
            conn.execute("ALTER TABLE feed_entries DROP COLUMN embed_url")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version','3')"
            )
            conn.commit()
        finally:
            conn.close()

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
            entries = feeds_db.list_entries(conn)
        assert "embed_url" in cols
        assert [e.guid for e in entries] == ["old"]
        assert entries[0].embed_url is None

    def test_v4_to_v5_adds_file_url_preserving_entries(self, tmp_path):
        """A v4 DB gains ``feed_entries.file_url`` without losing rows."""
        import sqlite3

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="arena:c", title="C", site_url=None,
                source_type="arena", category_id=None,
                poll_interval_minutes=60,
            )
            feeds_db.insert_entries(conn, feed_id, [
                EntryRecord(
                    id=0, feed_id=feed_id, guid="old", title="Old", url=None,
                    author=None, content_html=None, content_text=None,
                    image_urls=[], embed_url="https://youtu.be/keep",
                    published_at=None,
                    fetched_at="2026-05-01T00:00:00+00:00",
                ),
            ])
            conn.commit()

        # Rewind to v4: drop the column and the recorded version.
        conn = sqlite3.connect(path)
        try:
            conn.execute("ALTER TABLE feed_entries DROP COLUMN file_url")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version','4')"
            )
            conn.commit()
        finally:
            conn.close()

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
            entries = feeds_db.list_entries(conn)
        assert "file_url" in cols
        assert [e.guid for e in entries] == ["old"]
        assert entries[0].file_url is None
        # The v4 column and its data must survive the v5 migration.
        assert entries[0].embed_url == "https://youtu.be/keep"

    def test_v5_to_v6_adds_last_throttled_at_preserving_feeds(self, tmp_path):
        """A v5 DB gains ``feeds.last_throttled_at`` without losing rows.

        The column is where a 429 is recorded now that it is no longer written
        as a feed error (ISSUE-347). Existing rows keep NULL, which reads as
        "never throttled" and renders exactly as before.
        """
        import sqlite3

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feeds_db.upsert_feed(
                conn, url="arena:c", title="C", site_url="https://are.na/c",
                source_type="arena", category_id=None,
                poll_interval_minutes=60,
            )
            conn.execute(
                "UPDATE feeds SET last_error = ?, error_count = 2",
                ("HTTP 500 fetching feed",),
            )
            conn.commit()

        # Rewind to v5: drop the column and the recorded version.
        conn = sqlite3.connect(path)
        try:
            conn.execute("ALTER TABLE feeds DROP COLUMN last_throttled_at")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version','5')"
            )
            conn.commit()
        finally:
            conn.close()

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
            feeds = feeds_db.list_feeds(conn)
        assert "last_throttled_at" in cols
        assert [f.url for f in feeds] == ["arena:c"]
        assert feeds[0].last_throttled_at is None
        # The v5 state must survive the v6 migration.
        assert feeds[0].last_error == "HTTP 500 fetching feed"
        assert feeds[0].error_count == 2
        assert feeds[0].site_url == "https://are.na/c"

    def test_v6_to_v7_adds_media_columns_and_lifts_stored_videos(self, tmp_path):
        """A v6 DB gains the media columns, and the mp4s already sitting in
        ``image_urls`` move into them (ISSUE-356).

        Existing rows are the whole point of the backfill: ``insert_entries``
        matches on ``(feed_id, guid)`` and never overwrites a stored value
        with an empty one, so a re-poll of the same entry leaves the broken
        image URL exactly where it is.
        """
        import sqlite3

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="https://example.town/@a.rss", title="M", site_url=None,
                source_type="rss", category_id=None, poll_interval_minutes=60,
            )
            feeds_db.insert_entries(conn, feed_id, [
                EntryRecord(
                    id=0, feed_id=feed_id, guid="vid", title="Clip", url=None,
                    author=None, content_html=None, content_text=None,
                    image_urls=["https://assets.example.town/media/1/clip.mp4"],
                    published_at=None, fetched_at="2026-05-01T00:00:00+00:00",
                ),
                EntryRecord(
                    id=0, feed_id=feed_id, guid="mixed", title="Both", url=None,
                    author=None, content_html=None, content_text=None,
                    image_urls=[
                        "https://assets.example.town/media/2/still.jpg",
                        "https://assets.example.town/media/3/clip.mp4",
                    ],
                    published_at=None, fetched_at="2026-05-01T00:00:00+00:00",
                ),
                EntryRecord(
                    id=0, feed_id=feed_id, guid="pic", title="Pic", url=None,
                    author=None, content_html=None, content_text=None,
                    image_urls=["https://assets.example.town/media/4/photo.jpg"],
                    embed_url="https://youtu.be/keep",
                    published_at=None, fetched_at="2026-05-01T00:00:00+00:00",
                ),
            ])
            conn.commit()

        # Rewind to v6: drop the columns and the recorded version.
        conn = sqlite3.connect(path)
        try:
            conn.execute("ALTER TABLE feed_entries DROP COLUMN media_url")
            conn.execute("ALTER TABLE feed_entries DROP COLUMN media_type")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version','6')"
            )
            conn.commit()
        finally:
            conn.close()

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
            by_guid = {e.guid: e for e in feeds_db.list_entries(conn)}
            keys = {
                r["entry_id"]
                for r in conn.execute("SELECT entry_id FROM entry_images")
            }
            ids = {
                r["guid"]: r["id"]
                for r in conn.execute("SELECT id, guid FROM feed_entries")
            }

        assert {"media_url", "media_type"} <= cols
        assert set(by_guid) == {"vid", "mixed", "pic"}

        # A video-only entry loses its fake hero and gains a real player.
        assert by_guid["vid"].image_urls == []
        assert by_guid["vid"].media_url == "https://assets.example.town/media/1/clip.mp4"
        assert by_guid["vid"].media_type == "video/mp4"

        # A mixed entry keeps the still and lifts only the clip.
        assert by_guid["mixed"].image_urls == [
            "https://assets.example.town/media/2/still.jpg"
        ]
        assert by_guid["mixed"].media_url == "https://assets.example.town/media/3/clip.mp4"

        # An entry with no video is untouched, v6 data included.
        assert by_guid["pic"].image_urls == [
            "https://assets.example.town/media/4/photo.jpg"
        ]
        assert by_guid["pic"].media_url is None
        assert by_guid["pic"].embed_url == "https://youtu.be/keep"

        # entry_images is derived from image_urls, so a lifted video must
        # stop suppressing later posts through the dedupe index.
        assert ids["vid"] not in keys

    def test_v6_to_v7_refuses_to_promote_a_non_http_url(self, tmp_path):
        """A v6 row was written before anything checked a scheme (ISSUE-356).

        ``media_type_for_url`` reads the path, so ``javascript:x.mp4`` parses
        as a video. The poller refuses that on the way in and nothing
        downstream re-checks, so the migration has to apply the same bar — a
        promoted `javascript:` URL would be served as media by the API and the
        skill CLI alike.
        """
        import sqlite3

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="https://example.com/f.rss", title="F", site_url=None,
                source_type="rss", category_id=None, poll_interval_minutes=60,
            )
            feeds_db.insert_entries(conn, feed_id, [
                EntryRecord(
                    id=0, feed_id=feed_id, guid="bad", title="Bad", url=None,
                    author=None, content_html=None, content_text=None,
                    image_urls=["javascript:alert(1)//x.mp4"],
                    published_at=None, fetched_at="2026-05-01T00:00:00+00:00",
                ),
            ])
            conn.commit()

        conn = sqlite3.connect(path)
        try:
            conn.execute("ALTER TABLE feed_entries DROP COLUMN media_url")
            conn.execute("ALTER TABLE feed_entries DROP COLUMN media_type")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version','6')"
            )
            conn.commit()
        finally:
            conn.close()

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            entry = feeds_db.list_entries(conn)[0]
        assert entry.media_url is None
        # It stays where it was, rendering as the <img> that never loads —
        # harmless, and not something to launder into a media field.
        assert entry.image_urls == ["javascript:alert(1)//x.mp4"]

    def test_v6_to_v7_keeps_media_already_stored_when_it_runs_again(self, tmp_path):
        """The migration can see a row that already carries media.

        Reachable when an older binary has restamped ``schema_meta.version``
        back to 6 (``init_db`` writes its own ``SCHEMA_VERSION``
        unconditionally) and a newer one then re-runs. The stored attachment
        wins; only the image list is corrected.
        """
        import sqlite3

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="https://example.com/f.rss", title="F", site_url=None,
                source_type="rss", category_id=None, poll_interval_minutes=60,
            )
            feeds_db.insert_entries(conn, feed_id, [
                EntryRecord(
                    id=0, feed_id=feed_id, guid="both", title="Both", url=None,
                    author=None, content_html=None, content_text=None,
                    image_urls=["https://example.com/other.mp4"],
                    media_url="https://example.com/real.mp4",
                    media_type="video/mp4",
                    published_at=None, fetched_at="2026-05-01T00:00:00+00:00",
                ),
            ])
            conn.commit()

        # Rewind the version only — the columns and their data stay, which is
        # exactly the state a downgrade-then-upgrade leaves behind.
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version','6')"
            )
            conn.commit()
        finally:
            conn.close()

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            entry = feeds_db.list_entries(conn)[0]
        assert entry.media_url == "https://example.com/real.mp4"
        assert entry.media_type == "video/mp4"
        assert entry.image_urls == []

    def test_v6_to_v7_is_a_no_op_the_second_time(self, tmp_path):
        """Re-running over an already-lifted DB changes nothing.

        The version is rewound and the columns are left in place, which is the
        state a downgrade-then-upgrade actually leaves — an older binary
        restamps ``schema_meta.version`` but cannot drop a column it does not
        know about. The lifted row's ``image_urls`` is NULL by then, so the
        pass does not even visit it.
        """
        import sqlite3

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="https://example.com/f.rss", title="F", site_url=None,
                source_type="rss", category_id=None, poll_interval_minutes=60,
            )
            feeds_db.insert_entries(conn, feed_id, [
                EntryRecord(
                    id=0, feed_id=feed_id, guid="vid", title="Clip", url=None,
                    author=None, content_html=None, content_text=None,
                    image_urls=["https://example.com/a.mp4"],
                    published_at=None, fetched_at="2026-05-01T00:00:00+00:00",
                ),
            ])
            conn.commit()

        # First pass: from a genuine v6 shape, columns absent.
        conn = sqlite3.connect(path)
        try:
            conn.execute("ALTER TABLE feed_entries DROP COLUMN media_url")
            conn.execute("ALTER TABLE feed_entries DROP COLUMN media_type")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES ('version','6')"
            )
            conn.commit()
        finally:
            conn.close()
        feeds_db.init_db(path)

        def _read():
            with feeds_db.connect(path) as c:
                e = feeds_db.list_entries(c)[0]
            return (e.image_urls, e.media_url, e.media_type)

        first = _read()
        assert first == ([], "https://example.com/a.mp4", "video/mp4")

        # Second pass: version rewound, columns and data intact.
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES ('version','6')"
            )
            conn.commit()
        finally:
            conn.close()
        feeds_db.init_db(path)

        assert _read() == first

    def test_v6_to_v7_keeps_only_the_first_of_several_videos(self, tmp_path):
        """Matching the poller, which stores one attachment per entry.

        The extras leave ``image_urls`` and are stored nowhere; the migration
        counts them so the loss is in the log rather than silent.
        """
        import sqlite3

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="https://example.com/f.rss", title="F", site_url=None,
                source_type="rss", category_id=None, poll_interval_minutes=60,
            )
            feeds_db.insert_entries(conn, feed_id, [
                EntryRecord(
                    id=0, feed_id=feed_id, guid="two", title="Two", url=None,
                    author=None, content_html=None, content_text=None,
                    image_urls=[
                        "https://example.com/one.mp4",
                        "https://example.com/two.mp4",
                    ],
                    published_at=None, fetched_at="2026-05-01T00:00:00+00:00",
                ),
            ])
            conn.commit()

        conn = sqlite3.connect(path)
        try:
            conn.execute("ALTER TABLE feed_entries DROP COLUMN media_url")
            conn.execute("ALTER TABLE feed_entries DROP COLUMN media_type")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version','6')"
            )
            conn.commit()
        finally:
            conn.close()

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            entry = feeds_db.list_entries(conn)[0]
        assert entry.media_url == "https://example.com/one.mp4"
        assert entry.image_urls == []

    def test_v2_to_v3_backfills_the_image_key_index(self, tmp_path):
        """A v2 DB gains ``entry_images``, backfilled from stored entries."""
        import json
        import sqlite3

        from istota.feeds.sanitize import image_identity

        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)  # current schema…
        with feeds_db.connect(path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="tumblr:a", title="A", site_url=None,
                source_type="tumblr", category_id=None,
                poll_interval_minutes=60,
            )
            conn.commit()

        # …then rewind to v2: drop the index table and the version marker so
        # init_db replays the migration over pre-existing entry rows.
        url = "https://64.media.tumblr.com/aaa/bbb-01/s500x750/hash.jpg"
        conn = sqlite3.connect(path)
        try:
            conn.execute("DROP TABLE IF EXISTS entry_images")
            conn.execute(
                "INSERT INTO feed_entries(feed_id, guid, image_urls, "
                "published_at, fetched_at, status) VALUES (?,?,?,?,?,?)",
                (
                    feed_id, "p1", json.dumps([url, url]),
                    "2026-07-16T10:00:00+00:00",
                    "2026-07-16T11:00:00+00:00", "unread",
                ),
            )
            conn.execute(
                "INSERT INTO feed_entries(feed_id, guid, image_urls, "
                "published_at, fetched_at, status) VALUES (?,?,?,?,?,?)",
                (feed_id, "p2", None, None, "2026-07-16T11:00:00+00:00", "unread"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version','2')"
            )
            conn.commit()
        finally:
            conn.close()

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            rows = conn.execute(
                "SELECT image_key, seen_ts FROM entry_images"
            ).fetchall()
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()["value"]
        # The duplicate URL inside one entry collapses to a single key row,
        # and the image-less entry contributes nothing.
        assert [r["image_key"] for r in rows] == [image_identity(url)]
        assert rows[0]["seen_ts"] > 0
        assert version == str(feeds_db.SCHEMA_VERSION)

        feeds_db.init_db(path)  # replaying is a no-op


class TestCategories:
    def test_upsert_and_lookup(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            cat_id = feeds_db.upsert_category(conn, "blogs", "Blogs")
            conn.commit()
            assert cat_id > 0
            cat = feeds_db.get_category_by_slug(conn, "blogs")
            assert cat is not None
            assert cat.title == "Blogs"

    def test_upsert_updates_title(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            cat_id1 = feeds_db.upsert_category(conn, "blogs", "Blogs")
            cat_id2 = feeds_db.upsert_category(conn, "blogs", "Personal Blogs")
            conn.commit()
        assert cat_id1 == cat_id2
        with feeds_db.connect(path) as conn:
            cat = feeds_db.get_category_by_slug(conn, "blogs")
        assert cat.title == "Personal Blogs"


class TestFeedsTable:
    def test_upsert_and_list(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            cat_id = feeds_db.upsert_category(conn, "blogs", "Blogs")
            feeds_db.upsert_feed(
                conn,
                url="https://example.com/feed.xml",
                title="Example",
                site_url="https://example.com",
                source_type="rss",
                category_id=cat_id,
                poll_interval_minutes=30,
            )
            feeds_db.upsert_feed(
                conn,
                url="tumblr:nemfrog",
                title=None,
                site_url=None,
                source_type="tumblr",
                category_id=cat_id,
                poll_interval_minutes=60,
            )
            conn.commit()
            feeds = feeds_db.list_feeds(conn)
        urls = sorted(f.url for f in feeds)
        assert urls == ["https://example.com/feed.xml", "tumblr:nemfrog"]

    def test_due_for_poll_picks_unfetched_and_overdue(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        past = (now - timedelta(minutes=5)).isoformat()
        future = (now + timedelta(minutes=30)).isoformat()
        with feeds_db.connect(path) as conn:
            feeds_db.upsert_feed(
                conn, url="a", title=None, site_url=None,
                source_type="rss", category_id=None,
                poll_interval_minutes=30,
            )
            feeds_db.upsert_feed(
                conn, url="b", title=None, site_url=None,
                source_type="rss", category_id=None,
                poll_interval_minutes=30,
            )
            feeds_db.upsert_feed(
                conn, url="c", title=None, site_url=None,
                source_type="rss", category_id=None,
                poll_interval_minutes=30,
            )
            conn.commit()
            # b: in the past → due
            conn.execute(
                "UPDATE feeds SET next_poll_at = ? WHERE url = ?", (past, "b"),
            )
            # c: in the future → not due
            conn.execute(
                "UPDATE feeds SET next_poll_at = ? WHERE url = ?", (future, "c"),
            )
            conn.commit()
            due = feeds_db.feeds_due_for_poll(conn, now=now)
        urls = {f.url for f in due}
        assert urls == {"a", "b"}  # a has NULL next_poll_at, b is overdue


class TestEntries:
    def _seed_feed(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="x", title="X", site_url=None,
                source_type="rss", category_id=None,
                poll_interval_minutes=30,
            )
            conn.commit()
        return path, feed_id

    def test_insert_and_dedupe_by_guid(self, tmp_path):
        path, feed_id = self._seed_feed(tmp_path)
        items = [
            EntryRecord(
                id=0, feed_id=feed_id, guid="a", title="A", url=None,
                author=None, content_html=None, content_text=None,
                image_urls=["http://i/1.jpg"],
                published_at="2026-05-01T00:00:00+00:00",
                fetched_at="2026-05-01T00:00:00+00:00",
            ),
            EntryRecord(
                id=0, feed_id=feed_id, guid="a", title="A again", url=None,
                author=None, content_html=None, content_text=None,
                image_urls=[],
                published_at="2026-05-01T00:00:00+00:00",
                fetched_at="2026-05-01T00:00:00+00:00",
            ),
        ]
        with feeds_db.connect(path) as conn:
            n = feeds_db.insert_entries(conn, feed_id, items)
            conn.commit()
            entries = feeds_db.list_entries(conn)
        assert n == 1
        assert len(entries) == 1
        assert entries[0].image_urls == ["http://i/1.jpg"]

    def test_embed_url_round_trips(self, tmp_path):
        """A playable-media URL survives storage, for the reader's player."""
        path, feed_id = self._seed_feed(tmp_path)
        item = EntryRecord(
            id=0, feed_id=feed_id, guid="v", title="Vid", url=None,
            author=None, content_html=None, content_text=None,
            image_urls=["http://i/thumb.jpg"],
            embed_url="https://www.youtube.com/watch?v=abc",
            published_at="2026-05-01T00:00:00+00:00",
            fetched_at="2026-05-01T00:00:00+00:00",
        )
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [item])
            conn.commit()
            entries = feeds_db.list_entries(conn)
        assert entries[0].embed_url == "https://www.youtube.com/watch?v=abc"

    def test_embed_url_defaults_to_none(self, tmp_path):
        path, feed_id = self._seed_feed(tmp_path)
        item = EntryRecord(
            id=0, feed_id=feed_id, guid="p", title="Plain", url=None,
            author=None, content_html=None, content_text=None,
            image_urls=[],
            published_at="2026-05-01T00:00:00+00:00",
            fetched_at="2026-05-01T00:00:00+00:00",
        )
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [item])
            conn.commit()
            entries = feeds_db.list_entries(conn)
        assert entries[0].embed_url is None

    def test_file_url_round_trips(self, tmp_path):
        """An attached document survives storage, so the card can open it."""
        path, feed_id = self._seed_feed(tmp_path)
        item = EntryRecord(
            id=0, feed_id=feed_id, guid="d", title="Essay", url=None,
            author=None, content_html=None, content_text=None,
            image_urls=["http://i/cover.png"],
            file_url="https://attachments.are.na/1/essay.pdf",
            published_at="2026-05-01T00:00:00+00:00",
            fetched_at="2026-05-01T00:00:00+00:00",
        )
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [item])
            conn.commit()
            entries = feeds_db.list_entries(conn)
        assert entries[0].file_url == "https://attachments.are.na/1/essay.pdf"

    def test_embed_and_file_urls_are_independent(self, tmp_path):
        """A video sets one, a PDF the other; neither leaks into the other."""
        path, feed_id = self._seed_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [
                EntryRecord(
                    id=0, feed_id=feed_id, guid="v", title="V", url=None,
                    author=None, content_html=None, content_text=None,
                    image_urls=[], embed_url="https://youtu.be/abc",
                    published_at=None, fetched_at="2026-05-01T00:00:00+00:00",
                ),
                EntryRecord(
                    id=0, feed_id=feed_id, guid="p", title="P", url=None,
                    author=None, content_html=None, content_text=None,
                    image_urls=[], file_url="https://a.are.na/1.pdf",
                    published_at=None, fetched_at="2026-05-01T00:00:00+00:00",
                ),
            ])
            conn.commit()
            by_guid = {e.guid: e for e in feeds_db.list_entries(conn)}
        assert by_guid["v"].embed_url == "https://youtu.be/abc"
        assert by_guid["v"].file_url is None
        assert by_guid["p"].file_url == "https://a.are.na/1.pdf"
        assert by_guid["p"].embed_url is None

    def test_count_and_filter_by_status(self, tmp_path):
        path, feed_id = self._seed_feed(tmp_path)
        items = [
            EntryRecord(
                id=0, feed_id=feed_id, guid=str(i), title=None, url=None,
                author=None, content_html=None, content_text=None,
                image_urls=[],
                published_at=f"2026-05-01T00:00:0{i}+00:00",
                fetched_at="2026-05-01T00:00:00+00:00",
            )
            for i in range(3)
        ]
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, items)
            conn.commit()
            assert feeds_db.count_entries(conn) == 3
            unread = feeds_db.list_entries(conn, status="unread")
        assert len(unread) == 3

    def test_update_status(self, tmp_path):
        path, feed_id = self._seed_feed(tmp_path)
        items = [
            EntryRecord(
                id=0, feed_id=feed_id, guid="a", title=None, url=None,
                author=None, content_html=None, content_text=None,
                image_urls=[],
                published_at="2026-05-01T00:00:00+00:00",
                fetched_at="2026-05-01T00:00:00+00:00",
            ),
        ]
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, items)
            conn.commit()
            entry_id = feeds_db.list_entries(conn)[0].id
            feeds_db.update_entry_status(conn, [entry_id], "read")
            conn.commit()
            after = feeds_db.list_entries(conn)
        assert after[0].status == "read"


class TestEntryRefresh:
    """Re-polling an existing entry refreshes its *content*, not its state.

    The Are.na v3 upgrade rewrote what a block maps to (real HTML bodies,
    embed_url, file_url), but every already-stored block kept the v2 shape
    forever: the poller re-fetched them each cycle and the insert discarded
    the richer row on the (feed_id, guid) conflict, so video, PDF and text
    blocks stayed blank. Content columns now follow the feed; user state
    (read/starred) and the original fetch time do not.
    """

    def _seed_feed(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="arena:c", title="C", site_url=None,
                source_type="arena", category_id=None,
                poll_interval_minutes=60,
            )
            conn.commit()
        return path, feed_id

    def _stored(self, feed_id, **over):
        base = dict(
            id=0, feed_id=feed_id, guid="b1", title=None, url=None,
            author=None, content_html=None, content_text="raw text",
            image_urls=[], published_at="2026-05-01T00:00:00+00:00",
            fetched_at="2026-05-01T00:00:00+00:00",
        )
        base.update(over)
        return EntryRecord(**base)

    def test_repoll_fills_in_content_the_old_provider_missed(self, tmp_path):
        path, feed_id = self._seed_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [self._stored(feed_id)])
            conn.commit()
            feeds_db.insert_entries(conn, feed_id, [self._stored(
                feed_id,
                title="A manifesto",
                url="https://www.are.na/block/1",
                author="curator",
                content_html="<p>Body</p>",
                content_text="Body",
                image_urls=["http://i/thumb.jpg"],
                embed_url="https://www.youtube.com/watch?v=abc",
                fetched_at="2026-07-27T00:00:00+00:00",
            )])
            conn.commit()
            entries = feeds_db.list_entries(conn)
        assert len(entries) == 1
        e = entries[0]
        assert e.content_html == "<p>Body</p>"
        assert e.content_text == "Body"
        assert e.title == "A manifesto"
        assert e.url == "https://www.are.na/block/1"
        assert e.author == "curator"
        assert e.image_urls == ["http://i/thumb.jpg"]
        assert e.embed_url == "https://www.youtube.com/watch?v=abc"

    def test_file_url_reaches_an_already_stored_attachment(self, tmp_path):
        path, feed_id = self._seed_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [self._stored(
                feed_id, image_urls=["http://i/cover.png"],
            )])
            conn.commit()
            feeds_db.insert_entries(conn, feed_id, [self._stored(
                feed_id,
                image_urls=["http://i/cover.png"],
                file_url="https://attachments.are.na/1/essay.pdf",
            )])
            conn.commit()
            entries = feeds_db.list_entries(conn)
        assert entries[0].file_url == "https://attachments.are.na/1/essay.pdf"

    def test_refresh_is_not_counted_as_a_new_entry(self, tmp_path):
        """The return value drives the poller's "N new" log and notifications."""
        path, feed_id = self._seed_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            first = feeds_db.insert_entries(conn, feed_id, [self._stored(feed_id)])
            conn.commit()
            again = feeds_db.insert_entries(conn, feed_id, [self._stored(
                feed_id, content_html="<p>Body</p>",
            )])
            conn.commit()
        assert first == 1
        assert again == 0

    def test_read_and_starred_survive_a_refresh(self, tmp_path):
        """Refreshing content must never resurrect an entry as unread."""
        path, feed_id = self._seed_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [self._stored(feed_id)])
            conn.commit()
            entry_id = feeds_db.list_entries(conn)[0].id
            feeds_db.update_entry_status(conn, [entry_id], "read")
            feeds_db.update_entry_starred(conn, [entry_id], True)
            conn.commit()
            starred_at = conn.execute(
                "SELECT starred_at FROM feed_entries WHERE id = ?", (entry_id,),
            ).fetchone()[0]

            feeds_db.insert_entries(conn, feed_id, [self._stored(
                feed_id, content_html="<p>Body</p>", status="unread",
            )])
            conn.commit()
            row = conn.execute(
                "SELECT status, starred, starred_at, fetched_at "
                "FROM feed_entries WHERE id = ?", (entry_id,),
            ).fetchone()
        assert row["status"] == "read"
        assert row["starred"] == 1
        assert row["starred_at"] == starred_at
        # Original fetch time stands, so the "recently added" ordering and the
        # image-dedup look-back window don't jump on a repair pass.
        assert row["fetched_at"] == "2026-05-01T00:00:00+00:00"

    def test_a_field_the_feed_dropped_does_not_erase_what_we_hold(self, tmp_path):
        """A thinner later fetch degrades the card; keep the richer row."""
        path, feed_id = self._seed_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [self._stored(
                feed_id,
                title="Kept",
                content_html="<p>Kept</p>",
                image_urls=["http://i/1.jpg"],
                embed_url="https://youtu.be/abc",
            )])
            conn.commit()
            feeds_db.insert_entries(conn, feed_id, [self._stored(
                feed_id, title=None, content_html="", image_urls=[],
                content_text=None, embed_url=None,
            )])
            conn.commit()
            entries = feeds_db.list_entries(conn)
        e = entries[0]
        assert e.title == "Kept"
        assert e.content_html == "<p>Kept</p>"
        assert e.content_text == "raw text"
        assert e.image_urls == ["http://i/1.jpg"]
        assert e.embed_url == "https://youtu.be/abc"

    def test_new_images_are_indexed_for_dedup(self, tmp_path):
        """entry_images is derived from image_urls; a refresh must re-derive it."""
        path, feed_id = self._seed_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [self._stored(feed_id)])
            conn.commit()
            entry_id = feeds_db.list_entries(conn)[0].id
            assert conn.execute(
                "SELECT COUNT(*) FROM entry_images WHERE entry_id = ?", (entry_id,),
            ).fetchone()[0] == 0

            feeds_db.insert_entries(conn, feed_id, [self._stored(
                feed_id, image_urls=["http://i/thumb.jpg"],
            )])
            conn.commit()
            keys = conn.execute(
                "SELECT COUNT(*) FROM entry_images WHERE entry_id = ?", (entry_id,),
            ).fetchone()[0]
        assert keys == 1


class TestStarring:
    def _seed(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            cat_id = feeds_db.upsert_category(conn, "blogs", "Blogs")
            feed_a = feeds_db.upsert_feed(
                conn, url="a", title=None, site_url=None,
                source_type="rss", category_id=cat_id,
                poll_interval_minutes=30,
            )
            feed_b = feeds_db.upsert_feed(
                conn, url="b", title=None, site_url=None,
                source_type="rss", category_id=None,
                poll_interval_minutes=30,
            )
            for feed_id, guid in [(feed_a, "a1"), (feed_a, "a2"), (feed_b, "b1")]:
                feeds_db.insert_entries(conn, feed_id, [
                    EntryRecord(
                        id=0, feed_id=feed_id, guid=guid, title=guid,
                        url=None, author=None, content_html=None,
                        content_text=None, image_urls=[],
                        published_at="2026-05-01T00:00:00+00:00",
                        fetched_at="2026-05-01T00:00:00+00:00",
                    ),
                ])
            conn.commit()
        return path, cat_id, feed_a, feed_b

    def test_star_sets_starred_at_then_unstar_clears(self, tmp_path):
        path, _, feed_a, _ = self._seed(tmp_path)
        with feeds_db.connect(path) as conn:
            ids = [e.id for e in feeds_db.list_entries(conn, feed_id=feed_a)]
            n = feeds_db.update_entry_starred(conn, ids[:1], True)
            conn.commit()
            assert n == 1
            row = conn.execute(
                "SELECT starred, starred_at FROM feed_entries WHERE id = ?",
                (ids[0],),
            ).fetchone()
            assert row["starred"] == 1
            assert row["starred_at"] is not None

            feeds_db.update_entry_starred(conn, ids[:1], False)
            conn.commit()
            row = conn.execute(
                "SELECT starred, starred_at FROM feed_entries WHERE id = ?",
                (ids[0],),
            ).fetchone()
            assert row["starred"] == 0
            assert row["starred_at"] is None

    def test_star_survives_status_changes(self, tmp_path):
        path, _, feed_a, _ = self._seed(tmp_path)
        with feeds_db.connect(path) as conn:
            ids = [e.id for e in feeds_db.list_entries(conn, feed_id=feed_a)]
            feeds_db.update_entry_starred(conn, ids[:1], True)
            feeds_db.update_entry_status(conn, ids[:1], "read")
            feeds_db.update_entry_status(conn, ids[:1], "removed")
            conn.commit()
            row = conn.execute(
                "SELECT starred, status FROM feed_entries WHERE id = ?",
                (ids[0],),
            ).fetchone()
            assert row["starred"] == 1
            assert row["status"] == "removed"

    def test_starred_filter_independent_of_status(self, tmp_path):
        path, _, feed_a, _ = self._seed(tmp_path)
        with feeds_db.connect(path) as conn:
            all_ids = [e.id for e in feeds_db.list_entries(conn)]
            feeds_db.update_entry_starred(conn, all_ids[:2], True)
            feeds_db.update_entry_status(conn, all_ids[:1], "read")
            conn.commit()
            starred_only = feeds_db.list_entries(conn, starred=True)
            unstarred_only = feeds_db.list_entries(conn, starred=False)
            assert len(starred_only) == 2
            assert len(unstarred_only) == 1
            assert feeds_db.count_entries(conn, starred=True) == 2
            # Combined with status filter: starred + read = 1.
            mixed = feeds_db.list_entries(conn, status="read", starred=True)
            assert len(mixed) == 1


class TestMarkAsRead:
    def _seed(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            cat_id = feeds_db.upsert_category(conn, "blogs", "Blogs")
            feed_a = feeds_db.upsert_feed(
                conn, url="a", title=None, site_url=None,
                source_type="rss", category_id=cat_id,
                poll_interval_minutes=30,
            )
            feed_b = feeds_db.upsert_feed(
                conn, url="b", title=None, site_url=None,
                source_type="rss", category_id=None,
                poll_interval_minutes=30,
            )
            for feed_id, guid in [
                (feed_a, "a1"), (feed_a, "a2"), (feed_a, "a3"),
                (feed_b, "b1"), (feed_b, "b2"),
            ]:
                feeds_db.insert_entries(conn, feed_id, [
                    EntryRecord(
                        id=0, feed_id=feed_id, guid=guid, title=guid,
                        url=None, author=None, content_html=None,
                        content_text=None, image_urls=[],
                        published_at="2026-05-01T00:00:00+00:00",
                        fetched_at="2026-05-01T00:00:00+00:00",
                    ),
                ])
            conn.commit()
        return path, cat_id, feed_a, feed_b

    def test_scope_all(self, tmp_path):
        path, _, _, _ = self._seed(tmp_path)
        with feeds_db.connect(path) as conn:
            n = feeds_db.mark_as_read(conn, scope="all")
            conn.commit()
            assert n == 5
            assert feeds_db.count_entries(conn, status="unread") == 0

    def test_scope_feed(self, tmp_path):
        path, _, feed_a, feed_b = self._seed(tmp_path)
        with feeds_db.connect(path) as conn:
            n = feeds_db.mark_as_read(conn, scope="feed", scope_id=feed_a)
            conn.commit()
            assert n == 3
            assert feeds_db.count_entries(conn, status="unread", feed_id=feed_a) == 0
            assert feeds_db.count_entries(conn, status="unread", feed_id=feed_b) == 2

    def test_scope_category(self, tmp_path):
        path, cat_id, feed_a, feed_b = self._seed(tmp_path)
        with feeds_db.connect(path) as conn:
            n = feeds_db.mark_as_read(conn, scope="category", scope_id=cat_id)
            conn.commit()
            assert n == 3  # only feed_a is in the category
            assert feeds_db.count_entries(conn, status="unread", feed_id=feed_b) == 2

    def test_before_id_caps_operation(self, tmp_path):
        path, _, _, _ = self._seed(tmp_path)
        with feeds_db.connect(path) as conn:
            ids = sorted(e.id for e in feeds_db.list_entries(conn))
            cap = ids[2]  # first 3 entries
            n = feeds_db.mark_as_read(conn, scope="all", before_id=cap)
            conn.commit()
            assert n == 3
            unread = feeds_db.list_entries(conn, status="unread")
            assert {e.id for e in unread} == set(ids[3:])

    def test_already_read_entries_untouched(self, tmp_path):
        path, _, feed_a, _ = self._seed(tmp_path)
        with feeds_db.connect(path) as conn:
            ids = [e.id for e in feeds_db.list_entries(conn, feed_id=feed_a)]
            feeds_db.update_entry_status(conn, ids[:1], "read")
            conn.commit()
            n = feeds_db.mark_as_read(conn, scope="feed", scope_id=feed_a)
            conn.commit()
            assert n == 2  # not 3, the pre-marked entry is excluded

    def test_unknown_scope_raises(self, tmp_path):
        path, _, _, _ = self._seed(tmp_path)
        with feeds_db.connect(path) as conn:
            try:
                feeds_db.mark_as_read(conn, scope="nope")
            except ValueError:
                pass
            else:
                raise AssertionError("expected ValueError")

    def test_feed_scope_requires_id(self, tmp_path):
        path, _, _, _ = self._seed(tmp_path)
        with feeds_db.connect(path) as conn:
            try:
                feeds_db.mark_as_read(conn, scope="feed")
            except ValueError:
                pass
            else:
                raise AssertionError("expected ValueError")


class TestEntryImageIndex:
    """The ``entry_images`` key index backing cross-entry image suppression
    (ISSUE-162). Populated at insert, backfilled by the v2→v3 migration,
    cascaded away with its entry.
    """

    def _seed_two_feeds(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            cat_id = feeds_db.upsert_category(conn, "art", "Art")
            a = feeds_db.upsert_feed(
                conn, url="tumblr:a", title="A", site_url=None,
                source_type="tumblr", category_id=cat_id,
                poll_interval_minutes=60,
            )
            b = feeds_db.upsert_feed(
                conn, url="tumblr:b", title="B", site_url=None,
                source_type="tumblr", category_id=None,
                poll_interval_minutes=60,
            )
            conn.commit()
        return path, a, b, cat_id

    def _entry(self, feed_id, guid, urls, published_at):
        return EntryRecord(
            id=0, feed_id=feed_id, guid=guid, title=guid, url=None,
            author=None, content_html=None, content_text=None,
            image_urls=urls, published_at=published_at,
            fetched_at="2026-07-16T00:00:00+00:00",
        )

    def test_insert_records_normalised_keys(self, tmp_path):
        from istota.feeds.sanitize import image_identity

        path, feed_id, _, _ = self._seed_two_feeds(tmp_path)
        url = "https://64.media.tumblr.com/aaa/bbb-01/s500x750/hash.jpg"
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [
                self._entry(feed_id, "p1", [url], "2026-07-16T10:00:00+00:00"),
            ])
            conn.commit()
            rows = conn.execute(
                "SELECT image_key, seen_ts FROM entry_images"
            ).fetchall()
        assert [r["image_key"] for r in rows] == [image_identity(url)]
        assert rows[0]["seen_ts"] > 0

    def test_entry_without_images_indexes_nothing(self, tmp_path):
        path, feed_id, _, _ = self._seed_two_feeds(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [
                self._entry(feed_id, "p1", [], "2026-07-16T10:00:00+00:00"),
            ])
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) c FROM entry_images"
            ).fetchone()["c"]
        assert count == 0

    def test_ignored_duplicate_guid_does_not_reindex(self, tmp_path):
        path, feed_id, _, _ = self._seed_two_feeds(tmp_path)
        url = "https://64.media.tumblr.com/aaa/bbb-01/s500x750/hash.jpg"
        item = self._entry(feed_id, "p1", [url], "2026-07-16T10:00:00+00:00")
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [item])
            feeds_db.insert_entries(conn, feed_id, [item])
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) c FROM entry_images"
            ).fetchone()["c"]
        assert count == 1

    def test_variants_within_one_entry_collapse_to_one_row(self, tmp_path):
        path, feed_id, _, _ = self._seed_two_feeds(tmp_path)
        urls = [
            "https://64.media.tumblr.com/aaa/bbb-01/s500x750/hash.jpg",
            "https://72.media.tumblr.com/aaa/bbb-01/s1280x1920/hash.jpg",
        ]
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [
                self._entry(feed_id, "p1", urls, "2026-07-16T10:00:00+00:00"),
            ])
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) c FROM entry_images"
            ).fetchone()["c"]
        assert count == 1

    def test_deleting_a_feed_cascades_the_index(self, tmp_path):
        path, feed_id, _, _ = self._seed_two_feeds(tmp_path)
        url = "https://64.media.tumblr.com/aaa/bbb-01/s500x750/hash.jpg"
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [
                self._entry(feed_id, "p1", [url], "2026-07-16T10:00:00+00:00"),
            ])
            conn.commit()
            feeds_db.delete_feed(conn, "tumblr:a")
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) c FROM entry_images"
            ).fetchone()["c"]
        assert count == 0

    def test_image_key_owners_filters_by_window_and_scope(self, tmp_path):
        from istota.feeds.image_dedupe import parse_seen_ts
        from istota.feeds.sanitize import image_identity

        path, feed_a, feed_b, cat_id = self._seed_two_feeds(tmp_path)
        url = "https://64.media.tumblr.com/aaa/bbb-01/s500x750/hash.jpg"
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_a, [
                self._entry(feed_a, "a1", [url], "2026-07-16T10:00:00+00:00"),
            ])
            feeds_db.insert_entries(conn, feed_b, [
                self._entry(feed_b, "b1", [url], "2026-07-15T10:00:00+00:00"),
            ])
            conn.commit()
            key = image_identity(url)
            lo = parse_seen_ts("2026-07-01T00:00:00+00:00")
            hi = parse_seen_ts("2026-08-01T00:00:00+00:00")

            everything = feeds_db.image_key_owners(
                conn, [key], min_ts=lo, max_ts=hi,
            )
            by_feed = feeds_db.image_key_owners(
                conn, [key], min_ts=lo, max_ts=hi, feed_id=feed_a,
            )
            by_category = feeds_db.image_key_owners(
                conn, [key], min_ts=lo, max_ts=hi, category_id=cat_id,
            )
            narrow = feeds_db.image_key_owners(
                conn, [key],
                min_ts=parse_seen_ts("2026-07-16T00:00:00+00:00"),
                max_ts=hi,
            )

        assert len(everything) == 2
        assert len(by_feed) == 1
        assert len(by_category) == 1  # only feed A is in the category
        assert len(narrow) == 1  # the 07-15 owner falls outside the range
        assert all(k == key for k, _, _ in everything)

    def test_image_key_owners_empty_keys_short_circuits(self, tmp_path):
        path, _, _, _ = self._seed_two_feeds(tmp_path)
        with feeds_db.connect(path) as conn:
            assert feeds_db.image_key_owners(
                conn, [], min_ts=0, max_ts=1,
            ) == []

    def test_image_key_owners_chunks_large_key_lists(self, tmp_path):
        """More keys than SQLite's variable limit must not raise."""
        from istota.feeds.sanitize import image_identity

        path, feed_id, _, _ = self._seed_two_feeds(tmp_path)
        urls = [
            f"https://64.media.tumblr.com/k{i}/p-01/s500x750/hash{i}.jpg"
            for i in range(1200)
        ]
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [
                self._entry(feed_id, "p1", urls, "2026-07-16T10:00:00+00:00"),
            ])
            conn.commit()
            owners = feeds_db.image_key_owners(
                conn, [image_identity(u) for u in urls],
                min_ts=0, max_ts=2 ** 40,
            )
        assert len(owners) == 1200


class TestImageDedupeWindowSetting:
    def test_defaults_to_none_and_round_trips(self, tmp_path):
        path = tmp_path / "feeds.db"
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            assert feeds_db.get_image_dedupe_window_days(conn) is None
            feeds_db.set_image_dedupe_window_days(conn, 30)
            conn.commit()
            assert feeds_db.get_image_dedupe_window_days(conn) == 30
            feeds_db.set_image_dedupe_window_days(conn, 0)
            conn.commit()
            assert feeds_db.get_image_dedupe_window_days(conn) == 0
            feeds_db.set_image_dedupe_window_days(conn, None)
            conn.commit()
            assert feeds_db.get_image_dedupe_window_days(conn) is None


# ---------------------------------------------------------------------------
# schema v8 — observation state, snapshot marker, poll claims (ISSUE-388)
# ---------------------------------------------------------------------------


def _seed_one_feed(tmp_path, *, url="https://example.com/feed.xml"):
    """A migrated DB with one RSS feed. Returns ``(path, feed_id)``."""
    path = tmp_path / "feeds.db"
    feeds_db.init_db(path)
    with feeds_db.connect(path) as conn:
        feed_id = feeds_db.upsert_feed(
            conn, url=url, title=None, site_url=None,
            source_type="rss", category_id=None, poll_interval_minutes=30,
        )
        conn.commit()
    return path, feed_id


def _entry(feed_id, guid, fetched_at, **over):
    base = dict(
        id=0, feed_id=feed_id, guid=guid, title=guid, url=None, author=None,
        content_html=None, content_text=None, image_urls=[], published_at=None,
        fetched_at=fetched_at,
    )
    base.update(over)
    return EntryRecord(**base)


def _rewind_to_v7(path):
    """Strip every v8 addition from a migrated DB and re-stamp it as v7.

    The same shape the v3→v4 and v5→v6 tests use: rewinding a real DB rather
    than hand-building one keeps the fixture honest about what production
    actually holds. The index goes first — SQLite refuses to drop a column an
    index references.

    ``DROP COLUMN`` edits the stored ``CREATE TABLE`` text and re-parses it,
    and dropping the table's *physically last* column out of the commented
    schema this module ships left it unparseable ("incomplete input"). That is
    why ``feeds`` keeps ``next_poll_at`` last and the v8 columns sit beside the
    other poll-state ones.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_entries_feed_last_seen_unstarred")
        conn.execute("DROP INDEX IF EXISTS idx_entries_feed_fetched_unstarred")
        conn.execute("ALTER TABLE feed_entries DROP COLUMN last_seen_at")
        conn.execute("ALTER TABLE feeds DROP COLUMN last_items_seen_at")
        conn.execute("ALTER TABLE feeds DROP COLUMN poll_claimed_until")
        conn.execute(
            "DELETE FROM schema_meta WHERE key IN (?, ?, ?)",
            (
                feeds_db.ENTRY_RETENTION_DAYS_KEY,
                feeds_db.MAX_ENTRIES_PER_FEED_KEY,
                feeds_db.ENTRY_PRUNE_NOT_BEFORE_KEY,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version','7')"
        )
        conn.commit()
    finally:
        conn.close()


class TestSchemaV8Migration:
    def _v7_db_with_history(self, tmp_path):
        """A v7 DB holding one feed with live validators and two entries."""
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [
                _entry(feed_id, "old", "2026-01-01T00:00:00+00:00"),
                _entry(feed_id, "older", "2025-06-01T00:00:00+00:00"),
            ])
            feeds_db.update_feed_fetch_state(
                conn, feed_id,
                etag='"v1"', last_modified="Wed, 01 Jan 2026 00:00:00 GMT",
                last_fetched_at="2026-01-01T00:00:00+00:00",
                last_error=None, error_count=0,
                next_poll_at="2030-01-01T00:00:00+00:00",
            )
            conn.commit()
        _rewind_to_v7(path)
        return path, feed_id

    def test_migration_adds_columns_and_both_partial_indexes(self, tmp_path):
        path, _ = self._v7_db_with_history(tmp_path)

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            entry_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")
            }
            feed_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
            indexes = {
                r["name"]: r["sql"]
                for r in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='index'"
                )
            }
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()["value"]

        assert "last_seen_at" in entry_cols
        assert {"last_items_seen_at", "poll_claimed_until"} <= feed_cols
        # Both are partial on `starred = 0`, because every retention pass
        # excludes stars before it does anything else.
        for name in (
            "idx_entries_feed_last_seen_unstarred",
            "idx_entries_feed_fetched_unstarred",
        ):
            assert "starred = 0" in indexes[name]
        assert version == "8"

    def test_migration_stamps_one_observation_time(self, tmp_path):
        path, _ = self._v7_db_with_history(tmp_path)

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            rows = conn.execute(
                "SELECT guid, last_seen_at, fetched_at "
                "FROM feed_entries ORDER BY guid"
            ).fetchall()
        assert [r["guid"] for r in rows] == ["old", "older"]
        stamps = {r["last_seen_at"] for r in rows}
        assert len(stamps) == 1 and None not in stamps
        # `fetched_at` is the retention clock, so the migration must not move
        # it: rewriting it would reset every entry's age to the upgrade date.
        assert [r["fetched_at"] for r in rows] == [
            "2026-01-01T00:00:00+00:00", "2025-06-01T00:00:00+00:00",
        ]

    def test_migration_clears_validators_and_makes_the_feed_due(self, tmp_path):
        path, _ = self._v7_db_with_history(tmp_path)

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            feed = feeds_db.list_feeds(conn)[0]
            due = feeds_db.feeds_due_for_poll(
                conn, now=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
        assert feed.etag is None
        assert feed.last_modified is None
        assert feed.next_poll_at is None
        assert feed.last_items_seen_at is None
        assert feed.poll_claimed_until is None
        assert [f.id for f in due] == [feed.id]

    def test_migration_stores_explicit_defaults_and_a_ninety_day_grace(
        self, tmp_path,
    ):
        path, _ = self._v7_db_with_history(tmp_path)

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            meta = {
                r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM schema_meta")
            }
            stamp = conn.execute(
                "SELECT last_seen_at FROM feed_entries LIMIT 1"
            ).fetchone()["last_seen_at"]
        assert meta[feeds_db.ENTRY_RETENTION_DAYS_KEY] == str(
            DEFAULT_ENTRY_RETENTION_DAYS
        )
        assert meta[feeds_db.MAX_ENTRIES_PER_FEED_KEY] == str(
            DEFAULT_MAX_ENTRIES_PER_FEED
        )
        grace = datetime.fromisoformat(meta[feeds_db.ENTRY_PRUNE_NOT_BEFORE_KEY])
        assert grace - datetime.fromisoformat(stamp) == timedelta(days=90)

    def test_rerunning_init_db_preserves_overrides_and_grace(self, tmp_path):
        path, feed_id = self._v7_db_with_history(tmp_path)
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                (feeds_db.MAX_ENTRIES_PER_FEED_KEY, "10"),
            )
            conn.execute(
                "UPDATE feeds SET last_items_seen_at = ? WHERE id = ?",
                ("2026-09-01T00:00:00+00:00", feed_id),
            )
            conn.commit()
            before = {
                r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM schema_meta")
            }
            stamps_before = [
                r["last_seen_at"]
                for r in conn.execute("SELECT last_seen_at FROM feed_entries")
            ]

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            after = {
                r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM schema_meta")
            }
            stamps_after = [
                r["last_seen_at"]
                for r in conn.execute("SELECT last_seen_at FROM feed_entries")
            ]
            feed = feeds_db.list_feeds(conn)[0]
        assert after == before
        assert stamps_after == stamps_before
        assert feed.last_items_seen_at == "2026-09-01T00:00:00+00:00"

    def test_a_database_older_than_schema_meta_migrates_all_the_way(self, tmp_path):
        """v8 is the first migration to write to ``schema_meta``.

        ``_read_schema_version`` returns 1 for a database predating that table,
        and ``init_db`` runs the whole migration chain *before* ``SCHEMA_SQL``
        creates it — so a v8 step that assumed the table meant the oldest
        databases stopped opening at all, through every entry point that calls
        ``init_db``.
        """
        path = tmp_path / "feeds.db"
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE feed_categories (
                    id INTEGER PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL
                );
                CREATE TABLE feeds (
                    id INTEGER PRIMARY KEY,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT, site_url TEXT,
                    category_id INTEGER REFERENCES feed_categories(id),
                    source_type TEXT NOT NULL,
                    etag TEXT, last_modified TEXT, last_fetched_at TEXT,
                    last_error TEXT,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    poll_interval_minutes INTEGER NOT NULL DEFAULT 30,
                    next_poll_at TEXT
                );
                CREATE TABLE feed_entries (
                    id INTEGER PRIMARY KEY,
                    feed_id INTEGER NOT NULL REFERENCES feeds(id),
                    guid TEXT NOT NULL,
                    title TEXT, url TEXT, author TEXT,
                    content_html TEXT, content_text TEXT,
                    image_urls TEXT, published_at TEXT,
                    fetched_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unread',
                    UNIQUE(feed_id, guid)
                );
                INSERT INTO feeds(url, source_type)
                    VALUES ('https://example.com/feed.xml', 'rss');
                INSERT INTO feed_entries(feed_id, guid, fetched_at, status)
                    VALUES (1, 'ancient', '2024-01-01T00:00:00+00:00', 'read');
                """
            )
            conn.commit()
        finally:
            conn.close()

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()["value"]
            row = conn.execute(
                "SELECT guid, status, last_seen_at FROM feed_entries"
            ).fetchone()
            keys = {r["key"] for r in conn.execute("SELECT key FROM schema_meta")}
        assert version == "8"
        # The row survived every step, keeping its user state.
        assert row["guid"] == "ancient"
        assert row["status"] == "read"
        assert row["last_seen_at"] is not None
        assert feeds_db.ENTRY_PRUNE_NOT_BEFORE_KEY in keys

    def test_a_rerun_after_a_partial_migration_keeps_real_observations(
        self, tmp_path,
    ):
        """The ``WHERE last_seen_at IS NULL`` guard, exercised.

        ``ALTER TABLE`` autocommits under this driver while the data pass does
        not, so a crash part-way leaves the columns present, the stamps rolled
        back and the version still 7 — and the next ``init_db`` runs the
        migration again over rows that may by then hold real observations.
        Rewinding only the version reproduces that; the neighbouring
        idempotence test cannot, because at version 8 the migration never runs.
        """
        path, feed_id = self._v7_db_with_history(tmp_path)
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            conn.execute(
                "UPDATE feed_entries SET last_seen_at = ? WHERE guid = 'old'",
                ("2026-09-01T00:00:00+00:00",),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                (feeds_db.MAX_ENTRIES_PER_FEED_KEY, "10"),
            )
            conn.execute(
                "UPDATE feeds SET last_items_seen_at = ? WHERE id = ?",
                ("2026-09-01T00:00:00+00:00", feed_id),
            )
            conn.commit()
            grace_before = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (feeds_db.ENTRY_PRUNE_NOT_BEFORE_KEY,),
            ).fetchone()["value"]
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) "
                "VALUES ('version','7')"
            )
            conn.commit()

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            row = conn.execute(
                "SELECT last_seen_at FROM feed_entries WHERE guid='old'"
            ).fetchone()
            meta = {
                r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM schema_meta")
            }
            feed = feeds_db.list_feeds(conn)[0]
        # A real observation is never overwritten by the migration clock, and
        # neither the grace deadline nor a user override is moved.
        assert row["last_seen_at"] == "2026-09-01T00:00:00+00:00"
        assert meta[feeds_db.ENTRY_PRUNE_NOT_BEFORE_KEY] == grace_before
        assert meta[feeds_db.MAX_ENTRIES_PER_FEED_KEY] == "10"
        # The observation marker is a feed's own state, not the migration's.
        assert feed.last_items_seen_at == "2026-09-01T00:00:00+00:00"

    def test_a_fresh_database_has_the_columns_and_no_grace_row(self, tmp_path):
        path, _ = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            entry_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")
            }
            feed_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
            indexes = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            keys = {r["key"] for r in conn.execute("SELECT key FROM schema_meta")}
        assert "last_seen_at" in entry_cols
        assert {"last_items_seen_at", "poll_claimed_until"} <= feed_cols
        assert {
            "idx_entries_feed_last_seen_unstarred",
            "idx_entries_feed_fetched_unstarred",
        } <= indexes
        assert feeds_db.ENTRY_PRUNE_NOT_BEFORE_KEY not in keys
        # A fresh DB resolves the policy from the constants rather than storing
        # explicit rows, so an operator-visible default is never frozen at
        # install time.
        assert feeds_db.ENTRY_RETENTION_DAYS_KEY not in keys
        assert feeds_db.MAX_ENTRIES_PER_FEED_KEY not in keys


class TestPollClaims:
    def test_claim_succeeds_and_writes_the_lease(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with feeds_db.connect(path) as conn:
            assert feeds_db.claim_feed_for_poll(conn, feed_id, now=now) is True
            feed = feeds_db.list_feeds(conn)[0]
        expected = (now + timedelta(seconds=POLL_CLAIM_SECONDS)).isoformat()
        assert feed.poll_claimed_until == expected

    def test_a_live_claim_blocks_a_second_caller(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with feeds_db.connect(path) as conn:
            assert feeds_db.claim_feed_for_poll(conn, feed_id, now=now) is True
        # A genuinely separate connection, as a competing process would be.
        with feeds_db.connect(path) as other:
            assert feeds_db.claim_feed_for_poll(
                other, feed_id, now=now + timedelta(seconds=30),
            ) is False

    def test_the_claim_commits_so_another_connection_sees_it(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with feeds_db.connect(path) as conn:
            feeds_db.claim_feed_for_poll(conn, feed_id, now=now)
            with feeds_db.connect(path) as other:
                row = other.execute(
                    "SELECT poll_claimed_until FROM feeds WHERE id = ?", (feed_id,),
                ).fetchone()
        assert row["poll_claimed_until"] is not None

    def test_an_expired_claim_can_be_taken_again(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with feeds_db.connect(path) as conn:
            feeds_db.claim_feed_for_poll(conn, feed_id, now=now)
            later = now + timedelta(seconds=POLL_CLAIM_SECONDS + 1)
            assert feeds_db.claim_feed_for_poll(conn, feed_id, now=later) is True

    def test_a_feed_that_is_not_due_cannot_be_claimed(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with feeds_db.connect(path) as conn:
            conn.execute(
                "UPDATE feeds SET next_poll_at = ? WHERE id = ?",
                ((now + timedelta(hours=1)).isoformat(), feed_id),
            )
            conn.commit()
            assert feeds_db.claim_feed_for_poll(conn, feed_id, now=now) is False
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.poll_claimed_until is None

    def test_a_naive_clock_is_refused(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            with pytest.raises(ValueError):
                feeds_db.claim_feed_for_poll(
                    conn, feed_id, now=datetime(2026, 9, 1, 12, 0),
                )

    def test_a_non_utc_clock_writes_a_lease_another_process_reads_right(
        self, tmp_path,
    ):
        """Aware is not enough — the lease is read back as a lexical string.

        A `+02:00` clock renders 14:00 for the same instant a UTC reader
        renders 12:00, and `'14…' > '12…'`, so an unconverted lease reads as
        live for two hours past its end. Westward it reads as already expired
        and two processes fetch the same feed, which is the race the claim
        exists to close.
        """
        path, feed_id = _seed_one_feed(tmp_path)
        east = timezone(timedelta(hours=2))
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with feeds_db.connect(path) as conn:
            assert feeds_db.claim_feed_for_poll(
                conn, feed_id, now=now.astimezone(east),
            ) is True
            feed = feeds_db.list_feeds(conn)[0]
            # The same instant a UTC caller would have written.
            assert feed.poll_claimed_until == (
                now + timedelta(seconds=POLL_CLAIM_SECONDS)
            ).isoformat()
            # And a UTC reader agrees about when it ends.
            still_held = feeds_db.feeds_due_for_poll(
                conn, now=now + timedelta(seconds=POLL_CLAIM_SECONDS - 1),
            )
            expired = feeds_db.feeds_due_for_poll(
                conn, now=now + timedelta(seconds=POLL_CLAIM_SECONDS + 1),
            )
        assert still_held == []
        assert [f.id for f in expired] == [feed_id]

    def test_a_non_utc_reader_agrees_about_a_live_claim(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        west = timezone(timedelta(hours=-5))
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with feeds_db.connect(path) as conn:
            feeds_db.claim_feed_for_poll(conn, feed_id, now=now)
            during = feeds_db.feeds_due_for_poll(
                conn, now=(now + timedelta(seconds=10)).astimezone(west),
            )
            with feeds_db.connect(path) as other:
                taken = feeds_db.claim_feed_for_poll(
                    other, feed_id,
                    now=(now + timedelta(seconds=10)).astimezone(west),
                )
        assert during == []
        assert taken is False

    def test_the_due_list_excludes_a_live_claim(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with feeds_db.connect(path) as conn:
            feeds_db.claim_feed_for_poll(conn, feed_id, now=now)
            during = feeds_db.feeds_due_for_poll(
                conn, now=now + timedelta(seconds=10),
            )
            after = feeds_db.feeds_due_for_poll(
                conn, now=now + timedelta(seconds=POLL_CLAIM_SECONDS + 1),
            )
        assert during == []
        assert [f.id for f in after] == [feed_id]


class TestObservationState:
    def test_an_insert_stamps_the_observation(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(
                conn, feed_id, [_entry(feed_id, "a", "2026-09-01T12:00:00+00:00")],
            )
            row = conn.execute(
                "SELECT last_seen_at FROM feed_entries WHERE guid='a'"
            ).fetchone()
        assert row["last_seen_at"] == "2026-09-01T12:00:00+00:00"

    def test_a_refresh_advances_the_observation(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(
                conn, feed_id,
                [_entry(feed_id, "a", "2026-09-01T12:00:00+00:00")],
            )
            conn.execute("UPDATE feed_entries SET status = 'read' WHERE guid='a'")
            feeds_db.insert_entries(
                conn, feed_id,
                [_entry(
                    feed_id, "a", "2026-09-02T12:00:00+00:00",
                    content_html="<p>fuller</p>",
                )],
            )
            row = conn.execute(
                "SELECT last_seen_at, fetched_at, status, content_html "
                "FROM feed_entries WHERE guid='a'"
            ).fetchone()
        assert row["last_seen_at"] == "2026-09-02T12:00:00+00:00"
        # `fetched_at` is the retention clock, so a refresh must not move it —
        # and neither first-fetch ordering nor user state changes either.
        assert row["fetched_at"] == "2026-09-01T12:00:00+00:00"
        assert row["status"] == "read"
        assert row["content_html"] == "<p>fuller</p>"

    def test_an_unstamped_record_leaves_the_stored_observation_alone(
        self, tmp_path,
    ):
        """A hand-built record carrying no fetch time must not blank the stamp.

        A null ``last_seen_at`` reads as "never observed", which fails closed
        in the age pass — the row would be undeletable for good.
        """
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(
                conn, feed_id,
                [_entry(feed_id, "a", "2026-09-01T12:00:00+00:00")],
            )
            feeds_db.insert_entries(
                conn, feed_id,
                [_entry(feed_id, "a", "", content_html="<p>fuller</p>")],
            )
            row = conn.execute(
                "SELECT last_seen_at, content_html FROM feed_entries WHERE guid='a'"
            ).fetchone()
        assert row["last_seen_at"] == "2026-09-01T12:00:00+00:00"
        assert row["content_html"] == "<p>fuller</p>"

    def test_a_unique_conflict_loser_still_refreshes_and_stamps(self, tmp_path):
        """The ``INSERT OR IGNORE`` race branch must not skip the stamp.

        A competing writer inserts the same guid between our lookup and our
        insert. The old code took ``continue`` there, so the row kept whatever
        observation state the winner gave it and this poll's content was
        dropped. The poll claim makes this unreachable on the normal path;
        ``insert_entries`` still has to be right for a direct caller.
        """
        path, feed_id = _seed_one_feed(tmp_path)

        class _RacingConn:
            """Delegates to a real connection, inserting a competing row just
            before ``insert_entries`` issues its own INSERT."""

            def __init__(self, conn, db_path, feed_id):
                self._conn = conn
                self._db_path = db_path
                self._feed_id = feed_id
                self._fired = False

            def execute(self, sql, parameters=()):
                if not self._fired and "INSERT OR IGNORE INTO feed_entries" in sql:
                    self._fired = True
                    rival = sqlite3.connect(self._db_path)
                    try:
                        rival.execute(
                            "INSERT INTO feed_entries(feed_id, guid, title, "
                            "fetched_at, status) VALUES (?, ?, ?, ?, 'unread')",
                            (self._feed_id, "a", "thin",
                             "2026-08-01T00:00:00+00:00"),
                        )
                        rival.commit()
                    finally:
                        rival.close()
                return self._conn.execute(sql, parameters)

        with feeds_db.connect(path) as conn:
            racing = _RacingConn(conn, path, feed_id)
            inserted = feeds_db.insert_entries(
                racing, feed_id,
                [_entry(
                    feed_id, "a", "2026-09-01T12:00:00+00:00",
                    content_html="<p>ours</p>",
                )],
            )
            row = conn.execute(
                "SELECT last_seen_at, content_html, fetched_at "
                "FROM feed_entries WHERE guid='a'"
            ).fetchone()
        # The row was not ours to insert, so it is not counted as new.
        assert inserted == 0
        assert row["last_seen_at"] == "2026-09-01T12:00:00+00:00"
        assert row["content_html"] == "<p>ours</p>"
        # The winner's first-fetch time stands.
        assert row["fetched_at"] == "2026-08-01T00:00:00+00:00"


class TestFetchStateObservationFields:
    def _state_kwargs(self):
        return dict(
            etag=None, last_modified=None,
            last_fetched_at="2026-09-01T12:00:00+00:00",
            last_error=None, error_count=0,
            next_poll_at="2026-09-01T12:30:00+00:00",
        )

    def test_omitted_observation_arguments_leave_both_columns_alone(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            conn.execute(
                "UPDATE feeds SET last_items_seen_at = ?, poll_claimed_until = ? "
                "WHERE id = ?",
                ("2026-08-01T00:00:00+00:00", "2026-09-01T12:05:00+00:00", feed_id),
            )
            feeds_db.update_feed_fetch_state(conn, feed_id, **self._state_kwargs())
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.last_items_seen_at == "2026-08-01T00:00:00+00:00"
        assert feed.poll_claimed_until == "2026-09-01T12:05:00+00:00"

    def test_the_observation_marker_and_claim_can_be_written(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            conn.execute(
                "UPDATE feeds SET poll_claimed_until = ? WHERE id = ?",
                ("2026-09-01T12:05:00+00:00", feed_id),
            )
            feeds_db.update_feed_fetch_state(
                conn, feed_id,
                last_items_seen_at="2026-09-01T12:00:00+00:00",
                poll_claimed_until=None,
                **self._state_kwargs(),
            )
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.last_items_seen_at == "2026-09-01T12:00:00+00:00"
        assert feed.poll_claimed_until is None


# ---------------------------------------------------------------------------
# retention: settings, the two passes, grace, and the churn control (ISSUE-388)
# ---------------------------------------------------------------------------


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _ctx(tmp_path, db_path):
    return FeedsContext(
        user_id="alice", data_dir=tmp_path, db_path=db_path,
    )


def _store(
    conn, feed_id, guid, *, fetched_at, last_seen_at=None, status="unread",
    starred=False, published_at=None,
):
    """Write one entry row with every retention-relevant field set by hand.

    Direct SQL rather than ``insert_entries`` because these tests need to place
    a row at an arbitrary point in the past on both clocks, which the real
    writer deliberately refuses to do.
    """
    conn.execute(
        "INSERT INTO feed_entries(feed_id, guid, title, fetched_at, "
        "last_seen_at, status, starred, published_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (feed_id, guid, guid, fetched_at, last_seen_at, status,
         1 if starred else 0, published_at),
    )


def _mark(conn, feed_id, when):
    conn.execute(
        "UPDATE feeds SET last_items_seen_at = ? WHERE id = ?", (when, feed_id),
    )


def _guids(conn, feed_id=None):
    if feed_id is None:
        rows = conn.execute("SELECT guid FROM feed_entries ORDER BY guid")
    else:
        rows = conn.execute(
            "SELECT guid FROM feed_entries WHERE feed_id = ? ORDER BY guid",
            (feed_id,),
        )
    return [r["guid"] for r in rows]


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def _fill_to_floor(conn, feed_id, marker, *, days_ago=2):
    """``MIN_ENTRIES_PER_FEED`` current unread rows on one feed.

    Every rule below the floor is unobservable, because the floor protects a
    feed's whole contents. A test about some *other* rule therefore has to lift
    the feed over it first, or it passes for a reason it did not name — which
    for the churn control would be the whole point of the test lost.
    """
    for i in range(MIN_ENTRIES_PER_FEED):
        _store(
            conn, feed_id, f"filler{i:03d}", fetched_at=_iso(days_ago),
            last_seen_at=marker, status="unread",
        )


class TestRetentionSettings:
    def test_both_settings_round_trip_absent_positive_zero_and_cleared(
        self, tmp_path,
    ):
        path, _ = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            assert feeds_db.get_entry_retention_days(conn) is None
            assert feeds_db.get_max_entries_per_feed(conn) is None
            feeds_db.set_entry_retention_days(conn, 30)
            feeds_db.set_max_entries_per_feed(conn, 200)
            conn.commit()
            assert feeds_db.get_entry_retention_days(conn) == 30
            assert feeds_db.get_max_entries_per_feed(conn) == 200
            # `0` is a real value meaning "off", distinct from unset.
            feeds_db.set_entry_retention_days(conn, 0)
            feeds_db.set_max_entries_per_feed(conn, 0)
            conn.commit()
            assert feeds_db.get_entry_retention_days(conn) == 0
            assert feeds_db.get_max_entries_per_feed(conn) == 0
            feeds_db.set_entry_retention_days(conn, None)
            feeds_db.set_max_entries_per_feed(conn, None)
            conn.commit()
            assert feeds_db.get_entry_retention_days(conn) is None
            assert feeds_db.get_max_entries_per_feed(conn) is None

    def test_a_malformed_stored_value_reads_as_absent(self, tmp_path):
        path, _ = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                (feeds_db.ENTRY_RETENTION_DAYS_KEY, "not-a-number"),
            )
            conn.commit()
            assert feeds_db.get_entry_retention_days(conn) is None

    def test_a_negative_stored_value_resolves_to_the_default(self, tmp_path):
        """Nothing may resolve to a negative window.

        A negative retention would put the cutoff in the *future*, which makes
        every stored row past it — the whole reader deleted on one bad value.
        The API rejects negatives; this is the second guard, at the point of
        use.
        """
        path, _ = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.set_entry_retention_days(conn, -5)
            feeds_db.set_max_entries_per_feed(conn, -5)
            conn.commit()
            assert retention.resolve_retention_days(conn) == (
                DEFAULT_ENTRY_RETENTION_DAYS
            )
            assert retention.resolve_max_entries_per_feed(conn) == (
                DEFAULT_MAX_ENTRIES_PER_FEED
            )

    def test_absent_settings_resolve_to_the_constants(self, tmp_path):
        path, _ = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            assert retention.resolve_retention_days(conn) == (
                DEFAULT_ENTRY_RETENTION_DAYS
            )
            assert retention.resolve_max_entries_per_feed(conn) == (
                DEFAULT_MAX_ENTRIES_PER_FEED
            )

    def test_zero_resolves_to_zero_rather_than_the_default(self, tmp_path):
        path, _ = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.set_entry_retention_days(conn, 0)
            feeds_db.set_max_entries_per_feed(conn, 0)
            conn.commit()
            assert retention.resolve_retention_days(conn) == 0
            assert retention.resolve_max_entries_per_feed(conn) == 0


class TestAgePruning:
    """``prune_entries_by_age`` — the five predicates and their controls."""

    def _feed_with(self, tmp_path, rows, *, marker=_iso(1)):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            for row in rows:
                _store(conn, feed_id, **row)
            _mark(conn, feed_id, marker)
            conn.commit()
        return path, feed_id

    def _prune(self, path, *, days=90, floor=0, cap=0):
        with feeds_db.connect(path) as conn:
            deleted, held = feeds_db.prune_entries_by_age(
                conn,
                before_iso=(NOW - timedelta(days=days)).isoformat(),
                min_entries_per_feed=floor,
                max_entries_per_feed=cap,
            )
            conn.commit()
            return deleted, held, _guids(conn)

    def test_a_read_entry_past_the_cutoff_and_out_of_the_response_goes(
        self, tmp_path,
    ):
        path, _ = self._feed_with(tmp_path, [
            dict(guid="old-read", fetched_at=_iso(200), last_seen_at=_iso(150),
                 status="read"),
        ])
        deleted, held, guids = self._prune(path)
        assert (deleted, held) == (1, 0)
        assert guids == []

    def test_a_removed_entry_goes_the_same_way(self, tmp_path):
        path, _ = self._feed_with(tmp_path, [
            dict(guid="old-removed", fetched_at=_iso(200),
                 last_seen_at=_iso(150), status="removed"),
        ])
        deleted, _, guids = self._prune(path)
        assert deleted == 1
        assert guids == []

    def test_unread_starred_recent_and_in_response_rows_all_stay(self, tmp_path):
        marker = _iso(1)
        path, _ = self._feed_with(tmp_path, [
            # unread, however old
            dict(guid="unread", fetched_at=_iso(300), last_seen_at=_iso(200),
                 status="unread"),
            # starred, however old and however long read
            dict(guid="starred", fetched_at=_iso(300), last_seen_at=_iso(200),
                 status="read", starred=True),
            # read, but inside the window
            dict(guid="recent", fetched_at=_iso(10), last_seen_at=_iso(5),
                 status="read"),
            # read and ancient, but the latest response still returns it
            dict(guid="in-response", fetched_at=_iso(300), last_seen_at=marker,
                 status="read"),
            # never observed at all: fails closed
            dict(guid="never-seen", fetched_at=_iso(300), last_seen_at=None,
                 status="read"),
        ], marker=marker)
        deleted, held, guids = self._prune(path)
        assert (deleted, held) == (0, 0)
        assert guids == [
            "in-response", "never-seen", "recent", "starred", "unread",
        ]

    def test_a_feed_with_no_marker_loses_nothing(self, tmp_path):
        """No response has ever returned an item here, so nothing is known
        about what the source still offers — fail closed."""
        path, _ = self._feed_with(tmp_path, [
            dict(guid="old-read", fetched_at=_iso(300), last_seen_at=_iso(200),
                 status="read"),
        ], marker=None)
        deleted, held, guids = self._prune(path)
        assert (deleted, held) == (0, 0)
        assert guids == ["old-read"]

    def test_published_at_is_irrelevant_in_both_directions(self, tmp_path):
        """The clock is ``fetched_at``, and this is the pair that proves it.

        An Are.na block created in 2019 and added to a channel today arrives
        with a 2019 ``published_at``; deleting on that would purge it on the
        day it appeared.
        """
        path, _ = self._feed_with(tmp_path, [
            dict(guid="old-published-new-here", fetched_at=_iso(2),
                 last_seen_at=_iso(2), status="read",
                 published_at="2019-01-01T00:00:00+00:00"),
            dict(guid="new-published-old-here", fetched_at=_iso(300),
                 last_seen_at=_iso(200), status="read",
                 published_at="2026-08-31T00:00:00+00:00"),
        ])
        deleted, _, guids = self._prune(path)
        assert deleted == 1
        assert guids == ["old-published-new-here"]


class TestTheFloor:
    def _quiet_feed(self, tmp_path, count, *, status="read"):
        """``count`` old, read, out-of-response rows on one feed."""
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(count):
                _store(
                    conn, feed_id, f"e{i:03d}",
                    fetched_at=_iso(300 - i), last_seen_at=_iso(200),
                    status=status,
                )
            _mark(conn, feed_id, _iso(1))
            conn.commit()
        return path, feed_id

    def _prune(self, path, *, floor=MIN_ENTRIES_PER_FEED, cap=0):
        with feeds_db.connect(path) as conn:
            deleted, held = feeds_db.prune_entries_by_age(
                conn,
                before_iso=(NOW - timedelta(days=90)).isoformat(),
                min_entries_per_feed=floor,
                max_entries_per_feed=cap,
            )
            conn.commit()
            return deleted, held, _guids(conn)

    def test_a_feed_at_the_floor_loses_nothing_however_old(self, tmp_path):
        path, _ = self._quiet_feed(tmp_path, MIN_ENTRIES_PER_FEED)
        deleted, held, guids = self._prune(path)
        assert deleted == 0
        assert held == MIN_ENTRIES_PER_FEED
        assert len(guids) == MIN_ENTRIES_PER_FEED

    def test_a_feed_under_the_floor_loses_nothing(self, tmp_path):
        path, _ = self._quiet_feed(tmp_path, 3)
        deleted, held, guids = self._prune(path)
        assert (deleted, held) == (0, 3)
        assert len(guids) == 3

    def test_a_feed_above_the_floor_loses_only_its_oldest(self, tmp_path):
        path, _ = self._quiet_feed(tmp_path, MIN_ENTRIES_PER_FEED + 5)
        deleted, held, guids = self._prune(path)
        assert deleted == 5
        assert held == MIN_ENTRIES_PER_FEED
        # `e000` is the oldest `fetched_at`; the five oldest went.
        assert len(guids) == MIN_ENTRIES_PER_FEED
        assert "e000" not in guids
        assert "e004" not in guids
        assert "e005" in guids

    def test_the_floor_counts_every_stored_row_not_only_deletable_ones(
        self, tmp_path,
    ):
        """Fifty unread rows are plenty of history, so the read ones go.

        "Keep at least fifty entries" is a statement about what is in the
        reader, not about how many *read* ones survive.
        """
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(MIN_ENTRIES_PER_FEED):
                _store(conn, feed_id, f"unread{i:03d}", fetched_at=_iso(10 + i),
                       last_seen_at=_iso(5), status="unread")
            for i in range(7):
                _store(conn, feed_id, f"read{i:03d}", fetched_at=_iso(300 + i),
                       last_seen_at=_iso(200), status="read")
            _mark(conn, feed_id, _iso(1))
            conn.commit()
        deleted, held, guids = self._prune(path)
        assert deleted == 7
        assert held == 0
        assert [g for g in guids if g.startswith("read")] == []
        assert len([g for g in guids if g.startswith("unread")]) == (
            MIN_ENTRIES_PER_FEED
        )

    def test_a_maximum_below_the_floor_clamps_the_floor(self, tmp_path):
        """An explicit instruction to store at most twenty must not be
        overridden by a default that says fifty."""
        path, _ = self._quiet_feed(tmp_path, 30)
        deleted, held, guids = self._prune(path, cap=20)
        assert deleted == 10
        assert held == 20
        assert len(guids) == 20

    def test_a_maximum_of_zero_leaves_the_floor_alone(self, tmp_path):
        """There is no ceiling to clamp against, so the constant stands."""
        path, _ = self._quiet_feed(tmp_path, MIN_ENTRIES_PER_FEED + 2)
        deleted, _, guids = self._prune(path, cap=0)
        assert deleted == 2
        assert len(guids) == MIN_ENTRIES_PER_FEED

    def test_the_floor_is_per_feed_not_per_database(self, tmp_path):
        path, feed_a = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feed_b = feeds_db.upsert_feed(
                conn, url="https://other.example.com/feed.xml", title=None,
                site_url=None, source_type="rss", category_id=None,
                poll_interval_minutes=30,
            )
            for feed_id in (feed_a, feed_b):
                for i in range(4):
                    _store(conn, feed_id, f"f{feed_id}-{i}",
                           fetched_at=_iso(300 - i), last_seen_at=_iso(200),
                           status="read")
                _mark(conn, feed_id, _iso(1))
            conn.commit()
        deleted, held, guids = self._prune(path, floor=3)
        # One row over the floor on each feed, not one over a shared eight.
        assert deleted == 2
        assert held == 6
        assert len(guids) == 6


class TestTheBudget:
    """``unstarred_budget`` is one expression at two ends, so it gets its own
    tests: the count pass and ``plan_admission`` must agree, and the arithmetic
    is the thing they agree *on*."""

    def test_the_floor_clamps_to_the_maximum_and_zero_disables_it(self):
        assert feeds_db.budget_floor(MIN_ENTRIES_PER_FEED + 5) == (
            MIN_ENTRIES_PER_FEED
        )
        assert feeds_db.budget_floor(20) == 20
        assert feeds_db.budget_floor(1) == 1
        # No ceiling, so no clamp: a floor of fifty under a maximum that does
        # not exist would be a bound nobody asked for.
        assert feeds_db.budget_floor(0) == 0

    def test_stars_come_off_the_total_but_never_below_the_floor(self):
        assert feeds_db.unstarred_budget(5000, 0) == 5000
        assert feeds_db.unstarred_budget(5000, 100) == 4900
        # Floored: without this the budget is zero for good.
        assert feeds_db.unstarred_budget(5000, 4990) == MIN_ENTRIES_PER_FEED
        assert feeds_db.unstarred_budget(5000, 99999) == MIN_ENTRIES_PER_FEED
        # At or below the floor the clamp is the maximum itself.
        assert feeds_db.unstarred_budget(20, 25) == 20
        assert feeds_db.unstarred_budget(1, 1) == 1
        # No maximum: both callers short-circuit before here.
        assert feeds_db.unstarred_budget(0, 3) == 0

    @pytest.mark.parametrize("cap", [0, 3, 20, MIN_ENTRIES_PER_FEED + 5])
    @pytest.mark.parametrize("stars", [0, 2, 60])
    def test_the_count_pass_keeps_exactly_that_many_unstarred_rows(
        self, tmp_path, cap, stars,
    ):
        """The SQL restates the Python expression, so this is what holds them
        equal — and with them the pass and admission."""
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(stars):
                _store(conn, feed_id, f"star{i:03d}", fetched_at=_iso(10 + i),
                       last_seen_at=_iso(1), status="read", starred=True)
            for i in range(70):
                _store(conn, feed_id, f"plain{i:03d}", fetched_at=_iso(100 + i),
                       last_seen_at=_iso(1), status="read")
            conn.commit()
            feeds_db.prune_entries_to_feed_cap(conn, max_entries_per_feed=cap)
            conn.commit()
            kept = [g for g in _guids(conn) if g.startswith("plain")]
        expected = 70 if cap <= 0 else min(
            70, feeds_db.unstarred_budget(cap, stars),
        )
        assert len(kept) == expected
        # The newest by the retention clock, which is `plain000` upward.
        assert kept == [f"plain{i:03d}" for i in range(expected)]


class TestCountPruning:
    def _prune(self, path, cap):
        with feeds_db.connect(path) as conn:
            out = feeds_db.prune_entries_to_feed_cap(
                conn, max_entries_per_feed=cap,
            )
            conn.commit()
            return out, _guids(conn)

    def test_it_ranks_by_recency_alone_and_never_reads_status(self, tmp_path):
        """One ordering, no tiers, and read state is not part of it.

        An earlier draft kept unread ahead of read here. Admission ranks by
        source order, so the two disagreed: a feed near its maximum could have
        in-response *read* rows trimmed while older out-of-response unread rows
        were kept, and the next poll re-admitted the trimmed ones as unread.
        Ranking by recency alone makes this pass delete exactly what admission
        refuses. Unread protection lives in the age pass, which exempts it
        absolutely; this pass is a hard ceiling and nothing else.
        """
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            # The read rows are the newest, so a status tier would keep the
            # unread ones and delete one of these instead.
            _store(conn, feed_id, "read-new", fetched_at=_iso(1),
                   last_seen_at=_iso(1), status="read")
            _store(conn, feed_id, "read-old", fetched_at=_iso(2),
                   last_seen_at=_iso(1), status="read")
            _store(conn, feed_id, "unread-new", fetched_at=_iso(3),
                   last_seen_at=_iso(1), status="unread")
            _store(conn, feed_id, "unread-old", fetched_at=_iso(4),
                   last_seen_at=_iso(1), status="unread")
            conn.commit()
        (deleted, over, excess), guids = self._prune(path, 3)
        assert (deleted, over, excess) == (1, 0, 0)
        # The oldest row goes, though it is unread and two read rows are newer.
        assert guids == ["read-new", "read-old", "unread-new"]

    def test_stars_consume_the_budget_above_the_floor(self, tmp_path):
        """Stars come off the maximum, but only as far as the floor.

        Observable only above ``MIN_ENTRIES_PER_FEED``: at or below it the
        effective floor *is* the maximum, so stars take nothing off the budget
        and the feed goes over the maximum instead.
        """
        cap = MIN_ENTRIES_PER_FEED + 5
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(3):
                _store(conn, feed_id, f"star{i}", fetched_at=_iso(10 + i),
                       last_seen_at=_iso(1), status="read", starred=True)
            for i in range(cap):
                _store(conn, feed_id, f"plain{i:02d}",
                       fetched_at=_iso(100 + i), last_seen_at=_iso(1),
                       status="read")
            conn.commit()
        (deleted, over, excess), guids = self._prune(path, cap)
        # Budget is 55 - 3 stars = 52 unstarred rows, the newest by clock.
        assert deleted == 3
        assert (over, excess) == (0, 0)
        assert len(guids) == cap
        assert "plain51" in guids and "plain52" not in guids

    def test_stars_at_the_maximum_still_leave_a_working_budget(self, tmp_path):
        """The budget is floored at ``min(MIN_ENTRIES_PER_FEED, maximum)``.

        Without that floor this feed's budget is zero *permanently*: every
        unstarred row deleted — unread and in-response alike — and
        ``plan_admission`` admitting nothing ever again, so the feed goes
        silently inert with only ``protected_excess_entries`` hinting at it.
        """
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(7):
                _store(conn, feed_id, f"star{i}", fetched_at=_iso(10 + i),
                       last_seen_at=_iso(1), status="read", starred=True)
            for i in range(8):
                _store(conn, feed_id, f"plain{i}", fetched_at=_iso(20 + i),
                       last_seen_at=_iso(1), status="unread")
            conn.commit()
        (deleted, over, excess), guids = self._prune(path, 5)
        # min(50, 5) unstarred rows survive, the newest five.
        assert deleted == 3
        assert [g for g in guids if g.startswith("plain")] == [
            "plain0", "plain1", "plain2", "plain3", "plain4",
        ]
        assert len([g for g in guids if g.startswith("star")]) == 7
        # Twelve rows against a maximum of five: the overage is reported
        # rather than guessing that a starred row is safe to delete.
        assert (over, excess) == (1, 7)

    def test_stars_alone_can_leave_a_feed_over_the_maximum(self, tmp_path):
        """Above the floor, where the overage is star-driven and nothing else.

        Below it the budget floor contributes to the same number, so this is
        the shape that isolates the cause the reported overage is usually read
        as.
        """
        cap = MIN_ENTRIES_PER_FEED + 5
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(cap + 5):
                _store(conn, feed_id, f"star{i:02d}", fetched_at=_iso(10 + i),
                       last_seen_at=_iso(1), status="read", starred=True)
            conn.commit()
        (deleted, over, excess), guids = self._prune(path, cap)
        assert deleted == 0
        # Reported rather than guessing that a starred row is safe to delete.
        assert (over, excess) == (1, 5)
        assert len(guids) == cap + 5

    def test_a_small_maximum_stands_above_itself_on_stars_and_the_floor(
        self, tmp_path,
    ):
        """At or below the floor the clamp is the maximum itself.

        So stars take nothing off the budget, the unstarred row survives, and
        the feed is over its maximum by both causes at once — which is why the
        reported overage is the plain difference and names neither.
        """
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(5):
                _store(conn, feed_id, f"star{i}", fetched_at=_iso(10 + i),
                       last_seen_at=_iso(1), status="read", starred=True)
            _store(conn, feed_id, "plain", fetched_at=_iso(1),
                   last_seen_at=_iso(1), status="read")
            conn.commit()
        (deleted, over, excess), guids = self._prune(path, 3)
        assert deleted == 0
        assert (over, excess) == (1, 3)
        assert len(guids) == 6

    def test_the_count_pass_ignores_the_floor(self, tmp_path):
        """A feed over its configured maximum is by definition not empty, and
        honouring the floor here would make a maximum below fifty
        unenforceable."""
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(20):
                _store(conn, feed_id, f"e{i:02d}", fetched_at=_iso(1 + i),
                       last_seen_at=_iso(1), status="read")
            conn.commit()
        (deleted, _, _), guids = self._prune(path, 5)
        assert deleted == 15
        assert len(guids) == 5

    def test_a_maximum_of_zero_deletes_nothing(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(10):
                _store(conn, feed_id, f"e{i}", fetched_at=_iso(1 + i),
                       last_seen_at=_iso(1), status="read")
            conn.commit()
        (deleted, over, excess), guids = self._prune(path, 0)
        assert (deleted, over, excess) == (0, 0, 0)
        assert len(guids) == 10

    def test_the_delete_count_is_never_negative(self, tmp_path):
        """``cursor.rowcount`` is ``-1`` for a statement prefixed by ``WITH``
        under this driver, so both helpers read ``SELECT changes()``."""
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            _store(conn, feed_id, "only", fetched_at=_iso(1),
                   last_seen_at=_iso(1), status="read")
            conn.commit()
        (deleted, _, _), _ = self._prune(path, 500)
        assert deleted == 0
        with feeds_db.connect(path) as conn:
            aged, held = feeds_db.prune_entries_by_age(
                conn, before_iso=_iso(90), min_entries_per_feed=50,
                max_entries_per_feed=0,
            )
        assert aged >= 0 and held >= 0


class TestImageCascade:
    def test_deleting_an_entry_takes_its_image_rows_and_spares_the_rest(
        self, tmp_path,
    ):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [
                _entry(feed_id, "goes", _iso(300),
                       image_urls=["https://img.example.com/a.jpg"]),
                _entry(feed_id, "stays", _iso(300),
                       image_urls=["https://img.example.com/b.jpg"]),
            ])
            conn.execute(
                "UPDATE feed_entries SET status='read', last_seen_at=?",
                (_iso(200),),
            )
            conn.execute(
                "UPDATE feed_entries SET last_seen_at=? WHERE guid='stays'",
                (_iso(1),),
            )
            _mark(conn, feed_id, _iso(1))
            _fill_to_floor(conn, feed_id, _iso(1))
            conn.commit()
            before = conn.execute(
                "SELECT COUNT(*) c FROM entry_images"
            ).fetchone()["c"]
            feeds_db.prune_entries_by_age(
                conn, before_iso=_iso(90), min_entries_per_feed=0,
                max_entries_per_feed=0,
            )
            conn.commit()
            after = conn.execute(
                "SELECT COUNT(*) c FROM entry_images"
            ).fetchone()["c"]
            kept = conn.execute(
                "SELECT e.guid FROM entry_images i "
                "JOIN feed_entries e ON e.id = i.entry_id"
            ).fetchall()
        assert before == 2
        assert after == 1
        assert [r["guid"] for r in kept] == ["stays"]


class TestPruneFeeds:
    def _db(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        return path, feed_id, _ctx(tmp_path, path)

    def test_a_naive_clock_is_refused_before_anything_is_touched(self, tmp_path):
        path, feed_id, ctx = self._db(tmp_path)
        with feeds_db.connect(path) as conn:
            _store(conn, feed_id, "old", fetched_at=_iso(300),
                   last_seen_at=_iso(200), status="read")
            _mark(conn, feed_id, _iso(1))
            conn.commit()
        with pytest.raises(ValueError):
            retention.prune_feeds(ctx, now=datetime(2026, 9, 1, 12, 0))
        with feeds_db.connect(path) as conn:
            assert _guids(conn) == ["old"]

    def test_both_passes_run_and_commit_together(self, tmp_path):
        path, feed_id, ctx = self._db(tmp_path)
        with feeds_db.connect(path) as conn:
            # Age candidates: old, read, out of the response.
            for i in range(4):
                _store(conn, feed_id, f"age{i}", fetched_at=_iso(300 + i),
                       last_seen_at=_iso(200), status="read")
            # Count candidates: recent, read, in the response.
            for i in range(6):
                _store(conn, feed_id, f"cap{i}", fetched_at=_iso(1 + i),
                       last_seen_at=_iso(1), status="read")
            _mark(conn, feed_id, _iso(1))
            feeds_db.set_max_entries_per_feed(conn, 4)
            conn.commit()

        result = retention.prune_feeds(ctx, now=NOW)

        assert result.dry_run is False
        assert result.retention_days == DEFAULT_ENTRY_RETENTION_DAYS
        assert result.max_entries_per_feed == 4
        assert result.entries_deleted_by_age == 4
        assert result.entries_deleted_by_cap == 2
        assert result.entries_held_by_floor == 0
        assert result.entry_pruning_deferred_until is None
        assert result.feeds_over_cap_after == 0
        assert result.protected_excess_entries == 0
        assert result.page_size > 0
        with feeds_db.connect(path) as conn:
            assert _guids(conn) == ["cap0", "cap1", "cap2", "cap3"]

    def test_it_reports_the_rows_the_floor_held(self, tmp_path):
        path, feed_id, ctx = self._db(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(10):
                _store(conn, feed_id, f"e{i:02d}", fetched_at=_iso(300 + i),
                       last_seen_at=_iso(200), status="read")
            _mark(conn, feed_id, _iso(1))
            conn.commit()
        result = retention.prune_feeds(ctx, now=NOW)
        # Ten rows against a floor of fifty: nothing goes, and the count is
        # what tells that apart from a feed with nothing to prune.
        assert result.entries_deleted_by_age == 0
        assert result.entries_held_by_floor == 10

    def test_it_counts_the_image_rows_the_cascade_removed(self, tmp_path):
        path, feed_id, ctx = self._db(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(conn, feed_id, [
                _entry(feed_id, "a", _iso(300),
                       image_urls=["https://img.example.com/a.jpg",
                                   "https://img.example.com/b.jpg"]),
            ])
            conn.execute(
                "UPDATE feed_entries SET status='read', last_seen_at=?",
                (_iso(200),),
            )
            _mark(conn, feed_id, _iso(1))
            _fill_to_floor(conn, feed_id, _iso(1))
            conn.commit()
        result = retention.prune_feeds(ctx, now=NOW)
        assert result.entries_deleted_by_age == 1
        assert result.images_deleted_by_cascade == 2

    def test_a_dry_run_plans_the_same_counts_and_changes_nothing(self, tmp_path):
        path, feed_id, ctx = self._db(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(4):
                _store(conn, feed_id, f"age{i}", fetched_at=_iso(300 + i),
                       last_seen_at=_iso(200), status="read")
            for i in range(6):
                _store(conn, feed_id, f"cap{i}", fetched_at=_iso(1 + i),
                       last_seen_at=_iso(1), status="read")
            _mark(conn, feed_id, _iso(1))
            feeds_db.set_max_entries_per_feed(conn, 4)
            conn.execute(
                "UPDATE feeds SET poll_claimed_until = ?", (_iso(-1),),
            )
            conn.commit()

        planned = retention.prune_feeds(ctx, dry_run=True, now=NOW)
        real = retention.prune_feeds(ctx, now=NOW)

        assert planned.dry_run is True
        assert planned.entries_deleted_by_age == real.entries_deleted_by_age
        assert planned.entries_deleted_by_cap == real.entries_deleted_by_cap

    def test_a_dry_run_leaves_every_row_claim_and_setting_alone(self, tmp_path):
        path, feed_id, ctx = self._db(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(4):
                _store(conn, feed_id, f"age{i}", fetched_at=_iso(300 + i),
                       last_seen_at=_iso(200), status="read")
            for i in range(6):
                _store(conn, feed_id, f"cap{i}", fetched_at=_iso(1 + i),
                       last_seen_at=_iso(1), status="read")
            _mark(conn, feed_id, _iso(1))
            feeds_db.set_max_entries_per_feed(conn, 4)
            conn.execute(
                "UPDATE feeds SET poll_claimed_until = ?",
                ("2026-09-01T12:05:00+00:00",),
            )
            conn.commit()

        result = retention.prune_feeds(ctx, dry_run=True, now=NOW)

        assert result.entries_deleted_by_age == 4
        assert result.entries_deleted_by_cap == 2
        with feeds_db.connect(path) as conn:
            assert len(_guids(conn)) == 10
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.poll_claimed_until == "2026-09-01T12:05:00+00:00"
        assert feed.last_items_seen_at == _iso(1)

    def test_a_failing_second_pass_rolls_the_first_one_back(
        self, tmp_path, monkeypatch,
    ):
        """The feed is lifted over the floor first, or the age pass deletes
        nothing and the assertion holds whether or not the rollback runs."""
        def _seed(target):
            path, feed_id, ctx = self._db(target)
            with feeds_db.connect(path) as conn:
                for i in range(4):
                    _store(conn, feed_id, f"age{i}", fetched_at=_iso(300 + i),
                           last_seen_at=_iso(200), status="read")
                _mark(conn, feed_id, _iso(1))
                _fill_to_floor(conn, feed_id, _iso(1))
                conn.commit()
            return path, ctx

        control_path, control_ctx = _seed(tmp_path / "control")
        control = retention.prune_feeds(control_ctx, now=NOW)
        # Without the raise, this fixture really does lose its four old rows.
        assert control.entries_deleted_by_age == 4

        path, ctx = _seed(tmp_path / "rolled-back")
        seeded = MIN_ENTRIES_PER_FEED + 4

        def _boom(conn, **kwargs):
            raise sqlite3.OperationalError("no such table: nope")

        monkeypatch.setattr(feeds_db, "prune_entries_to_feed_cap", _boom)
        with pytest.raises(sqlite3.OperationalError):
            retention.prune_feeds(ctx, now=NOW)
        with feeds_db.connect(path) as conn:
            assert len(_guids(conn)) == seeded
        assert control_path != path

    def test_age_retention_of_zero_skips_only_the_age_pass(self, tmp_path):
        path, feed_id, ctx = self._db(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(4):
                _store(conn, feed_id, f"age{i}", fetched_at=_iso(300 + i),
                       last_seen_at=_iso(200), status="read")
            feeds_db.set_entry_retention_days(conn, 0)
            feeds_db.set_max_entries_per_feed(conn, 2)
            _mark(conn, feed_id, _iso(1))
            conn.commit()
        result = retention.prune_feeds(ctx, now=NOW)
        assert result.entries_deleted_by_age == 0
        assert result.entries_deleted_by_cap == 2

    def test_both_limits_off_deletes_nothing(self, tmp_path):
        path, feed_id, ctx = self._db(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(4):
                _store(conn, feed_id, f"age{i}", fetched_at=_iso(300 + i),
                       last_seen_at=_iso(200), status="read")
            feeds_db.set_entry_retention_days(conn, 0)
            feeds_db.set_max_entries_per_feed(conn, 0)
            _mark(conn, feed_id, _iso(1))
            conn.commit()
        result = retention.prune_feeds(ctx, now=NOW)
        assert (result.entries_deleted_by_age, result.entries_deleted_by_cap) == (0, 0)
        with feeds_db.connect(path) as conn:
            assert len(_guids(conn)) == 4


class TestUpgradeGrace:
    def _deferred_db(self, tmp_path, not_before):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            for i in range(4):
                _store(conn, feed_id, f"age{i}", fetched_at=_iso(300 + i),
                       last_seen_at=_iso(200), status="read")
            for i in range(6):
                _store(conn, feed_id, f"cap{i}", fetched_at=_iso(1 + i),
                       last_seen_at=_iso(1), status="read")
            _mark(conn, feed_id, _iso(1))
            feeds_db.set_max_entries_per_feed(conn, 4)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                (feeds_db.ENTRY_PRUNE_NOT_BEFORE_KEY, not_before),
            )
            conn.commit()
        return path, _ctx(tmp_path, path)

    def test_before_the_boundary_both_passes_are_blocked(self, tmp_path):
        not_before = (NOW + timedelta(seconds=1)).isoformat()
        path, ctx = self._deferred_db(tmp_path, not_before)
        result = retention.prune_feeds(ctx, now=NOW)
        assert result.entry_pruning_deferred_until == not_before
        assert result.entries_deleted_by_age == 0
        assert result.entries_deleted_by_cap == 0
        assert result.images_deleted_by_cascade == 0
        with feeds_db.connect(path) as conn:
            assert len(_guids(conn)) == 10
            keys = {r["key"] for r in conn.execute("SELECT key FROM schema_meta")}
        assert feeds_db.ENTRY_PRUNE_NOT_BEFORE_KEY in keys

    def test_at_the_boundary_both_passes_run_and_the_row_goes(self, tmp_path):
        path, ctx = self._deferred_db(tmp_path, NOW.isoformat())
        result = retention.prune_feeds(ctx, now=NOW)
        assert result.entry_pruning_deferred_until is None
        assert result.entries_deleted_by_age == 4
        assert result.entries_deleted_by_cap == 2
        with feeds_db.connect(path) as conn:
            keys = {r["key"] for r in conn.execute("SELECT key FROM schema_meta")}
        assert feeds_db.ENTRY_PRUNE_NOT_BEFORE_KEY not in keys

    def test_a_dry_run_at_the_boundary_leaves_the_grace_row_in_place(
        self, tmp_path,
    ):
        path, ctx = self._deferred_db(tmp_path, NOW.isoformat())
        retention.prune_feeds(ctx, dry_run=True, now=NOW)
        with feeds_db.connect(path) as conn:
            keys = {r["key"] for r in conn.execute("SELECT key FROM schema_meta")}
            assert len(_guids(conn)) == 10
        assert feeds_db.ENTRY_PRUNE_NOT_BEFORE_KEY in keys

    def test_a_run_with_both_limits_off_does_not_spend_the_grace(self, tmp_path):
        """Nothing could have been deleted, so the safety period is not used up.

        Otherwise a user who turns retention on after the first post-upgrade
        prune gets no grace at all.
        """
        path, ctx = self._deferred_db(tmp_path, NOW.isoformat())
        with feeds_db.connect(path) as conn:
            feeds_db.set_entry_retention_days(conn, 0)
            feeds_db.set_max_entries_per_feed(conn, 0)
            conn.commit()

        result = retention.prune_feeds(ctx, now=NOW)

        assert (result.entries_deleted_by_age, result.entries_deleted_by_cap) == (0, 0)
        with feeds_db.connect(path) as conn:
            keys = {r["key"] for r in conn.execute("SELECT key FROM schema_meta")}
        assert feeds_db.ENTRY_PRUNE_NOT_BEFORE_KEY in keys

    def test_a_malformed_grace_timestamp_fails_closed(self, tmp_path):
        path, ctx = self._deferred_db(tmp_path, "not-a-timestamp")
        with pytest.raises(ValueError):
            retention.prune_feeds(ctx, now=NOW)
        with feeds_db.connect(path) as conn:
            assert len(_guids(conn)) == 10


class TestOneBatchOrdering:
    """Every entry of one poll carries that poll's single ``fetched_at``.

    So a whole response ties on the retention clock and the tie-break decides
    all of it. Every other test in this file gives each row a distinct
    ``fetched_at``, which is the one shape a real poll never produces.
    """

    def _batch(self, path, feed_id, guids, when):
        from istota.feeds import poller

        with feeds_db.connect(path) as conn:
            feed = feeds_db.list_feeds(conn)[0]
            poller._persist_poll(
                conn, feed,
                FetchResult(
                    feed_url=feed.url,
                    items=[FetchedItem(guid=g, title=g) for g in guids],
                ),
                now=when, backoff_max_minutes=60, jitter_fraction=0.0,
            )

    def test_the_count_pass_keeps_the_head_of_the_response(self, tmp_path):
        """The head is what ``plan_admission`` keeps, so it is what survives.

        Under the opposite tie-break this pass kept the *tail* of the batch and
        deleted its head — precisely the entries the next response hands back,
        which is churn every poll rather than a one-off trim.
        """
        path, feed_id = _seed_one_feed(tmp_path)
        self._batch(path, feed_id, [f"n{i:02d}" for i in range(1, 21)],
                    NOW - timedelta(days=1))
        with feeds_db.connect(path) as conn:
            deleted, _, _ = feeds_db.prune_entries_to_feed_cap(
                conn, max_entries_per_feed=15,
            )
            conn.commit()
            kept = _guids(conn)
        assert deleted == 5
        assert kept == [f"n{i:02d}" for i in range(1, 16)]

    def test_the_count_tie_break_does_not_read_status(self, tmp_path):
        """The head survives whatever each row's read state.

        The tie-break decides the whole of a real poll, since every entry of
        one response shares that poll's ``fetched_at``. A status tier laid over
        it kept the unread tail and deleted the read head — which is exactly
        what the next response hands back, as unread, every poll.
        """
        path, feed_id = _seed_one_feed(tmp_path)
        self._batch(path, feed_id, [f"n{i:02d}" for i in range(1, 21)],
                    NOW - timedelta(days=1))
        with feeds_db.connect(path) as conn:
            conn.execute(
                "UPDATE feed_entries SET status = 'read' WHERE guid <= 'n10'"
            )
            deleted, _, _ = feeds_db.prune_entries_to_feed_cap(
                conn, max_entries_per_feed=15,
            )
            conn.commit()
            kept = _guids(conn)
        assert deleted == 5
        # The response's head, read rows and all; its tail goes, unread rows
        # and all — which is what `plan_admission` refuses next time.
        assert kept == [f"n{i:02d}" for i in range(1, 16)]

    def test_the_age_floor_holds_the_head_of_the_response(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        self._batch(path, feed_id, [f"p{i:02d}" for i in range(60)],
                    NOW - timedelta(days=300))
        with feeds_db.connect(path) as conn:
            conn.execute("UPDATE feed_entries SET status = 'read'")
            _mark(conn, feed_id, _iso(1))
            conn.commit()
            deleted, held = feeds_db.prune_entries_by_age(
                conn, before_iso=_iso(90),
                min_entries_per_feed=MIN_ENTRIES_PER_FEED,
                max_entries_per_feed=0,
            )
            conn.commit()
            kept = _guids(conn)
        assert (deleted, held) == (10, MIN_ENTRIES_PER_FEED)
        # The floor keeps the fifty the response listed first, not its tail.
        assert kept == [f"p{i:02d}" for i in range(MIN_ENTRIES_PER_FEED)]


class TestTheChurnControl:
    """The point of the whole design: a prune can never fight a poll.

    ``_persist_poll`` is driven directly with a hand-built ``FetchResult``, so
    the control is about what the storage layer does with a response rather
    than about parsing one.
    """

    def _feed(self, path):
        with feeds_db.connect(path) as conn:
            return feeds_db.list_feeds(conn)[0]

    def _persist(self, path, items, when):
        from istota.feeds import poller

        with feeds_db.connect(path) as conn:
            feed = feeds_db.list_feeds(conn)[0]
            poller._persist_poll(
                conn, feed,
                FetchResult(feed_url=feed.url, items=items),
                now=when, backoff_max_minutes=60, jitter_fraction=0.0,
            )

    def test_a_pruned_entry_is_not_resurrected_by_the_same_response(
        self, tmp_path,
    ):
        path, feed_id = _seed_one_feed(tmp_path)
        ctx = _ctx(tmp_path, path)
        # Two polls: the first brings in an entry the second no longer offers.
        self._persist(path, [FetchedItem(guid="gone", title="Gone")],
                      NOW - timedelta(days=300))
        self._persist(path, [FetchedItem(guid="current", title="Current")],
                      NOW - timedelta(days=1))
        with feeds_db.connect(path) as conn:
            conn.execute("UPDATE feed_entries SET status = 'read'")
            marker = feeds_db.list_feeds(conn)[0].last_items_seen_at
            _fill_to_floor(conn, feed_id, marker)
            conn.commit()

        result = retention.prune_feeds(ctx, now=NOW)
        assert result.entries_deleted_by_age == 1
        assert result.entries_deleted_by_cap == 0
        with feeds_db.connect(path) as conn:
            assert "gone" not in _guids(conn)
            assert "current" in _guids(conn)

        # Now re-run the response that is still live. Nothing it returned was
        # deleted, so nothing comes back and nothing flips to unread.
        self._persist(path, [FetchedItem(guid="current", title="Current")],
                      NOW + timedelta(minutes=5))
        with feeds_db.connect(path) as conn:
            rows = conn.execute(
                "SELECT guid, status FROM feed_entries WHERE guid IN "
                "('gone', 'current')"
            ).fetchall()
        assert [r["guid"] for r in rows] == ["current"]
        assert [r["status"] for r in rows] == ["read"]

    def test_the_count_pass_deletes_only_what_admission_will_refuse(
        self, tmp_path,
    ):
        """The count pass's own churn control, and the reason it needs no
        most-recent-response clause of its own.

        A response larger than the maximum is the shape that exposes it: the
        pass must delete exactly the rows the next admission will refuse, or
        the two take turns and read entries come back unread every poll.
        """
        from istota.feeds import poller

        path, feed_id = _seed_one_feed(tmp_path)
        ctx = _ctx(tmp_path, path)
        with feeds_db.connect(path) as conn:
            feeds_db.set_max_entries_per_feed(conn, 20)
            conn.commit()

        def _poll(guids, when):
            with feeds_db.connect(path) as conn:
                feed = feeds_db.list_feeds(conn)[0]
                poller._persist_poll(
                    conn, feed,
                    FetchResult(
                        feed_url=feed.url,
                        items=[FetchedItem(guid=g, title=g) for g in guids],
                    ),
                    now=when, backoff_max_minutes=60, jitter_fraction=0.0,
                )

        page_one = [f"n{i:02d}" for i in range(1, 26)]
        _poll(page_one, NOW - timedelta(days=2))
        with feeds_db.connect(path) as conn:
            conn.execute("UPDATE feed_entries SET status = 'read'")
            conn.commit()
            assert _guids(conn) == [f"n{i:02d}" for i in range(1, 21)]

        # Three new items at the head push the response's tail out of the
        # admission window.
        page_two = [f"m{i:02d}" for i in range(1, 4)] + page_one
        _poll(page_two, NOW - timedelta(days=1))
        with feeds_db.connect(path) as conn:
            rows = {
                r["guid"]: r["last_seen_at"]
                for r in conn.execute("SELECT guid, last_seen_at FROM feed_entries")
            }
            marker = feeds_db.list_feeds(conn)[0].last_items_seen_at
        # Returned but not admitted, and still stamped: the source is handing
        # them over, so they are not history whatever the budget said.
        assert rows["n18"] == rows["n19"] == rows["n20"] == marker

        result = retention.prune_feeds(ctx, now=NOW)
        assert result.entries_deleted_by_cap == 3
        with feeds_db.connect(path) as conn:
            survivors = _guids(conn)
        # The three that went are the three the next admission refuses.
        assert [g for g in ("n18", "n19", "n20") if g in survivors] == []
        assert {"m01", "m02", "m03", "n01", "n17"} <= set(survivors)

        with feeds_db.connect(path) as conn:
            before = {
                r["guid"]: r["status"]
                for r in conn.execute("SELECT guid, status FROM feed_entries")
            }
        _poll(page_two, NOW + timedelta(minutes=5))
        with feeds_db.connect(path) as conn:
            after = {
                r["guid"]: r["status"]
                for r in conn.execute("SELECT guid, status FROM feed_entries")
            }
        # Nothing came back and nothing flipped. The three `m` rows were new in
        # the second poll and were never read, so the comparison is against the
        # state before the re-poll rather than a blanket "everything is read".
        assert after == before
        assert sorted(after) == sorted(survivors)
        assert {g: after[g] for g in ("n01", "n17")} == {
            "n01": "read", "n17": "read",
        }

    def test_a_maximum_near_the_window_size_does_not_churn_read_rows(
        self, tmp_path,
    ):
        """The case that removed the count pass's status tier.

        A user lowers the maximum toward a feed's own window size and reads
        the older half of it. Under a tier the pass ranked those read rows
        last and trimmed them, while admission — which ranks by source order —
        kept them and dropped the newer unread tail instead. So the next poll
        re-admitted what the prune had just deleted, as unread, for good.

        The whole feed shares two poll timestamps here, which is what a real
        poll produces and what makes the tie-break decisive.
        """
        path, feed_id = _seed_one_feed(tmp_path)
        ctx = _ctx(tmp_path, path)
        with feeds_db.connect(path) as conn:
            feeds_db.set_max_entries_per_feed(conn, 20)
            conn.commit()

        page_one = [f"n{i:02d}" for i in range(1, 26)]
        self._persist(path, [FetchedItem(guid=g, title=g) for g in page_one],
                      NOW - timedelta(days=2))
        with feeds_db.connect(path) as conn:
            # The head of the window has been read; its tail has not.
            conn.execute(
                "UPDATE feed_entries SET status = 'read' WHERE guid <= 'n15'"
            )
            conn.commit()
            assert _guids(conn) == [f"n{i:02d}" for i in range(1, 21)]

        # Three new items at the head push the stored tail out of the window.
        page_two = [f"m{i:02d}" for i in range(1, 4)] + page_one
        self._persist(path, [FetchedItem(guid=g, title=g) for g in page_two],
                      NOW - timedelta(days=1))

        result = retention.prune_feeds(ctx, now=NOW)

        assert result.entries_deleted_by_age == 0
        assert result.entries_deleted_by_cap == 3
        with feeds_db.connect(path) as conn:
            before = {
                r["guid"]: r["status"]
                for r in conn.execute("SELECT guid, status FROM feed_entries")
            }
        # The three that went are the three the next admission refuses — the
        # unread tail, not the read head. That is the stated cost of a ceiling
        # that does not read status, and the reason it cannot churn.
        assert [g for g in ("n18", "n19", "n20") if g in before] == []
        assert {"m01", "m02", "m03", "n01", "n15", "n17"} <= set(before)
        assert before["n01"] == "read" and before["n17"] == "unread"

        self._persist(path, [FetchedItem(guid=g, title=g) for g in page_two],
                      NOW + timedelta(minutes=5))
        with feeds_db.connect(path) as conn:
            after = {
                r["guid"]: r["status"]
                for r in conn.execute("SELECT guid, status FROM feed_entries")
            }
        # Nothing came back and nothing flipped to unread.
        assert after == before

    def test_an_entry_the_latest_response_returned_is_never_age_deleted(
        self, tmp_path,
    ):
        """The mirror image, and the reason the clause is phrased on the
        response rather than on the entry's age.

        This entry is 300 days old on the retention clock and has been read
        the whole time; the source still hands it over, so it stays.
        """
        path, feed_id = _seed_one_feed(tmp_path)
        ctx = _ctx(tmp_path, path)
        self._persist(path, [FetchedItem(guid="archival", title="Archival")],
                      NOW - timedelta(days=300))
        with feeds_db.connect(path) as conn:
            conn.execute("UPDATE feed_entries SET status = 'read'")
            conn.commit()
        # An archive-shaped source: the same block is in the newest response.
        self._persist(path, [FetchedItem(guid="archival", title="Archival")],
                      NOW - timedelta(minutes=5))
        with feeds_db.connect(path) as conn:
            marker = feeds_db.list_feeds(conn)[0].last_items_seen_at
            _fill_to_floor(conn, feed_id, marker)
            conn.commit()

        result = retention.prune_feeds(ctx, now=NOW)

        assert result.entries_deleted_by_age == 0
        # Nothing was held back by the floor either, so the response clause is
        # the only thing that can have saved it. Its twin above, at the same
        # rank and the same age, is deleted.
        assert result.entries_held_by_floor == 0
        with feeds_db.connect(path) as conn:
            row = conn.execute(
                "SELECT guid, fetched_at, status FROM feed_entries "
                "WHERE guid = 'archival'"
            ).fetchone()
        assert row["guid"] == "archival"
        # The refresh left the retention clock where it was, so the entry is
        # protected by the response rather than by looking new.
        assert row["fetched_at"] == (NOW - timedelta(days=300)).isoformat()
        assert row["status"] == "read"


class TestAStarredFeedKeepsWorking:
    """``starred_count >= max_entries_per_feed`` used to zero the budget.

    Permanently: the count pass deleted every unstarred row, unread and
    in-response alike, and ``plan_admission`` then admitted nothing ever again,
    so the feed stopped storing anything with only ``protected_excess_entries``
    hinting at it. Stars already sit outside the ceiling by design, so
    reserving room beneath them breaks no promise the setting makes — a feed
    over its maximum because of stars is over it either way, and the difference
    is only whether it still works.
    """

    CAP = 5

    def _feed(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.set_max_entries_per_feed(conn, self.CAP)
            for i in range(7):
                _store(conn, feed_id, f"star{i}", fetched_at=_iso(10 + i),
                       last_seen_at=_iso(1), status="read", starred=True)
            for i in range(8):
                _store(conn, feed_id, f"plain{i}", fetched_at=_iso(20 + i),
                       last_seen_at=_iso(1), status="read")
            conn.commit()
        return path, _ctx(tmp_path, path)

    def _poll(self, path, guids, when):
        from istota.feeds import poller

        with feeds_db.connect(path) as conn:
            feed = feeds_db.list_feeds(conn)[0]
            poller._persist_poll(
                conn, feed,
                FetchResult(
                    feed_url=feed.url,
                    items=[FetchedItem(guid=g, title=g) for g in guids],
                ),
                now=when, backoff_max_minutes=60, jitter_fraction=0.0,
            )

    def test_it_keeps_a_floored_budget_and_goes_on_admitting(self, tmp_path):
        path, ctx = self._feed(tmp_path)

        result = retention.prune_feeds(ctx, now=NOW)

        assert result.entries_deleted_by_cap == 3
        with feeds_db.connect(path) as conn:
            survivors = _guids(conn)
        # min(MIN_ENTRIES_PER_FEED, 5) unstarred rows survive, not none.
        assert [g for g in survivors if g.startswith("plain")] == [
            "plain0", "plain1", "plain2", "plain3", "plain4",
        ]

        self._poll(path, ["fresh0", "fresh1"], NOW + timedelta(minutes=5))
        with feeds_db.connect(path) as conn:
            after = _guids(conn)
        assert {"fresh0", "fresh1"} <= set(after)

    def test_without_the_floor_the_same_feed_stores_nothing(
        self, tmp_path, monkeypatch,
    ):
        """The control, and the reason the test above is not vacuous.

        ``MIN_ENTRIES_PER_FEED`` reaches the maximum through one function,
        ``feeds_db.budget_floor``, which both the count pass and
        ``plan_admission`` go through, so zeroing the constant here reproduces
        the pre-floor behaviour exactly: every unstarred row deleted, and
        nothing admitted afterwards.
        """
        monkeypatch.setattr(feeds_db, "MIN_ENTRIES_PER_FEED", 0)
        path, ctx = self._feed(tmp_path)

        result = retention.prune_feeds(ctx, now=NOW)

        assert result.entries_deleted_by_cap == 8
        self._poll(path, ["fresh0", "fresh1"], NOW + timedelta(minutes=5))
        with feeds_db.connect(path) as conn:
            after = _guids(conn)
        assert [g for g in after if not g.startswith("star")] == []
