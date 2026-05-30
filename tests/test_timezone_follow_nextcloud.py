"""ISSUE-102: explicit 'follow Nextcloud timezone' toggle.

A user who pins their timezone in the Istota web UI (toggle off) must not have
it silently reverted to the Nextcloud profile value on every scheduler restart.
The toggle defaults to on (legacy behavior: Nextcloud is canonical and keeps
syncing across restarts).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from istota import db, user_profiles
from istota.config import Config, NextcloudConfig, UserConfig


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "istota.db"
    db.init_db(p)
    return p


class TestProfileColumn:
    def test_new_row_defaults_to_following_nextcloud(self, db_path: Path):
        prof = user_profiles.ensure_profile(db_path, "alice")
        assert prof.timezone_follow_nextcloud is True
        # Round-trips through the DB.
        again = user_profiles.get_profile(db_path, "alice")
        assert again is not None
        assert again.timezone_follow_nextcloud is True

    def test_update_pins_timezone(self, db_path: Path):
        user_profiles.ensure_profile(db_path, "bob")
        user_profiles.update_profile(
            db_path, "bob", timezone="Europe/Berlin", timezone_follow_nextcloud=False
        )
        prof = user_profiles.get_profile(db_path, "bob")
        assert prof is not None
        assert prof.timezone == "Europe/Berlin"
        assert prof.timezone_follow_nextcloud is False

    def test_update_with_status_noop_on_same_toggle(self, db_path: Path):
        user_profiles.ensure_profile(db_path, "carol")
        user_profiles.update_profile(db_path, "carol", timezone_follow_nextcloud=False)
        _, state = user_profiles.update_profile_with_status(
            db_path, "carol", timezone_follow_nextcloud=False
        )
        assert state == "noop"
        _, state2 = user_profiles.update_profile_with_status(
            db_path, "carol", timezone_follow_nextcloud=True
        )
        assert state2 == "updated"

    def test_import_from_user_configs_seeds_toggle(self, db_path: Path):
        uc = UserConfig(display_name="Dave", timezone="America/New_York")
        uc.timezone_follow_nextcloud = False
        written = user_profiles.import_from_user_configs(db_path, {"dave": uc})
        assert written == 1
        prof = user_profiles.get_profile(db_path, "dave")
        assert prof is not None
        assert prof.timezone_follow_nextcloud is False

    def test_merge_sets_toggle_on_user_config(self, db_path: Path):
        uc = UserConfig(display_name="Eve")
        prof = user_profiles.UserProfile(
            user_id="eve", timezone="Asia/Tokyo", timezone_follow_nextcloud=False
        )
        user_profiles.merge_into_user_config(prof, uc)
        assert uc.timezone == "Asia/Tokyo"
        assert uc.timezone_follow_nextcloud is False


def _make_config(db_path: Path, user_id: str, follow: bool, tz: str) -> Config:
    config = Config()
    config.db_path = db_path
    config.nextcloud = NextcloudConfig(url="https://nc.example", username="bot", app_password="x")
    uc = UserConfig(display_name=user_id, timezone=tz)
    uc.timezone_follow_nextcloud = follow
    config.users = {user_id: uc}
    return config


class TestHydration:
    def test_follow_on_overrides_and_persists(self, db_path: Path, monkeypatch):
        # DB row currently has the old NC value; NC now reports a new tz.
        user_profiles.upsert_profile(
            db_path,
            user_profiles.UserProfile(
                user_id="alice", timezone="UTC", timezone_follow_nextcloud=True
            ),
        )
        config = _make_config(db_path, "alice", follow=True, tz="UTC")

        from istota import nextcloud_api

        monkeypatch.setattr(nextcloud_api, "fetch_user_info", lambda *a, **k: None)
        monkeypatch.setattr(
            nextcloud_api, "fetch_user_timezone", lambda *a, **k: "America/Chicago"
        )

        nextcloud_api.hydrate_user_configs(config)

        # In-memory updated...
        assert config.users["alice"].timezone == "America/Chicago"
        # ...and persisted so it survives the post-hydrate DB overlay + restarts.
        prof = user_profiles.get_profile(db_path, "alice")
        assert prof is not None
        assert prof.timezone == "America/Chicago"

    def test_follow_off_keeps_pinned_value_and_does_not_write(self, db_path: Path, monkeypatch):
        user_profiles.upsert_profile(
            db_path,
            user_profiles.UserProfile(
                user_id="bob", timezone="Europe/Berlin", timezone_follow_nextcloud=False
            ),
        )
        config = _make_config(db_path, "bob", follow=False, tz="Europe/Berlin")

        from istota import nextcloud_api

        called = {"tz": 0}

        def _tz(*a, **k):
            called["tz"] += 1
            return "America/Chicago"

        monkeypatch.setattr(nextcloud_api, "fetch_user_info", lambda *a, **k: None)
        monkeypatch.setattr(nextcloud_api, "fetch_user_timezone", _tz)

        nextcloud_api.hydrate_user_configs(config)

        # NC tz never fetched, in-memory value untouched, DB untouched.
        assert called["tz"] == 0
        assert config.users["bob"].timezone == "Europe/Berlin"
        prof = user_profiles.get_profile(db_path, "bob")
        assert prof is not None
        assert prof.timezone == "Europe/Berlin"

    def test_sync_user_timezone_fetches_and_persists(self, db_path: Path, monkeypatch):
        user_profiles.upsert_profile(
            db_path,
            user_profiles.UserProfile(
                user_id="alice", timezone="UTC", timezone_follow_nextcloud=True
            ),
        )
        config = _make_config(db_path, "alice", follow=True, tz="UTC")

        from istota import nextcloud_api

        monkeypatch.setattr(
            nextcloud_api, "fetch_user_timezone", lambda *a, **k: "Europe/Berlin"
        )
        result = nextcloud_api.sync_user_timezone(config, "alice")
        assert result == "Europe/Berlin"
        prof = user_profiles.get_profile(db_path, "alice")
        assert prof is not None and prof.timezone == "Europe/Berlin"

    def test_sync_user_timezone_noop_without_nextcloud_url(self, db_path: Path, monkeypatch):
        config = _make_config(db_path, "alice", follow=True, tz="UTC")
        config.nextcloud.url = ""

        from istota import nextcloud_api

        called = {"n": 0}

        def _tz(*a, **k):
            called["n"] += 1
            return "Europe/Berlin"

        monkeypatch.setattr(nextcloud_api, "fetch_user_timezone", _tz)
        assert nextcloud_api.sync_user_timezone(config, "alice") is None
        assert called["n"] == 0

    def test_follow_on_seeds_new_user_without_row(self, db_path: Path, monkeypatch):
        # No DB row yet — hydrate should still set the in-memory tz (seed path);
        # persistence is a no-op (the row is created later by the importer).
        config = _make_config(db_path, "newbie", follow=True, tz="UTC")

        from istota import nextcloud_api

        monkeypatch.setattr(nextcloud_api, "fetch_user_info", lambda *a, **k: None)
        monkeypatch.setattr(
            nextcloud_api, "fetch_user_timezone", lambda *a, **k: "Australia/Sydney"
        )

        nextcloud_api.hydrate_user_configs(config)
        assert config.users["newbie"].timezone == "Australia/Sydney"
        assert user_profiles.get_profile(db_path, "newbie") is None
