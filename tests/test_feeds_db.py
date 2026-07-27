"""Tests for the native feeds SQLite layer."""

from datetime import datetime, timedelta, timezone

from istota.feeds import db as feeds_db
from istota.feeds.models import EntryRecord


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
