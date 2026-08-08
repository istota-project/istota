"""Config parsing + validation for `advisor_model` (advisor-model spec, Stage 2)."""

import logging
import textwrap

from istota.config import load_config


class TestAdvisorModelParses:
    def test_default_empty(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('bot_name = "Test"\n')
        config = load_config(cfg)
        assert config.advisor_model == ""

    def test_top_level_value_parses(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('advisor_model = "opus"\n')
        config = load_config(cfg)
        assert config.advisor_model == "opus"

    def test_canonical_id_parses(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('advisor_model = "claude-opus-4-8"\n')
        config = load_config(cfg)
        assert config.advisor_model == "claude-opus-4-8"


class TestAdvisorModelEffortModifierWarns:
    def test_effort_suffix_warns_but_value_kept_raw(self, tmp_path, caplog):
        # The value itself is stored raw; only executor-time resolution
        # (resolve_model_name) actually strips the modifier. The loader just
        # warns so an operator who typed "smart:high" isn't left guessing why
        # effort never shows up on an advisor call.
        cfg = tmp_path / "config.toml"
        cfg.write_text('advisor_model = "smart:high"\n')
        with caplog.at_level(logging.WARNING):
            config = load_config(cfg)
        assert config.advisor_model == "smart:high"
        assert any(
            "advisor_model" in r.message and "effort" in r.message
            for r in caplog.records
        )

    def test_no_warning_without_effort_suffix(self, tmp_path, caplog):
        cfg = tmp_path / "config.toml"
        cfg.write_text('advisor_model = "opus"\n')
        with caplog.at_level(logging.WARNING):
            load_config(cfg)
        assert not any("advisor_model" in r.message for r in caplog.records)


class TestAdvisorModelNativeKindWarns:
    def test_native_kind_no_fallback_warns(self, tmp_path, caplog):
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            advisor_model = "opus"
            [brain]
            kind = "native"
        """))
        with caplog.at_level(logging.WARNING):
            config = load_config(cfg)
        # Warn-and-ignore, per the design: the value survives config load...
        assert config.advisor_model == "opus"
        # ...but the operator is told it will never do anything.
        assert any(
            "advisor_model" in r.message and "native" in r.message
            for r in caplog.records
        )

    def test_native_kind_warns_even_with_an_anthropic_fallback(self, tmp_path, caplog):
        # The executor only ever resolves `advisor` for the *primary* brain
        # when its namespace is anthropic; _run_fallback only ever drops an
        # inherited advisor crossing into native, it never adds one crossing
        # back out on a native->anthropic fallback. So an anthropic fallback
        # doesn't rescue the setting — it stays just as dead, and the warning
        # still fires.
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            advisor_model = "opus"
            [brain]
            kind = "native"
            fallback = "claude_code"
        """))
        with caplog.at_level(logging.WARNING):
            load_config(cfg)
        assert any(
            "advisor_model" in r.message and "native" in r.message
            for r in caplog.records
        )

    def test_claude_code_kind_never_warns(self, tmp_path, caplog):
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            advisor_model = "opus"
            [brain]
            kind = "claude_code"
        """))
        with caplog.at_level(logging.WARNING):
            load_config(cfg)
        assert not any("advisor_model" in r.message for r in caplog.records)

    def test_empty_advisor_model_never_warns(self, tmp_path, caplog):
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "native"
        """))
        with caplog.at_level(logging.WARNING):
            load_config(cfg)
        assert not any("advisor_model" in r.message for r in caplog.records)


class TestAdvisorModelDefaultAliasTrapWarns:
    def test_default_alias_warns(self, tmp_path, caplog):
        # DEFAULT_ALIASES["default"] = (None, None) — resolve_model_name falls
        # through to a literal "default" pass-through, which the CLI rejects
        # as an --advisor value. Warn at load time rather than fail every task.
        cfg = tmp_path / "config.toml"
        cfg.write_text('advisor_model = "default"\n')
        with caplog.at_level(logging.WARNING):
            config = load_config(cfg)
        assert config.advisor_model == "default"
        assert any(
            "advisor_model" in r.message and "no concrete model" in r.message
            for r in caplog.records
        )

    def test_unknown_raw_id_does_not_warn(self, tmp_path, caplog):
        # A raw canonical-looking id that isn't a known alias is a legitimate
        # pass-through (operator typed a version pin directly) — not the trap.
        cfg = tmp_path / "config.toml"
        cfg.write_text('advisor_model = "claude-opus-4-9"\n')
        with caplog.at_level(logging.WARNING):
            load_config(cfg)
        assert not any(
            "advisor_model" in r.message and "no concrete model" in r.message
            for r in caplog.records
        )
