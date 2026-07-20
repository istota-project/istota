"""Tests for briefings module workspace synthesis + loader gate."""

from pathlib import Path

import pytest

from istota.briefings import UserNotFoundError, resolve_for_user, list_users
from istota.briefings.workspace import synthesize_briefings_context
from istota.config import Config, UserConfig


class TestWorkspaceSynth:
    def test_defaults(self, tmp_path):
        ctx = synthesize_briefings_context("stefan", tmp_path)
        assert ctx.user_id == "stefan"
        assert ctx.data_dir == (tmp_path / "briefings").resolve()
        assert ctx.db_path == ctx.data_dir / "data" / "briefings.db"
        assert ctx.workspace_root == tmp_path.resolve()

    def test_explicit_db_path(self, tmp_path):
        explicit = tmp_path / "custom.db"
        ctx = synthesize_briefings_context("stefan", tmp_path, db_path=explicit)
        assert ctx.db_path == explicit.resolve()

    def test_ensure_dirs(self, tmp_path):
        ctx = synthesize_briefings_context("stefan", tmp_path)
        ctx.ensure_dirs()
        assert ctx.data_dir.is_dir()
        assert ctx.db_path.parent.is_dir()


def _config(tmp_path: Path, *, users, disabled=None) -> Config:
    disabled = disabled or {}
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={
            uid: UserConfig(disabled_modules=disabled.get(uid, []))
            for uid in users
        },
    )


class TestResolveForUser:
    def test_resolves_enabled_user(self, tmp_path):
        cfg = _config(tmp_path, users=["stefan"])
        ctx = resolve_for_user("stefan", cfg)
        assert ctx.user_id == "stefan"
        # DB relocated to local disk via module_db_path (not under the mount).
        assert "briefings.db" in str(ctx.db_path)
        assert not str(ctx.db_path).startswith(str(tmp_path / "mount"))

    def test_disabled_module_raises(self, tmp_path):
        cfg = _config(tmp_path, users=["stefan"], disabled={"stefan": ["briefings"]})
        with pytest.raises(UserNotFoundError):
            resolve_for_user("stefan", cfg)

    def test_unknown_user_raises(self, tmp_path):
        cfg = _config(tmp_path, users=["stefan"])
        with pytest.raises(UserNotFoundError):
            resolve_for_user("nobody", cfg)

    def test_no_mount_raises(self, tmp_path):
        cfg = Config(db_path=tmp_path / "istota.db",
                    users={"stefan": UserConfig()})
        with pytest.raises(UserNotFoundError):
            resolve_for_user("stefan", cfg)

    def test_none_config_raises(self):
        with pytest.raises(UserNotFoundError):
            resolve_for_user("stefan", None)


class TestListUsers:
    def test_lists_enabled(self, tmp_path):
        cfg = _config(tmp_path, users=["a", "b"], disabled={"b": ["briefings"]})
        assert list_users(cfg) == ["a"]

    def test_none_config(self):
        assert list_users(None) == []
