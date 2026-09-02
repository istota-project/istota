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

import asyncio
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


class TestTheFallbackCopyCarriesTheComposedSplit:
    """A reroute must keep both prompt channels, in both directions.

    `req.prompt` is the user half and `composed_system_prompt_path` names the
    system half. `_run_fallback` copies with `dataclasses.replace(req, model=…,
    effort=…, advisor=…, is_fallback=True)`, which names neither — so the
    carrying is free and what needs holding is that each brain then *reads* the
    field it was handed. A field that carried across and was consumed by
    nobody would leave the fallback running with no standing instructions,
    which is the failure this whole split exists to close.
    """

    def _composed(self, tmp_path):
        p = tmp_path / "task_9_system_prompt.txt"
        p.write_text("COMPOSED-SENTINEL")
        return p

    def _copy(self, req):
        return dataclasses.replace(
            req, model="m", effort="high", advisor="", is_fallback=True,
        )

    def test_the_replace_call_keeps_both_halves(self, tmp_path):
        composed = self._composed(tmp_path)
        fb = self._copy(
            _req(tmp_path, prompt="USER-HALF", composed_system_prompt_path=composed)
        )
        assert fb.prompt == "USER-HALF"
        assert fb.composed_system_prompt_path == composed

    def test_claude_code_to_native_keeps_the_composed_file(self, tmp_path):
        """The shipped reroute. A request assembled for ClaudeCodeBrain,
        copied, then composed by NativeBrain."""
        composed = self._composed(tmp_path)
        fb = self._copy(
            _req(
                tmp_path,
                prompt="USER-HALF",
                allowed_tools=["Bash"],
                composed_system_prompt_path=composed,
            )
        )
        brain = NativeBrain(NativeBrainConfig(model="m"), provider=object())

        assert "COMPOSED-SENTINEL" in brain._extract_system_prompt(fb)
        assert brain._system_prompt_source(fb) == "builtin+composed"
        assert fb.prompt == "USER-HALF"

    def test_native_to_claude_code_keeps_the_composed_file(self, tmp_path):
        """The other direction, through the shared CLI flag builder."""
        from istota.brain.claude_code import build_claude_cli_flags

        composed = self._composed(tmp_path)
        fb = self._copy(
            _req(
                tmp_path,
                prompt="USER-HALF",
                allowed_tools=["Bash"],
                composed_system_prompt_path=composed,
            )
        )
        flags = build_claude_cli_flags(fb)

        assert flags[flags.index("--append-system-prompt-file") + 1] == str(composed)
        assert fb.prompt == "USER-HALF"


class TestNativeBrainReadsTheNativeWrap:
    """`_start_tool_server` is where the wrap reaches bubblewrap now.

    It used to be `_build_tools`, which put the closure on `ToolEnv` for the
    Bash tool to apply per call. The tools moved into one sandboxed server per
    attempt, so the wrap is applied once, to that server's argv — but *which*
    of the request's two closures gets applied is unchanged and is what this
    class holds.
    """

    def _captured_wrap(self, monkeypatch, req):
        """Spy on the spawn without performing one.

        `start_tool_server` is patched at the name `native.py` imports it
        under, so the assertion is about the argument NativeBrain passes rather
        than about anything the spawn does with it.
        """
        seen = {}

        async def _spy(hello, **kwargs):
            seen["kwargs"] = kwargs
            raise AssertionError("stop here; the spawn itself is not under test")

        import istota.session.tools as tools_mod

        monkeypatch.setattr(tools_mod, "start_tool_server", _spy)
        brain = NativeBrain(NativeBrainConfig(model="m"), provider=object())
        with pytest.raises(AssertionError, match="stop here"):
            asyncio.run(brain._start_tool_server(req, asyncio.Event()))
        assert "kwargs" in seen, "_start_tool_server spawned nothing"
        return seen["kwargs"]["sandbox_wrap"]

    def test_the_tool_server_is_spawned_through_the_native_wrap(self, monkeypatch, tmp_path):
        req = _req(tmp_path, allowed_tools=["Bash"])
        wrap = self._captured_wrap(monkeypatch, req)

        assert wrap is _native_wrap
        assert wrap is not _claude_wrap

    def test_it_survives_the_fallback_copy(self, monkeypatch, tmp_path):
        """The reroute this design exists for: a request assembled for
        ClaudeCodeBrain, copied by `_run_fallback`, executed by NativeBrain."""
        req = dataclasses.replace(
            _req(tmp_path, allowed_tools=["Bash"]),
            model="m", effort="high", advisor="", is_fallback=True,
        )

        assert self._captured_wrap(monkeypatch, req) is _native_wrap

    def test_a_request_with_no_native_wrap_leaves_the_server_unwrapped(
        self, monkeypatch, tmp_path,
    ):
        """The control. Without it the assertions above would pass on a
        `_start_tool_server` that had hardcoded the wrap from somewhere else.

        `sandbox_wrap` is still set here, so a build that fell back to it would
        be visible rather than reading as "unsandboxed".
        """
        req = _req(tmp_path, allowed_tools=["Bash"], native_sandbox_wrap=None)

        assert self._captured_wrap(monkeypatch, req) is None

    def test_a_text_only_request_spawns_no_server_at_all(self, monkeypatch, tmp_path):
        """Empty `allowed_tools` is the sleep cycle, health OCR, briefing
        synthesis and conversation triage. They get no tools, so they must also
        get no subprocess — an unconditional spawn would put a bwrap namespace
        behind every one of those."""
        spawned = []

        async def _spy(hello, **kwargs):
            spawned.append(kwargs)

        import istota.session.tools as tools_mod

        monkeypatch.setattr(tools_mod, "start_tool_server", _spy)
        brain = NativeBrain(NativeBrainConfig(model="m"), provider=object())
        server = asyncio.run(brain._start_tool_server(_req(tmp_path), asyncio.Event()))

        assert server is None and spawned == []


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
