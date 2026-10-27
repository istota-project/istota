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
        # ISSUE-212: on by default, and the loader must agree with the dataclass
        # — a key omitted from TOML previously resolved to False regardless.
        assert config.brain.fallback_on_transient is True
        assert config.brain.fallback_on_transient is BrainConfig().fallback_on_transient
        assert config.brain.fallback_cooldown_seconds == 900

    def test_explicit_false_is_honoured(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[brain]\nkind = "claude_code"\nfallback_on_transient = false\n')
        config = load_config(cfg)
        assert config.brain.fallback_on_transient is False

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

    def test_a_fallback_equal_to_this_configs_kind_is_no_fallback(self):
        # Rerunning the same brain cannot help. Checked here rather than only at
        # config load because a routed config inherits `fallback` (ISSUE-362).
        bc = BrainConfig(kind="claude_code", fallback="claude_code")
        assert effective_fallback_kind(bc) is None

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

    def test_a_self_fallback_survives_when_an_override_routes_elsewhere(
        self, tmp_path, caplog
    ):
        """ISSUE-362: `fallback == kind` is not useless when a task runs elsewhere.

        `resolve_brain_kind` hands the executor a config with `kind` replaced and
        `fallback` inherited, so this is the only spelling of "route scheduled
        work to native, fail it over to the CLI". Blanking it at load — which is
        what the old guard did on the string comparison alone — removed that
        shape with nothing to replace it.
        """
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "claude_code"
            fallback = "claude_code"

            [brain.source_type_overrides]
            scheduled = "native"
        """))
        import logging
        with caplog.at_level(logging.WARNING):
            config = load_config(cfg)
        assert config.brain.fallback == "claude_code"
        assert not [r for r in caplog.records if "self-fallback" in r.message]
        # Per-task: useless for an interactive task, real for a scheduled one.
        from istota.brain import resolve_brain_kind

        assert effective_fallback_kind(config.brain) is None
        routed = resolve_brain_kind("scheduled", config.brain)
        assert routed.kind == "native"
        assert effective_fallback_kind(routed) == "claude_code"

    def test_an_override_to_an_unknown_kind_does_not_rescue_it(
        self, tmp_path, caplog
    ):
        """`resolve_brain_kind` logs and ignores an unknown target.

        Such a task runs on `kind`, so the override is not a second brain and the
        self-fallback really is useless.
        """
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "claude_code"
            fallback = "claude_code"

            [brain.source_type_overrides]
            scheduled = "clade_code"
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
