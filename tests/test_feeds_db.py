"""Tests for the native feeds SQLite layer."""

import sqlite3

from datetime import datetime, timedelta, timezone

import pytest

from istota.feeds import db as feeds_db
from istota.feeds.models import (
    DEFAULT_ENTRY_RETENTION_DAYS,
    DEFAULT_MAX_ENTRIES_PER_FEED,
    POLL_CLAIM_SECONDS,
    EntryRecord,
)


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
        conn.execute("ALTER TABLE feed_entries DROP COLUMN last_seen_at")
        conn.execute("ALTER TABLE feed_entries DROP COLUMN document_rank")
        conn.execute("ALTER TABLE feeds DROP COLUMN current_document_at")
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

    def test_migration_adds_columns_and_the_partial_index(self, tmp_path):
        path, _ = self._v7_db_with_history(tmp_path)

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            entry_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")
            }
            feed_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
            index = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_entries_feed_last_seen_unstarred",),
            ).fetchone()
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()["value"]

        assert {"last_seen_at", "document_rank"} <= entry_cols
        assert {"current_document_at", "poll_claimed_until"} <= feed_cols
        assert index is not None and "starred = 0" in index["sql"]
        assert version == "8"

    def test_migration_stamps_one_observation_time_and_no_ranks(self, tmp_path):
        path, _ = self._v7_db_with_history(tmp_path)

        feeds_db.init_db(path)

        with feeds_db.connect(path) as conn:
            rows = conn.execute(
                "SELECT guid, last_seen_at, document_rank, fetched_at "
                "FROM feed_entries ORDER BY guid"
            ).fetchall()
        assert [r["guid"] for r in rows] == ["old", "older"]
        stamps = {r["last_seen_at"] for r in rows}
        assert len(stamps) == 1 and None not in stamps
        assert all(r["document_rank"] is None for r in rows)
        # First-fetch ordering is untouched by the migration.
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
        assert feed.current_document_at is None
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

    def test_rerunning_init_db_preserves_overrides_ranks_and_grace(self, tmp_path):
        path, feed_id = self._v7_db_with_history(tmp_path)
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                (feeds_db.MAX_ENTRIES_PER_FEED_KEY, "10"),
            )
            conn.execute("UPDATE feed_entries SET document_rank = 3 WHERE guid='old'")
            conn.execute(
                "UPDATE feeds SET current_document_at = ? WHERE id = ?",
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
            ranks = {
                r["guid"]: r["document_rank"]
                for r in conn.execute("SELECT guid, document_rank FROM feed_entries")
            }
            stamps_after = [
                r["last_seen_at"]
                for r in conn.execute("SELECT last_seen_at FROM feed_entries")
            ]
            feed = feeds_db.list_feeds(conn)[0]
        assert after == before
        assert ranks["old"] == 3
        assert stamps_after == stamps_before
        assert feed.current_document_at == "2026-09-01T00:00:00+00:00"

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
                "UPDATE feed_entries SET last_seen_at = ?, document_rank = 2 "
                "WHERE guid = 'old'",
                ("2026-09-01T00:00:00+00:00",),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                (feeds_db.MAX_ENTRIES_PER_FEED_KEY, "10"),
            )
            conn.execute(
                "UPDATE feeds SET current_document_at = ? WHERE id = ?",
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
                "SELECT last_seen_at, document_rank FROM feed_entries "
                "WHERE guid='old'"
            ).fetchone()
            meta = {
                r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM schema_meta")
            }
            feed = feeds_db.list_feeds(conn)[0]
        # A real observation is never overwritten by the migration clock, and
        # neither the grace deadline nor a user override is moved.
        assert row["last_seen_at"] == "2026-09-01T00:00:00+00:00"
        assert row["document_rank"] == 2
        assert meta[feeds_db.ENTRY_PRUNE_NOT_BEFORE_KEY] == grace_before
        assert meta[feeds_db.MAX_ENTRIES_PER_FEED_KEY] == "10"
        # The snapshot marker is a feed's own state, not the migration's.
        assert feed.current_document_at == "2026-09-01T00:00:00+00:00"

    def test_a_fresh_database_has_the_columns_and_no_grace_row(self, tmp_path):
        path, _ = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            entry_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")
            }
            feed_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
            index = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_entries_feed_last_seen_unstarred",),
            ).fetchone()
            keys = {r["key"] for r in conn.execute("SELECT key FROM schema_meta")}
        assert {"last_seen_at", "document_rank"} <= entry_cols
        assert {"current_document_at", "poll_claimed_until"} <= feed_cols
        assert index is not None
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
    def test_an_insert_stamps_the_observation_and_leaves_the_rank_null(
        self, tmp_path,
    ):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(
                conn, feed_id, [_entry(feed_id, "a", "2026-09-01T12:00:00+00:00")],
            )
            row = conn.execute(
                "SELECT last_seen_at, document_rank FROM feed_entries WHERE guid='a'"
            ).fetchone()
        assert row["last_seen_at"] == "2026-09-01T12:00:00+00:00"
        assert row["document_rank"] is None

    def test_document_ranks_are_written_when_supplied(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(
                conn, feed_id,
                [
                    _entry(feed_id, "a", "2026-09-01T12:00:00+00:00"),
                    _entry(feed_id, "b", "2026-09-01T12:00:00+00:00"),
                ],
                document_ranks={"a": 0, "b": 1},
            )
            ranks = {
                r["guid"]: r["document_rank"]
                for r in conn.execute("SELECT guid, document_rank FROM feed_entries")
            }
        assert ranks == {"a": 0, "b": 1}

    def test_a_refresh_advances_the_observation_and_holds_the_rank(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(
                conn, feed_id,
                [_entry(feed_id, "a", "2026-09-01T12:00:00+00:00")],
                document_ranks={"a": 4},
            )
            conn.execute("UPDATE feed_entries SET status = 'read' WHERE guid='a'")
            # An incomplete response: the entry is observed again, but nothing
            # about the feed's window is being asserted, so the rank stands.
            feeds_db.insert_entries(
                conn, feed_id,
                [_entry(
                    feed_id, "a", "2026-09-02T12:00:00+00:00",
                    content_html="<p>fuller</p>",
                )],
            )
            row = conn.execute(
                "SELECT last_seen_at, document_rank, fetched_at, status, "
                "content_html FROM feed_entries WHERE guid='a'"
            ).fetchone()
        assert row["last_seen_at"] == "2026-09-02T12:00:00+00:00"
        assert row["document_rank"] == 4
        # First-fetch time and user state are untouched, as before.
        assert row["fetched_at"] == "2026-09-01T12:00:00+00:00"
        assert row["status"] == "read"
        assert row["content_html"] == "<p>fuller</p>"

    def test_a_later_complete_response_replaces_the_rank(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(
                conn, feed_id,
                [_entry(feed_id, "a", "2026-09-01T12:00:00+00:00")],
                document_ranks={"a": 4},
            )
            feeds_db.insert_entries(
                conn, feed_id,
                [_entry(feed_id, "a", "2026-09-02T12:00:00+00:00")],
                document_ranks={"a": 0},
            )
            row = conn.execute(
                "SELECT document_rank FROM feed_entries WHERE guid='a'"
            ).fetchone()
        assert row["document_rank"] == 0

    def test_a_partial_rank_map_does_not_blank_the_guids_it_omits(self, tmp_path):
        """The gate is the rank, not the presence of a mapping.

        Today's only caller builds the map from exactly the items it passes,
        so the two readings agree there. A direct caller is the reader this
        function's race branch is also written for, and for that one a missing
        guid must leave the stored rank standing rather than erasing it.
        """
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            feeds_db.insert_entries(
                conn, feed_id,
                [
                    _entry(feed_id, "a", "2026-09-01T12:00:00+00:00"),
                    _entry(feed_id, "b", "2026-09-01T12:00:00+00:00"),
                ],
                document_ranks={"a": 0, "b": 1},
            )
            feeds_db.insert_entries(
                conn, feed_id,
                [
                    _entry(feed_id, "a", "2026-09-02T12:00:00+00:00"),
                    _entry(feed_id, "b", "2026-09-02T12:00:00+00:00"),
                ],
                document_ranks={"a": 5},
            )
            ranks = {
                r["guid"]: r["document_rank"]
                for r in conn.execute("SELECT guid, document_rank FROM feed_entries")
            }
        assert ranks == {"a": 5, "b": 1}

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
                document_ranks={"a": 0},
            )
            row = conn.execute(
                "SELECT last_seen_at, document_rank, content_html, fetched_at "
                "FROM feed_entries WHERE guid='a'"
            ).fetchone()
        # The row was not ours to insert, so it is not counted as new.
        assert inserted == 0
        assert row["last_seen_at"] == "2026-09-01T12:00:00+00:00"
        assert row["document_rank"] == 0
        assert row["content_html"] == "<p>ours</p>"
        # The winner's first-fetch time stands.
        assert row["fetched_at"] == "2026-08-01T00:00:00+00:00"


class TestFetchStateSnapshotFields:
    def _state_kwargs(self):
        return dict(
            etag=None, last_modified=None,
            last_fetched_at="2026-09-01T12:00:00+00:00",
            last_error=None, error_count=0,
            next_poll_at="2026-09-01T12:30:00+00:00",
        )

    def test_omitted_snapshot_arguments_leave_both_columns_alone(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            conn.execute(
                "UPDATE feeds SET current_document_at = ?, poll_claimed_until = ? "
                "WHERE id = ?",
                ("2026-08-01T00:00:00+00:00", "2026-09-01T12:05:00+00:00", feed_id),
            )
            feeds_db.update_feed_fetch_state(conn, feed_id, **self._state_kwargs())
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.current_document_at == "2026-08-01T00:00:00+00:00"
        assert feed.poll_claimed_until == "2026-09-01T12:05:00+00:00"

    def test_the_snapshot_marker_and_claim_can_be_written(self, tmp_path):
        path, feed_id = _seed_one_feed(tmp_path)
        with feeds_db.connect(path) as conn:
            conn.execute(
                "UPDATE feeds SET poll_claimed_until = ? WHERE id = ?",
                ("2026-09-01T12:05:00+00:00", feed_id),
            )
            feeds_db.update_feed_fetch_state(
                conn, feed_id,
                current_document_at="2026-09-01T12:00:00+00:00",
                poll_claimed_until=None,
                **self._state_kwargs(),
            )
            feed = feeds_db.list_feeds(conn)[0]
        assert feed.current_document_at == "2026-09-01T12:00:00+00:00"
        assert feed.poll_claimed_until is None
