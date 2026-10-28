"""One sandbox wrap on `BrainRequest`, `tool_server_wrap`, read by both brains.

There used to be two, `sandbox_wrap` (the CLAUDE profile, around the `claude`
CLI itself) and `native_sandbox_wrap`, because `executor._run_fallback` copies
the request with

    dataclasses.replace(req, model=…, effort=…, advisor=…, is_fallback=True)

and a single field would have carried the Claude namespace into NativeBrain on
a `claude_code -> native` reroute (ISSUE-389). That invariant is retired: the
CLI runs outside the sandbox on the tool harness (cli-brain-tools part 2), so
there is one namespace, the tool server's, for every brain, and nothing that
could cross a reroute with the wrong profile.
"""

import asyncio
import dataclasses

import pytest

from istota.brain._types import BrainRequest, BrainResult
from istota.brain.claude_code import ClaudeCodeBrain
from istota.brain.native import NativeBrain
from istota.config import NativeBrainConfig


def _wrap(cmd):
    return ["bwrap", "TOOL-SERVER", "--", *cmd]


def _req(tmp_path, **kw):
    base = dict(
        prompt="hello",
        allowed_tools=[],
        cwd=tmp_path,
        env={},
        timeout_seconds=60,
        # Non-streaming, so the assertions below can watch one exit path.
        streaming=False,
        tool_server_wrap=_wrap,
    )
    base.update(kw)
    return BrainRequest(**base)


class TestOneWrap:
    def test_there_is_no_cli_wrap_field(self):
        names = {f.name for f in dataclasses.fields(BrainRequest)}
        assert "tool_server_wrap" in names
        assert "sandbox_wrap" not in names
        assert "native_sandbox_wrap" not in names

    def test_the_fallback_copy_carries_it(self, tmp_path):
        """The exact call `_run_fallback` makes, field for field."""
        fb = dataclasses.replace(
            _req(tmp_path), model="m", effort="high", advisor="", is_fallback=True,
        )
        assert fb.tool_server_wrap is _wrap

    def test_a_direct_caller_may_leave_it_unset(self, tmp_path):
        bare = BrainRequest(
            prompt="x", allowed_tools=[], cwd=tmp_path, env={}, timeout_seconds=1,
        )
        assert bare.tool_server_wrap is None


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


class TestNativeBrainReadsTheWrap:
    """`_start_tool_server` is where the wrap reaches bubblewrap: once, round
    the attempt's tool server."""

    def _captured_wrap(self, monkeypatch, req):
        """Spy on the spawn without performing one."""
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

    def test_the_tool_server_is_spawned_through_the_wrap(self, monkeypatch, tmp_path):
        req = _req(tmp_path, allowed_tools=["Bash"])
        assert self._captured_wrap(monkeypatch, req) is _wrap

    def test_it_survives_the_fallback_copy(self, monkeypatch, tmp_path):
        req = dataclasses.replace(
            _req(tmp_path, allowed_tools=["Bash"]),
            model="m", effort="high", advisor="", is_fallback=True,
        )
        assert self._captured_wrap(monkeypatch, req) is _wrap

    def test_a_request_with_no_wrap_leaves_the_server_unwrapped(
        self, monkeypatch, tmp_path,
    ):
        """The control: the assertions above are not a hardcoded wrap."""
        req = _req(tmp_path, allowed_tools=["Bash"], tool_server_wrap=None)
        assert self._captured_wrap(monkeypatch, req) is None

    def test_a_text_only_request_spawns_no_server_at_all(self, monkeypatch, tmp_path):
        """Empty `allowed_tools` is the sleep cycle, the explainer, briefing
        synthesis and conversation triage: no tools, no subprocess."""
        spawned = []

        async def _spy(hello, **kwargs):
            spawned.append(kwargs)

        import istota.session.tools as tools_mod

        monkeypatch.setattr(tools_mod, "start_tool_server", _spy)
        brain = NativeBrain(NativeBrainConfig(model="m"), provider=object())
        server = asyncio.run(brain._start_tool_server(_req(tmp_path), asyncio.Event()))

        assert server is None and spawned == []


class TestTheClaudeBrainNeverWrapsTheCli:
    def test_a_text_only_cli_call_runs_unwrapped(self, monkeypatch, tmp_path):
        """The wrap is the tool server's; the CLI is never inside it."""
        brain = ClaudeCodeBrain()
        captured = {}

        def _fake_simple(cmd, req):
            captured["cmd"] = cmd
            return BrainResult(success=True, result_text="sentinel")

        monkeypatch.setattr(brain, "_execute_simple", _fake_simple)
        assert brain.execute(_req(tmp_path)).result_text == "sentinel"
        assert captured["cmd"][0] == "claude"
        assert "TOOL-SERVER" not in captured["cmd"]
