"""Tests for the Brain abstraction (factory + config parsing).

Per-brain implementation tests live with the executor tests
(`test_executor.py::TestExecuteStreamingRetry` exercises ClaudeCodeBrain's
retry loop) and the streaming integration tests
(`test_executor_streaming.py` covers end-to-end paths).
"""

import textwrap
import typing

import pytest

from istota.brain import (
    Brain,
    BrainConfig,
    BrainRequest,
    BrainResult,
    ClaudeCodeBrain,
    StreamEvent,
    TextDeltaEvent,
    make_brain,
)
from istota.config import load_config


class TestTextDeltaEvent:
    def test_in_stream_event_union(self):
        assert TextDeltaEvent in typing.get_args(StreamEvent)

    def test_carries_incremental_fragment(self):
        ev = TextDeltaEvent(text="par")
        assert ev.text == "par"


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
        assert req.advisor == ""
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


class TestBuildCommandDisallowedTools:
    def _req(self, tmp_path, allowed_tools):
        return BrainRequest(
            prompt="hi",
            allowed_tools=allowed_tools,
            cwd=tmp_path,
            env={},
            timeout_seconds=60,
        )

    def test_orchestration_tools_disallowed_when_tools_allowed(self, tmp_path):
        cmd = ClaudeCodeBrain._build_command(self._req(tmp_path, ["Bash"]))
        assert "--disallowedTools" in cmd
        flag_idx = cmd.index("--disallowedTools")
        disallowed = cmd[flag_idx + 1 : flag_idx + 3]
        assert disallowed == ["Agent", "Workflow"]

    def test_no_allowlist_skip_permissions_when_tools_allowed(self, tmp_path):
        # We run non-interactively with --dangerously-skip-permissions instead
        # of an --allowedTools allowlist; the sandbox + network proxy are the
        # boundary. Agent stays denied (deny wins even under skip-permissions).
        cmd = ClaudeCodeBrain._build_command(self._req(tmp_path, ["Bash"]))
        assert "--allowedTools" not in cmd
        assert "--dangerously-skip-permissions" in cmd

    def test_no_tool_flags_when_text_only(self, tmp_path):
        # Empty allowed_tools => text-only invocation: no tool flags and no
        # skip-permissions, so the call can't reach a tool.
        cmd = ClaudeCodeBrain._build_command(self._req(tmp_path, []))
        assert "--allowedTools" not in cmd
        assert "--disallowedTools" not in cmd
        assert "--dangerously-skip-permissions" not in cmd


class TestAdvisorFlag:
    """`--advisor` is gated on both `req.advisor` and `req.allowed_tools` being
    non-empty, mirroring the other model flags (advisor-model spec, Stage 2)."""

    def _req(self, tmp_path, *, advisor="", allowed_tools=("Bash",)):
        return BrainRequest(
            prompt="hi",
            allowed_tools=list(allowed_tools),
            cwd=tmp_path,
            env={},
            timeout_seconds=60,
            advisor=advisor,
        )

    def test_emitted_when_advisor_and_tools_present(self, tmp_path):
        from istota.brain.claude_code import build_claude_cli_flags

        flags = build_claude_cli_flags(
            self._req(tmp_path, advisor="claude-opus-5", allowed_tools=["Bash"])
        )
        assert "--advisor" in flags
        idx = flags.index("--advisor")
        assert flags[idx + 1] == "claude-opus-5"

    def test_omitted_when_advisor_empty(self, tmp_path):
        from istota.brain.claude_code import build_claude_cli_flags

        flags = build_claude_cli_flags(
            self._req(tmp_path, advisor="", allowed_tools=["Bash"])
        )
        assert "--advisor" not in flags

    def test_omitted_when_text_only(self, tmp_path):
        # A text-only call has no judgement moments to escalate — the advisor
        # is cost with no mechanism to pay off, so it's dropped even when set.
        from istota.brain.claude_code import build_claude_cli_flags

        flags = build_claude_cli_flags(
            self._req(tmp_path, advisor="claude-opus-5", allowed_tools=[])
        )
        assert "--advisor" not in flags

    def test_placed_after_effort(self, tmp_path):
        from istota.brain.claude_code import build_claude_cli_flags

        req = BrainRequest(
            prompt="hi", allowed_tools=["Bash"], cwd=tmp_path, env={},
            timeout_seconds=60, effort="high", advisor="claude-opus-5",
        )
        flags = build_claude_cli_flags(req)
        assert flags.index("--effort") < flags.index("--advisor")


