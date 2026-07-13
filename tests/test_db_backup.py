"""Tests for the local-DB -> mount snapshot backup."""

from __future__ import annotations

import sqlite3
import stat
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

FIXED_DAY = "2026-07-12"


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


def _add_place(path: Path) -> None:
    """Insert one data row into a module DB so it has non-zero data rows."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO places (name, lat, lon) VALUES ('home', 1.0, 2.0)"
        )
        conn.commit()
    finally:
        conn.close()


def _root(tmp_path: Path) -> Path:
    return tmp_path / "mount" / "istota-db-backups"


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

        results = db_backup.backup_databases(cfg, today=FIXED_DAY)
        by_label = {r["label"]: r["status"] for r in results}

        assert by_label["framework"] == "ok"
        assert by_label["location:alice"] == "ok"
        # A module with no local DB is skipped, not errored.
        assert by_label["money:alice"] == "skip_missing"

        dated = _root(tmp_path) / FIXED_DAY
        assert (dated / "framework" / "istota.db").exists()
        assert (dated / "alice" / "location.db").exists()

    def test_snapshot_is_readable_and_has_data(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.create_task(conn, prompt="hi", user_id="alice")

        db_backup.backup_databases(cfg, today=FIXED_DAY)

        snap = _root(tmp_path) / FIXED_DAY / "framework" / "istota.db"
        conn = sqlite3.connect(snap)
        try:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        finally:
            conn.close()

    def test_disabled_is_noop(self, tmp_path):
        cfg = _config(tmp_path, db_backup_enabled=False)
        db.init_db(cfg.db_path)
        assert db_backup.backup_databases(cfg, today=FIXED_DAY) == []
        assert not _root(tmp_path).exists()

    def test_no_destination_is_noop(self, tmp_path):
        cfg = _config(tmp_path)
        cfg.nextcloud_mount_path = None
        db.init_db(cfg.db_path)
        assert db_backup.backup_databases(cfg, today=FIXED_DAY) == []

    def test_defaults_to_real_date_when_today_none(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg)
        # Exactly one dated dir was created (today's real date).
        dated = [p for p in _root(tmp_path).iterdir() if p.is_dir()]
        assert len(dated) == 1
        assert (dated[0] / "framework" / "istota.db").exists()


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
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        after = db_backup.last_backup_time(cfg)
        assert before == 0.0
        assert after > 0.0  # a real timestamp was written

    def test_noop_does_not_persist_last_run(self, tmp_path):
        cfg = _config(tmp_path)
        cfg.nextcloud_mount_path = None
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert db_backup.last_backup_time(cfg) == 0.0

    def test_disabled_does_not_persist_last_run(self, tmp_path):
        cfg = _config(tmp_path, db_backup_enabled=False)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        assert db_backup.last_backup_time(cfg) == 0.0


class TestColdCopyIsDeleteMode:
    def test_snapshot_is_delete_journal_mode(self, tmp_path):
        # The cold copy on the mount must not carry a WAL header (its -shm would
        # SIGBUS on FUSE if ever opened in place).
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        snap = _root(tmp_path) / FIXED_DAY / "framework" / "istota.db"
        conn = sqlite3.connect(snap)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        finally:
            conn.close()


class TestDatedSnapshotsAndRetention:
    def test_each_run_writes_its_own_dated_dir(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today="2026-07-10")
        db_backup.backup_databases(cfg, today="2026-07-11")
        dates = sorted(p.name for p in _root(tmp_path).iterdir() if p.is_dir())
        assert dates == ["2026-07-10", "2026-07-11"]

    def test_retention_prunes_oldest_beyond_keep(self, tmp_path):
        cfg = _config(tmp_path, db_backup_retention=2)
        db.init_db(cfg.db_path)
        for day in ("2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11"):
            db_backup.backup_databases(cfg, today=day)
        dates = sorted(p.name for p in _root(tmp_path).iterdir() if p.is_dir())
        # Only the 2 newest remain.
        assert dates == ["2026-07-10", "2026-07-11"]

    def test_retention_zero_disables_pruning(self, tmp_path):
        cfg = _config(tmp_path, db_backup_retention=0)
        db.init_db(cfg.db_path)
        for day in ("2026-07-08", "2026-07-09", "2026-07-10"):
            db_backup.backup_databases(cfg, today=day)
        dates = {p.name for p in _root(tmp_path).iterdir() if p.is_dir()}
        assert dates == {"2026-07-08", "2026-07-09", "2026-07-10"}

    def test_same_day_rerun_refreshes_today(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        db_backup.backup_databases(cfg, today=FIXED_DAY)
        dated = [p for p in _root(tmp_path).iterdir() if p.is_dir()]
        assert len(dated) == 1


class TestCollapseGuard:
    def test_emptied_module_db_marked_suspect(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)  # prior snapshot has data

        db_backup.backup_databases(cfg, today="2026-07-11")

        # Now the live DB is emptied (the ISSUE-156 empty-shadow scenario).
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM places")
        conn.commit()
        conn.close()

        results = db_backup.backup_databases(cfg, today="2026-07-12")
        loc = next(r for r in results if r["label"] == "location:alice")
        assert loc["status"] == "suspect"
        assert loc["prior_rows"] == 1
        assert loc["new_rows"] == 0

        dated = _root(tmp_path) / "2026-07-12" / "alice"
        # The fresh empty snapshot is quarantined, not treated as latest-good.
        assert (dated / "location.db.suspect").exists()
        assert not (dated / "location.db").exists()

    def test_prior_good_snapshot_survives_collapse(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-11")

        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM places")
        conn.commit()
        conn.close()
        db_backup.backup_databases(cfg, today="2026-07-12")

        good = _root(tmp_path) / "2026-07-11" / "alice" / "location.db"
        assert good.exists()
        conn = sqlite3.connect(good)
        try:
            assert conn.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 1
        finally:
            conn.close()

    def test_zero_to_zero_is_not_suspect(self, tmp_path):
        # A DB that was legitimately empty and stays empty is fine, not suspect.
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        _seed_module_db(cfg, "alice", "location")  # 0 rows
        db_backup.backup_databases(cfg, today="2026-07-11")
        results = db_backup.backup_databases(cfg, today="2026-07-12")
        loc = next(r for r in results if r["label"] == "location:alice")
        assert loc["status"] == "ok"

    def test_first_ever_snapshot_never_suspect(self, tmp_path):
        # No prior snapshot to compare against -> can't be a collapse.
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        _seed_module_db(cfg, "alice", "location")  # 0 rows, no prior
        results = db_backup.backup_databases(cfg, today="2026-07-12")
        loc = next(r for r in results if r["label"] == "location:alice")
        assert loc["status"] == "ok"


class TestRetentionProtectsNewestGood:
    def test_prune_keeps_dir_holding_newest_good_copy(self, tmp_path):
        # keep=1, but the newest dir's copy is suspect -> the older good dir must
        # be protected from pruning so we don't lose the last good copy.
        cfg = _config(tmp_path, db_backup_retention=1)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-11")  # good

        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM places")
        conn.commit()
        conn.close()
        db_backup.backup_databases(cfg, today="2026-07-12")  # collapse -> suspect

        # The 07-11 dir holds the newest *good* location copy; keep it.
        assert (_root(tmp_path) / "2026-07-11" / "alice" / "location.db").exists()


class TestBackupPermissions:
    def test_backup_tree_and_files_are_locked_down(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        db_backup.backup_databases(cfg, today=FIXED_DAY)

        dated = _root(tmp_path) / FIXED_DAY
        snap = dated / "framework" / "istota.db"
        # Dir 0700, file 0600 -> no group/other bits.
        assert stat.S_IMODE(dated.stat().st_mode) == 0o700
        assert stat.S_IMODE(snap.stat().st_mode) == 0o600
