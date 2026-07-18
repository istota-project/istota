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

    async def test_exclude_from_context_stubs_model_output(self, tmp_path):
        updates = []

        async def _on_update(text):
            updates.append(text)

        result = await _run(
            make_bash_tool(_env(tmp_path)),
            {"command": "printf 'secret123\\n'", "exclude_from_context": True},
            on_update=_on_update,
        )
        # The model-facing content is a stub — the real output is kept out of
        # context…
        assert "secret123" not in _text(result)
        assert "omitted from context" in _text(result)
        # …but the full output still reached the progress surface.
        assert any("secret123" in u for u in updates)

    async def test_exclude_from_context_default_includes_output(self, tmp_path):
        result = await _run(make_bash_tool(_env(tmp_path)), {"command": "echo visible"})
        assert "visible" in _text(result)


class TestBashProcessHandling:
    """NB-6/NB-7/NB-11: long lines must not crash the tool, and timeout/abort/
    cancel must kill the whole process group (no orphaned grandchildren)."""

    async def test_long_line_does_not_crash(self, tmp_path):
        # A single line far larger than asyncio's default 64 KiB StreamReader
        # limit used to raise ValueError (minified JS, base64, `jq -c`).
        env = _env(tmp_path, max_output_bytes=500_000)
        big = 200_000
        result = await _run(
            make_bash_tool(env),
            {"command": f"printf 'x%.0s' $(seq 1 {big})"},
        )
        text = _text(result)
        assert "Failed to start" not in text
        # The bulk of the long line is captured (up to the cap), not lost.
        assert text.count("x") > 100_000

    async def test_timeout_kills_backgrounded_grandchild(self, tmp_path):
        # A command that backgrounds a child which holds the stdout pipe open.
        # Without a process-group kill the grandchild survives and the poll
        # would hang on the open pipe. The whole group must die on timeout.
        marker = tmp_path / "alive.txt"
        cmd = (
            f"(while true; do echo tick > {marker}; sleep 0.1; done) & "
            "echo started; sleep 30"
        )
        result = await _run(
            make_bash_tool(_env(tmp_path)),
            {"command": cmd, "timeout": 500},
        )
        assert "timed out" in _text(result).lower()
        # Give any surviving grandchild a moment to prove it's still writing.
        await asyncio.sleep(0.5)
        mtime1 = marker.stat().st_mtime if marker.exists() else 0
        await asyncio.sleep(0.5)
        mtime2 = marker.stat().st_mtime if marker.exists() else 0
        assert mtime1 == mtime2, "grandchild survived the timeout (process group not killed)"

    async def test_cancel_reaps_subprocess(self, tmp_path):
        # A hard task cancellation (CancelledError) landing inside _execute must
        # not leak the subprocess — the finally block kills the group.
        marker = tmp_path / "alive.txt"
        cmd = f"while true; do echo tick > {marker}; sleep 0.1; done"
        tool = make_bash_tool(_env(tmp_path))
        task = asyncio.ensure_future(_run(tool, {"command": cmd}))
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.5)
        mtime1 = marker.stat().st_mtime if marker.exists() else 0
        await asyncio.sleep(0.5)
        mtime2 = marker.stat().st_mtime if marker.exists() else 0
        assert mtime1 == mtime2, "subprocess survived cancellation"
