"""Tests for restoring cold DB snapshots back to local disk."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from istota import db
from istota.maintenance import db_backup
from istota.maintenance import db_restore
from tests.support import snapshots
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
    # Explicit db_backup_dir so backup_databases doesn't ismount-gate the tmp dir.
    sched.setdefault("db_backup_dir", str(tmp_path / "backups"))
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud=NextcloudConfig(),
        talk=TalkConfig(),
        email=EmailConfig(),
        scheduler=SchedulerConfig(**sched),
        workspace_path=mount,
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


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch):
    """Default every restore test to 'no scheduler daemon running' so a real
    dev daemon holding the flock can't turn restores into daemon_running."""
    monkeypatch.setattr(db_restore, "_daemon_running", lambda *a, **k: False)


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

    def test_a_virtual_table_whose_module_is_not_loaded_does_not_read_as_empty(self, tmp_path):
        # Memory search keeps a sqlite-vec table in the framework DB; a plain
        # connection has no vec0 module, so counting it raises.
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.create_task(conn, prompt="hi", user_id="alice")
        conn = sqlite3.connect(cfg.db_path)
        try:
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute(
                "INSERT INTO sqlite_master (type, name, tbl_name, rootpage, sql) VALUES "
                "('table', 'chunks_vec', 'chunks_vec', 0, "
                "'CREATE VIRTUAL TABLE chunks_vec USING vec0(chunk_id INTEGER PRIMARY KEY)')"
            )
            conn.commit()
        finally:
            conn.close()
        db_backup.backup_databases(cfg, today="2026-07-12")
        cfg.db_path.unlink()

        res = db_restore.restore_database(cfg, label="framework")
        assert res["status"] == "restored"
        assert res["rows"] > 0


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


