"""Tests for the briefings module SQLite layer."""

from datetime import datetime, timedelta, timezone
import json
import sqlite3

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

    def test_reorder_rejects_foreign_ids(self, conn):
        a = bdb.add_block(conn, briefing_name="M", title="A")
        b = bdb.add_block(conn, briefing_name="M", title="B")
        foreign = bdb.add_block(conn, briefing_name="OTHER", title="Z")
        with pytest.raises(ValueError, match="permutation"):
            bdb.reorder_blocks(conn, "M", [b, a, foreign])
        assert [x.title for x in bdb.list_blocks(conn, "M")] == ["A", "B"]

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

    def test_delete_archived(self, conn):
        keep = bdb.insert_archive(conn, briefing_name="M", subject="keep", body_md="x")
        drop = bdb.insert_archive(conn, briefing_name="M", subject="drop", body_md="y")
        assert bdb.delete_archived(conn, drop) is True
        assert bdb.get_archived(conn, drop) is None
        # Sibling rows are untouched — a single-row delete, not a name purge.
        assert bdb.get_archived(conn, keep) is not None
        assert bdb.count_archive(conn, briefing_name="M") == 1

    def test_delete_archived_missing(self, conn):
        assert bdb.delete_archived(conn, 999) is False


class TestDocumentStorage:
    def test_fresh_and_v1_upgrade_keep_legacy_archive(self, tmp_path):
        path = tmp_path / "briefings.db"
        # Build the version-1 schema independently of the current schema text.
        with bdb.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE briefing_archive (
                    id INTEGER PRIMARY KEY, briefing_name TEXT NOT NULL,
                    subject TEXT, body_md TEXT NOT NULL, generated_at TEXT NOT NULL,
                    task_id INTEGER, block_meta TEXT NOT NULL DEFAULT '{}',
                    delivered_to TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_meta VALUES ('version', '1');
                INSERT INTO briefing_archive (briefing_name, subject, body_md, generated_at)
                    VALUES ('morning', 'Original title', '# Original body', '2026-01-01');
            """)
            conn.commit()
        bdb.init_db(path)
        bdb.init_db(path)
        with bdb.connect(path) as conn:
            assert bdb.meta_get(conn, "version") == "3"
            archived = bdb.get_archived(conn, 1)
            assert (archived.subject, archived.body_md, archived.document) == (
                "Original title", "# Original body", None,
            )
            assert conn.execute("SELECT document_json FROM briefing_archive").fetchone()[0] is None
        fresh = tmp_path / "fresh.db"
        bdb.init_db(fresh)
        with bdb.connect(fresh) as conn:
            assert "document_json" in {r["name"] for r in conn.execute("PRAGMA table_info(briefing_archive)")}

    def test_document_round_trip_and_corrupt_fallback(self, conn, caplog):
        document = {"version": 1, "title": "Edition", "blocks": [
            {"block_id": 42, "title": "News", "kind": "markdown", "body_md": "Complete body",
             "presentation": {"width": "wide"}, "source_status": "complete"},
        ]}
        aid = bdb.insert_archive(conn, briefing_name="morning", subject="Edition",
                                  body_md="News\n\nComplete body", document=document)
        assert bdb.get_archived(conn, aid).document == document
        for corrupt in ('not json', '{"version": 99}', '[]'):
            conn.execute("UPDATE briefing_archive SET document_json = ? WHERE id = ?", (corrupt, aid))
            archived = bdb.get_archived(conn, aid)
            assert archived.document is None and archived.body_md == "News\n\nComplete body"
        assert "briefing_document_invalid" in caplog.text
        assert "not json" not in caplog.text

    def test_invalid_document_write_cannot_change_archive(self, conn):
        with pytest.raises(ValueError):
            bdb.insert_archive(conn, briefing_name="morning", subject="Edition", body_md="Body",
                               document={"version": 99})
        assert bdb.count_archive(conn) == 0

    def test_invalid_presentation_update_leaves_other_fields_unchanged(self, conn):
        bid = bdb.add_block(conn, briefing_name="morning", title="Original", options={"tone": "neutral"})
        with pytest.raises(ValueError, match="presentation"):
            bdb.update_block(conn, bid, title="Changed", options={"presentation": {"width": "bad"}})
        block = bdb.get_block(conn, bid)
        assert block.title == "Original" and block.options == {"tone": "neutral"}

    def test_old_invalid_presentation_is_read_as_auto(self, conn, caplog):
        bid = bdb.add_block(conn, briefing_name="morning", title="Old")
        conn.execute("UPDATE briefing_blocks SET options = ? WHERE id = ?",
                     ('{"tone":"neutral","presentation":{"width":"bad"}}', bid))
        assert bdb.get_block(conn, bid).options == {"tone": "neutral", "presentation": {"width": "auto"}}
        assert "briefing_presentation_invalid" in caplog.text


class TestDefinitionSave:
    def test_complete_edit_retains_ids_and_replaces_order(self, conn):
        original = bdb.save_block_definition(conn, payload={
            "briefing_name": "M", "title": "News", "directive": "Brief",
            "options": {"custom": 42, "presentation": {"width": "wide"}},
            "sources": [{"kind": "notes", "config": {"path": "shared/notes.md"}},
                        {"kind": "calendar"}, {"kind": "markets"}],
        })
        first, second, removed = original.sources
        saved = bdb.save_block_definition(conn, payload={
            "id": original.id, "title": "Changed", "directive": "",
            "sources": [{"id": second.id, "kind": "calendar", "enabled": False},
                        {"id": first.id, "kind": "todos", "config": {"path": "shared/tasks.md"}},
                        {"kind": "reminders", "config": {"path": "shared/reminders.md"}}],
        })
        assert saved.title == "Changed" and saved.directive == ""
        assert saved.options == original.options
        assert [s.id for s in saved.sources[:2]] == [second.id, first.id]
        assert [s.position for s in saved.sources] == [0, 1, 2]
        assert [s.kind for s in saved.sources] == ["calendar", "todos", "reminders"]
        assert saved.sources[0].enabled is False
        assert saved.sources[1].config == {"path": "shared/tasks.md"}
        assert removed.id not in [s.id for s in saved.sources]

    def test_more_new_sources_before_retained_source_do_not_collide(self, conn):
        original = bdb.save_block_definition(conn, payload={
            "briefing_name": "M", "title": "News", "sources": [{"kind": "notes"}],
        })
        retained = original.sources[0]
        saved = bdb.save_block_definition(conn, payload={
            "id": original.id, "sources": [{"kind": "calendar"}, {"kind": "markets"},
                                          {"id": retained.id, "kind": "notes"}],
        })
        assert [source.kind for source in saved.sources] == ["calendar", "markets", "notes"]
        assert saved.sources[2].id == retained.id

    @pytest.mark.parametrize("creating", [False, True])
    def test_late_database_failure_rolls_back_only_definition(self, conn, creating):
        bid = bdb.add_block(conn, briefing_name="M", title="Original")
        sid = bdb.add_source(conn, block_id=bid, kind="notes")
        conn.commit()
        # A real storage failure after block writes must undo the whole save,
        # while preserving a caller's unrelated uncommitted work.
        conn.execute("""CREATE TRIGGER refuse_calendar BEFORE INSERT ON briefing_block_sources
                        WHEN NEW.kind = 'calendar'
                        BEGIN SELECT RAISE(ABORT, 'calendar refused'); END""")
        unrelated = bdb.add_block(conn, briefing_name="Other", title="Keep")
        payload = {"title": "Changed", "sources": [{"kind": "notes"}, {"kind": "calendar"}]}
        payload.update({"briefing_name": "M"} if creating else {"id": bid})
        with pytest.raises(sqlite3.IntegrityError, match="calendar refused"):
            bdb.save_block_definition(conn, payload=payload)
        conn.commit()
        assert bdb.get_block(conn, unrelated).title == "Keep"
        blocks = bdb.list_blocks(conn, "M")
        assert len(blocks) == 1
        assert blocks[0].title == "Original"
        assert [s.id for s in blocks[0].sources] == [sid]


class TestPreviewBlockIdentity:
    def test_deleted_highest_block_cannot_inherit_archived_preview_content(self, conn):
        from istota.briefings.document import document_to_markdown

        old = bdb.add_block(conn, briefing_name="M", title="Old")
        document = {"version": 1, "title": "Old edition", "blocks": [{
            "block_id": old, "title": "Old", "kind": "markdown", "body_md": "Private old content",
            "presentation": {"width": "auto"}, "source_status": "complete",
        }]}
        bdb.insert_archive(conn, briefing_name="M", subject=document["title"],
                           body_md=document_to_markdown(document), document=document)
        conn.commit()
        bdb.delete_block(conn, old)
        conn.commit()
        # An existing v2 DB has no sequence metadata yet.
        conn.execute("DELETE FROM schema_meta WHERE key = 'block_id_high_water'")
        conn.commit()
        new = bdb.add_block(conn, briefing_name="M", title="Unrelated new block")
        assert new > old
        assert new not in {block["block_id"] for block in document["blocks"]}
        bdb.delete_block(conn, new)
        conn.commit()
        assert bdb.add_block(conn, briefing_name="Other", title="Next") > new

    def test_repeated_delete_recreate_and_rollback_keep_identity(self, conn):
        first = bdb.add_block(conn, briefing_name="M", title="First")
        conn.commit()
        bdb.delete_block(conn, first)
        conn.commit()
        second = bdb.add_block(conn, briefing_name="M", title="Second")
        conn.commit()
        assert second > first
        conn.execute("SAVEPOINT aborted")
        aborted = bdb.add_block(conn, briefing_name="M", title="Aborted")
        conn.execute("ROLLBACK TO aborted")
        conn.execute("RELEASE aborted")
        assert bdb.add_block(conn, briefing_name="M", title="Actual") == aborted


    @pytest.mark.parametrize("scope", ["block", "briefing"])
    def test_upgrade_delete_before_first_allocation_preserves_inflight_identity(self, conn, scope):
        old = bdb.add_block(conn, briefing_name="M", title="In-flight edition block")
        conn.commit()
        # Simulate an older DB with a generated manifest but no archive yet.
        conn.execute("DELETE FROM schema_meta WHERE key = 'block_id_high_water'")
        conn.commit()
        if scope == "briefing":
            bdb.delete_briefing_definitions(conn, "M")
        else:
            bdb.delete_block(conn, old)
        conn.commit()
        assert bdb.add_block(conn, briefing_name="M", title="New block") > old


class TestArchiveReadState:
    @pytest.mark.parametrize("interrupted", [False, True])
    def test_v1_upgrade_backfills_only_historical_rows(self, tmp_path, monkeypatch, interrupted):
        path = tmp_path / "v1.db"
        with bdb.connect(path) as conn:
            # Version 1's actual archive shape, with no read column or index.
            conn.executescript("""
                CREATE TABLE briefing_archive (
                    id INTEGER PRIMARY KEY, briefing_name TEXT NOT NULL,
                    subject TEXT, body_md TEXT NOT NULL, generated_at TEXT NOT NULL,
                    task_id INTEGER, block_meta TEXT NOT NULL DEFAULT '{}',
                    delivered_to TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_meta VALUES ('version', '1');
                INSERT INTO schema_meta VALUES ('migrated_from_components:Morning', '1');
                INSERT INTO briefing_archive (briefing_name, subject, body_md, generated_at)
                    VALUES ('Morning', 'Old edition', 'Body', '2026-01-01T00:00:00Z'),
                           ('Evening', 'Older edition', 'Body', '2026-01-01T01:00:00Z');
            """)
        if interrupted:
            def fail_timestamp():
                raise RuntimeError("interrupted backfill")

            with monkeypatch.context() as patch:
                patch.setattr(bdb, "_now_iso", fail_timestamp)
                with pytest.raises(RuntimeError, match="interrupted backfill"):
                    bdb.init_db(path)
            with bdb.connect(path) as conn:
                assert bdb.meta_get(conn, "version") == "1"
                assert "read_at" not in {
                    row["name"] for row in conn.execute("PRAGMA table_info(briefing_archive)")
                }
        bdb.init_db(path)
        with bdb.connect(path) as conn:
            rows = bdb.list_archive(conn)
            assert len({row.read_at for row in rows}) == 1
            assert rows[0].read_at is not None
            assert datetime.fromisoformat(rows[0].read_at).tzinfo is not None
            assert bdb.meta_get(conn, "version") == "3"
            assert bdb.meta_get(conn, "migrated_from_components:Morning") == "1"
            assert bdb.count_unread_archive(conn) == 0
            new_id = bdb.insert_archive(conn, briefing_name="Morning", subject="New", body_md="New body")
            conn.commit()
        bdb.init_db(path)
        with bdb.connect(path) as conn:
            assert bdb.get_archived(conn, new_id).read_at is None
            assert bdb.count_unread_archive(conn) == 1
            assert conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'idx_archive_unread'"
            ).fetchone()[0].endswith("WHERE read_at IS NULL")

    def test_single_read_preserves_first_timestamp_and_missing_id(self, conn, monkeypatch):
        first = bdb.insert_archive(conn, briefing_name="Morning", subject="One", body_md="Body")
        second = bdb.insert_archive(conn, briefing_name="Morning", subject="Two", body_md="Body")
        assert bdb.get_archived(conn, first).read_at is None
        assert bdb.count_unread_archive(conn) == 2
        monkeypatch.setattr(bdb, "_now_iso", lambda: "2026-10-01T10:00:00Z")
        read = bdb.mark_archived_read(conn, first)
        assert read.id == first
        assert read.read_at == "2026-10-01T10:00:00Z"
        monkeypatch.setattr(bdb, "_now_iso", lambda: "2026-10-02T10:00:00Z")
        assert bdb.mark_archived_read(conn, first).read_at == read.read_at
        assert bdb.mark_archived_read(conn, 999) is None
        assert bdb.get_archived(conn, second).read_at is None
        assert bdb.count_unread_archive(conn) == 1

    def test_bulk_exact_scope_all_pages_and_later_arrivals(self, conn, monkeypatch):
        assert bdb.mark_archive_as_read(conn) == 0
        ids = [bdb.insert_archive(conn, briefing_name="Morning", subject=str(i), body_md="Body")
               for i in range(125)]
        for name in ("morning", "Morning extra", " Morning "):
            bdb.insert_archive(conn, briefing_name=name, subject="Other", body_md="Body")
        monkeypatch.setattr(bdb, "_now_iso", lambda: "2026-10-01T10:00:00Z")
        first = bdb.mark_archived_read(conn, ids[0])
        monkeypatch.setattr(bdb, "_now_iso", lambda: "2026-10-02T10:00:00Z")
        assert bdb.mark_archive_as_read(conn, briefing_name="Morning") == 124
        assert bdb.get_archived(conn, ids[0]).read_at == first.read_at
        assert bdb.get_archived(conn, ids[-1]).read_at == "2026-10-02T10:00:00Z"
        assert bdb.count_unread_archive(conn) == 3
        assert bdb.mark_archive_as_read(conn, briefing_name="Morning") == 0
        assert bdb.mark_archive_as_read(conn, briefing_name="Missing") == 0
        assert bdb.mark_archive_as_read(conn) == 3
        assert bdb.mark_archive_as_read(conn) == 0
        conn.commit()
        later = bdb.insert_archive(conn, briefing_name="Morning", subject="Later", body_md="Body")
        assert bdb.get_archived(conn, later).read_at is None
        assert bdb.count_unread_archive(conn) == 1

    def test_reads_belong_to_callers_transaction(self, conn):
        aid = bdb.insert_archive(conn, briefing_name="Morning", subject="One", body_md="Body")
        conn.commit()
        bdb.mark_archived_read(conn, aid)
        conn.rollback()
        assert bdb.get_archived(conn, aid).read_at is None
        bdb.mark_archive_as_read(conn)
        conn.rollback()
        assert bdb.count_unread_archive(conn) == 1


def test_v2_upgrade_preserves_document_and_backfills_reads(tmp_path):
    path = tmp_path / "v2.db"
    document = {"version": 1, "title": "Archived edition", "blocks": []}
    with bdb.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE briefing_archive (
                id INTEGER PRIMARY KEY, briefing_name TEXT NOT NULL,
                subject TEXT, body_md TEXT NOT NULL, generated_at TEXT NOT NULL,
                task_id INTEGER, block_meta TEXT NOT NULL DEFAULT '{}',
                delivered_to TEXT NOT NULL DEFAULT '[]', document_json TEXT
            );
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('version', '2');
        """)
        conn.execute(
            "INSERT INTO briefing_archive (briefing_name, body_md, generated_at, document_json) "
            "VALUES (?, ?, ?, ?)",
            ("Morning", "Archived body", "2026-01-01T00:00:00Z", json.dumps(document)),
        )
        conn.commit()
    bdb.init_db(path)
    with bdb.connect(path) as conn:
        item = bdb.get_archived(conn, 1)
        assert item.document == document
        assert item.body_md == "Archived body"
        assert item.read_at is not None
        assert bdb.meta_get(conn, "version") == "3"
        new_id = bdb.insert_archive(conn, briefing_name="Morning", subject="New", body_md="New body")
        conn.commit()
    bdb.init_db(path)
    with bdb.connect(path) as conn:
        assert bdb.get_archived(conn, new_id).read_at is None
        assert bdb.count_unread_archive(conn) == 1


@pytest.mark.parametrize("retire", ["delete", "prune"])
@pytest.mark.parametrize("legacy", [False, True])
def test_archive_identity_survives_retirement(conn, retire, legacy):
    if legacy:
        conn.execute(
            "INSERT INTO briefing_archive (id, briefing_name, body_md, generated_at) "
            "VALUES (41, 'Morning', 'Old body', '2000-01-01T00:00:00Z')"
        )
        old_id = 41
    else:
        old_id = bdb.insert_archive(
            conn, briefing_name="Morning", subject="Old", body_md="Old body",
            generated_at="2000-01-01T00:00:00Z",
        )
    conn.commit()
    if retire == "delete":
        assert bdb.delete_archived(conn, old_id)
    else:
        assert bdb.prune_archive(conn, briefing_name="Morning", retention_days=1) == 1
    conn.commit()
    new_id = bdb.insert_archive(conn, briefing_name="Morning", subject="New", body_md="New body")
    conn.commit()
    assert new_id > old_id
    assert bdb.mark_archived_read(conn, old_id) is None
    assert bdb.get_archived(conn, new_id).read_at is None
