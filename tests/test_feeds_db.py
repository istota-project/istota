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
        assert row["value"] == "1"


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
