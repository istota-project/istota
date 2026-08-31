"""Config + validation for brain fallback (brain-fallback spec, Stage 3)."""

import textwrap

import pytest

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

    def test_tmux_has_no_implicit_fallback(self):
        # ISSUE-362: tmux_claude used to resolve to claude_code with nothing
        # configured. Failover is explicit config only now, for every kind.
        bc = BrainConfig(kind="tmux_claude")
        assert effective_fallback_kind(bc) is None

    def test_tmux_configured_is_honoured(self):
        bc = BrainConfig(kind="tmux_claude", fallback="native")
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
        work to tmux, fail it over to the CLI". Blanking it at load — which is
        what the old guard did on the string comparison alone — removed the one
        shape the implicit tmux target used to cover, with nothing to replace it.
        """
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "claude_code"
            fallback = "claude_code"

            [brain.source_type_overrides]
            scheduled = "tmux_claude"
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
        assert routed.kind == "tmux_claude"
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


class TestNeutralizedFallbackReallyIsDisabled:
    """ISSUE-362: both guards blank ``fallback``; that must mean no failover.

    On a ``tmux_claude`` primary the blanked field used to fall through to a
    hardcoded ``claude_code``, so each WARNING said the opposite of what
    happened — fallback was not disabled, it was switched to another brain.
    """

    def test_self_fallback_on_tmux_leaves_no_failover(self, tmp_path, caplog):
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "tmux_claude"
            fallback = "tmux_claude"
        """))
        import logging
        with caplog.at_level(logging.WARNING):
            config = load_config(cfg)
        assert any("self-fallback" in r.message for r in caplog.records)
        assert config.brain.fallback == ""
        assert effective_fallback_kind(config.brain) is None

    def test_unknown_kind_on_tmux_leaves_no_failover(self, tmp_path, caplog):
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "tmux_claude"
            fallback = "clade_code"
        """))
        import logging
        with caplog.at_level(logging.WARNING):
            config = load_config(cfg)
        assert any("not a known brain kind" in r.message for r in caplog.records)
        assert config.brain.fallback == ""
        assert effective_fallback_kind(config.brain) is None


class TestTmuxWithoutFallbackIsAnnounced:
    """Tmux with no failover is legitimate but worth one INFO line.

    It used to be impossible to configure, so an operator upgrading past
    ISSUE-362 loses failover silently otherwise.
    """

    @pytest.fixture(autouse=True)
    def _fresh_process(self, monkeypatch):
        """The notice is said once per process, so each case needs a clean slate.

        `load_config` runs in every CLI invocation and in every host-side skill
        CLI the proxy spawns, so a per-call line would be one per skill call
        rather than one per start-up. The cost is this fixture.
        """
        import istota.config

        monkeypatch.setattr(istota.config, "_TMUX_NO_FALLBACK_NOTICE_SAID", False)

    def test_tmux_primary_with_no_fallback_logs_once(self, tmp_path, caplog):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[brain]\nkind = "tmux_claude"\n')
        import logging
        with caplog.at_level(logging.INFO, logger="istota.config"):
            config = load_config(cfg)
        assert config.brain.fallback == ""
        notices = [r for r in caplog.records if "no [brain] fallback" in r.message]
        assert len(notices) == 1
        assert notices[0].levelno == logging.INFO

    def test_configured_tmux_fallback_is_silent(self, tmp_path, caplog):
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "tmux_claude"
            fallback = "claude_code"
        """))
        import logging
        with caplog.at_level(logging.INFO, logger="istota.config"):
            config = load_config(cfg)
        assert config.brain.fallback == "claude_code"
        assert not [r for r in caplog.records if "no [brain] fallback" in r.message]

    def test_other_primaries_are_not_announced(self, tmp_path, caplog):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[brain]\nkind = "claude_code"\n')
        import logging
        with caplog.at_level(logging.INFO, logger="istota.config"):
            load_config(cfg)
        assert not [r for r in caplog.records if "no [brain] fallback" in r.message]

    def test_a_routed_tmux_target_is_announced_too(self, tmp_path, caplog):
        """`resolve_brain_kind` returns a config that inherits `fallback`.

        So `kind = "claude_code"` routing `scheduled` to tmux is the same lost
        failover as a tmux primary, with nothing in `kind` to see it by. A guard
        reading `kind` alone would miss the shape it most needs to catch.
        """
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "claude_code"

            [brain.source_type_overrides]
            scheduled = "tmux_claude"
        """))
        import logging
        with caplog.at_level(logging.INFO, logger="istota.config"):
            config = load_config(cfg)
        assert config.brain.source_type_overrides == {"scheduled": "tmux_claude"}
        assert len([r for r in caplog.records if "no [brain] fallback" in r.message]) == 1

    def test_the_notice_is_said_once_per_process_not_once_per_load(
        self, tmp_path, caplog
    ):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[brain]\nkind = "tmux_claude"\n')
        import logging
        with caplog.at_level(logging.INFO, logger="istota.config"):
            for _ in range(3):
                load_config(cfg)
        assert len([r for r in caplog.records if "no [brain] fallback" in r.message]) == 1
