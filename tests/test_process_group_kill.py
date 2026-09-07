"""Killing a task's subprocess must take its descendants with it (ISSUE-257).

A timeout, a `!stop` or a web cancel used to signal the `claude` CLI alone.
The CLI was spawned without ``start_new_session``, so its bash grandchildren —
a `pytest -n auto` run, in the incident that filed the issue — were orphaned
and ran to completion. Giving the CLI its own session and signalling the
*group* is what NativeBrain's bash tool already does (`session/tools/bash.py`).

The safety property matters as much as the fix: a pid that does **not** lead
its own group shares one with whoever spawned it, and for a child the daemon
started without ``start_new_session`` that group is the daemon's. Signalling it
would kill the scheduler. `kill_process_group` therefore signals a group only
when the pid leads it, and falls back to the single process otherwise. Both of
today's writers of ``worker_pid`` record leaders (ClaudeCodeBrain's own child
after this change, and a tmux pane pid, which tmux ``setsid``s already), so the
fallback is what keeps the helper safe for a caller that records a pid it did
not spawn rather than something either brain reaches today.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.brain import BrainRequest
from istota.brain.claude_code import ClaudeCodeBrain
from istota.process_group import kill_group_if_live, kill_process_group


# =============================================================================
# The helper itself, against real processes
# =============================================================================


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    """Poll until `pid` no longer exists. Signal 0 is the existence probe."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


