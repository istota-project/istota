"""Tests for feeds workspace synthesis + config IO."""

from istota.feeds._config_io import read_feeds_config, write_feeds_config
from istota.feeds.workspace import synthesize_feeds_context


class TestSynthesizeFeedsContext:
    def test_defaults(self, tmp_path):
        ctx = synthesize_feeds_context("stefan", tmp_path)
        assert ctx.user_id == "stefan"
        assert ctx.data_dir == (tmp_path / "feeds").resolve()
        assert ctx.db_path == ctx.data_dir / "data" / "feeds.db"
        assert ctx.config_path.name == "FEEDS.md"

    def test_picks_existing_md_in_data_config(self, tmp_path):
        cfg = tmp_path / "feeds" / "config" / "FEEDS.md"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("```toml\n```\n")
        ctx = synthesize_feeds_context("stefan", tmp_path)
        assert ctx.config_path == cfg.resolve()

    def test_falls_back_to_workspace_config_dir(self, tmp_path):
        cfg = tmp_path / "config" / "FEEDS.md"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("```toml\n```\n")
        ctx = synthesize_feeds_context("stefan", tmp_path)
        assert ctx.config_path == cfg.resolve()

    def test_explicit_config_dir_overrides(self, tmp_path):
        explicit = tmp_path / "elsewhere"
        explicit.mkdir()
        ctx = synthesize_feeds_context(
            "stefan", tmp_path, config_dir=explicit,
        )
        # No FEEDS.* under explicit/, so falls back to FEEDS.md in explicit/
        assert ctx.config_path == explicit / "FEEDS.md"

    def test_explicit_config_path_wins(self, tmp_path):
        explicit = tmp_path / "anywhere" / "MY_FEEDS.toml"
        explicit.parent.mkdir(parents=True)
        explicit.write_text("")
        ctx = synthesize_feeds_context(
            "stefan", tmp_path, config_path=explicit,
        )
        assert ctx.config_path == explicit.resolve()

    def test_legacy_toml_filename(self, tmp_path):
        cfg = tmp_path / "feeds" / "config" / "feeds.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("")
        ctx = synthesize_feeds_context("stefan", tmp_path)
        assert ctx.config_path.name == "feeds.toml"


class TestReadFeedsConfig:
    def test_missing_file_returns_empty(self, tmp_path):
        cfg = read_feeds_config(tmp_path / "missing.toml")
        assert cfg == {"settings": {}, "categories": [], "feeds": []}

    def test_plain_toml(self, tmp_path):
        p = tmp_path / "FEEDS.toml"
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

    def test_md_with_toml_block(self, tmp_path):
        p = tmp_path / "FEEDS.md"
        p.write_text(
            "# Feeds\n\nProse.\n\n"
            '```toml\n[[feeds]]\nurl = "x"\n```\n'
        )
        cfg = read_feeds_config(p)
        assert cfg["feeds"][0]["url"] == "x"

    def test_md_without_toml_block_raises(self, tmp_path):
        p = tmp_path / "FEEDS.md"
        p.write_text("# Feeds\n\nNo toml here.\n")
        try:
            read_feeds_config(p)
        except ValueError as e:
            assert "toml" in str(e).lower()
        else:
            raise AssertionError("expected ValueError")


class TestWriteFeedsConfig:
    def test_round_trip_toml(self, tmp_path):
        path = tmp_path / "FEEDS.toml"
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

    def test_md_wraps_toml_in_fenced_block(self, tmp_path):
        path = tmp_path / "FEEDS.md"
        write_feeds_config(path, {"feeds": [{"url": "x"}]})
        text = path.read_text()
        assert "```toml" in text
        # Round-trip
        cfg = read_feeds_config(path)
        assert cfg["feeds"][0]["url"] == "x"

    def test_creates_parent_dir(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "FEEDS.toml"
        write_feeds_config(path, {"feeds": []})
        assert path.exists()
