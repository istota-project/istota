"""Background jobs: Bash `run_in_background`, BashOutput and KillShell."""

import asyncio
import re

import pytest

from istota.session.tools import ToolEnv, build_default_tools
from istota.session.tools import bash_jobs

pytestmark = pytest.mark.asyncio


@pytest.fixture
def tools(tmp_path):
    return _tools(tmp_path)


def _tools(tmp_path, **kw):
    kw.setdefault("deferred_dir", tmp_path / "deferred")
    return {t.schema.name: t for t in build_default_tools(ToolEnv(cwd=tmp_path, **kw))}


@pytest.fixture(autouse=True)
async def _no_job_outlives_a_test():
    yield
    bash_jobs.kill_all_jobs()


async def _call(tool, args, abort=None):
    return await tool.execute("c1", args, None, abort)


def _text(result):
    return result.content[0].text


def _body(text):
    """A read's output, without the status line that closes it."""
    return text.rpartition("[job ")[0].removesuffix("\n")


async def _start(tools, command, **args):
    result = await _call(tools["Bash"], {"command": command, "run_in_background": True, **args})
    match = re.search(r"\bbg_\d+\b", _text(result))
    assert match, _text(result)
    return match.group(0)


async def _read(tools, job_id, **args):
    return await _call(tools["BashOutput"], {"job_id": job_id, **args})


class TestStart:
    async def test_it_returns_at_once_and_the_job_keeps_running(self, tools):
        started = asyncio.get_running_loop().time()
        job_id = await _start(tools, "sleep 30")
        assert asyncio.get_running_loop().time() - started < 5
        assert "running" in _text(await _read(tools, job_id))

    async def test_a_false_flag_stays_in_the_foreground(self, tools):
        result = await _call(tools["Bash"], {"command": "echo here", "run_in_background": False})
        assert "here" in _text(result)
        assert "bg_" not in _text(result)

    async def test_it_runs_in_the_cwd_under_pipefail(self, tools, tmp_path):
        job_id = await _start(tools, "pwd -P; false | cat")
        text = _text(await _read(tools, job_id, wait_seconds=10))
        assert str(tmp_path.resolve()) in text
        assert "exit code: 1" in text

    async def test_running_jobs_are_capped_and_the_refusal_names_them(self, tools):
        for _ in range(bash_jobs.MAX_RUNNING_JOBS):
            await _start(tools, "sleep 30")
        refused = await _call(tools["Bash"], {"command": "echo no", "run_in_background": True})
        assert "bg_1" in _text(refused)
        assert "running" in _text(refused)

    async def test_a_finished_job_frees_its_slot(self, tools):
        for _ in range(bash_jobs.MAX_RUNNING_JOBS):
            last = await _start(tools, "true")
            await _read(tools, last, wait_seconds=10)
        await _start(tools, "true")

    async def test_the_oldest_finished_jobs_are_forgotten(self, tmp_path):
        tools = _tools(tmp_path)
        first = await _start(tools, "true")
        await _read(tools, first, wait_seconds=10)
        for _ in range(bash_jobs.MAX_KEPT_JOBS):
            job_id = await _start(tools, "true")
            await _read(tools, job_id, wait_seconds=10)
        assert (await _read(tools, first)).is_error