class TestKillProcessGroup:
    def test_a_backgrounded_grandchild_dies_with_the_group(self, tmp_path):
        # The incident's shape: the direct child backgrounds a long-running
        # worker and the worker is what holds the CPU. Killing the child alone
        # leaves it running.
        pidfile = tmp_path / "grandchild.pid"
        script = f"sleep 30 & echo $! > {pidfile}; sleep 30"
        proc = subprocess.Popen(["bash", "-c", script], start_new_session=True)
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not pidfile.exists():
                time.sleep(0.02)
            grandchild = int(pidfile.read_text().strip())

            assert kill_process_group(proc.pid) == "group"

            assert _wait_gone(grandchild), (
                "backgrounded grandchild survived the group kill"
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_a_pid_that_does_not_lead_its_group_is_killed_alone(self, monkeypatch):
        # The load-bearing guard. This child shares the test runner's process
        # group, so a killpg here would take down pytest itself — and in
        # production, the scheduler. os.killpg must not be reached at all.
        def _forbidden(*args, **kwargs):
            raise AssertionError("killpg called on a pid that leads no group")

        monkeypatch.setattr(os, "killpg", _forbidden)

        proc = subprocess.Popen(["sleep", "30"])  # deliberately no new session
        try:
            assert os.getpgid(proc.pid) != proc.pid  # precondition
            assert kill_process_group(proc.pid) == "process"
            assert _wait_gone(proc.pid) or proc.poll() is not None
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_a_dead_pid_reports_gone_rather_than_raising(self, monkeypatch):
        # A task that finished between the cancel and the signal is the normal
        # race, not an error. Faked rather than reaped-for-real: the OS is free
        # to reuse a just-reaped number, and under `-n auto` that would have the
        # test SIGKILL an unrelated process (and its group).
        def _gone(*_a, **_kw):
            raise ProcessLookupError

        monkeypatch.setattr(os, "getpgid", _gone)
        monkeypatch.setattr(os, "killpg", _gone)
        monkeypatch.setattr(os, "kill", _gone)

        assert kill_process_group(4242) == "gone"

    def test_a_raising_helper_never_reaches_the_caller(self, monkeypatch):
        # The streaming brain's timeout calls this from a threading.Timer
        # callback with no try/except: an exception there kills the timer
        # thread after the timeout flag is set, so the task reports a timeout
        # while the process runs on — the ISSUE-257 symptom itself.
        def _boom(*_a, **_kw):
            raise RuntimeError("kernel said no")

        monkeypatch.setattr(os, "getpgid", _boom)

        assert kill_process_group(4242) == "gone"

    @pytest.mark.parametrize("pid", [0, -1, -4242])
    def test_non_positive_pids_signal_nothing(self, pid, monkeypatch):
        # os.kill(0, sig) signals our *own* process group and os.kill(-1, sig)
        # signals every process we may signal. A NULL/absent worker_pid must
        # never reach either.
        def _forbidden(*args, **kwargs):
            raise AssertionError(f"signalled pid {pid}")

        monkeypatch.setattr(os, "kill", _forbidden)
        monkeypatch.setattr(os, "killpg", _forbidden)

        assert kill_process_group(pid) == "gone"

    def test_the_signal_is_configurable(self, tmp_path):
        # !stop and the web cancel endpoint send SIGTERM, not SIGKILL, so the
        # CLI gets a chance to shut down cleanly.
        sent = []
        with patch("istota.process_group.os.getpgid", return_value=1234), \
             patch("istota.process_group.os.killpg",
                   side_effect=lambda pgid, sig: sent.append((pgid, sig))):
            assert kill_process_group(1234, signal.SIGTERM) == "group"
        assert sent == [(1234, signal.SIGTERM)]


# =============================================================================
# The reaped-process guard, shared by the three kill sites
# =============================================================================


class _FakeProcess:
    """A Popen/asyncio-subprocess stand-in: `.returncode` and `.pid`, nothing else."""

    def __init__(self, *, returncode, pid=4242):
        self.returncode = returncode
        self.pid = pid


class TestKillGroupIfLive:
    """`kill_group_if_live` is the guard three sites carried separately (F5).

    `scheduler._kill_process_group` did not carry it at all — it signalled the
    pid it was handed whatever state the process was in. The guard matters
    because every caller signals from somewhere that can fire *after* the reap:
    a `threading.Timer` outliving `process.wait()`, a `finally` unwinding a
    cancelled coroutine, a timeout handler running after `communicate()`
    returned. By then the number may belong to an unrelated process, and
    `kill_process_group` would take its whole group.
    """

    def test_a_reaped_process_is_not_signalled(self):
        signalled = []
        with patch("istota.process_group.kill_process_group",
                   side_effect=lambda pid, *a, **k: signalled.append(pid) or "group"):
            kill_group_if_live(_FakeProcess(returncode=-9))
        assert signalled == [], "signalled a pid that had already been reaped"

    def test_a_reaped_process_with_a_zero_exit_status_is_not_signalled(self):
        # `returncode == 0` is falsy and `if not process.returncode` would let
        # a cleanly-exited process through — the shape this guard is easiest to
        # write wrong.
        signalled = []
        with patch("istota.process_group.kill_process_group",
                   side_effect=lambda pid, *a, **k: signalled.append(pid) or "group"):
            kill_group_if_live(_FakeProcess(returncode=0))
        assert signalled == []

    def test_a_live_process_is_signalled(self):
        # The control. Without it, a guard that refused everything would pass
        # both tests above.
        signalled = []
        with patch("istota.process_group.kill_process_group",
                   side_effect=lambda pid, *a, **k: signalled.append(pid) or "group"):
            kill_group_if_live(_FakeProcess(returncode=None))
        assert signalled == [4242]

    def test_the_default_signal_is_sigkill(self):
        # All three converted call sites passed no signal and got SIGKILL from
        # `kill_process_group`'s own default. A different default here would be
        # a behaviour change hidden inside a refactor.
        sent = []
        with patch("istota.process_group.kill_process_group",
                   side_effect=lambda pid, sig="sentinel": sent.append(sig) or "group"):
            kill_group_if_live(_FakeProcess(returncode=None))
        assert sent == [signal.SIGKILL]

    def test_the_signal_is_configurable(self):
        sent = []
        with patch("istota.process_group.kill_process_group",
                   side_effect=lambda pid, sig=None: sent.append(sig) or "group"):
            kill_group_if_live(_FakeProcess(returncode=None), signal.SIGTERM)
        assert sent == [signal.SIGTERM]

    def test_it_never_raises_when_the_helper_below_it_fails(self):
        # Two of the three call sites are in a `finally` or a timer callback
        # with no `try` around them.
        with patch("istota.process_group._signal", side_effect=RuntimeError("boom")):
            kill_group_if_live(_FakeProcess(returncode=None))

    def test_a_real_process_group_dies(self):
        # End to end against a real child, so the guard cannot be green against
        # a mock while the actual signal path is broken. Asserted on the exit
        # status rather than on the pid disappearing: our own child stays a
        # zombie until `wait()`, so signal 0 still finds it.
        proc = subprocess.Popen(["sh", "-c", "sleep 30"], start_new_session=True)
        try:
            kill_group_if_live(proc)
            assert proc.wait(timeout=5) == -signal.SIGKILL
        finally:
            if proc.returncode is None:  # pragma: no cover - cleanup
                proc.kill()
                proc.wait(timeout=5)


class TestSchedulerRunCaptureKillsTheGroup:
    """The one converted call site nothing covered.

    A negative control run at this stage found that deleting the kill from
    `scheduler._run_capture` outright turned **zero** tests red — the timeout
    path that ISSUE-144's comment describes at length had no test at all, and
    it is also the site that gained the reaped-process guard in this change.
    Two tests, because the real risk runs both ways: the kill has to still
    happen, and it has to still take the grandchild with it.
    """

    def test_a_timeout_kills_the_backgrounded_grandchild(self, tmp_path):
        # The ISSUE-144 shape: a CRON `command:` backgrounds a child, the
        # command times out, and `subprocess.run`'s direct-child-only kill
        # leaves the grandchild holding the stdout pipe — which then wedges the
        # worker in the post-kill `communicate()` past its own timeout.
        from istota.scheduler import _run_capture

        pidfile = tmp_path / "grandchild.pid"
        script = f"sleep 30 & echo $! > {pidfile}; sleep 30"
        with pytest.raises(subprocess.TimeoutExpired):
            _run_capture(
                ["bash", "-c", script],
                timeout=1.0, cwd=str(tmp_path), env=dict(os.environ),
            )

        grandchild = int(pidfile.read_text().strip())
        assert _wait_gone(grandchild), (
            "the backgrounded grandchild survived the timeout kill"
        )

    def test_the_timeout_signals_the_live_child_through_the_guarded_helper(
        self, tmp_path,
    ):
        # The guard is a no-op on this path today (the child is unreaped when
        # `communicate` times out), so what this pins is that the call happens
        # at all and reaches the shared helper rather than a local copy.
        from istota.scheduler import _run_capture

        signalled = []
        with patch("istota.process_group.kill_process_group",
                   side_effect=lambda pid, *a, **k: signalled.append(pid) or "group"):
            with pytest.raises(subprocess.TimeoutExpired):
                # Short enough to exit on its own: the kill is patched out, so
                # `_run_capture`'s bounded post-kill `communicate` would
                # otherwise wait out its full 10s.
                _run_capture(
                    ["sleep", "2"],
                    timeout=0.5, cwd=str(tmp_path), env=dict(os.environ),
                )
        assert len(signalled) == 1, "the timeout did not signal the process group"


# =============================================================================
# ClaudeCodeBrain wiring
# =============================================================================


def _req(tmp_path: Path, **kw) -> BrainRequest:
    defaults = dict(
        prompt="hi",
        allowed_tools=["Bash"],
        cwd=tmp_path,
        env={},
        timeout_seconds=60,
        model="",
        effort="",
    )
    defaults.update(kw)
    return BrainRequest(**defaults)


def _mock_process(stdout_lines, returncode, pid=4242):
    mock = MagicMock()
    mock.stdout = iter(stdout_lines)
    mock.stderr = iter([])
    mock.pid = pid
    # Unreaped until wait(). The kill paths skip a reaped pid (its number can
    # have been handed to someone else), so a mock that is "already reaped" at
    # spawn would make every kill assertion below vacuously unreachable.
    mock.returncode = None

    def _wait(*_a, **_kw):
        mock.returncode = returncode
        return returncode

    mock.wait.side_effect = _wait
    return mock


def _tool_use_line() -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "uv run pytest"}},
        ]},
    }) + "\n"


