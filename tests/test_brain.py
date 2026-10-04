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
        assert req.composed_system_prompt_path is None


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

    _WRITE_AND_NETWORK_TOOLS = [
        "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit",
        "WebFetch", "WebSearch", "TodoWrite",
    ]

    def _disallowed(self, cmd):
        start = cmd.index("--disallowedTools") + 1
        end = start
        while end < len(cmd) and not cmd[end].startswith("--"):
            end += 1
        return cmd[start:end]

    @pytest.mark.parametrize(
        "tools", [["Read"], ["Read", "Grep", "Glob"], ["Glob", "Grep"]],
    )
    def test_a_read_only_grant_denies_the_write_and_network_tools(
        self, tmp_path, tools
    ):
        # Skip-permissions hands the model the CLI's whole default toolset, so
        # a grant of read tools alone has to deny the rest by name.
        cmd = ClaudeCodeBrain._build_command(self._req(tmp_path, tools))
        assert self._disallowed(cmd) == [
            "Agent", "Workflow", *self._WRITE_AND_NETWORK_TOOLS,
        ]
        assert cmd.count("--disallowedTools") == 1

    @pytest.mark.parametrize(
        ("tools", "named"),
        [
            (["Read"], "Read"),
            (["Read", "Grep", "Glob"], "Read,Grep,Glob"),
            (["Glob", "Grep", "Glob"], "Grep,Glob"),
        ],
    )
    def test_a_read_only_grant_has_only_its_built_ins_and_no_mcp(
        self, tmp_path, tools, named
    ):
        # The deny list fails open on a tool a later CLI adds; `--tools` names
        # the built-ins that exist, and `--strict-mcp-config` with no config
        # drops every MCP server, the account's claude.ai connectors included.
        cmd = ClaudeCodeBrain._build_command(self._req(tmp_path, tools))
        assert cmd[cmd.index("--tools") + 1] == named
        assert cmd.count("--tools") == 1
        assert "--strict-mcp-config" in cmd
        assert "--mcp-config" not in cmd

    @pytest.mark.parametrize(
        "tools",
        [["Read", "Bash"], ["Bash"], ["Read", "Write"],
         ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "WebFetch"]],
    )
    def test_any_other_grant_keeps_todays_argv(self, tmp_path, tools):
        cmd = ClaudeCodeBrain._build_command(self._req(tmp_path, tools))
        assert self._disallowed(cmd) == ["Agent", "Workflow"]
        assert "--tools" not in cmd
        assert "--strict-mcp-config" not in cmd

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


