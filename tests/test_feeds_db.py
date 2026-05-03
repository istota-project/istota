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