class TestRootSkipPermissionsEnv:
    """`claude --dangerously-skip-permissions` is refused under root/sudo unless
    IS_SANDBOX=1 signals an external isolation boundary (the Docker
    container-as-sandbox case). The headless brain sets it for tool-bearing
    tasks, mirroring the tmux brain."""

    def _execute_capturing_env(self, tmp_path, *, root, allowed_tools, env=None):
        from unittest.mock import patch

        req = BrainRequest(
            prompt="hi",
            allowed_tools=allowed_tools,
            cwd=tmp_path,
            env=dict(env or {}),
            timeout_seconds=60,
            streaming=False,
        )
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs.get("env") or {})
            return typing.cast(
                typing.Any,
                type("R", (), {"stdout": "ok", "stderr": "", "returncode": 0})(),
            )

        with patch("istota.brain.claude_code._is_root", lambda: root), patch(
            "istota.brain.claude_code.subprocess.run", side_effect=fake_run
        ):
            ClaudeCodeBrain().execute(req)
        return req.env, captured

    def test_root_with_tools_sets_is_sandbox(self, tmp_path):
        env, captured = self._execute_capturing_env(
            tmp_path, root=True, allowed_tools=["Bash"]
        )
        assert env.get("IS_SANDBOX") == "1"
        assert captured.get("IS_SANDBOX") == "1"

    def test_non_root_leaves_is_sandbox_unset(self, tmp_path):
        env, _ = self._execute_capturing_env(
            tmp_path, root=False, allowed_tools=["Bash"]
        )
        assert "IS_SANDBOX" not in env

    def test_root_text_only_leaves_is_sandbox_unset(self, tmp_path):
        # No tools => no skip-permissions => no need for IS_SANDBOX.
        env, _ = self._execute_capturing_env(tmp_path, root=True, allowed_tools=[])
        assert "IS_SANDBOX" not in env

    def test_existing_is_sandbox_preserved(self, tmp_path):
        env, _ = self._execute_capturing_env(
            tmp_path, root=True, allowed_tools=["Bash"], env={"IS_SANDBOX": "custom"}
        )
        assert env.get("IS_SANDBOX") == "custom"


class TestAdvisorEnvExclusivity:
    """Exactly one of {`--advisor` in argv, `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1`
    in the child env} holds for every request that reaches ClaudeCodeBrain.
    Both would mean a silently dead flag; neither would mean the host's
    `~/.claude/settings.json` `advisorModel` is back in charge (advisor-model
    spec, Tests section). Parametrized over constructed shapes, not just the
    seven real call sites, so a shape nothing constructs today (advisor set
    with empty allowed_tools) still proves the invariant structural."""

    def _execute_capturing(self, tmp_path, *, advisor, allowed_tools, env=None):
        from unittest.mock import patch

        req = BrainRequest(
            prompt="hi",
            allowed_tools=allowed_tools,
            cwd=tmp_path,
            env=dict(env or {}),
            timeout_seconds=60,
            streaming=False,
            advisor=advisor,
        )
        captured_env = {}
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            captured_cmd.extend(cmd)
            return typing.cast(
                typing.Any,
                type("R", (), {"stdout": "ok", "stderr": "", "returncode": 0})(),
            )

        with patch("istota.brain.claude_code.subprocess.run", side_effect=fake_run):
            ClaudeCodeBrain().execute(req)
        return captured_cmd, captured_env

    @pytest.mark.parametrize(
        "advisor,allowed_tools",
        [
            ("", ["Bash"]),           # no advisor configured, tool-bearing
            ("", []),                 # no advisor configured, text-only
            ("claude-opus-5", ["Bash"]),  # advisor + tools: the flag fires
            ("claude-opus-5", []),    # advisor set, but text-only — the shape
                                       # nothing constructs today, per the spec.
        ],
    )
    def test_exactly_one_of_flag_or_disable_var(self, tmp_path, advisor, allowed_tools):
        cmd, env = self._execute_capturing(
            tmp_path, advisor=advisor, allowed_tools=allowed_tools
        )
        flag_present = "--advisor" in cmd
        disable_present = env.get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL") == "1"
        assert flag_present != disable_present, (
            f"advisor={advisor!r} allowed_tools={allowed_tools!r}: "
            f"flag_present={flag_present} disable_present={disable_present}"
        )

    def test_inherited_disable_var_is_popped_when_advisor_active(self, tmp_path):
        # req.env isn't guaranteed clean — a passthrough env var, or a fallback
        # request built via dataclasses.replace sharing the primary's dict,
        # could already carry the disable var even when this request wants an
        # advisor. The positive branch must pop it, not leave it, or the flag
        # would be set alongside a var that silently kills it.
        cmd, env = self._execute_capturing(
            tmp_path,
            advisor="claude-opus-5",
            allowed_tools=["Bash"],
            env={"CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1"},
        )
        assert "--advisor" in cmd
        assert "CLAUDE_CODE_DISABLE_ADVISOR_TOOL" not in env