class TestCliSettingsFlags:
    """`--settings` and `--setting-sources` on the shared flag builder.

    The CLI's own prompt tells the model to sign commits and PRs, which the
    `commit` skill forbids; the settings document removes that instruction at
    the source. `--setting-sources user` keeps a repository's own
    `.claude/settings*.json` out of the run.
    """

    def _req(self, tmp_path, *, allowed_tools=("Bash",), settings_path=None):
        return BrainRequest(
            prompt="hi", allowed_tools=list(allowed_tools), cwd=tmp_path,
            env={}, timeout_seconds=60, cli_settings_path=settings_path,
        )

    def test_the_document_blanks_commit_and_pr_attribution(self):
        from istota.brain.claude_code import cli_settings_document

        assert cli_settings_document()["attribution"] == {"commit": "", "pr": ""}

    def test_a_set_path_is_passed_as_given_without_an_exists_check(self, tmp_path):
        from istota.brain.claude_code import build_claude_cli_flags

        missing = tmp_path / "control" / "cli_settings.json"
        flags = build_claude_cli_flags(self._req(tmp_path, settings_path=missing))
        assert flags[flags.index("--settings") + 1] == str(missing)

    def test_no_path_with_tools_carries_the_same_document_inline(self, tmp_path):
        import json

        from istota.brain.claude_code import (
            build_claude_cli_flags,
            cli_settings_document,
        )

        flags = build_claude_cli_flags(self._req(tmp_path))
        inline = json.loads(flags[flags.index("--settings") + 1])
        assert inline == cli_settings_document()

    def test_a_text_only_call_with_a_path_still_gets_it(self, tmp_path):
        from istota.brain.claude_code import build_claude_cli_flags

        path = tmp_path / "cli_settings.json"
        flags = build_claude_cli_flags(
            self._req(tmp_path, allowed_tools=(), settings_path=path)
        )
        assert flags[flags.index("--settings") + 1] == str(path)

    def test_a_text_only_call_with_no_path_gets_no_settings(self, tmp_path):
        from istota.brain.claude_code import build_claude_cli_flags

        flags = build_claude_cli_flags(self._req(tmp_path, allowed_tools=()))
        assert "--settings" not in flags

    @pytest.mark.parametrize("allowed_tools", [(), ("Bash",)])
    def test_only_the_user_source_is_loaded(self, tmp_path, allowed_tools):
        from istota.brain.claude_code import build_claude_cli_flags

        flags = build_claude_cli_flags(
            self._req(tmp_path, allowed_tools=allowed_tools)
        )
        assert flags[flags.index("--setting-sources") + 1] == "user"

    def test_the_tmux_launch_carries_both(self, tmp_path):
        from istota.brain.claude_code import build_claude_cli_flags
        from istota.brain.tmux_claude import _TMUX_UNSUPPORTED_FLAGS

        path = tmp_path / "cli_settings.json"
        flags = build_claude_cli_flags(
            self._req(tmp_path, settings_path=path),
            unsupported=_TMUX_UNSUPPORTED_FLAGS,
        )
        assert flags[flags.index("--settings") + 1] == str(path)
        assert "--setting-sources" in flags