class TestReadingOutput:
    async def test_a_read_resumes_where_the_last_one_ended(self, tools):
        job_id = await _start(tools, "echo one; sleep 0.5; echo two")
        await _read(tools, job_id, wait_seconds=10)
        second = _text(await _read(tools, job_id))
        assert "one" not in second
        assert "two" not in second

    async def test_an_explicit_offset_rereads(self, tools):
        job_id = await _start(tools, "printf 0123456789")
        await _read(tools, job_id, wait_seconds=10)
        assert _text(await _read(tools, job_id, offset=4)).startswith("456789")

    async def test_wait_returns_as_soon_as_the_job_ends(self, tools):
        job_id = await _start(tools, "sleep 0.2; echo done")
        started = asyncio.get_running_loop().time()
        text = _text(await _read(tools, job_id, wait_seconds=30))
        assert asyncio.get_running_loop().time() - started < 10
        assert "done" in text and "exit code: 0" in text

    async def test_wait_ends_when_the_window_does(self, tools):
        job_id = await _start(tools, "sleep 30")
        started = asyncio.get_running_loop().time()
        text = _text(await _read(tools, job_id, wait_seconds=1))
        assert 0.8 < asyncio.get_running_loop().time() - started < 10
        assert "running" in text

    async def test_wait_is_held_to_the_bash_timeout(self, tmp_path):
        tools = _tools(tmp_path, bash_timeout_seconds=1)
        job_id = await _start(tools, "sleep 30")
        started = asyncio.get_running_loop().time()
        await _read(tools, job_id, wait_seconds=3600)
        assert asyncio.get_running_loop().time() - started < 10

    async def test_an_abort_ends_the_wait_and_leaves_the_job_running(self, tools):
        job_id = await _start(tools, "sleep 30")
        abort = asyncio.Event()
        waiting = asyncio.ensure_future(
            _call(tools["BashOutput"], {"job_id": job_id, "wait_seconds": 60}, abort)
        )
        await asyncio.sleep(0.1)
        abort.set()
        result = await asyncio.wait_for(waiting, timeout=10)
        assert "running" in _text(result)

    async def test_no_job_id_lists_the_jobs(self, tools):
        job_id = await _start(tools, "sleep 30", description="a long nap")
        text = _text(await _call(tools["BashOutput"], {}))
        assert job_id in text and "sleep 30" in text and "running" in text

    async def test_an_unknown_job_is_an_error(self, tools):
        result = await _read(tools, "bg_99")
        assert result.is_error
        assert "bg_99" in _text(result)

    async def test_a_large_output_pages_through_the_spill_file(self, tmp_path):
        tools = _tools(tmp_path, max_output_bytes=1000)
        job_id = await _start(tools, "head -c 5000 /dev/zero | tr '\\0' x")
        first = _text(await _read(tools, job_id, wait_seconds=10))
        assert len(_body(first)) == 1000
        assert "offset=1000" in first
        assert "bash_output_" in first
        seen = 1000
        while True:
            got = len(_body(_text(await _read(tools, job_id))))
            if not got:
                break
            seen += got
        assert seen == 5000

    async def test_with_the_spill_off_the_tail_is_dropped_and_said_to_be(self, tmp_path):
        tools = _tools(tmp_path, max_output_bytes=100, bash_spill_full_output=False)
        job_id = await _start(tools, "head -c 500 /dev/zero | tr '\\0' x")
        text = _text(await _read(tools, job_id, wait_seconds=10))
        assert len(_body(text)) == 100
        assert "400 bytes" in text
        assert not list(tmp_path.rglob("bash_output_*"))

    async def test_a_finished_jobs_invalid_bytes_are_still_returned(self, tools):
        job_id = await _start(tools, "printf 'a\\303'")
        assert _body(_text(await _read(tools, job_id, wait_seconds=10))) == "a�"

    async def test_a_read_never_splits_a_character(self, tmp_path):
        tools = _tools(tmp_path, max_output_bytes=10)
        job_id = await _start(tools, "printf 'aaaaaaaaa\\xc3\\xa9bbb'")
        text = _text(await _read(tools, job_id, wait_seconds=10))
        assert _body(text) == "aaaaaaaaa"
        assert "offset=9" in text

    async def test_what_is_kept_on_disk_is_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bash_jobs, "JOB_OUTPUT_LIMIT_BYTES", 2000)
        tools = _tools(tmp_path, max_output_bytes=100)
        job_id = await _start(tools, "head -c 500000 /dev/zero | tr '\\0' x")
        text = _text(await _read(tools, job_id, wait_seconds=10))
        assert "exit code: 0" in text
        (spill,) = list(tmp_path.rglob("bash_output_*"))
        assert spill.stat().st_size == 2000
        assert "498000 bytes" in text


class TestStopping:
    async def test_kill_takes_the_whole_group_and_the_output_stays_readable(self, tools, tmp_path):
        marker = tmp_path / "beat"
        job_id = await _start(
            tools,
            f"echo started; (while :; do date +%s%N >> {marker}; sleep 0.05; done) & wait",
        )
        await _read(tools, job_id, wait_seconds=1)
        killed = await _call(tools["KillShell"], {"job_id": job_id})
        assert "killed" in _text(killed)
        await asyncio.sleep(0.3)
        size = marker.stat().st_size
        await asyncio.sleep(0.3)
        assert marker.stat().st_size == size
        assert "started" in _text(await _read(tools, job_id, offset=0))

    async def test_killing_a_finished_job_says_how_it_ended(self, tools):
        job_id = await _start(tools, "exit 4")
        await _read(tools, job_id, wait_seconds=10)
        text = _text(await _call(tools["KillShell"], {"job_id": job_id}))
        assert "exit code: 4" in text
        assert "killed" not in text

    async def test_killing_an_unknown_job_is_an_error(self, tools):
        assert (await _call(tools["KillShell"], {"job_id": "bg_99"})).is_error

    async def test_an_explicit_timeout_ends_the_job(self, tools):
        job_id = await _start(tools, "sleep 30", timeout=200)
        text = _text(await _read(tools, job_id, wait_seconds=10))
        assert "timed out" in text

    async def test_no_timeout_means_the_foreground_default_does_not_apply(self, tmp_path):
        tools = _tools(tmp_path, bash_timeout_seconds=1)
        job_id = await _start(tools, "sleep 30")
        await asyncio.sleep(1.5)
        assert "running" in _text(await _read(tools, job_id))

    async def test_kill_all_jobs_reaches_a_running_job(self, tools, tmp_path):
        marker = tmp_path / "beat"
        await _start(tools, f"while :; do date +%s%N >> {marker}; sleep 0.05; done")
        while not marker.exists():
            await asyncio.sleep(0.02)
        bash_jobs.kill_all_jobs()
        await asyncio.sleep(0.3)
        size = marker.stat().st_size
        await asyncio.sleep(0.3)
        assert marker.stat().st_size == size


