"""The Apps module's registration: the `[apps]` block, the flag, the loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from istota import modules
from istota.config import AppsConfig, Config, UserConfig, load_config
from istota.config_mapper import collect_problems
from istota.experimental import KNOWN_FEATURES


def _load(tmp_path: Path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body)
    with collect_problems() as problems:
        cfg = load_config(path)
    return cfg, problems


class TestAppsConfig:
    def test_the_defaults(self):
        cfg = Config()
        assert cfg.apps == AppsConfig()
        assert cfg.apps.bundle_max_bytes == 2_000_000
        assert cfg.apps.kv_max_bytes == 5_000_000
        assert cfg.apps.kv_value_max_bytes == 262_144
        assert cfg.apps.files_max_bytes == 50_000_000
        assert cfg.apps.calls_per_minute == 120
        assert cfg.apps.calls_retention_days == 30
        assert cfg.apps.errors_per_version == 50

    def test_the_block_loads(self, tmp_path):
        cfg, problems = _load(tmp_path, (
            "[apps]\n"
            "bundle_max_bytes = 1000\n"
            "kv_max_bytes = 2000\n"
            "kv_value_max_bytes = 300\n"
            "files_max_bytes = 4000\n"
            "calls_per_minute = 5\n"
            "calls_retention_days = 6\n"
            "errors_per_version = 7\n"
        ))
        assert cfg.apps == AppsConfig(1000, 2000, 300, 4000, 5, 6, 7)
        assert [p for p in problems if p["path"][:1] == ("apps",)] == []

    @pytest.mark.parametrize("key", [
        "bundle_max_bytes", "kv_max_bytes", "kv_value_max_bytes", "files_max_bytes",
        "calls_per_minute", "calls_retention_days", "errors_per_version",
    ])
    @pytest.mark.parametrize("bad", ["0", "-1"])
    def test_a_non_positive_value_keeps_the_default(self, tmp_path, key, bad):
        cfg, problems = _load(tmp_path, f"[apps]\n{key} = {bad}\n")
        assert getattr(cfg.apps, key) == getattr(AppsConfig(), key)
        assert any(
            p["kind"] == "discarded" and p["path"] == ("apps", key) for p in problems
        )

    def test_the_example_documents_every_key(self):
        example = (Path(__file__).resolve().parents[1] / "config" / "config.example.toml").read_text()
        assert "[apps]" in example
        for key in AppsConfig.__dataclass_fields__:
            assert key in example


class TestTheFlag:
    def test_module_apps_is_a_known_feature(self, tmp_path, caplog):
        assert "module_apps" in KNOWN_FEATURES
        assert modules.EXPERIMENTAL_MODULES["apps"] == "module_apps"
        assert "apps" in modules.MODULE_NAMES
        _, problems = _load(tmp_path, "[experimental]\nfeatures = ['module_apps']\n")
        assert not any("module_apps" in p["message"] for p in problems)

    def test_off_by_default(self):
        cfg = Config(users={"alice": UserConfig()})
        assert cfg.is_module_enabled("alice", "apps") is False

    def test_on_with_the_flag(self):
        cfg = Config(users={"alice": UserConfig()})
        cfg.experimental.features = ["module_apps"]
        assert cfg.is_module_enabled("alice", "apps") is True

    def test_a_user_can_still_turn_it_off(self):
        cfg = Config(users={"alice": UserConfig(disabled_modules=["apps"])})
        cfg.experimental.features = ["module_apps"]
        assert cfg.is_module_enabled("alice", "apps") is False


class TestLoader:
    def _config(self, tmp_path, *, flag=True, users=None):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        cfg = Config(
            workspace_path=mount,
            users=users if users is not None else {"alice": UserConfig()},
            db_path=tmp_path / "db" / "istota.db",
        )
        if flag:
            cfg.experimental.features = ["module_apps"]
        return cfg

    def test_the_module_loader_protocol_resolves(self):
        list_users, resolve_for_user, connect, not_found = modules.module_loader("apps")
        from istota.apps import _loader, db

        assert resolve_for_user is _loader.resolve_for_user
        assert connect is db.connect
        assert not_found is _loader.UserNotFoundError

    def test_resolve_for_user_gives_the_module_db_path(self, tmp_path):
        from istota.apps import resolve_for_user

        cfg = self._config(tmp_path)
        ctx = resolve_for_user("alice", cfg)
        assert ctx.user_id == "alice"
        assert ctx.db_path == cfg.module_db_path("alice", "apps")
        assert ctx.workspace == tmp_path / "mount" / "Users" / "alice" / "istota"

    def test_without_the_flag_it_refuses(self, tmp_path):
        from istota.apps import UserNotFoundError, list_users, resolve_for_user

        cfg = self._config(tmp_path, flag=False)
        with pytest.raises(UserNotFoundError):
            resolve_for_user("alice", cfg)
        assert list_users(cfg) == []

    def test_list_users_filters_the_opted_out(self, tmp_path):
        from istota.apps import list_users

        cfg = self._config(tmp_path, users={
            "alice": UserConfig(),
            "bob": UserConfig(disabled_modules=["apps"]),
        })
        assert list_users(cfg) == ["alice"]


class TestStorage:
    def test_the_apps_path(self):
        from istota.storage import get_user_apps_path, get_user_bot_path

        assert get_user_apps_path("alice", "istota") == "/Users/alice/istota/apps"
        assert get_user_apps_path("alice", "istota").startswith(get_user_bot_path("alice", "istota"))
