"""Phase 2 — Bash tool: output capture, exit codes, timeout, abort, streaming."""

import asyncio
from unittest.mock import patch

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

    async def test_the_argv_is_the_pipefail_shell_and_nothing_wraps_it(self, tmp_path):
        """This tool no longer wraps anything, and that is the seam change
        rather than a simplification: it runs inside `istota.tool_server`,
        which is itself the process bubblewrap wrapped once for the attempt.
        A per-call wrap here would nest bubblewrap inside a namespace built
        with `--unshare-user --disable-userns`, which fails every command.

        `pipefail` is still on the argv, because `[exit code: N]` is a claim
        about whether the command worked and without it a pipeline reports its
        last stage.
        """
        seen = {}

        async def _fake_exec(*cmd, **kwargs):
            seen["cmd"] = list(cmd)
            seen["kwargs"] = kwargs
            raise OSError("not actually spawning")

        with patch("asyncio.create_subprocess_exec", _fake_exec):
            await _run(make_bash_tool(_env(tmp_path)), {"command": "echo hi"})

        assert seen["cmd"] == ["bash", "-o", "pipefail", "-c", "echo hi"]
        # No `preexec_fn` either: cgroup membership is inherited at fork from
        # the server, which the daemon placed before it could fork (ISSUE-285).
        assert seen["kwargs"].get("preexec_fn") is None
        assert "task_cgroup" not in seen["kwargs"]

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


class TestBashPipelineStatus:
    """The tool appends `[exit code: N]` to what the model reads, so that number
    is a claim about whether the command worked.

    It ran under `bash -c`, which starts with `pipefail` off, so a pipeline
    reported its *last* stage — `pytest … | tail -3` came back clean on a suite
    that failed. Same defect ISSUE-307 fixed for `devbox exec`, on the shell the
    native brain actually uses.
    """

    async def test_a_failing_stage_is_reported(self, tmp_path):
        result = await _run(
            make_bash_tool(_env(tmp_path)), {"command": "false | tail -1"},
        )
        assert "exit code: 1" in _text(result), _text(result)

    async def test_a_succeeding_pipeline_is_not_reported_as_a_failure(self, tmp_path):
        """Control: the option must not colour every pipeline."""
        result = await _run(
            make_bash_tool(_env(tmp_path)), {"command": "echo hi | tail -1"},
        )
        text = _text(result)
        assert "hi" in text
        assert "exit code" not in text, text

    async def test_sigpipe_is_named_rather_than_left_as_a_bare_number(self, tmp_path):
        """`pipefail`'s one recognisable cost, answered where the model reads.

        `yes | head -1` is a correct command that now reports 141. Left as a
        bare number it reads as a failure; the tool says what it is instead.
        """
        result = await _run(
            make_bash_tool(_env(tmp_path)), {"command": "yes | head -1"},
        )
        text = _text(result)
        assert "141" in text, text
        assert "SIGPIPE" in text, text


class TestBashOutputSpill:
    """Stage 5 — over-cap output spills to a temp file named in the result."""

    async def test_spill_file_created_and_named(self, tmp_path):
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = _env(tmp_path, max_output_bytes=50, deferred_dir=deferred)
        result = await _run(
            make_bash_tool(env),
            {"command": "for i in $(seq 1 100); do echo loooong line $i; done"},
        )
        text = _text(result)
        assert "truncated" in text.lower()
        assert "full output:" in text
        # The named file exists, lives under the deferred dir, and holds the full
        # output (more than the in-context cap).
        spills = list(deferred.glob("bash_output_*.txt"))
        assert len(spills) == 1
        contents = spills[0].read_text()
        assert "loooong line 100" in contents  # the dropped tail is preserved
        assert str(spills[0]) in text

    async def test_spill_disabled_falls_back_to_cap_only(self, tmp_path):
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = _env(tmp_path, max_output_bytes=50, deferred_dir=deferred, bash_spill_full_output=False)
        result = await _run(
            make_bash_tool(env),
            {"command": "for i in $(seq 1 100); do echo loooong line $i; done"},
        )
        text = _text(result)
        assert "truncated" in text.lower()
        assert "full output:" not in text
        assert list(deferred.glob("bash_output_*.txt")) == []

    async def test_under_cap_no_spill(self, tmp_path):
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = _env(tmp_path, max_output_bytes=10_000, deferred_dir=deferred)
        result = await _run(make_bash_tool(env), {"command": "echo small"})
        assert "small" in _text(result)
        assert list(deferred.glob("bash_output_*.txt")) == []

    async def test_exclude_from_context_skips_spill(self, tmp_path):
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = _env(tmp_path, max_output_bytes=50, deferred_dir=deferred)
        await _run(
            make_bash_tool(env),
            {
                "command": "for i in $(seq 1 100); do echo loooong line $i; done",
                "exclude_from_context": True,
            },
        )
        assert list(deferred.glob("bash_output_*.txt")) == []


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


class TestBashDoesNotContainItself:
    """The three per-call placement tests that used to live here are gone, and
    their claim moved rather than being dropped: the process this tool runs in
    is placed in the task cgroup from `preexec_fn` at spawn
    (`tests/test_task_cgroup_placement.py::TestTheToolServerIsPlacedBeforeExec`),
    and every command below it is a member by being forked from there. The
    kernel-level version of that inheritance is
    `tests/linux/test_tool_server_lifecycle.py`.
    """

    async def test_it_writes_no_cgroup_procs_of_its_own(self, tmp_path):
        # The fail-open path this file used to test is now structural: there is
        # no field to read a cgroup from, so there is nothing to fail open.
        result = await _run(make_bash_tool(_env(tmp_path)), {"command": "echo fine"})

        assert "fine" in _text(result)
        assert not (tmp_path / "cgroup.procs").exists()
