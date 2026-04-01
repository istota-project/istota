"""Tests for status file writer."""

import json
from pathlib import Path

from istota.config import Config
from istota.status_writer import init_status_writer, write_status


class TestStatusWriter:
    def test_writes_valid_json(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        cfg = Config(users_dir=users_dir)

        init_status_writer()
        write_status(cfg, active_workers=2, pending_fg=3, pending_bg=1)

        status_path = tmp_path / "status.json"
        assert status_path.exists()
        data = json.loads(status_path.read_text())
        assert data["status"] == "online"
        assert data["worker_pool"]["active"] == 2
        assert data["queue"]["pending_foreground"] == 3
        assert data["queue"]["pending_background"] == 1

    def test_includes_bot_name_and_version(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        cfg = Config(bot_name="Zorg", users_dir=users_dir)

        init_status_writer()
        write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

        data = json.loads((tmp_path / "status.json").read_text())
        assert data["bot_name"] == "Zorg"
        assert "version" in data

    def test_no_users_dir_is_noop(self):
        cfg = Config()
        # Should not raise
        write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

    def test_atomic_write(self, tmp_path):
        """Status file is written atomically (via tmp + rename)."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        cfg = Config(users_dir=users_dir)

        init_status_writer()
        write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

        # No leftover .tmp file
        assert not (tmp_path / "status.tmp").exists()
        assert (tmp_path / "status.json").exists()

    def test_users_configured_count(self, tmp_path):
        from istota.config import UserConfig
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        cfg = Config(users_dir=users_dir)
        cfg.users["alice"] = UserConfig(display_name="Alice")
        cfg.users["bob"] = UserConfig(display_name="Bob")

        init_status_writer()
        write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

        data = json.loads((tmp_path / "status.json").read_text())
        assert data["users_configured"] == 2
