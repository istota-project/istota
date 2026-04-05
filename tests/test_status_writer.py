"""Tests for status file writer."""

import json
from unittest.mock import MagicMock, patch

from istota.config import Config, NextcloudConfig
from istota.status_writer import init_status_writer, write_status


def _make_config(**kwargs):
    return Config(
        nextcloud=NextcloudConfig(
            url="https://cloud.example.com",
            username="botuser",
            app_password="secret",
        ),
        **kwargs,
    )


class TestStatusWriter:
    def test_writes_valid_json(self):
        cfg = _make_config()
        init_status_writer()

        with patch("istota.status_writer.httpx") as mock_httpx:
            mock_httpx.put.return_value = MagicMock(raise_for_status=MagicMock())
            write_status(cfg, active_workers=2, pending_fg=3, pending_bg=1)

            # Verify MKCOL was called for config dir
            mock_httpx.request.assert_called_once()
            assert mock_httpx.request.call_args[0][0] == "MKCOL"

            # Verify PUT was called with valid JSON
            mock_httpx.put.assert_called_once()
            content = mock_httpx.put.call_args[1]["content"]
            data = json.loads(content)
            assert data["status"] == "online"
            assert data["worker_pool"]["active"] == 2
            assert data["queue"]["pending_foreground"] == 3
            assert data["queue"]["pending_background"] == 1

    def test_includes_bot_name_and_version(self):
        cfg = _make_config(bot_name="Zorg")
        init_status_writer()

        with patch("istota.status_writer.httpx") as mock_httpx:
            mock_httpx.put.return_value = MagicMock(raise_for_status=MagicMock())
            write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

            content = mock_httpx.put.call_args[1]["content"]
            data = json.loads(content)
            assert data["bot_name"] == "Zorg"
            assert "version" in data

    def test_no_nextcloud_config_is_noop(self):
        cfg = Config()
        # Should not raise
        write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

    def test_webdav_url_correct(self):
        cfg = _make_config()
        init_status_writer()

        with patch("istota.status_writer.httpx") as mock_httpx:
            mock_httpx.put.return_value = MagicMock(raise_for_status=MagicMock())
            write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

            put_url = mock_httpx.put.call_args[0][0]
            assert put_url == "https://cloud.example.com/remote.php/dav/files/botuser/config/status.json"

    def test_users_configured_count(self):
        from istota.config import UserConfig
        cfg = _make_config()
        cfg.users["alice"] = UserConfig(display_name="Alice")
        cfg.users["bob"] = UserConfig(display_name="Bob")

        init_status_writer()

        with patch("istota.status_writer.httpx") as mock_httpx:
            mock_httpx.put.return_value = MagicMock(raise_for_status=MagicMock())
            write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

            content = mock_httpx.put.call_args[1]["content"]
            data = json.loads(content)
            assert data["users_configured"] == 2
