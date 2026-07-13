"""Tests for the module-DB relocation migrator (mount -> local disk, WAL flip)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from istota import db_relocate
from istota.config import Config, UserConfig
from istota.storage import get_user_bot_path


def _config(tmp_path: Path) -> Config:
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    return Config(
        nextcloud_mount_path=mount,
        module_data_dir=tmp_path / "local",
        users={"alice": UserConfig()},
        bot_name="Istota",
        db_path=tmp_path / "istota.db",
    )


def _make_legacy_delete_db(config: Config, user_id: str, module: str) -> Path:
    """Create an old-style DELETE-mode DB at the on-mount legacy path."""
    old = db_relocate.legacy_db_path(config, user_id, module)
    old.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(old)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE marker (id INTEGER)")
    conn.execute("INSERT INTO marker (id) VALUES (42)")
    conn.commit()
    conn.close()
    return old


def _journal_mode(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        conn.close()


class TestRelocateModule:
    def test_no_source_when_nothing_on_mount(self, tmp_path):
        cfg = _config(tmp_path)
        r = db_relocate.relocate_module(cfg, "alice", "location")
        assert r["status"] == "no_source"
        assert not cfg.module_db_path("alice", "location").exists()

    def test_migrates_and_flips_to_wal(self, tmp_path):
        cfg = _config(tmp_path)
        old = _make_legacy_delete_db(cfg, "alice", "location")

        r = db_relocate.relocate_module(cfg, "alice", "location")
        assert r["status"] == "migrated"

        new = cfg.module_db_path("alice", "location")
        assert new.exists()
        assert _journal_mode(new) == "wal"

        # Data survived the copy.
        conn = sqlite3.connect(new)
        try:
            assert conn.execute("SELECT id FROM marker").fetchone()[0] == 42
        finally:
            conn.close()

        # Old file archived, not left in place.
        assert not old.exists()
        assert list(old.parent.glob("location.db.migrated-*"))

    def test_idempotent_skip_when_destination_exists(self, tmp_path):
        cfg = _config(tmp_path)
        _make_legacy_delete_db(cfg, "alice", "location")
        first = db_relocate.relocate_module(cfg, "alice", "location")
        assert first["status"] == "migrated"

        # A second legacy file should NOT clobber the migrated destination.
        _make_legacy_delete_db(cfg, "alice", "location")
        second = db_relocate.relocate_module(cfg, "alice", "location")
        assert second["status"] == "skip_exists"

    def test_dry_run_moves_nothing(self, tmp_path):
        cfg = _config(tmp_path)
        _make_legacy_delete_db(cfg, "alice", "location")
        r = db_relocate.relocate_module(cfg, "alice", "location", dry_run=True)
        assert r["status"] == "would_migrate"
        assert not cfg.module_db_path("alice", "location").exists()


class TestRelocateAll:
    def test_sweep_reports_every_user_module(self, tmp_path):
        cfg = _config(tmp_path)
        _make_legacy_delete_db(cfg, "alice", "health")

        results = db_relocate.relocate_all(cfg)
        # one row per user x module
        assert len(results) == len(db_relocate.MODULES)
        by_module = {r["module"]: r["status"] for r in results}
        assert by_module["health"] == "migrated"
        assert by_module["feeds"] == "no_source"
