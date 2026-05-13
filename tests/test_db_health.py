"""Tests for ``istota.db_health``.

The integration path (REINDEX repairs index corruption) is exercised in
production (the deathcults-tumblr incident) — synthesizing genuine SQLite
index corruption from Python is fiddly. These tests cover the control
flow with mocked ``quick_check`` results plus the easy real-DB cases
(clean DB, missing file).
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

from istota import db_health


class TestQuickCheck:
    def test_clean_db_returns_empty_list(self, tmp_path):
        path = tmp_path / "x.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
        conn.commit()
        try:
            assert db_health.quick_check(conn) == []
        finally:
            conn.close()


class TestCheckAndRepair:
    def test_missing_file_is_clean(self, tmp_path):
        report = db_health.check_and_repair(tmp_path / "nope.db", label="x")
        assert report.ok is True
        assert report.issues_before == []
        assert report.issues_after == []
        assert report.repair_attempted is False
        assert report.repaired is False

    def test_clean_db_is_clean(self, tmp_path):
        path = tmp_path / "clean.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        report = db_health.check_and_repair(path, label="clean")
        assert report.ok is True
        assert report.repair_attempted is False
        assert report.repaired is False

    def test_dirty_db_triggers_reindex_and_recovers(self, tmp_path):
        path = tmp_path / "dirty.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, s TEXT)")
        conn.execute("CREATE INDEX i ON t(s)")
        conn.commit()
        conn.close()

        # First call returns a fake corruption signal, second (post-REINDEX)
        # returns clean. The reindex itself is real — we just don't need to
        # forge a real corrupt page to verify the control flow.
        results = iter([
            ["wrong # of entries in index i"],
            [],
        ])

        def fake_quick_check(_conn):
            return next(results)

        with patch.object(db_health, "quick_check", side_effect=fake_quick_check):
            report = db_health.check_and_repair(path, label="dirty")

        assert report.ok is True
        assert report.issues_before == ["wrong # of entries in index i"]
        assert report.issues_after == []
        assert report.repair_attempted is True
        assert report.repaired is True

    def test_unrepairable_db_is_reported(self, tmp_path):
        path = tmp_path / "broken.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        results = iter([
            ["row 1 missing from index i"],
            ["row 1 missing from index i"],
        ])

        with patch.object(
            db_health, "quick_check", side_effect=lambda _c: next(results),
        ):
            report = db_health.check_and_repair(path, label="broken")

        assert report.ok is False
        assert report.repair_attempted is True
        assert report.repaired is False
        assert report.issues_after == ["row 1 missing from index i"]

    def test_unreadable_db_is_reported(self, tmp_path):
        path = tmp_path / "garbage.db"
        path.write_bytes(b"not a sqlite database")
        report = db_health.check_and_repair(path, label="garbage")
        assert report.ok is False
        assert report.issues_before  # populated with the open error
        assert report.issues_after == report.issues_before
