"""Config + validation for brain fallback (brain-fallback spec, Stage 3)."""

import textwrap

from istota.brain._fallback import effective_fallback_kind
from istota.config import BrainConfig, load_config


class TestFallbackKeysParse:
    def test_defaults(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[brain]\nkind = "claude_code"\n')
        config = load_config(cfg)
        assert config.brain.fallback == ""
        assert config.brain.fallback_on_transient is False
        assert config.brain.fallback_cooldown_seconds == 900

    def test_parses_all_three(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "claude_code"
            fallback = "native"
            fallback_on_transient = true
            fallback_cooldown_seconds = 120
        """))
        config = load_config(cfg)
        assert config.brain.fallback == "native"
        assert config.brain.fallback_on_transient is True
        assert config.brain.fallback_cooldown_seconds == 120


class TestEffectiveFallbackKind:
    def test_configured_wins(self):
        bc = BrainConfig(kind="claude_code", fallback="native")
        assert effective_fallback_kind(bc) == "native"

    def test_tmux_defaults_to_claude_code(self):
        bc = BrainConfig(kind="tmux_claude")
        assert effective_fallback_kind(bc) == "claude_code"

    def test_tmux_configured_overrides_default(self):
        bc = BrainConfig(kind="tmux_claude", fallback="native")
        assert effective_fallback_kind(bc) == "native"

    def test_other_primary_no_fallback_when_unset(self):
        bc = BrainConfig(kind="claude_code")
        assert effective_fallback_kind(bc) is None

    def test_native_no_fallback_when_unset(self):
        bc = BrainConfig(kind="native")
        assert effective_fallback_kind(bc) is None


class TestValidateBrainFallback:
    def test_unknown_kind_neutralized(self, tmp_path, caplog):
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "claude_code"
            fallback = "bogus_brain"
        """))
        import logging
        with caplog.at_level(logging.WARNING):
            config = load_config(cfg)
        assert config.brain.fallback == ""
        assert any("not a known brain kind" in r.message for r in caplog.records)

    def test_self_fallback_neutralized(self, tmp_path, caplog):
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "native"
            fallback = "native"
        """))
        import logging
        with caplog.at_level(logging.WARNING):
            config = load_config(cfg)
        assert config.brain.fallback == ""
        assert any("self-fallback" in r.message for r in caplog.records)

    def test_valid_fallback_survives(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "claude_code"
            fallback = "native"
        """))
        config = load_config(cfg)
        assert config.brain.fallback == "native"
