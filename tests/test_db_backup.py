"""Tests for the local-DB -> mount snapshot backup."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from istota import db, db_backup
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
    from istota.location.db import init_db  # any module init works for the shape

    path = cfg.module_db_path(user, module)
    init_db(path)
    return path


class TestBackupDestination:
    def test_defaults_under_mount(self, tmp_path):
        cfg = _config(tmp_path)
        assert db_backup.backup_destination(cfg) == (tmp_path / "mount" / "istota-db-backups")

    def test_explicit_dir_wins(self, tmp_path):
        cfg = _config(tmp_path, db_backup_dir=str(tmp_path / "elsewhere"))
        assert db_backup.backup_destination(cfg) == (tmp_path / "elsewhere")

    def test_none_without_mount_or_dir(self, tmp_path):
        cfg = _config(tmp_path)
        cfg.nextcloud_mount_path = None
        assert db_backup.backup_destination(cfg) is None


class TestBackupDatabases:
    def test_snapshots_framework_and_module_dbs(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        _seed_module_db(cfg, "alice", "location")

        results = db_backup.backup_databases(cfg)
        by_label = {r["label"]: r["status"] for r in results}

        assert by_label["framework"] == "ok"
        assert by_label["location:alice"] == "ok"
        # A module with no local DB is skipped, not errored.
        assert by_label["money:alice"] == "skip_missing"

        root = tmp_path / "mount" / "istota-db-backups"
        assert (root / "framework" / "istota.db").exists()
        assert (root / "alice" / "location.db").exists()

    def test_snapshot_is_readable_and_has_data(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.create_task(conn, prompt="hi", user_id="alice")

        db_backup.backup_databases(cfg)

        snap = tmp_path / "mount" / "istota-db-backups" / "framework" / "istota.db"
        conn = sqlite3.connect(snap)
        try:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        finally:
            conn.close()

    def test_disabled_is_noop(self, tmp_path):
        cfg = _config(tmp_path, db_backup_enabled=False)
        db.init_db(cfg.db_path)
        assert db_backup.backup_databases(cfg) == []
        assert not (tmp_path / "mount" / "istota-db-backups").exists()

    def test_no_destination_is_noop(self, tmp_path):
        cfg = _config(tmp_path)
        cfg.nextcloud_mount_path = None
        db.init_db(cfg.db_path)
        assert db_backup.backup_databases(cfg) == []


class TestBackupClockPersistence:
    """The daily-backup clock must survive scheduler restarts (else frequent
    deploys defer it forever). last_backup_time reads a persisted timestamp;
    backup_databases writes it after a real attempt."""

    def test_last_backup_time_zero_when_never_run(self, tmp_path):
        cfg = _config(tmp_path)
        assert db_backup.last_backup_time(cfg) == 0.0

    def test_backup_persists_last_run(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        before = db_backup.last_backup_time(cfg)
        db_backup.backup_databases(cfg)
        after = db_backup.last_backup_time(cfg)
        assert before == 0.0
        assert after > 0.0  # a real timestamp was written

    def test_noop_does_not_persist_last_run(self, tmp_path):
        # No destination (no mount) → nothing attempted → clock stays at 0 so
        # the daemon keeps retrying instead of marking a phantom backup.
        cfg = _config(tmp_path)
        cfg.nextcloud_mount_path = None
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg)
        assert db_backup.last_backup_time(cfg) == 0.0

    def test_disabled_does_not_persist_last_run(self, tmp_path):
        cfg = _config(tmp_path, db_backup_enabled=False)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg)
        assert db_backup.last_backup_time(cfg) == 0.0


class TestColdCopyIsDeleteMode:
    def test_snapshot_is_delete_journal_mode(self, tmp_path):
        # The cold copy on the mount must not carry a WAL header (its -shm would
        # SIGBUS on FUSE if ever opened in place).
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg)
        snap = tmp_path / "mount" / "istota-db-backups" / "framework" / "istota.db"
        conn = sqlite3.connect(snap)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        finally:
            conn.close()
