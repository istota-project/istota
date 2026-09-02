"""Two sandbox wraps on `BrainRequest`, and each brain reading only its own.

`build_bwrap_cmd` has two profiles now (`executor.SandboxProfile`). The request
carries one closure per profile rather than one closure plus a profile
argument, and that is a structural decision rather than a stylistic one:
`executor._run_fallback` reroutes an attempt to the other brain by copying the
request with

    dataclasses.replace(req, model=…, effort=…, advisor=…, is_fallback=True)

which names neither wrap field. With a single field, the shipped
`claude_code -> native` reroute would hand NativeBrain the *Claude* namespace —
`~/.claude/.credentials.json` and all — which is exactly the bug the split
exists to close (ISSUE-389). With two, each carries across harmlessly, because
each brain reads only the one addressed to it.

These are the tests for that. The `replace` guard is the regression the design
note predicts; the three reader tests are what make the guard mean anything,
since carrying a field nothing reads proves nothing.
"""

import dataclasses
from pathlib import Path

import pytest

from istota.brain._types import BrainRequest, BrainResult
from istota.brain.claude_code import ClaudeCodeBrain
from istota.brain.native import NativeBrain
from istota.brain.tmux_claude import TmuxClaudeBrain, reset_circuit_breaker
from istota.config import NativeBrainConfig


def _claude_wrap(cmd):
    return ["bwrap", "CLAUDE-PROFILE", "--", *cmd]


def _native_wrap(cmd):
    return ["bwrap", "NATIVE-PROFILE", "--", *cmd]


def _req(tmp_path, **kw):
    base = dict(
        prompt="hello",
        allowed_tools=[],
        cwd=tmp_path,
        env={},
        timeout_seconds=60,
        # Non-streaming, so the assertions below can watch one exit path.
        streaming=False,
        sandbox_wrap=_claude_wrap,
        native_sandbox_wrap=_native_wrap,
    )
    base.update(kw)
    return BrainRequest(**base)


class TestTheFallbackCopyCarriesBothWraps:
    def test_replace_as_run_fallback_spells_it_keeps_both(self, tmp_path):
        """The exact call `_run_fallback` makes, field for field."""
        req = _req(tmp_path)

        fb = dataclasses.replace(
            req, model="m", effort="high", advisor="", is_fallback=True,
        )

        assert fb.sandbox_wrap is _claude_wrap
        assert fb.native_sandbox_wrap is _native_wrap

    def test_the_two_fields_are_independent(self, tmp_path):
        """Neither defaults from the other, and either may be absent alone.

        A direct brain caller (the sleep cycle, a health OCR pass) builds a
        request with no sandbox at all; the executor supplies both.
        """
        bare = BrainRequest(
            prompt="x", allowed_tools=[], cwd=tmp_path, env={}, timeout_seconds=1,
        )
        assert bare.sandbox_wrap is None
        assert bare.native_sandbox_wrap is None

        only_native = _req(tmp_path, sandbox_wrap=None)
        assert only_native.sandbox_wrap is None
        assert only_native.native_sandbox_wrap is _native_wrap


class TestNativeBrainReadsTheNativeWrap:
    """`_build_tools` is where the wrap reaches the Bash tool."""

    def _captured_env(self, monkeypatch, req):
        import istota.session.tools as tools_mod

        real = tools_mod.ToolEnv
        seen = []

        def _spy(**kwargs):
            env = real(**kwargs)
            seen.append(env)
            return env

        monkeypatch.setattr(tools_mod, "ToolEnv", _spy)
        brain = NativeBrain(NativeBrainConfig(model="m"), provider=object())
        brain._build_tools(req)
        assert seen, "_build_tools built no ToolEnv"
        return seen[-1]

    def test_the_tool_env_gets_the_native_wrap(self, monkeypatch, tmp_path):
        req = _req(tmp_path, allowed_tools=["Bash"])
        env = self._captured_env(monkeypatch, req)

        assert env.sandbox_wrap is _native_wrap
        assert env.sandbox_wrap is not _claude_wrap

    def test_it_survives_the_fallback_copy(self, monkeypatch, tmp_path):
        """The reroute this design exists for: a request assembled for
        ClaudeCodeBrain, copied by `_run_fallback`, executed by NativeBrain."""
        req = dataclasses.replace(
            _req(tmp_path, allowed_tools=["Bash"]),
            model="m", effort="high", advisor="", is_fallback=True,
        )
        env = self._captured_env(monkeypatch, req)

        assert env.sandbox_wrap is _native_wrap

    def test_a_request_with_no_native_wrap_leaves_the_tools_unwrapped(
        self, monkeypatch, tmp_path,
    ):
        """The control. Without it the assertions above would pass on a
        `_build_tools` that had hardcoded the wrap from somewhere else.

        `sandbox_wrap` is still set here, so a build that fell back to it would
        be visible rather than reading as "unsandboxed".
        """
        req = _req(tmp_path, allowed_tools=["Bash"], native_sandbox_wrap=None)
        env = self._captured_env(monkeypatch, req)

        assert env.sandbox_wrap is None


class TestTheClaudeBrainsReadTheClaudeWrap:
    def test_claude_code_wraps_with_the_claude_profile(self, monkeypatch, tmp_path):
        brain = ClaudeCodeBrain()
        captured = {}

        def _fake_simple(cmd, req):
            captured["cmd"] = cmd
            return BrainResult(success=True, result_text="sentinel")

        monkeypatch.setattr(brain, "_execute_simple", _fake_simple)
        assert brain.execute(_req(tmp_path)).result_text == "sentinel"
        assert captured["cmd"][:2] == ["bwrap", "CLAUDE-PROFILE"]

    def test_claude_code_ignores_the_native_wrap(self, monkeypatch, tmp_path):
        """The control: with only the native wrap set, the CLI argv is bare."""
        brain = ClaudeCodeBrain()
        captured = {}
        def _fake_simple(cmd, req):
            captured["cmd"] = cmd
            return BrainResult(success=True, result_text="sentinel")

        monkeypatch.setattr(brain, "_execute_simple", _fake_simple)
        brain.execute(_req(tmp_path, sandbox_wrap=None))

        assert "NATIVE-PROFILE" not in captured["cmd"]
        assert captured["cmd"][0] != "bwrap"

    @pytest.fixture(autouse=True)
    def _tmux_preconditions(self, monkeypatch):
        import istota.brain.tmux_claude as mod

        monkeypatch.setattr(mod, "_VERSION_CHECKED", True)
        reset_circuit_breaker()
        yield
        reset_circuit_breaker()

    def _launch(self, monkeypatch, req):
        class _CP:
            stdout = ""
            returncode = 0

        brain = TmuxClaudeBrain()
        sent = []
        monkeypatch.setattr(brain, "_tmux", lambda *a: sent.append(a) or _CP())
        brain._launch_claude("s1", req, Path(req.cwd))
        literal = next(a for a in sent if a[:4] == ("send-keys", "-t", "s1", "-l"))
        return literal[4]

    def test_tmux_wraps_with_the_claude_profile(self, monkeypatch, tmp_path):
        cmd = self._launch(monkeypatch, _req(tmp_path))
        assert "bwrap CLAUDE-PROFILE --" in cmd

    def test_tmux_ignores_the_native_wrap(self, monkeypatch, tmp_path):
        cmd = self._launch(monkeypatch, _req(tmp_path, sandbox_wrap=None))
        assert "bwrap" not in cmd
        assert "NATIVE-PROFILE" not in cmd
