"""ISSUE-102: Nextcloud timezone is seed-only.

A timezone the user sets in the Istota UI (stored in user_profiles.timezone and
overlaid onto UserConfig.timezone before hydration runs) must win over Nextcloud
and survive scheduler restarts. Nextcloud only fills in the timezone when the
user hasn't set one (the default "UTC", or empty).
"""

from __future__ import annotations

from istota import nextcloud_api
from istota.config import Config, NextcloudConfig, UserConfig


def _make_config(user_id: str, tz: str) -> Config:
    config = Config()
    config.nextcloud = NextcloudConfig(
        url="https://nc.example", username="bot", app_password="x"
    )
    uc = UserConfig(display_name=user_id, timezone=tz)
    config.users = {user_id: uc}
    return config


class TestHydrateTimezoneSeedOnly:
    def test_seeds_when_default_utc(self, monkeypatch):
        config = _make_config("alice", tz="UTC")
        monkeypatch.setattr(nextcloud_api, "fetch_user_info", lambda *a, **k: None)
        monkeypatch.setattr(
            nextcloud_api, "fetch_user_timezone", lambda *a, **k: "America/Chicago"
        )

        nextcloud_api.hydrate_user_configs(config)

        assert config.users["alice"].timezone == "America/Chicago"

    def test_seeds_when_empty(self, monkeypatch):
        config = _make_config("alice", tz="")
        monkeypatch.setattr(nextcloud_api, "fetch_user_info", lambda *a, **k: None)
        monkeypatch.setattr(
            nextcloud_api, "fetch_user_timezone", lambda *a, **k: "Europe/Berlin"
        )

        nextcloud_api.hydrate_user_configs(config)

        assert config.users["alice"].timezone == "Europe/Berlin"

    def test_does_not_overwrite_user_set_value(self, monkeypatch):
        # User picked America/New_York in the Istota UI; Nextcloud reports
        # something else. The UI value must win and Nextcloud must not even be
        # consulted.
        config = _make_config("bob", tz="America/New_York")
        called = {"tz": 0}

        def _tz(*a, **k):
            called["tz"] += 1
            return "Asia/Tokyo"

        monkeypatch.setattr(nextcloud_api, "fetch_user_info", lambda *a, **k: None)
        monkeypatch.setattr(nextcloud_api, "fetch_user_timezone", _tz)

        nextcloud_api.hydrate_user_configs(config)

        assert config.users["bob"].timezone == "America/New_York"
        assert called["tz"] == 0

    def test_stays_utc_when_nextcloud_reports_nothing(self, monkeypatch):
        config = _make_config("carol", tz="UTC")
        monkeypatch.setattr(nextcloud_api, "fetch_user_info", lambda *a, **k: None)
        monkeypatch.setattr(nextcloud_api, "fetch_user_timezone", lambda *a, **k: None)

        nextcloud_api.hydrate_user_configs(config)

        assert config.users["carol"].timezone == "UTC"