class TestClaudeCodeBrainKillsTheGroup:
    def test_streaming_spawn_gets_its_own_session(self, tmp_path):
        proc = _mock_process([], returncode=0)
        with patch("istota.brain.claude_code.subprocess.Popen",
                   return_value=proc) as popen:
            ClaudeCodeBrain()._execute_streaming_once(["claude"], _req(tmp_path))

        assert popen.call_args.kwargs["start_new_session"] is True

    def test_cancel_kills_the_whole_group(self, tmp_path):
        proc = _mock_process([_tool_use_line()], returncode=-9)
        killed = []
        with patch("istota.brain.claude_code.subprocess.Popen", return_value=proc), \
             patch("istota.process_group.kill_process_group",
                   side_effect=lambda pid, *a, **k: killed.append(pid) or "group"):
            result = ClaudeCodeBrain()._execute_streaming_once(
                ["claude"], _req(tmp_path, cancel_check=lambda: True),
            )

        assert killed == [4242], "cancel signalled the CLI alone, not its group"
        assert result.stop_reason == "cancelled"
        proc.kill.assert_not_called()

    def test_timeout_kills_the_whole_group(self, tmp_path):
        # The timer fires while the reader thread is still blocked on stdout,
        # which is the real sequence: the CLI is mid-run with children alive.
        def _slow_stdout():
            time.sleep(0.4)
            return
            yield  # pragma: no cover - makes this a generator

        proc = _mock_process([], returncode=-9)
        proc.stdout = _slow_stdout()
        killed = []
        with patch("istota.brain.claude_code.subprocess.Popen", return_value=proc), \
             patch("istota.process_group.kill_process_group",
                   side_effect=lambda pid, *a, **k: killed.append(pid) or "group"):
            result = ClaudeCodeBrain()._execute_streaming_once(
                ["claude"], _req(tmp_path, timeout_seconds=0.05),
            )

        assert killed == [4242], "timeout signalled the CLI alone, not its group"
        assert result.stop_reason == "timeout"


    def test_a_reaped_process_is_not_signalled(self, tmp_path):
        # The timer can still fire during the two 5s thread joins that follow
        # `process.wait()`, by which point the pid has been reaped and the OS
        # may have handed the number to someone else — whose group we would
        # otherwise kill. `process.kill()` was safe here by construction
        # (Popen.send_signal no-ops on a reaped child); a raw pid is not, so
        # the kill paths check first. Same setup as the cancel test above, with
        # the process already reaped.
        proc = _mock_process([_tool_use_line()], returncode=-9)
        proc.returncode = -9  # reaped before the cancel poll runs
        killed = []
        with patch("istota.brain.claude_code.subprocess.Popen", return_value=proc), \
             patch("istota.process_group.kill_process_group",
                   side_effect=lambda pid, *a, **k: killed.append(pid) or "group"):
            ClaudeCodeBrain()._execute_streaming_once(
                ["claude"], _req(tmp_path, cancel_check=lambda: True),
            )

        assert killed == [], "signalled a pid that had already been reaped"


