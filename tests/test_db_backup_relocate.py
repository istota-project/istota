"""Tests for the one-shot legacy snapshot-tree relocation."""

from __future__ import annotations

import fcntl
from pathlib import Path

import pytest
import yaml

from istota import db_backup_relocate
from istota.config import Config, NextcloudConfig, SchedulerConfig


def _config(tmp_path: Path, *, nextcloud_url: str = "") -> Config:
    mount = tmp_path / "mount"
    mount.mkdir()
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud=NextcloudConfig(url=nextcloud_url),
        scheduler=SchedulerConfig(db_backup_dir=""),
        nextcloud_mount_path=mount,
    )


def _legacy_snapshot(config: Config, day: str, payload: bytes = b"db") -> Path:
    path = (
        Path(config.nextcloud_mount_path)
        / "istota-db-backups"
        / day
        / "framework"
        / "istota.db"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _destination(config: Config) -> Path:
    return (
        Path(config.nextcloud_mount_path)
        / "Backups"
        / "db"
        / "snapshots"
    )


class TestRelocateSnapshots:
    def test_moves_every_dated_directory_and_marks_the_layout(self, tmp_path):
        config = _config(tmp_path)
        _legacy_snapshot(config, "2026-09-01", b"one")
        _legacy_snapshot(config, "2026-09-02", b"two")

        report = db_backup_relocate.relocate_snapshots(
            config, daemon_running=lambda: False
        )

        assert report.status == "migrated"
        assert report.moved == ("2026-09-01", "2026-09-02")
        assert (_destination(config) / "2026-09-01" / "framework" / "istota.db").read_bytes() == b"one"
        assert (_destination(config) / "2026-09-02" / "framework" / "istota.db").read_bytes() == b"two"
        assert (_destination(config) / db_backup_relocate.MARKER_NAME).read_text().strip() == db_backup_relocate.LAYOUT_VERSION
        assert not (Path(config.nextcloud_mount_path) / "istota-db-backups").exists()

    def test_refuses_when_destination_already_has_dated_directories(self, tmp_path):
        config = _config(tmp_path)
        old = _legacy_snapshot(config, "2026-09-01")
        existing = _destination(config) / "2026-09-07" / "framework" / "istota.db"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"new")

        report = db_backup_relocate.relocate_snapshots(
            config, daemon_running=lambda: False
        )

        assert report.status == "destination_has_snapshots"
        assert old.exists()
        assert existing.read_bytes() == b"new"
        assert not (_destination(config) / db_backup_relocate.MARKER_NAME).exists()

    def test_refuses_while_scheduler_holds_the_flock(self, tmp_path):
        config = _config(tmp_path)
        old = _legacy_snapshot(config, "2026-09-01")

        report = db_backup_relocate.relocate_snapshots(
            config, daemon_running=lambda: True
        )

        assert report.status == "daemon_running"
        assert old.exists()
        assert not _destination(config).exists()

    def test_dry_run_reports_without_writing(self, tmp_path):
        config = _config(tmp_path)
        old = _legacy_snapshot(config, "2026-09-01")

        report = db_backup_relocate.relocate_snapshots(
            config, dry_run=True, daemon_running=lambda: True
        )

        assert report.status == "would_migrate"
        assert report.moved == ("2026-09-01",)
        assert old.exists()
        assert not _destination(config).exists()

    def test_second_run_reads_the_marker_as_complete(self, tmp_path):
        config = _config(tmp_path)
        _legacy_snapshot(config, "2026-09-01")
        first = db_backup_relocate.relocate_snapshots(
            config, daemon_running=lambda: False
        )

        second = db_backup_relocate.relocate_snapshots(
            config, daemon_running=lambda: True
        )

        assert first.status == "migrated"
        assert second.status == "already_migrated"

    def test_marker_refuses_legacy_dates_created_after_a_rollback(self, tmp_path):
        config = _config(tmp_path)
        _legacy_snapshot(config, "2026-09-01")
        first = db_backup_relocate.relocate_snapshots(
            config, daemon_running=lambda: False
        )
        late = _legacy_snapshot(config, "2026-09-08")

        second = db_backup_relocate.relocate_snapshots(
            config, daemon_running=lambda: False
        )

        assert first.status == "migrated"
        assert second.status == "legacy_after_marker"
        assert late.exists()

    def test_explicit_legacy_destination_is_not_moved_away_from_config(self, tmp_path):
        config = _config(tmp_path)
        old = _legacy_snapshot(config, "2026-09-01")
        config.scheduler.db_backup_dir = str(
            Path(config.nextcloud_mount_path) / "istota-db-backups"
        )

        report = db_backup_relocate.relocate_snapshots(
            config, daemon_running=lambda: False
        )

        assert report.status == "non_default_destination"
        assert old.exists()
        assert not _destination(config).exists()

    def test_holds_the_daemon_lock_until_the_last_rename(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _legacy_snapshot(config, "2026-09-01")
        lock_path = tmp_path / "scheduler.lock"
        original_rename = Path.rename

        def rename_while_checking_lock(path, target):
            with open(lock_path, "a") as competing_daemon:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(
                        competing_daemon,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
            return original_rename(path, target)

        monkeypatch.setattr(Path, "rename", rename_while_checking_lock)

        report = db_backup_relocate.relocate_snapshots(
            config, daemon_lock_path=lock_path
        )

        assert report.status == "migrated"

    def test_non_dated_entries_are_reported_and_left_in_place(self, tmp_path):
        config = _config(tmp_path)
        _legacy_snapshot(config, "2026-09-01")
        legacy = Path(config.nextcloud_mount_path) / "istota-db-backups"
        note = legacy / "README.txt"
        note.write_text("operator note")

        report = db_backup_relocate.relocate_snapshots(
            config, daemon_running=lambda: False
        )

        assert report.status == "migrated"
        assert report.left == ("README.txt",)
        assert note.exists()
        assert (_destination(config) / "2026-09-01").is_dir()

    def test_no_legacy_snapshots_is_a_read_only_noop(self, tmp_path):
        config = _config(tmp_path)

        report = db_backup_relocate.relocate_snapshots(
            config, daemon_running=lambda: True
        )

        assert report.status == "no_snapshots"
        assert not _destination(config).exists()

    def test_nextcloud_mount_must_be_live_before_moving(self, tmp_path, monkeypatch):
        config = _config(tmp_path, nextcloud_url="https://cloud.example.com")
        old = _legacy_snapshot(config, "2026-09-01")
        monkeypatch.setattr(db_backup_relocate.os.path, "ismount", lambda _p: False)

        report = db_backup_relocate.relocate_snapshots(
            config, daemon_running=lambda: False
        )

        assert report.status == "mount_unavailable"
        assert old.exists()


class TestCommand:
    def test_cli_loads_config_and_moves_the_tree(self, tmp_path, monkeypatch):
        mount = tmp_path / "mount"
        mount.mkdir()
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'db_path = "{tmp_path / "istota.db"}"\n'
            f'nextcloud_mount_path = "{mount}"\n'
        )
        (tmp_path / "fixture").mkdir()
        config = _config(tmp_path / "fixture")
        config.nextcloud_mount_path = mount
        _legacy_snapshot(config, "2026-09-01")
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(config_path))
        monkeypatch.setattr(
            db_backup_relocate,
            "_hold_daemon_lock",
            lambda _path: db_backup_relocate._injected_daemon_lock(lambda: False),
        )

        assert db_backup_relocate.main([]) == db_backup_relocate.EXIT_OK
        assert (_destination(config) / "2026-09-01").is_dir()


class TestAnsibleIntegration:
    def test_role_stops_scheduler_around_the_migrator(self):
        repo = Path(__file__).resolve().parent.parent
        tasks = yaml.safe_load(
            (repo / "deploy" / "ansible" / "tasks" / "main.yml").read_text()
        )
        relocation = next(
            task
            for task in tasks
            if task.get("name") == "Relocate legacy database snapshots"
        )
        rendered = str(relocation)
        assert "db_backup_relocate" in rendered
        assert "state': 'stopped" in rendered
        assert "state': 'started" in rendered
        assert "_legacy_db_snapshot_dirs" in rendered

        detection = next(
            task
            for task in tasks
            if task.get("name") == "Detect legacy database snapshot directories"
        )
        assert detection.get("failed_when") is None
