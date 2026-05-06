"""Tests for the feeds + money module loader gates.

Both loaders gate on ``Config.is_module_enabled(user_id, <module>)`` after
the modules / connected services refactor — they no longer require a
matching ``[[resources]]`` entry. These tests guard the gate in both
directions and the workspace-mode resolution path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from istota.config import Config, UserConfig


def _config(tmp_path: Path, *, users: dict[str, UserConfig]) -> Config:
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    return Config(
        nextcloud_mount_path=mount,
        users=users,
        bot_name="Istota",
        db_path=tmp_path / "no.db",  # not exercised — keeps best-effort paths quiet
    )


# ---- feeds ---------------------------------------------------------------


class TestFeedsLoader:
    def test_resolve_for_user_with_module_enabled(self, tmp_path):
        from istota.feeds import resolve_for_user

        cfg = _config(tmp_path, users={"alice": UserConfig(display_name="Alice")})
        ctx = resolve_for_user("alice", cfg)
        # Workspace path comes from get_user_bot_path. Just check that the
        # synthesized context points under the mount.
        assert str(ctx.data_dir).startswith(str(tmp_path / "mount"))

    def test_resolve_for_user_raises_when_module_disabled(self, tmp_path):
        from istota.feeds import UserNotFoundError, resolve_for_user

        cfg = _config(
            tmp_path,
            users={"alice": UserConfig(display_name="Alice", disabled_modules=["feeds"])},
        )
        with pytest.raises(UserNotFoundError, match="feeds module disabled"):
            resolve_for_user("alice", cfg)

    def test_resolve_for_user_raises_when_user_unknown(self, tmp_path):
        from istota.feeds import UserNotFoundError, resolve_for_user

        cfg = _config(tmp_path, users={"alice": UserConfig()})
        with pytest.raises(UserNotFoundError):
            resolve_for_user("ghost", cfg)

    def test_list_users_filters_disabled(self, tmp_path):
        from istota.feeds import list_users

        cfg = _config(tmp_path, users={
            "alice": UserConfig(),
            "bob": UserConfig(disabled_modules=["feeds"]),
        })
        assert list_users(cfg) == ["alice"]


# ---- money ---------------------------------------------------------------


class TestMoneyLoader:
    def test_resolve_for_user_with_module_enabled(self, tmp_path):
        from istota.money import resolve_for_user

        cfg = _config(tmp_path, users={"alice": UserConfig(display_name="Alice")})
        ctx = resolve_for_user("alice", cfg)
        assert str(ctx.data_dir).startswith(str(tmp_path / "mount"))

    def test_resolve_for_user_raises_when_module_disabled(self, tmp_path):
        from istota.money import UserNotFoundError, resolve_for_user

        cfg = _config(
            tmp_path,
            users={"alice": UserConfig(display_name="Alice", disabled_modules=["money"])},
        )
        with pytest.raises(UserNotFoundError, match="money module disabled"):
            resolve_for_user("alice", cfg)

    def test_list_users_filters_disabled(self, tmp_path):
        from istota.money import list_users

        cfg = _config(tmp_path, users={
            "alice": UserConfig(),
            "bob": UserConfig(disabled_modules=["money"]),
        })
        assert list_users(cfg) == ["alice"]
