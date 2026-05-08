"""Tests for the rewritten feeds skill — in-process facade over feeds.cli."""

from __future__ import annotations

import pytest

from istota.config import Config, UserConfig


@pytest.fixture
def istota_config(tmp_path, monkeypatch):
    """Build a minimal istota Config that resolves to a workspace under tmp_path.

    Workspace mode is the only resolution path now; ``resolve_for_user``
    derives ``{nextcloud_mount}/{get_user_bot_path}`` automatically and
    creates ``feeds/`` lazily — no ResourceConfig needed.
    """
    config = Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        nextcloud_mount_path=tmp_path,
        users={"alice": UserConfig()},
    )

    # Stub load_config so the skill picks up our test config.
    monkeypatch.setattr(
        "istota.config.load_config",
        lambda *a, **kw: config,
    )
    monkeypatch.setenv("FEEDS_USER", "alice")
    return config


class TestSkillRun:
    def test_list_empty(self, istota_config):
        from istota.skills.feeds import _run
        out = _run(["list"])
        assert out["status"] == "ok"
        assert out["count"] == 0

    def test_add_then_list(self, istota_config):
        from istota.skills.feeds import _run
        added = _run(["add", "--url", "https://example.com/feed.xml", "--category", "blogs"])
        assert added["status"] == "ok"

        listed = _run(["list"])
        urls = [f["url"] for f in listed["feeds"]]
        assert urls == ["https://example.com/feed.xml"]
        assert listed["feeds"][0]["category_slug"] == "blogs"

    def test_no_user_returns_error(self, monkeypatch):
        monkeypatch.delenv("FEEDS_USER", raising=False)
        from istota.skills.feeds import _run
        out = _run(["list"])
        assert out["status"] == "error"
        assert "FEEDS_USER" in out["error"]


class TestSkillExitCodes:
    """Module-skill subprocesses must exit non-zero when they emit a
    `{"status":"error",…}` envelope. The scheduler keys success/failure off
    returncode (with a JSON-envelope fallback as defense-in-depth), so a
    silent zero exit lets failed runs masquerade as successful."""

    def test_main_exits_nonzero_on_error_envelope(self, monkeypatch):
        monkeypatch.delenv("FEEDS_USER", raising=False)
        from istota.skills.feeds import main
        with pytest.raises(SystemExit) as exc_info:
            main(["list"])
        assert exc_info.value.code == 1

    def test_main_exits_zero_on_ok(self, istota_config):
        from istota.skills.feeds import main
        # No SystemExit raised, or SystemExit with code 0/None.
        try:
            main(["list"])
        except SystemExit as e:
            assert e.code in (0, None)


class TestParser:
    def test_subcommands_present(self):
        from istota.skills.feeds import build_parser
        p = build_parser()
        for cmd in ["list", "categories", "entries", "add", "remove",
                    "refresh", "poll", "run-scheduled", "import-opml",
                    "export-opml"]:
            args = p.parse_args([cmd] + (["--url", "u"] if cmd == "add"
                                          else ["x"] if cmd == "import-opml"
                                          else []))
            assert args.command == cmd

    def test_add_requires_url(self):
        from istota.skills.feeds import build_parser
        p = build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["add"])


class TestLoaderEnvFirst:
    """Phase 1.2 — feeds loader reads env vars before consulting secrets_store.

    Pinned because Phase 1.4 strips ISTOTA_SECRET_KEY from subprocess env;
    once that lands the secrets_store fallback returns None silently and
    cron module jobs would lose access to TUMBLR_API_KEY without env-first
    resolution.
    """

    def test_env_takes_precedence_over_store(self, istota_config, monkeypatch):
        from istota.feeds import _loader
        monkeypatch.setenv("TUMBLR_API_KEY", "from-env")
        called = []
        monkeypatch.setattr(
            "istota.secrets_store.get_secret",
            lambda *a, **kw: called.append(a) or "from-store",
        )
        ctx = _loader.resolve_for_user("alice", istota_config)
        assert ctx.tumblr_api_key == "from-env"
        assert called == []

    def test_store_fallback_when_env_unset(self, istota_config, monkeypatch):
        """Daemon-context: env is unset, master key is present, store wins."""
        from istota.feeds import _loader
        monkeypatch.delenv("TUMBLR_API_KEY", raising=False)
        monkeypatch.setattr(
            "istota.secrets_store.get_secret",
            lambda db, u, s, k: "from-store" if (s, k) == ("feeds", "tumblr_api_key") else None,
        )
        ctx = _loader.resolve_for_user("alice", istota_config)
        assert ctx.tumblr_api_key == "from-store"
