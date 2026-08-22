"""The `notifications` table migration.

Run against a DB that already holds rows, not a fresh one: the fresh-install
shape is what let ISSUE-261 ship green. `_run_migrations` shares one connection
in legacy `isolation_level` mode, so whether a transaction is already open when
this migration runs depends on which tables the DB being upgraded happens to
have — and a migration that assumes otherwise fails only on upgraded DBs.

The backfill half of this file arrives with stage 4.
"""

import sqlite3

import pytest

from istota import db


def _table_names(conn):
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _index_names(conn):
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }


def _columns(conn):
    """`{name: (type, notnull, default)}` for the notifications table."""
    return {
        r[1]: (r[2], r[3], r[4])
        for r in conn.execute("PRAGMA table_info(notifications)").fetchall()
    }


def _indexed_columns(conn):
    indexes = {}
    for index in conn.execute("PRAGMA index_list(notifications)").fetchall():
        name = index[1]
        indexes[name] = [
            r[2] for r in conn.execute(f"PRAGMA index_info({name})").fetchall()
        ]
    return indexes


@pytest.fixture
def upgraded_db(tmp_path):
    """A DB with history, then stripped of `notifications` as an old deploy is."""
    path = tmp_path / "istota.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        conn.execute(
            "INSERT INTO tasks (prompt, user_id, source_type, status) "
            "VALUES ('done thing', 'alice', 'talk', 'completed')"
        )
        conn.execute(
            "INSERT INTO outbound_drafts (user_id, status, to_addrs, subject, body) "
            "VALUES ('alice', 'discarded', '[\"x@y.invalid\"]', 'Re: hi', 'body')"
        )
        conn.execute("DROP TABLE notifications")
    return path


class TestFreshInstall:
    def test_schema_creates_the_table_and_indexes(self, tmp_path):
        path = tmp_path / "fresh.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            assert "notifications" in _table_names(conn)
            assert "idx_notifications_user_state" in _index_names(conn)
            assert "idx_notifications_object" in _index_names(conn)

    def test_the_unique_key_is_user_source_dedup(self, tmp_path):
        path = tmp_path / "fresh.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            conn.execute(
                "INSERT INTO notifications (user_id, source, dedup_key, title) "
                "VALUES ('alice', 'confirmation', 'task:7', 't')"
            )
            # A different user with the same key is a different row.
            conn.execute(
                "INSERT INTO notifications (user_id, source, dedup_key, title) "
                "VALUES ('bob', 'confirmation', 'task:7', 't')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO notifications (user_id, source, dedup_key, title) "
                    "VALUES ('alice', 'confirmation', 'task:7', 't')"
                )

    def test_defaults_match_the_declared_shape(self, tmp_path):
        path = tmp_path / "fresh.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            conn.execute(
                "INSERT INTO notifications (user_id, source, dedup_key, title) "
                "VALUES ('alice', 'confirmation', 'task:7', 't')"
            )
            row = conn.execute("SELECT * FROM notifications").fetchone()
        assert row["state"] == "open"
        assert row["severity"] == "info"
        assert row["actionable"] == 0
        assert row["occurrences"] == 1
        assert row["body"] == ""
        assert row["params"] == "{}"
        assert row["seen_at"] is None
        assert row["last_delivered_at"] is None
        # The ISO-Z millisecond form `db.iso_utc_now` writes, so Python-built
        # bounds and SQL defaults compare lexicographically.
        assert row["created_at"].endswith("Z") and "T" in row["created_at"]
        assert len(row["created_at"]) == len(db.iso_utc_now())


class TestUpgradedInstall:
    def test_migration_creates_the_table_on_a_db_with_history(self, upgraded_db):
        """Driven through the migration alone.

        `init_db` runs the migrations and *then* executes schema.sql, which
        carries the same CREATE IF NOT EXISTS statements — so an `init_db`-driven
        assertion here passes with `_migrate_notifications` deleted outright and
        proves nothing about it.
        """
        conn = sqlite3.connect(upgraded_db)
        conn.row_factory = sqlite3.Row
        try:
            assert "notifications" not in _table_names(conn)

            db._migrate_notifications(conn)
            conn.commit()

            assert "notifications" in _table_names(conn)
            assert "idx_notifications_user_state" in _index_names(conn)
            assert "idx_notifications_object" in _index_names(conn)
            # The history the migration ran over is untouched.
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM outbound_drafts"
            ).fetchone()[0] == 1
        finally:
            conn.close()

    def test_the_migrated_table_matches_the_schema_file(self, upgraded_db, tmp_path):
        """The two copies of the DDL must not drift apart.

        `_migrate_notifications` is what an upgraded deploy gets and schema.sql
        is what a fresh install gets; a column present in one and not the other
        is a defect that surfaces only on whichever half nobody tested.
        """
        migrated = sqlite3.connect(upgraded_db)
        migrated.row_factory = sqlite3.Row
        fresh_path = tmp_path / "fresh.db"
        db.init_db(fresh_path)
        try:
            db._migrate_notifications(migrated)
            migrated.commit()
            with db.get_db(fresh_path) as fresh:
                assert _columns(migrated) == _columns(fresh)
                assert _indexed_columns(migrated) == _indexed_columns(fresh)
        finally:
            migrated.close()

    def test_migration_runs_standalone_on_an_inherited_transaction(self, upgraded_db):
        """The shape `_run_migrations` actually calls it in: DML already open."""
        conn = sqlite3.connect(upgraded_db)
        conn.row_factory = sqlite3.Row
        try:
            # A zero-row UPDATE is enough to open the implicit transaction.
            conn.execute("UPDATE tasks SET status = 'x' WHERE id = -1")
            db._migrate_notifications(conn)
            conn.commit()
            assert "notifications" in _table_names(conn)
        finally:
            conn.close()

    def test_migration_is_idempotent(self, upgraded_db):
        """Re-run directly, not through `init_db` — see the note above."""
        conn = sqlite3.connect(upgraded_db)
        conn.row_factory = sqlite3.Row
        try:
            db._migrate_notifications(conn)
            conn.execute(
                "INSERT INTO notifications (user_id, source, dedup_key, title) "
                "VALUES ('alice', 'confirmation', 'task:7', 't')"
            )
            conn.commit()

            db._migrate_notifications(conn)
            db._migrate_notifications(conn)
            conn.commit()

            assert conn.execute(
                "SELECT COUNT(*) FROM notifications"
            ).fetchone()[0] == 1
        finally:
            conn.close()

        # And the whole init path stays clean over an already-migrated DB.
        db.init_db(upgraded_db)
        with db.get_db(upgraded_db) as check:
            assert check.execute(
                "SELECT COUNT(*) FROM notifications"
            ).fetchone()[0] == 1

    def test_migration_tolerates_a_very_early_database(self, tmp_path):
        """No tables at all — the migration must not be what breaks init."""
        path = tmp_path / "bare.db"
        conn = sqlite3.connect(path)
        try:
            db._migrate_notifications(conn)
            conn.commit()
            assert "notifications" in _table_names(conn)
        finally:
            conn.close()
