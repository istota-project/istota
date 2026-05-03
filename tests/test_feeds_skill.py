"""Tests for the rewritten feeds skill — in-process facade over feeds.cli."""

from __future__ import annotations

import json

import pytest

from istota.config import Config, ResourceConfig, UserConfig
from istota.feeds._config_io import write_feeds_config


@pytest.fixture
def istota_config(tmp_path, monkeypatch):
    """Build a minimal istota Config that resolves to a workspace under tmp_path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "feeds").mkdir()
    config = Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        nextcloud_mount_path=tmp_path,
        users={
            "alice": UserConfig(
                resources=[
                    ResourceConfig(
                        type="feeds",
                        extra={
                            "data_dir": str(workspace / "feeds"),
                            "config_path": str(workspace / "feeds" / "feeds.toml"),
                            "db_path": str(workspace / "feeds" / "feeds.db"),
                        },
                    ),
                ],
            ),
        },
    )

    # Stub load_config so the skill picks up our test config.
    import istota.skills.feeds as feeds_skill
    monkeypatch.setattr(
        "istota.config.load_config",
        lambda *a, **kw: config,
    )
    monkeypatch.setenv("FEEDS_USER", "alice")
    return config


class TestSkillRun:
    def test_list_empty(self, istota_config, tmp_path):
        # Empty config -> empty list
        cfg_path = tmp_path / "workspace" / "feeds" / "feeds.toml"
        write_feeds_config(cfg_path, {"settings": {}, "categories": [], "feeds": []})

        from istota.skills.feeds import _run
        out = _run(["list"])
        assert out["status"] == "ok"
        assert out["count"] == 0

    def test_add_then_list(self, istota_config, tmp_path):
        cfg_path = tmp_path / "workspace" / "feeds" / "feeds.toml"
        write_feeds_config(cfg_path, {"settings": {}, "categories": [], "feeds": []})

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

    def test_main_exits_nonzero_on_error_envelope(self, monkeypatch, capsys):
        monkeypatch.delenv("FEEDS_USER", raising=False)
        from istota.skills.feeds import main
        with pytest.raises(SystemExit) as exc_info:
            main(["list"])
        assert exc_info.value.code == 1

    def test_main_exits_zero_on_ok(self, istota_config, tmp_path):
        cfg_path = tmp_path / "workspace" / "feeds" / "feeds.toml"
        from istota.feeds._config_io import write_feeds_config
        write_feeds_config(cfg_path, {"settings": {}, "categories": [], "feeds": []})
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
                                          else ["--path", "/tmp/x"] if False
                                          else ["x"] if cmd == "import-opml"
                                          else []))
            assert args.command == cmd

    def test_add_requires_url(self):
        from istota.skills.feeds import build_parser
        p = build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["add"])