# =============================================================================
# The two user-facing cancel paths
# =============================================================================


class TestCancelEndpointsSignalTheGroup:
    @pytest.mark.asyncio
    async def test_stop_command_signals_the_group(self, tmp_path):
        from istota.commands import CommandContext, cmd_stop
        from istota.config import Config, SchedulerConfig, UserConfig

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        config = Config()
        config.db_path = db_path
        config.users = {"alice": UserConfig()}
        config.scheduler = SchedulerConfig()

        sent = []
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="long one", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.update_task_status(conn, task_id, "running")
            db.update_task_pid(conn, task_id, 4242)

            with patch("istota.commands.kill_process_group",
                       side_effect=lambda pid, sig: sent.append((pid, sig))):
                await cmd_stop(CommandContext(
                    config=config, conn=conn, user_id="alice",
                    conversation_token="room1", args="", surface="talk",
                ))

        assert sent == [(4242, signal.SIGTERM)]

    def test_web_cancel_signals_the_group(self, tmp_path):
        import istota.web_app as mod
        from istota.config import Config, UserConfig

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        config = Config()
        config.db_path = db_path
        config.users = {"alice": UserConfig()}

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="long one", user_id="alice",
                source_type="web", conversation_token="web-alice-1",
            )
            db.update_task_status(conn, task_id, "running")
            db.update_task_pid(conn, task_id, 4242)

        sent = []
        original = mod._config
        mod._config = config
        try:
            with patch("istota.web_app.kill_process_group",
                       side_effect=lambda pid, sig: sent.append((pid, sig))):
                mod._chat_cancel_task(task_id)
        finally:
            mod._config = original

        assert sent == [(4242, signal.SIGTERM)]

    def test_web_cancel_skips_a_task_that_already_finished(self, tmp_path):
        # `worker_pid` is cleared on every transition out of `running`, but a
        # cancel racing a task that just finished reads the pre-clear row. With
        # a group kill that mistake costs a whole group, so the status is
        # checked too — `cmd_stop` already selects on it, this path did not.
        import istota.web_app as mod
        from istota.config import Config, UserConfig

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        config = Config()
        config.db_path = db_path
        config.users = {"alice": UserConfig()}

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="done already", user_id="alice",
                source_type="web", conversation_token="web-alice-1",
            )
            db.update_task_status(conn, task_id, "running")
            db.update_task_pid(conn, task_id, 4242)
            # Simulate the race: the row still names a pid, but it has moved on.
            conn.execute(
                "UPDATE tasks SET status = 'completed' WHERE id = ?", (task_id,)
            )
            conn.commit()

        sent = []
        original = mod._config
        mod._config = config
        try:
            with patch("istota.web_app.kill_process_group",
                       side_effect=lambda pid, sig: sent.append((pid, sig))):
                mod._chat_cancel_task(task_id)
        finally:
            mod._config = original

        assert sent == [], "signalled the group of a task that had finished"
