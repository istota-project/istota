"""Tests for the shared-block definition store (admin-shared-briefing-blocks)."""

from istota import db
from istota.config import BriefingSharedBlock
from istota.shared_blocks_store import import_from_config


class TestImportFromConfig:
    def test_seeds_missing_only(self, tmp_path):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        blocks = [
            BriefingSharedBlock(name="a", cron="0 6 * * *", title="A"),
            BriefingSharedBlock(name="b", cron="0 7 * * *", title="B", trusted=True),
        ]
        assert import_from_config(db_path, blocks) == 2
        with db.get_db(db_path) as conn:
            rows = {r.name: r for r in db.list_shared_block_configs(conn)}
        assert set(rows) == {"a", "b"}
        assert rows["b"].trusted is True

    def test_idempotent_and_edit_preserving(self, tmp_path):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        block = BriefingSharedBlock(name="a", cron="0 6 * * *", title="A")
        import_from_config(db_path, [block])
        # Admin edits the row (e.g. changed title / disabled it).
        with db.get_db(db_path) as conn:
            db.upsert_shared_block_config(
                conn, name="a", cron="0 9 * * *", title="Edited", enabled=False,
            )
        # A re-seed (operator re-run) must NOT clobber the edit.
        assert import_from_config(db_path, [block]) == 0
        with db.get_db(db_path) as conn:
            row = db.get_shared_block_config(conn, "a")
        assert row.title == "Edited"
        assert row.cron == "0 9 * * *"
        assert row.enabled is False

    def test_missing_db_returns_zero(self, tmp_path):
        assert import_from_config(tmp_path / "nope.db", []) == 0


class TestCrudRoundTrip:
    def test_upsert_get_list_delete(self, tmp_path):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            row = db.upsert_shared_block_config(
                conn, name="mk", cron="0 6 * * *", title="Markets",
                render_mode="structured", trusted=True,
                sources=[{"kind": "markets", "config": {}}],
            )
            assert row.name == "mk"
            assert row.render_mode == "structured"
            assert row.trusted is True
            assert row.sources == [{"kind": "markets", "config": {}}]

            got = db.get_shared_block_config(conn, "mk")
            assert got.title == "Markets"

            # Update in place.
            row2 = db.upsert_shared_block_config(
                conn, name="mk", cron="30 6 * * *", title="Markets2",
            )
            assert row2.cron == "30 6 * * *"
            assert row2.title == "Markets2"

            assert [r.name for r in db.list_shared_block_configs(conn)] == ["mk"]
            assert db.delete_shared_block_config(conn, "mk") is True
            assert db.get_shared_block_config(conn, "mk") is None
            assert db.delete_shared_block_config(conn, "mk") is False
