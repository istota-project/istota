"""Tests for restoring cold DB snapshots back to local disk."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from istota import db, db_backup, db_restore
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)


def _config(tmp_path: Path, **sched) -> Config:
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud=NextcloudConfig(),
        talk=TalkConfig(),
        email=EmailConfig(),
        scheduler=SchedulerConfig(**sched),
        nextcloud_mount_path=mount,
        module_data_dir=tmp_path / "local",
        users={"alice": UserConfig()},
    )


def _seed_module_db(cfg: Config, user: str, module: str) -> Path:
    from istota.location.db import init_db

    path = cfg.module_db_path(user, module)
    init_db(path)
    return path


def _add_place(path: Path, name: str = "home") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO places (name, lat, lon) VALUES (?, 1.0, 2.0)", (name,)
        )
        conn.commit()
    finally:
        conn.close()


def _place_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    finally:
        conn.close()


class TestRestoreFramework:
    def test_restore_recreates_deleted_framework_db(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.create_task(conn, prompt="hi", user_id="alice")
        db_backup.backup_databases(cfg, today="2026-07-12")

        cfg.db_path.unlink()  # disaster: live DB gone

        res = db_restore.restore_database(cfg, label="framework")
        assert res["status"] == "restored"
        assert cfg.db_path.exists()
        conn = sqlite3.connect(cfg.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        finally:
            conn.close()


class TestRestoreModule:
    def test_restore_module_db(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-12")

        path.unlink()
        res = db_restore.restore_database(cfg, user="alice", module="location")
        assert res["status"] == "restored"
        assert _place_count(path) == 1

    def test_dry_run_does_not_write(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-12")
        path.unlink()

        res = db_restore.restore_database(
            cfg, user="alice", module="location", dry_run=True
        )
        assert res["status"] == "would_restore"
        assert not path.exists()


class TestRestoreSafety:
    def test_no_snapshot_reports_cleanly(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        res = db_restore.restore_database(cfg, user="alice", module="location")
        assert res["status"] == "no_snapshot"

    def test_refuses_empty_snapshot_without_force(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        _seed_module_db(cfg, "alice", "location")  # 0 rows
        db_backup.backup_databases(cfg, today="2026-07-12")

        dest = cfg.module_db_path("alice", "location")
        dest.unlink()
        res = db_restore.restore_database(cfg, user="alice", module="location")
        assert res["status"] == "refused_empty"
        assert not dest.exists()

    def test_force_restores_empty_snapshot(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        _seed_module_db(cfg, "alice", "location")
        db_backup.backup_databases(cfg, today="2026-07-12")

        dest = cfg.module_db_path("alice", "location")
        dest.unlink()
        res = db_restore.restore_database(
            cfg, user="alice", module="location", force=True
        )
        assert res["status"] == "restored"
        assert dest.exists()

    def test_picks_newest_good_over_suspect(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-11")  # good

        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM places")
        conn.commit()
        conn.close()
        db_backup.backup_databases(cfg, today="2026-07-12")  # suspect

        path.unlink()
        res = db_restore.restore_database(cfg, user="alice", module="location")
        # The suspect newest is skipped; the 07-11 good copy is used.
        assert res["status"] == "restored"
        assert res["date"] == "2026-07-11"
        assert _place_count(path) == 1

    def test_explicit_date_selects_that_snapshot(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-11")
        _add_place(path, name="work")  # now 2 rows
        db_backup.backup_databases(cfg, today="2026-07-12")

        path.unlink()
        res = db_restore.restore_database(
            cfg, user="alice", module="location", date="2026-07-11"
        )
        assert res["date"] == "2026-07-11"
        assert _place_count(path) == 1  # the older snapshot's count


class TestListSnapshots:
    def test_lists_dates_newest_first(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today="2026-07-10")
        db_backup.backup_databases(cfg, today="2026-07-12")
        db_backup.backup_databases(cfg, today="2026-07-11")
        dates = [s["date"] for s in db_restore.list_snapshots(cfg)]
        assert dates == ["2026-07-12", "2026-07-11", "2026-07-10"]


class TestRestoreAll:
    def test_restores_framework_and_modules(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.create_task(conn, prompt="hi", user_id="alice")
        loc = _seed_module_db(cfg, "alice", "location")
        _add_place(loc)
        db_backup.backup_databases(cfg, today="2026-07-12")

        cfg.db_path.unlink()
        loc.unlink()

        results = db_restore.restore_all(cfg)
        by_label = {r["label"]: r["status"] for r in results}
        assert by_label["framework"] == "restored"
        assert by_label["location:alice"] == "restored"
        assert cfg.db_path.exists()
        assert _place_count(loc) == 1