async def _gone(marker):
    for _ in range(50):
        size = marker.stat().st_size
        await asyncio.sleep(0.3)
        if marker.stat().st_size == size:
            return True
    return False


class TestACommandThatExitsBeforeItsChildren:
    """`npm run dev &` shapes: the shell has gone and the job has not."""

    async def test_kill_returns_and_stops_a_child_still_writing(self, tools):
        job_id = await _start(tools, "(while :; do echo tick; sleep 0.1; done) & echo started")
        await asyncio.sleep(1)
        status = _text(await _read(tools, job_id))
        assert "running" in status and "exited 0" in status
        killed = await asyncio.wait_for(_call(tools["KillShell"], {"job_id": job_id}), timeout=10)
        assert "killed" in _text(killed)

    async def test_kill_stops_a_quiet_child_holding_the_pipe(self, tools, tmp_path):
        marker = tmp_path / "beat"
        job_id = await _start(
            tools, f"(while :; do date >> {marker}; sleep 0.1; done) & echo started"
        )
        await asyncio.sleep(1)
        assert "running" in _text(await _read(tools, job_id))
        await _call(tools["KillShell"], {"job_id": job_id})
        assert await _gone(marker)

    async def test_kill_all_jobs_stops_a_child_holding_the_pipe(self, tools, tmp_path):
        marker = tmp_path / "beat"
        await _start(tools, f"(while :; do date >> {marker}; sleep 0.1; done) & echo started")
        await asyncio.sleep(1)
        bash_jobs.kill_all_jobs()
        assert await _gone(marker)

    async def test_a_child_that_redirected_away_has_detached(self, tools):
        job_id = await _start(tools, "sleep 30 >/dev/null 2>&1 & echo started")
        text = _text(await _read(tools, job_id, wait_seconds=10))
        assert "exit code: 0" in text


class TestACommandThatMovesItsOutput:
    async def test_it_is_not_killed_for_closing_the_pipe(self, tools, tmp_path):
        job_id = await _start(tools, f"exec > {tmp_path / 'log'} 2>&1; sleep 6; exit 3")
        text = _text(await _read(tools, job_id, wait_seconds=20))
        assert "exit code: 3" in text

    async def test_kill_reaches_it(self, tools, tmp_path):
        job_id = await _start(tools, f"exec > {tmp_path / 'log'} 2>&1; sleep 30")
        await asyncio.sleep(0.5)
        killed = await asyncio.wait_for(_call(tools["KillShell"], {"job_id": job_id}), timeout=10)
        assert "killed" in _text(killed)


class TestPollingAQuietJob:
    async def test_two_polls_differ_so_the_native_loop_detector_lets_it_run(self, tools):
        """`session/loop_detection.py` hashes the tool, arguments and result and
        ends the attempt on repeats, which would take the job with it."""
        job_id = await _start(tools, "sleep 30")
        first = _text(await _read(tools, job_id, wait_seconds=1))
        second = _text(await _read(tools, job_id, wait_seconds=1))
        assert first != second


class TestCharactersAcrossReads:
    async def test_a_running_jobs_tail_holds_back_a_cut_character(self, tools):
        job_id = await _start(tools, "printf 'a\\303'; sleep 1; printf '\\251b'")
        await asyncio.sleep(0.5)
        assert _body(_text(await _read(tools, job_id))) == "a"
        assert _body(_text(await _read(tools, job_id, wait_seconds=10))) == "éb"


class TestSchemas:
    async def test_bash_output_is_parallel_and_kill_shell_is_not(self, tools):
        assert tools["BashOutput"].execution_mode == "parallel"
        assert tools["KillShell"].execution_mode == "sequential"

    async def test_a_bash_tool_with_no_table_refuses_rather_than_runs_in_the_foreground(self, tmp_path):
        from istota.session.tools import make_bash_tool

        tool = make_bash_tool(ToolEnv(cwd=tmp_path))
        marker = tmp_path / "ran"
        result = await _call(tool, {"command": f"touch {marker}", "run_in_background": True})
        assert result.is_error
        assert not marker.exists()
