"""Tests for money.config — per-user config resolver and cache."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from money import config as cfg_module
from money.config import (
    ConfigNotFoundError,
    UserNotFoundError,
    invalidate_all,
    invalidate_user,
    list_users,
    resolve_user_config,
    set_loader,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    invalidate_all()
    set_loader(None)
    yield
    invalidate_all()
    set_loader(None)


@pytest.fixture
def multi_user_config(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    (tmp_path / "alice").mkdir()
    (tmp_path / "bob").mkdir()
    (tmp_path / "alice" / "ledgers").mkdir()
    (tmp_path / "alice" / "ledgers" / "main.beancount").write_text("")
    (tmp_path / "bob" / "ledgers").mkdir()
    (tmp_path / "bob" / "ledgers" / "main.beancount").write_text("")
    config.write_text(
        f'[users.alice]\n'
        f'data_dir = "{tmp_path / "alice"}"\n'
        f'ledgers = ["main"]\n\n'
        f'[users.bob]\n'
        f'data_dir = "{tmp_path / "bob"}"\n'
        f'ledgers = ["main"]\n'
    )
    monkeypatch.setenv("MONEYMAN_CONFIG", str(config))
    return config


@pytest.fixture
def config_with_invoicing(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    invoicing = tmp_path / "invoicing.toml"
    (tmp_path / "ledgers").mkdir()
    (tmp_path / "ledgers" / "main.beancount").write_text("")
    invoicing.write_text(
        '[company]\nname = "Acme"\nemail = "a@b.com"\naddress = "1 St"\n'
        'ar_account = "Assets:AR"\nbank_account = "Assets:Bank"\n'
        'payment_instructions = "pay"\n'
        '[clients.foo]\nname = "Foo"\nemail = "f@b.com"\n'
        'address = "X"\nterms = "Net 30"\n'
    )
    config.write_text(
        f'[users.alice]\n'
        f'data_dir = "{tmp_path}"\n'
        f'ledgers = ["main"]\n'
        f'invoicing_config = "invoicing.toml"\n'
    )
    monkeypatch.setenv("MONEYMAN_CONFIG", str(config))
    return config, invoicing


class TestResolve:
    def test_returns_user_config(self, multi_user_config):
        uc = resolve_user_config("alice")
        assert uc.data_dir.name == "alice"
        assert len(uc.ledgers) == 1

    def test_unknown_user_raises(self, multi_user_config):
        with pytest.raises(UserNotFoundError):
            resolve_user_config("eve")

    def test_no_config_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MONEYMAN_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ConfigNotFoundError):
            resolve_user_config("alice")


class TestCache:
    def test_second_call_uses_cache(self, multi_user_config, monkeypatch):
        first = resolve_user_config("alice")
        # If the cache is hit, load_context must not be called again.
        from money import config as cfg_mod
        called = {"n": 0}

        def boom(_):
            called["n"] += 1
            raise AssertionError("loader should not be called on cache hit")

        # Replacing _loader through set_loader would itself invalidate cache,
        # so monkeypatch the module-level reference directly.
        monkeypatch.setattr(cfg_mod, "_default_loader", boom)
        second = resolve_user_config("alice")
        assert second is first
        assert called["n"] == 0

    def test_ttl_expiry_triggers_reload(self, multi_user_config, monkeypatch):
        from money import config as cfg_mod
        first = resolve_user_config("alice")
        # Push the cached entry's expiry into the past.
        entry = cfg_mod._cache["alice"]
        cfg_mod._cache["alice"] = cfg_mod._CacheEntry(
            user_ctx=entry.user_ctx,
            expires_at=0.0,
            mtimes=entry.mtimes,
        )
        second = resolve_user_config("alice")
        # New UserContext instance, same data
        assert second.data_dir == first.data_dir

    def test_mtime_change_triggers_reload(self, multi_user_config):
        first = resolve_user_config("alice")
        # Change config file mtime
        os.utime(multi_user_config, (1, 1))
        second = resolve_user_config("alice")
        assert second.data_dir == first.data_dir
        # Different cached entry
        from money import config as cfg_mod
        assert cfg_mod._cache["alice"].mtimes[0][1] == 1.0

    def test_invoicing_file_mtime_tracked(self, config_with_invoicing):
        _, invoicing_path = config_with_invoicing
        first = resolve_user_config("alice")
        from money import config as cfg_mod
        tracked_paths = {p for p, _ in cfg_mod._cache["alice"].mtimes}
        assert invoicing_path in tracked_paths

    def test_invalidate_user(self, multi_user_config):
        resolve_user_config("alice")
        from money import config as cfg_mod
        assert "alice" in cfg_mod._cache
        invalidate_user("alice")
        assert "alice" not in cfg_mod._cache

    def test_invalidate_all(self, multi_user_config):
        resolve_user_config("alice")
        resolve_user_config("bob")
        from money import config as cfg_mod
        assert len(cfg_mod._cache) == 2
        invalidate_all()
        assert cfg_mod._cache == {}


class TestListUsers:
    def test_returns_user_keys(self, multi_user_config):
        users = list_users()
        assert sorted(users) == ["alice", "bob"]

    def test_excludes_default(self, tmp_path, monkeypatch):
        # Legacy single-user config gets an implicit "default" user
        config = tmp_path / "config.toml"
        (tmp_path / "ledgers").mkdir()
        (tmp_path / "ledgers" / "main.beancount").write_text("")
        config.write_text(
            f'data_dir = "{tmp_path}"\n'
            f'ledgers = [{{name = "main", path = "ledgers/main.beancount"}}]\n'
        )
        monkeypatch.setenv("MONEYMAN_CONFIG", str(config))
        assert list_users() == []

    def test_no_config_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MONEYMAN_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        assert list_users() == []


class TestSetLoader:
    def test_custom_loader_used(self, multi_user_config):
        from money.cli import UserContext

        sentinel = UserContext(data_dir=Path("/sentinel"))

        def loader(user_id):
            assert user_id == "alice"
            return sentinel

        set_loader(loader)
        result = resolve_user_config("alice")
        assert result is sentinel

    def test_set_loader_invalidates_cache(self, multi_user_config):
        first = resolve_user_config("alice")
        from money import config as cfg_mod
        assert "alice" in cfg_mod._cache

        set_loader(lambda _: first)  # any loader, forces invalidation
        assert cfg_mod._cache == {}