class TestComposedSystemPromptFlag:
    """`--append-system-prompt-file` carries Istota's composed system half.

    Not `--system-prompt-file`. That one *replaces* Claude Code's default
    harness prompt, so using it for the composed half would silently discard the
    harness on the default deployment, where no operator file is configured. The
    two flags are independent on the pinned CLI: 2.1.241 rejects only
    `--system-prompt` against `--system-prompt-file` and `--append-system-prompt`
    against `--append-system-prompt-file`, so passing an operator file and a
    composed file together is legal.

    The composed path is *required* input created by the executor, so unlike
    `custom_system_prompt_path` it gets no `exists()` gate: a file that has gone
    missing must reach the CLI and fail the attempt (the pinned binary answers
    `Error: Append system prompt file not found: <path>` and exits without
    running), never be dropped quietly — a run with the user half alone is a
    task with no persona, no rules and no tool descriptions, which is ISSUE-375
    in a smaller frame.
    """

    def _req(self, tmp_path, *, custom=None, composed=None, streaming=False):
        return BrainRequest(
            prompt="hi",
            allowed_tools=["Bash"],
            cwd=tmp_path,
            env={},
            timeout_seconds=60,
            streaming=streaming,
            custom_system_prompt_path=custom,
            composed_system_prompt_path=composed,
        )

    def _files(self, tmp_path):
        custom = tmp_path / "operator-system-prompt.md"
        custom.write_text("operator override")
        composed = tmp_path / "task_7_system_prompt.txt"
        composed.write_text("You are Istota.")
        return custom, composed

    def test_neither_file_emits_neither_flag(self, tmp_path):
        from istota.brain.claude_code import build_claude_cli_flags

        flags = build_claude_cli_flags(self._req(tmp_path))
        assert "--system-prompt-file" not in flags
        assert "--append-system-prompt-file" not in flags

    def test_only_the_operator_file_emits_only_the_replace_flag(self, tmp_path):
        from istota.brain.claude_code import build_claude_cli_flags

        custom, _ = self._files(tmp_path)
        flags = build_claude_cli_flags(self._req(tmp_path, custom=custom))
        assert flags[flags.index("--system-prompt-file") + 1] == str(custom)
        assert "--append-system-prompt-file" not in flags

    def test_only_the_composed_file_emits_only_the_append_flag(self, tmp_path):
        from istota.brain.claude_code import build_claude_cli_flags

        _, composed = self._files(tmp_path)
        flags = build_claude_cli_flags(self._req(tmp_path, composed=composed))
        assert flags[flags.index("--append-system-prompt-file") + 1] == str(composed)
        assert "--system-prompt-file" not in flags

    def test_both_files_each_appear_exactly_once(self, tmp_path):
        from istota.brain.claude_code import build_claude_cli_flags

        custom, composed = self._files(tmp_path)
        flags = build_claude_cli_flags(
            self._req(tmp_path, custom=custom, composed=composed)
        )
        assert flags.count("--system-prompt-file") == 1
        assert flags.count("--append-system-prompt-file") == 1
        assert flags[flags.index("--system-prompt-file") + 1] == str(custom)
        assert flags[flags.index("--append-system-prompt-file") + 1] == str(composed)
        # The operator file's replace semantics come first; the composed block
        # is appended beside it.
        assert flags.index("--system-prompt-file") < flags.index(
            "--append-system-prompt-file"
        )

    def test_a_missing_composed_file_still_emits_the_flag(self, tmp_path):
        """Fail closed. The CLI opens the path and refuses to run."""
        from istota.brain.claude_code import build_claude_cli_flags

        missing = tmp_path / "gone.txt"
        assert not missing.exists()
        flags = build_claude_cli_flags(self._req(tmp_path, composed=missing))
        assert flags[flags.index("--append-system-prompt-file") + 1] == str(missing)

    def test_a_missing_operator_file_is_still_omitted(self, tmp_path):
        """The control for the case above: the optional file keeps its existing
        `exists()` gate, so the two contracts stay visibly different."""
        from istota.brain.claude_code import build_claude_cli_flags

        missing = tmp_path / "absent.md"
        flags = build_claude_cli_flags(self._req(tmp_path, custom=missing))
        assert "--system-prompt-file" not in flags

    def test_a_text_only_call_still_carries_a_composed_file(self, tmp_path):
        """`allowed_tools=[]` suppresses the tool flags, not the standing
        instructions. A direct text-only caller is unaffected because it
        supplies no composed path at all."""
        from istota.brain.claude_code import build_claude_cli_flags

        _, composed = self._files(tmp_path)
        req = self._req(tmp_path, composed=composed)
        req.allowed_tools = []
        flags = build_claude_cli_flags(req)
        assert "--disallowedTools" not in flags
        assert flags[flags.index("--append-system-prompt-file") + 1] == str(composed)

    def test_unsupported_drops_the_flag_and_warns(self, tmp_path, caplog):
        """Exercised independently of the production tmux set, which keeps the
        flag supported. This is the shared helper's own drop behaviour."""
        import logging

        from istota.brain import claude_code

        claude_code._WARNED_UNSUPPORTED_FLAGS.clear()
        _, composed = self._files(tmp_path)
        with caplog.at_level(logging.WARNING):
            flags = claude_code.build_claude_cli_flags(
                self._req(tmp_path, composed=composed),
                unsupported=frozenset({"--append-system-prompt-file"}),
            )
        assert "--append-system-prompt-file" not in flags
        assert str(composed) not in flags
        assert any("unsupported_flag" in r.message for r in caplog.records)

    @pytest.mark.parametrize("streaming", [False, True])
    def test_the_headless_argv_carries_both_flags(self, tmp_path, streaming):
        custom, composed = self._files(tmp_path)
        req = self._req(
            tmp_path, custom=custom, composed=composed, streaming=streaming
        )
        cmd = ClaudeCodeBrain._build_command(req)
        assert cmd[:3] == ["claude", "-p", "-"]
        assert cmd[cmd.index("--append-system-prompt-file") + 1] == str(composed)
        assert cmd[cmd.index("--system-prompt-file") + 1] == str(custom)
        # The user half is still what goes on stdin: `-p -` reads the prompt
        # from there, and neither half is concatenated onto the other.
        assert req.prompt not in cmd


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