class TestRestoreDaemonGuard:
    def test_refuses_when_daemon_running(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-12")
        path.unlink()

        monkeypatch.setattr(db_restore, "_daemon_running", lambda *a, **k: True)
        res = db_restore.restore_database(cfg, user="alice", module="location")
        assert res["status"] == "daemon_running"
        assert not path.exists()  # nothing copied while the daemon holds the DB

    def test_dry_run_allowed_while_daemon_running(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-12")
        path.unlink()

        monkeypatch.setattr(db_restore, "_daemon_running", lambda *a, **k: True)
        res = db_restore.restore_database(cfg, user="alice", module="location", dry_run=True)
        assert res["status"] == "would_restore"  # read-only, safe even if daemon up


class TestRestoreSidecars:
    def test_clears_stale_wal_shm(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        path = _seed_module_db(cfg, "alice", "location")
        _add_place(path)
        db_backup.backup_databases(cfg, today="2026-07-12")

        # Simulate a live-WAL DB at the restore target: main file + stale sidecars.
        wal = path.with_name(path.name + "-wal")
        shm = path.with_name(path.name + "-shm")
        wal.write_bytes(b"stale-wal")
        shm.write_bytes(b"stale-shm")

        res = db_restore.restore_database(cfg, user="alice", module="location")
        assert res["status"] == "restored"
        # The stale sidecars must be gone so they can't be replayed onto the copy.
        assert not wal.exists()
        assert not shm.exists()
        assert _place_count(path) == 1


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


def _task_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()


def _no_restore_leftovers(directory: Path) -> None:
    assert [p.name for p in directory.iterdir() if p.name.startswith(".")] == []


class TestRestoreCompressed:
    """#988: snapshots are ``<rel>.gz``, and a week of plain ones from before
    the change sit beside them."""

    def _backed_up(self, tmp_path, day: str = "2026-07-12"):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.create_task(conn, prompt="hi", user_id="alice")
        loc = _seed_module_db(cfg, "alice", "location")
        _add_place(loc)
        db_backup.backup_databases(cfg, today=day)
        snapshots.compress_tree(tmp_path / "backups" / day)
        return cfg, loc

    def test_the_newest_compressed_snapshot_is_restored(self, tmp_path):
        cfg, loc = self._backed_up(tmp_path)
        cfg.db_path.unlink()
        loc.unlink()

        framework = db_restore.restore_database(cfg, label="framework")
        module = db_restore.restore_database(cfg, user="alice", module="location")
        assert framework["status"] == "restored"
        assert framework["src"].endswith("framework/istota.db.gz")
        assert module["status"] == "restored"
        assert _task_count(cfg.db_path) == 1
        assert _place_count(loc) == 1
        _no_restore_leftovers(loc.parent)

    def test_a_compressed_snapshot_is_restored_by_date(self, tmp_path):
        cfg, loc = self._backed_up(tmp_path, "2026-07-11")
        _add_place(loc, name="work")
        db_backup.backup_databases(cfg, today="2026-07-12")
        loc.unlink()

        res = db_restore.restore_database(cfg, user="alice", module="location", date="2026-07-11")
        assert res["status"] == "restored"
        assert res["date"] == "2026-07-11"
        assert _place_count(loc) == 1

    def test_restore_all_takes_the_compressed_set(self, tmp_path):
        cfg, loc = self._backed_up(tmp_path)
        cfg.db_path.unlink()
        loc.unlink()

        by_label = {r["label"]: r["status"] for r in db_restore.restore_all(cfg)}
        assert by_label["framework"] == "restored"
        assert by_label["location:alice"] == "restored"
        assert _place_count(loc) == 1

    def test_a_mixed_set_restores_either_form(self, tmp_path):
        """An older plain snapshot beside a newer compressed one."""
        cfg, loc = self._backed_up(tmp_path, "2026-07-11")
        snapshots.plain(tmp_path / "backups" / "2026-07-11", "alice/location.db")
        _add_place(loc, name="work")
        db_backup.backup_databases(cfg, today="2026-07-12")
        snapshots.compress_tree(tmp_path / "backups" / "2026-07-12")
        loc.unlink()

        newest = db_restore.restore_database(cfg, user="alice", module="location")
        assert newest["date"] == "2026-07-12"
        assert _place_count(loc) == 2
        older = db_restore.restore_database(cfg, user="alice", module="location", date="2026-07-11")
        assert older["status"] == "restored"
        assert older["src"].endswith("alice/location.db")
        assert _place_count(loc) == 1

    def test_newest_good_skips_a_compressed_quarantine(self, tmp_path):
        cfg, loc = self._backed_up(tmp_path, "2026-07-11")
        conn = sqlite3.connect(loc)
        conn.execute("DELETE FROM places")
        conn.commit()
        conn.close()
        db_backup.backup_databases(cfg, today="2026-07-12")
        quarantined = tmp_path / "backups" / "2026-07-12" / "alice"
        assert all(p.name.endswith(".suspect") for p in quarantined.iterdir())
        loc.unlink()

        res = db_restore.restore_database(cfg, user="alice", module="location")
        assert res["date"] == "2026-07-11"
        assert _place_count(loc) == 1

    def test_a_dry_run_counts_rows_and_writes_nothing(self, tmp_path):
        cfg, loc = self._backed_up(tmp_path)
        loc.unlink()

        res = db_restore.restore_database(cfg, user="alice", module="location", dry_run=True)
        assert res["status"] == "would_restore"
        assert res["rows"] == 1
        assert not loc.exists()
        _no_restore_leftovers(loc.parent)
        staging = cfg.db_path.parent / ".backup-staging"
        assert not staging.exists() or not any(staging.rglob("*"))

    def test_an_empty_compressed_snapshot_is_refused(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        loc = _seed_module_db(cfg, "alice", "location")
        db_backup.backup_databases(cfg, today="2026-07-12")
        snapshots.compress_tree(tmp_path / "backups" / "2026-07-12")
        loc.unlink()

        res = db_restore.restore_database(cfg, user="alice", module="location")
        assert res["status"] == "refused_empty"
        assert not loc.exists()

    def test_a_truncated_compressed_snapshot_leaves_the_live_database(self, tmp_path):
        cfg, loc = self._backed_up(tmp_path)
        packed = snapshots.compressed(tmp_path / "backups" / "2026-07-12", "alice/location.db")
        packed.write_bytes(packed.read_bytes()[: packed.stat().st_size // 2])
        _add_place(loc, name="work")
        before = loc.read_bytes()

        res = db_restore.restore_database(cfg, user="alice", module="location", force=True)
        assert res["status"] == "error"
        assert loc.read_bytes() == before
        _no_restore_leftovers(loc.parent)
