"""Phase 2 — Bash tool: output capture, exit codes, timeout, abort, streaming."""

import asyncio

import pytest

from istota.session.tools import ToolEnv, make_bash_tool

pytestmark = pytest.mark.asyncio


def _env(tmp_path, **kw):
    return ToolEnv(cwd=tmp_path, **kw)


async def _run(tool, args, on_update=None, abort=None):
    return await tool.execute("c1", args, on_update, abort)


def _text(result):
    return result.content[0].text


class TestBash:
    async def test_captures_stdout(self, tmp_path):
        result = await _run(make_bash_tool(_env(tmp_path)), {"command": "echo hello"})
        assert "hello" in _text(result)

    async def test_captures_stderr(self, tmp_path):
        result = await _run(make_bash_tool(_env(tmp_path)), {"command": "echo oops 1>&2"})
        assert "oops" in _text(result)

    async def test_nonzero_exit_code_reported(self, tmp_path):
        result = await _run(make_bash_tool(_env(tmp_path)), {"command": "exit 3"})
        assert "exit code: 3" in _text(result)

    async def test_runs_in_cwd(self, tmp_path):
        (tmp_path / "marker.txt").write_text("")
        result = await _run(make_bash_tool(_env(tmp_path)), {"command": "ls"})
        assert "marker.txt" in _text(result)

    async def test_sandbox_wrap_applied(self, tmp_path):
        seen = {}

        def _wrap(cmd):
            seen["cmd"] = cmd
            return cmd  # no-op passthrough

        env = _env(tmp_path)
        env.sandbox_wrap = _wrap
        await _run(make_bash_tool(env), {"command": "echo hi"})
        assert seen["cmd"][:2] == ["bash", "-c"]

    async def test_streaming_on_update(self, tmp_path):
        updates = []

        async def _on_update(text):
            updates.append(text)

        await _run(make_bash_tool(_env(tmp_path)), {"command": "printf 'a\\nb\\n'"}, on_update=_on_update)
        assert any("a" in u for u in updates)

    async def test_timeout_kills_command(self, tmp_path):
        result = await _run(make_bash_tool(_env(tmp_path)), {"command": "sleep 5", "timeout": 200})
        assert "timed out" in _text(result).lower()

    async def test_abort_kills_command(self, tmp_path):
        abort = asyncio.Event()

        async def _trigger():
            await asyncio.sleep(0.2)
            abort.set()

        tool = make_bash_tool(_env(tmp_path))
        result, _ = await asyncio.gather(
            _run(tool, {"command": "sleep 5"}, abort=abort),
            _trigger(),
        )
        assert "aborted" in _text(result).lower()

    async def test_output_truncation(self, tmp_path):
        env = _env(tmp_path, max_output_bytes=50)
        result = await _run(make_bash_tool(env), {"command": "for i in $(seq 1 100); do echo loooong line $i; done"})
        assert "truncated" in _text(result).lower()

    async def test_no_output(self, tmp_path):
        result = await _run(make_bash_tool(_env(tmp_path)), {"command": "true"})
        assert "no output" in _text(result).lower()
