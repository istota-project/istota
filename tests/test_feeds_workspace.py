"""Tests for feeds workspace synthesis + config IO."""

from istota.feeds._config_io import read_feeds_config, write_feeds_config
from istota.feeds.workspace import synthesize_feeds_context


class TestSynthesizeFeedsContext:
    def test_defaults(self, tmp_path):
        ctx = synthesize_feeds_context("stefan", tmp_path)
        assert ctx.user_id == "stefan"
        assert ctx.data_dir == (tmp_path / "feeds").resolve()
        assert ctx.db_path == ctx.data_dir / "data" / "feeds.db"
        assert ctx.config_path.name == "feeds.toml"

    def test_picks_existing_toml_in_data_config(self, tmp_path):
        cfg = tmp_path / "feeds" / "config" / "feeds.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("")
        ctx = synthesize_feeds_context("stefan", tmp_path)
        assert ctx.config_path == cfg.resolve()

    def test_falls_back_to_workspace_config_dir(self, tmp_path):
        cfg = tmp_path / "config" / "feeds.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("")
        ctx = synthesize_feeds_context("stefan", tmp_path)
        assert ctx.config_path == cfg.resolve()

    def test_explicit_config_dir_overrides(self, tmp_path):
        explicit = tmp_path / "elsewhere"
        explicit.mkdir()
        ctx = synthesize_feeds_context(
            "stefan", tmp_path, config_dir=explicit,
        )
        # No feeds.toml under explicit/, so falls back to feeds.toml in explicit/
        assert ctx.config_path == explicit / "feeds.toml"

    def test_explicit_config_path_wins(self, tmp_path):
        explicit = tmp_path / "anywhere" / "MY_feeds.toml"
        explicit.parent.mkdir(parents=True)
        explicit.write_text("")
        ctx = synthesize_feeds_context(
            "stefan", tmp_path, config_path=explicit,
        )
        assert ctx.config_path == explicit.resolve()


class TestReadFeedsConfig:
    def test_missing_file_returns_empty(self, tmp_path):
        cfg = read_feeds_config(tmp_path / "missing.toml")
        assert cfg == {"settings": {}, "categories": [], "feeds": []}

    def test_plain_toml(self, tmp_path):
        p = tmp_path / "feeds.toml"
        p.write_text("""
[settings]
default_poll_interval_minutes = 30

[[categories]]
slug = "blogs"
title = "Blogs"

[[feeds]]
url = "https://example.com/feed.xml"
title = "Example"
category = "blogs"
""")
        cfg = read_feeds_config(p)
        assert cfg["settings"]["default_poll_interval_minutes"] == 30
        assert cfg["categories"][0]["slug"] == "blogs"
        assert cfg["feeds"][0]["url"] == "https://example.com/feed.xml"


class TestWriteFeedsConfig:
    def test_round_trip_toml(self, tmp_path):
        path = tmp_path / "feeds.toml"
        data = {
            "settings": {"default_poll_interval_minutes": 30},
            "categories": [{"slug": "blogs", "title": "Blogs"}],
            "feeds": [
                {
                    "url": "https://example.com/feed.xml",
                    "title": "Example",
                    "category": "blogs",
                    "poll_interval_minutes": 60,
                },
            ],
        }
        write_feeds_config(path, data)
        round_tripped = read_feeds_config(path)
        assert round_tripped["settings"]["default_poll_interval_minutes"] == 30
        assert round_tripped["categories"][0]["slug"] == "blogs"
        assert round_tripped["feeds"][0]["url"] == "https://example.com/feed.xml"
        assert round_tripped["feeds"][0]["poll_interval_minutes"] == 60

    def test_creates_parent_dir(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "feeds.toml"
        write_feeds_config(path, {"feeds": []})
        assert path.exists()
