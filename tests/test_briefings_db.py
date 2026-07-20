"""Tests for the briefings module SQLite layer."""

from datetime import datetime, timedelta, timezone

import pytest

from istota.briefings import db as bdb


class TestInitDb:
    def test_creates_tables(self, tmp_path):
        path = tmp_path / "briefings.db"
        bdb.init_db(path)
        assert path.exists()
        with bdb.connect(path) as conn:
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        # Content model + archive + Phase 2 substrate (empty but present) + meta.
        assert {
            "briefing_blocks",
            "briefing_block_sources",
            "briefing_archive",
            "briefing_items",
            "briefing_item_state",
            "schema_meta",
        } <= tables

    def test_idempotent(self, tmp_path):
        path = tmp_path / "briefings.db"
        bdb.init_db(path)
        bdb.init_db(path)  # must not raise

    def test_wal_mode(self, tmp_path):
        path = tmp_path / "briefings.db"
        bdb.init_db(path)
        with bdb.connect(path) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_records_schema_version(self, tmp_path):
        path = tmp_path / "briefings.db"
        bdb.init_db(path)
        with bdb.connect(path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
        assert row["value"] == str(bdb.SCHEMA_VERSION)

    def test_phase2_item_tables_empty(self, tmp_path):
        path = tmp_path / "briefings.db"
        bdb.init_db(path)
        with bdb.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM briefing_items").fetchone()[0] == 0
            assert (
                conn.execute("SELECT COUNT(*) FROM briefing_item_state").fetchone()[0]
                == 0
            )


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "briefings.db"
    bdb.init_db(path)
    with bdb.connect(path) as c:
        yield c


class TestBlockCrud:
    def test_add_and_get_block(self, conn):
        bid = bdb.add_block(
            conn, briefing_name="Morning", title="News",
            directive="3-5 stories", options={"tone": "neutral"},
        )
        block = bdb.get_block(conn, bid)
        assert block.title == "News"
        assert block.directive == "3-5 stories"
        assert block.options == {"tone": "neutral"}
        assert block.position == 0
        assert block.render_mode == "synthesis"

    def test_append_positions(self, conn):
        b0 = bdb.add_block(conn, briefing_name="M", title="A")
        b1 = bdb.add_block(conn, briefing_name="M", title="B")
        assert bdb.get_block(conn, b0).position == 0
        assert bdb.get_block(conn, b1).position == 1

    def test_list_blocks_ordered(self, conn):
        bdb.add_block(conn, briefing_name="M", title="A")
        bdb.add_block(conn, briefing_name="M", title="B")
        blocks = bdb.list_blocks(conn, "M")
        assert [b.title for b in blocks] == ["A", "B"]

    def test_list_blocks_scoped_by_name(self, conn):
        bdb.add_block(conn, briefing_name="M", title="A")
        bdb.add_block(conn, briefing_name="E", title="Z")
        assert [b.title for b in bdb.list_blocks(conn, "M")] == ["A"]
        assert [b.title for b in bdb.list_blocks(conn, "E")] == ["Z"]

    def test_update_block(self, conn):
        bid = bdb.add_block(conn, briefing_name="M", title="A")
        bdb.update_block(conn, bid, title="B", directive="d", render_mode="structured")
        block = bdb.get_block(conn, bid)
        assert block.title == "B"
        assert block.directive == "d"
        assert block.render_mode == "structured"

    def test_delete_block(self, conn):
        bid = bdb.add_block(conn, briefing_name="M", title="A")
        bdb.delete_block(conn, bid)
        assert bdb.get_block(conn, bid) is None

    def test_reorder_blocks(self, conn):
        a = bdb.add_block(conn, briefing_name="M", title="A")
        b = bdb.add_block(conn, briefing_name="M", title="B")
        c = bdb.add_block(conn, briefing_name="M", title="C")
        bdb.reorder_blocks(conn, "M", [c, a, b])
        assert [x.title for x in bdb.list_blocks(conn, "M")] == ["C", "A", "B"]

    def test_reorder_ignores_foreign_ids(self, conn):
        a = bdb.add_block(conn, briefing_name="M", title="A")
        b = bdb.add_block(conn, briefing_name="M", title="B")
        foreign = bdb.add_block(conn, briefing_name="OTHER", title="Z")
        bdb.reorder_blocks(conn, "M", [b, a, foreign])
        assert [x.title for x in bdb.list_blocks(conn, "M")] == ["B", "A"]

    def test_list_briefing_names(self, conn):
        bdb.add_block(conn, briefing_name="M", title="A")
        bdb.add_block(conn, briefing_name="E", title="B")
        assert bdb.list_briefing_names(conn) == ["E", "M"]


class TestSourceCrud:
    def test_add_and_list_sources(self, conn):
        bid = bdb.add_block(conn, briefing_name="M", title="News")
        s0 = bdb.add_source(conn, block_id=bid, kind="email",
                            config={"mode": "shared"})
        s1 = bdb.add_source(conn, block_id=bid, kind="rss",
                            config={"limit": 10})
        sources = bdb.list_sources(conn, bid)
        assert [s.kind for s in sources] == ["email", "rss"]
        assert sources[0].config == {"mode": "shared"}
        assert sources[0].position == 0
        assert sources[1].position == 1
        assert s0 != s1

    def test_block_get_includes_sources(self, conn):
        bid = bdb.add_block(conn, briefing_name="M", title="News")
        bdb.add_source(conn, block_id=bid, kind="email")
        block = bdb.get_block(conn, bid)
        assert len(block.sources) == 1
        assert block.sources[0].kind == "email"

    def test_update_source(self, conn):
        bid = bdb.add_block(conn, briefing_name="M", title="News")
        sid = bdb.add_source(conn, block_id=bid, kind="email")
        bdb.update_source(conn, sid, config={"mode": "senders"}, enabled=False)
        s = bdb.list_sources(conn, bid)[0]
        assert s.config == {"mode": "senders"}
        assert s.enabled is False

    def test_delete_source(self, conn):
        bid = bdb.add_block(conn, briefing_name="M", title="News")
        sid = bdb.add_source(conn, block_id=bid, kind="email")
        bdb.delete_source(conn, sid)
        assert bdb.list_sources(conn, bid) == []

    def test_cascade_on_block_delete(self, conn):
        bid = bdb.add_block(conn, briefing_name="M", title="News")
        bdb.add_source(conn, block_id=bid, kind="email")
        bdb.delete_block(conn, bid)
        # Source rows for the deleted block are gone (FK cascade).
        rows = conn.execute(
            "SELECT COUNT(*) FROM briefing_block_sources WHERE block_id = ?",
            (bid,),
        ).fetchone()[0]
        assert rows == 0


class TestArchive:
    def test_insert_and_get(self, conn):
        aid = bdb.insert_archive(
            conn, briefing_name="M", subject="Morning Briefing",
            body_md="📰 NEWS\n...", task_id=42,
            block_meta={"news": {"sources": 2}}, delivered_to=["talk", "email"],
        )
        arch = bdb.get_archived(conn, aid)
        assert arch.subject == "Morning Briefing"
        assert arch.body_md.startswith("📰")
        assert arch.task_id == 42
        assert arch.block_meta == {"news": {"sources": 2}}
        assert arch.delivered_to == ["talk", "email"]

    def test_list_newest_first(self, conn):
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        new = datetime.now(timezone.utc).isoformat()
        bdb.insert_archive(conn, briefing_name="M", subject="old",
                          body_md="x", generated_at=old)
        bdb.insert_archive(conn, briefing_name="M", subject="new",
                          body_md="y", generated_at=new)
        rows = bdb.list_archive(conn, briefing_name="M")
        assert [r.subject for r in rows] == ["new", "old"]

    def test_latest_archived(self, conn):
        bdb.insert_archive(conn, briefing_name="M", subject="a", body_md="x")
        bdb.insert_archive(conn, briefing_name="M", subject="b", body_md="y")
        assert bdb.latest_archived(conn, briefing_name="M").subject == "b"

    def test_list_scoped_by_name(self, conn):
        bdb.insert_archive(conn, briefing_name="M", subject="m", body_md="x")
        bdb.insert_archive(conn, briefing_name="E", subject="e", body_md="y")
        assert [r.subject for r in bdb.list_archive(conn, briefing_name="M")] == ["m"]
        assert bdb.count_archive(conn, briefing_name="E") == 1
        assert bdb.count_archive(conn) == 2

    def test_prune_by_retention(self, conn):
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        new = datetime.now(timezone.utc).isoformat()
        bdb.insert_archive(conn, briefing_name="M", subject="old",
                          body_md="x", generated_at=old)
        bdb.insert_archive(conn, briefing_name="M", subject="new",
                          body_md="y", generated_at=new)
        deleted = bdb.prune_archive(conn, briefing_name="M", retention_days=90)
        assert deleted == 1
        assert [r.subject for r in bdb.list_archive(conn, briefing_name="M")] == ["new"]

    def test_prune_zero_keeps_all(self, conn):
        old = (datetime.now(timezone.utc) - timedelta(days=1000)).isoformat()
        bdb.insert_archive(conn, briefing_name="M", subject="old",
                          body_md="x", generated_at=old)
        assert bdb.prune_archive(conn, briefing_name="M", retention_days=0) == 0
        assert bdb.count_archive(conn, briefing_name="M") == 1
