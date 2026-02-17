"""Configuration loading for istota.nextcloud_api module."""

from unittest.mock import patch, MagicMock

import pytest

from istota.config import Config, NextcloudConfig, UserConfig
from istota.nextcloud_api import (
    fetch_user_info,
    fetch_user_timezone,
    hydrate_user_configs,
)


class TestFetchUserInfo:
    def test_no_nextcloud_config(self):
        config = Config()
        assert fetch_user_info(config, "alice") is None

    @patch("istota.nextcloud_api.httpx.get")
    def test_success(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "ocs": {
                    "data": {
                        "displayname": "Alice Smith",
                        "email": "alice@example.com",
                    }
                }
            },
        )
        mock_get.return_value.raise_for_status = MagicMock()

        config = Config(nextcloud=NextcloudConfig(
            url="https://nc.example.com",
            username="istota",
            app_password="secret",
        ))
        result = fetch_user_info(config, "alice")
        assert result == {"displayname": "Alice Smith", "email": "alice@example.com"}
        mock_get.assert_called_once()

    @patch("istota.nextcloud_api.httpx.get")
    def test_http_error(self, mock_get):
        mock_get.return_value.raise_for_status.side_effect = Exception("404")
        config = Config(nextcloud=NextcloudConfig(
            url="https://nc.example.com",
            username="istota",
            app_password="secret",
        ))
        result = fetch_user_info(config, "alice")
        assert result is None


class TestFetchUserTimezone:
    def test_no_nextcloud_config(self):
        config = Config()
        assert fetch_user_timezone(config, "alice") is None

    @patch("istota.nextcloud_api.httpx.get")
    def test_success(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "ocs": {"data": {"data": "America/New_York"}}
            },
        )
        mock_get.return_value.raise_for_status = MagicMock()

        config = Config(nextcloud=NextcloudConfig(
            url="https://nc.example.com",
            username="istota",
            app_password="secret",
        ))
        result = fetch_user_timezone(config, "alice")
        assert result == "America/New_York"

    @patch("istota.nextcloud_api.httpx.get")
    def test_empty_timezone(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ocs": {"data": {"data": ""}}},
        )
        mock_get.return_value.raise_for_status = MagicMock()

        config = Config(nextcloud=NextcloudConfig(
            url="https://nc.example.com",
            username="istota",
            app_password="secret",
        ))
        assert fetch_user_timezone(config, "alice") is None

    @patch("istota.nextcloud_api.httpx.get")
    def test_error(self, mock_get):
        mock_get.side_effect = Exception("connection error")
        config = Config(nextcloud=NextcloudConfig(
            url="https://nc.example.com",
            username="istota",
            app_password="secret",
        ))
        assert fetch_user_timezone(config, "alice") is None


class TestHydrateUserConfigs:
    def test_no_nextcloud_config(self):
        config = Config(users={"alice": UserConfig(display_name="Alice")})
        hydrate_user_configs(config)
        assert config.users["alice"].display_name == "Alice"

    @patch("istota.nextcloud_api.fetch_user_timezone")
    @patch("istota.nextcloud_api.fetch_user_info")
    def test_fills_empty_display_name(self, mock_info, mock_tz):
        mock_info.return_value = {"displayname": "Alice S.", "email": ""}
        mock_tz.return_value = None
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="x"),
            users={"alice": UserConfig(display_name="alice")},
        )
        hydrate_user_configs(config)
        assert config.users["alice"].display_name == "Alice S."

    @patch("istota.nextcloud_api.fetch_user_timezone")
    @patch("istota.nextcloud_api.fetch_user_info")
    def test_preserves_explicit_display_name(self, mock_info, mock_tz):
        mock_info.return_value = {"displayname": "Alice S.", "email": ""}
        mock_tz.return_value = None
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="x"),
            users={"alice": UserConfig(display_name="Custom Name")},
        )
        hydrate_user_configs(config)
        assert config.users["alice"].display_name == "Custom Name"

    @patch("istota.nextcloud_api.fetch_user_timezone")
    @patch("istota.nextcloud_api.fetch_user_info")
    def test_merges_email(self, mock_info, mock_tz):
        mock_info.return_value = {"displayname": "", "email": "alice@nc.com"}
        mock_tz.return_value = None
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="x"),
            users={"alice": UserConfig(email_addresses=["alice@work.com"])},
        )
        hydrate_user_configs(config)
        assert "alice@nc.com" in config.users["alice"].email_addresses
        assert "alice@work.com" in config.users["alice"].email_addresses

    @patch("istota.nextcloud_api.fetch_user_timezone")
    @patch("istota.nextcloud_api.fetch_user_info")
    def test_skips_duplicate_email_case_insensitive(self, mock_info, mock_tz):
        mock_info.return_value = {"displayname": "", "email": "Alice@Work.com"}
        mock_tz.return_value = None
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="x"),
            users={"alice": UserConfig(email_addresses=["alice@work.com"])},
        )
        hydrate_user_configs(config)
        assert len(config.users["alice"].email_addresses) == 1

    @patch("istota.nextcloud_api.fetch_user_timezone")
    @patch("istota.nextcloud_api.fetch_user_info")
    def test_fills_default_timezone(self, mock_info, mock_tz):
        mock_info.return_value = None
        mock_tz.return_value = "Europe/Berlin"
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="x"),
            users={"alice": UserConfig(timezone="UTC")},
        )
        hydrate_user_configs(config)
        assert config.users["alice"].timezone == "Europe/Berlin"

    @patch("istota.nextcloud_api.fetch_user_timezone")
    @patch("istota.nextcloud_api.fetch_user_info")
    def test_preserves_explicit_timezone(self, mock_info, mock_tz):
        mock_info.return_value = None
        mock_tz.return_value = "Europe/Berlin"
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="x"),
            users={"alice": UserConfig(timezone="America/New_York")},
        )
        hydrate_user_configs(config)
        assert config.users["alice"].timezone == "America/New_York"

    @patch("istota.nextcloud_api.fetch_user_timezone")
    @patch("istota.nextcloud_api.fetch_user_info")
    def test_api_failure_graceful(self, mock_info, mock_tz):
        mock_info.return_value = None
        mock_tz.return_value = None
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="x"),
            users={"alice": UserConfig(display_name="alice", timezone="UTC")},
        )
        hydrate_user_configs(config)
        assert config.users["alice"].display_name == "alice"
        assert config.users["alice"].timezone == "UTC"
