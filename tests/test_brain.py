"""Tests for the Brain abstraction (factory + config parsing).

Per-brain implementation tests live with the executor tests
(`test_executor.py::TestExecuteStreamingRetry` exercises ClaudeCodeBrain's
retry loop) and the streaming integration tests
(`test_executor_streaming.py` covers end-to-end paths).
"""

import textwrap
from pathlib import Path

import pytest

from istota.brain import (
    Brain,
    BrainConfig,
    BrainRequest,
    BrainResult,
    ClaudeCodeBrain,
    make_brain,
)
from istota.config import load_config


class TestMakeBrain:
    def test_default_kind_returns_claude_code(self):
        brain = make_brain(BrainConfig())
        assert isinstance(brain, ClaudeCodeBrain)

    def test_explicit_claude_code_kind(self):
        brain = make_brain(BrainConfig(kind="claude_code"))
        assert isinstance(brain, ClaudeCodeBrain)

    def test_unknown_kind_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown brain kind"):
            make_brain(BrainConfig(kind="bogus"))

    def test_unknown_kind_quotes_value_in_message(self):
        with pytest.raises(ValueError, match="'definitely_not_a_brain'"):
            make_brain(BrainConfig(kind="definitely_not_a_brain"))


class TestBrainProtocolConformance:
    def test_claude_code_satisfies_brain_protocol(self):
        # Protocol checks are runtime-checkable only when decorated; this is
        # a duck-type check that the method signature matches.
        brain: Brain = ClaudeCodeBrain()
        assert hasattr(brain, "execute")
        assert callable(brain.execute)


class TestBrainConfigTomlParsing:
    def test_brain_section_parsed(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            bot_name = "Test"

            [brain]
            kind = "claude_code"
        """))
        config = load_config(cfg)
        assert config.brain.kind == "claude_code"

    def test_brain_section_optional_defaults_to_claude_code(self, tmp_path):
        # No [brain] section — should still default
        cfg = tmp_path / "config.toml"
        cfg.write_text('bot_name = "Test"\n')
        config = load_config(cfg)
        assert config.brain.kind == "claude_code"

    def test_unknown_kind_loads_but_make_brain_rejects(self, tmp_path):
        # Loading config doesn't validate kind — that happens at make_brain.
        # This keeps config loading cheap and decouples config parsing
        # from the set of available brains.
        cfg = tmp_path / "config.toml"
        cfg.write_text(textwrap.dedent("""
            [brain]
            kind = "future_brain"
        """))
        config = load_config(cfg)
        assert config.brain.kind == "future_brain"

        with pytest.raises(ValueError):
            make_brain(config.brain)


class TestBrainRequestDefaults:
    def test_minimal_request_constructs(self, tmp_path):
        req = BrainRequest(
            prompt="hi",
            allowed_tools=["Bash"],
            cwd=tmp_path,
            env={},
            timeout_seconds=60,
        )
        assert req.model == ""
        assert req.effort == ""
        assert req.streaming is True
        assert req.on_progress is None
        assert req.cancel_check is None
        assert req.on_pid is None
        assert req.sandbox_wrap is None
        assert req.result_file is None
        assert req.custom_system_prompt_path is None


class TestBrainResultDefaults:
    def test_minimal_result_defaults(self):
        result = BrainResult(success=True, result_text="ok")
        assert result.actions_taken is None
        assert result.execution_trace is None
        assert result.stop_reason == "completed"
