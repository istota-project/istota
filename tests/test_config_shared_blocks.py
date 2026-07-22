"""Tests for [[briefing_shared_blocks]] config parsing + canonical defaults."""

import tomli_w

from istota.config import (
    DEFAULT_SHARED_BLOCKS,
    BriefingSharedBlock,
    _parse_shared_block_specs,
    load_config,
)


class TestParseSharedBlockSpecs:
    def test_parses_full_block(self):
        entries = [{
            "name": "world-headlines",
            "cron": "0 6 * * *",
            "title": "🌍 World",
            "directive": "Do it.",
            "render_mode": "synthesis",
            "enabled": True,
            "sources": [{"kind": "browse", "config": {"preset": "ap"}}],
        }]
        blocks = _parse_shared_block_specs(entries)
        assert len(blocks) == 1
        b = blocks[0]
        assert isinstance(b, BriefingSharedBlock)
        assert b.name == "world-headlines"
        assert b.cron == "0 6 * * *"
        assert b.sources[0]["kind"] == "browse"

    def test_skips_entry_missing_name(self):
        blocks = _parse_shared_block_specs([{"cron": "0 6 * * *"}])
        assert blocks == []

    def test_skips_entry_missing_cron(self):
        blocks = _parse_shared_block_specs([{"name": "x"}])
        assert blocks == []

    def test_empty_directive_becomes_none(self):
        blocks = _parse_shared_block_specs([
            {"name": "x", "cron": "* * * * *", "directive": ""},
        ])
        assert blocks[0].directive is None

    def test_trusted_parses(self):
        blocks = _parse_shared_block_specs([
            {"name": "x", "cron": "* * * * *", "trusted": True},
            {"name": "y", "cron": "* * * * *"},
        ])
        assert blocks[0].trusted is True
        assert blocks[1].trusted is False


class TestMarketsDefaultVerbatim:
    def test_markets_summary_structured_verbatim_trusted(self):
        mk = next(b for b in DEFAULT_SHARED_BLOCKS if b["name"] == "markets-summary")
        assert mk["render_mode"] == "structured"
        assert mk.get("trusted") is True
        assert not mk.get("directive")  # verbatim → no synthesis directive


class TestCanonicalDefaults:
    def test_default_set_present(self):
        names = {b["name"] for b in DEFAULT_SHARED_BLOCKS}
        assert "world-headlines" in names
        assert "markets-summary" in names

    def test_defaults_parse_cleanly(self):
        blocks = _parse_shared_block_specs(DEFAULT_SHARED_BLOCKS)
        assert len(blocks) == len(DEFAULT_SHARED_BLOCKS)


class TestLoadConfigSeeding:
    def test_absent_section_seeds_defaults(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(tomli_w.dumps({"bot_name": "Istota"}))
        config = load_config(cfg_path)
        names = {b.name for b in config.briefing_shared_blocks}
        assert "world-headlines" in names
        assert "markets-summary" in names

    def test_explicit_section_replaces_defaults(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(tomli_w.dumps({
            "briefing_shared_blocks": [
                {"name": "custom", "cron": "0 5 * * *",
                 "sources": [{"kind": "markets", "config": {}}]},
            ],
        }))
        config = load_config(cfg_path)
        names = {b.name for b in config.briefing_shared_blocks}
        assert names == {"custom"}

    def test_explicit_empty_opts_out(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(tomli_w.dumps({"briefing_shared_blocks": []}))
        config = load_config(cfg_path)
        assert config.briefing_shared_blocks == []

    def test_shared_block_timezone_default_utc(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(tomli_w.dumps({"bot_name": "Istota"}))
        config = load_config(cfg_path)
        assert config.briefings.shared_block_timezone == "UTC"

    def test_shared_block_timezone_parsed(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(tomli_w.dumps({
            "briefings": {"shared_block_timezone": "America/Los_Angeles"},
        }))
        config = load_config(cfg_path)
        assert config.briefings.shared_block_timezone == "America/Los_Angeles"


class TestApplySharedBlocksOverlay:
    def _base_config(self, tmp_path):
        from istota import db
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(tomli_w.dumps({
            "db_path": str(tmp_path / "istota.db"),
            "briefing_shared_blocks": [
                {"name": "world-headlines", "cron": "0 6 * * *", "title": "TOML",
                 "sources": [{"kind": "browse", "config": {}}]},
            ],
        }))
        db.init_db(tmp_path / "istota.db")
        return cfg_path

    def test_db_wins_by_name(self, tmp_path):
        from istota import db
        cfg_path = self._base_config(tmp_path)
        with db.get_db(tmp_path / "istota.db") as conn:
            db.upsert_shared_block_config(
                conn, name="world-headlines", cron="0 9 * * *", title="DB wins",
                trusted=True,
            )
        config = load_config(cfg_path)
        wh = next(b for b in config.briefing_shared_blocks if b.name == "world-headlines")
        assert wh.title == "DB wins"
        assert wh.cron == "0 9 * * *"
        assert wh.trusted is True

    def test_db_only_row_appended(self, tmp_path):
        from istota import db
        cfg_path = self._base_config(tmp_path)
        with db.get_db(tmp_path / "istota.db") as conn:
            db.upsert_shared_block_config(conn, name="extra", cron="0 8 * * *")
        config = load_config(cfg_path)
        names = {b.name for b in config.briefing_shared_blocks}
        assert names == {"world-headlines", "extra"}

    def test_disabled_row_overlays_but_muted(self, tmp_path):
        from istota import db
        cfg_path = self._base_config(tmp_path)
        with db.get_db(tmp_path / "istota.db") as conn:
            db.upsert_shared_block_config(
                conn, name="world-headlines", cron="0 6 * * *", enabled=False,
            )
        config = load_config(cfg_path)
        wh = next(b for b in config.briefing_shared_blocks if b.name == "world-headlines")
        assert wh.enabled is False  # present-but-muted

    def test_no_db_rows_keeps_config(self, tmp_path):
        cfg_path = self._base_config(tmp_path)
        config = load_config(cfg_path)
        wh = next(b for b in config.briefing_shared_blocks if b.name == "world-headlines")
        assert wh.title == "TOML"  # overlay is a no-op when no DB rows
