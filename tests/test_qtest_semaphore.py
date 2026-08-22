"""Tests for scripts/qtest, the machine-wide test-run semaphore.

Several worktrees running `pytest -n auto` at once ask for `cpu_count()`
workers each, which oversubscribes the machine and turns timeouts into
failures that have nothing to do with the code. qtest is the mutex that
stops that.

Its failure modes are the reason these tests are careful: a lock that never
releases wedges every worktree on the machine, and a lock that never holds
looks exactly like a working one until the day it matters. So mutual
exclusion and release-on-crash both get a positive control, and the
multi-slot case gets one too -- otherwise a qtest that simply ran everything
serially for the wrong reason would pass.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
QTEST = REPO_ROOT / "scripts" / "qtest"

# Exit codes qtest promises to its callers.
EXIT_USAGE = 2
EXIT_NO_SLOT = 75  # EX_TEMPFAIL


def run_qtest(*args, lock_dir, env=None, timeout=30, **kwargs):
    """Invoke qtest with an isolated lock dir so tests never contend."""
    child_env = {**os.environ, "QTEST_LOCK_DIR": str(lock_dir)}
    if env:
        child_env.update(env)
    return subprocess.run(
        [sys.executable, str(QTEST), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=child_env,
        **kwargs,
    )


def popen_qtest(*args, lock_dir, env=None):
    child_env = {**os.environ, "QTEST_LOCK_DIR": str(lock_dir)}
    if env:
        child_env.update(env)
    return subprocess.Popen(
        [sys.executable, str(QTEST), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_env,
    )


@pytest.fixture
def lock_dir(tmp_path):
    d = tmp_path / "locks"
    d.mkdir()
    return d


@pytest.fixture
def hold_slot(lock_dir, tmp_path):
    """Start a qtest that occupies a slot, and block until it really has one.

    Racing a bare `popen_qtest("sleep", ...)` against the run under test is the
    obvious way to write these and it is wrong: both sides pay Python's startup
    cost, so the "blocked" run sometimes wins the slot and the assertion fails
    for reasons that have nothing to do with locking. The held command touches
    a marker as its first act, which gives us something to wait on.
    """
    started = []

    def start(name="holder"):
        marker = tmp_path / f"{name}.held"
        proc = popen_qtest(
            "sh", "-c", f'touch "{marker}"; sleep 30', lock_dir=lock_dir
        )
        started.append(proc)
        deadline = time.monotonic() + 30
        while not marker.exists():
            assert proc.poll() is None, "slot holder exited before taking a slot"
            assert time.monotonic() < deadline, "slot holder never started"
            time.sleep(0.02)
        return proc

    yield start

    for proc in started:
        proc.kill()
        proc.wait(timeout=10)


class TestScriptIsWired:
    def test_script_exists_and_is_executable(self):
        assert QTEST.exists(), "scripts/qtest must exist"
        assert os.access(QTEST, os.X_OK), "qtest must be executable to be run directly"

    def test_agents_md_points_at_it(self):
        """The convention only propagates because AGENTS.md is loaded everywhere."""
        agents = (REPO_ROOT / "AGENTS.md").read_text()
        assert "scripts/qtest" in agents


class TestCommandPassthrough:
    def test_runs_the_command_and_returns_its_output(self, lock_dir):
        result = run_qtest("echo", "hello", lock_dir=lock_dir)
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_propagates_a_nonzero_exit_code(self, lock_dir):
        result = run_qtest("sh", "-c", "exit 17", lock_dir=lock_dir)
        assert result.returncode == 17

    def test_passes_flag_like_arguments_through_untouched(self, lock_dir):
        """qtest takes no flags of its own, so `-n auto` belongs to the child."""
        result = run_qtest(
            "sh",
            "-c",
            'printf "%s|" "$@"',
            "sh",
            "-n",
            "auto",
            "--timeout",
            "5",
            lock_dir=lock_dir,
        )
        assert result.returncode == 0
        assert result.stdout == "-n|auto|--timeout|5|"

    def test_no_command_is_a_usage_error(self, lock_dir):
        result = run_qtest(lock_dir=lock_dir)
        assert result.returncode == EXIT_USAGE
        assert "usage" in result.stderr.lower()

    def test_quiet_when_the_slot_is_free(self, lock_dir):
        """The common case is one run at a time. The verdict is the only thing
        qtest is allowed to say then -- no queueing notice, no progress."""
        result = run_qtest("true", lock_dir=lock_dir)
        lines = result.stderr.splitlines()
        assert len(lines) == 1, f"qtest added noise: {lines}"
        assert lines[0].startswith("qtest: PASS exit=0 ")


class TestVerdict:
    """Every run ends with one line naming the outcome.

    The exit code is correct already, and that has not been enough: these runs
    are invoked as `scripts/qtest uv run pytest | tail -40`, where the shell
    reports *tail's* status and a failed suite reads as exit 0. The verdict is
    the answer in a form a filter cannot drop.
    """

    def last_line(self, stderr):
        lines = [line for line in stderr.splitlines() if line.strip()]
        assert lines, "qtest said nothing"
        return lines[-1]

    def test_a_pass_is_named(self, lock_dir):
        result = run_qtest("true", lock_dir=lock_dir)
        assert result.returncode == 0
        assert self.last_line(result.stderr).startswith("qtest: PASS exit=0 ")

    def test_a_failure_is_named_with_its_own_code(self, lock_dir):
        result = run_qtest("sh", "-c", "exit 3", lock_dir=lock_dir)
        assert result.returncode == 3
        assert self.last_line(result.stderr).startswith("qtest: FAIL exit=3 ")

    def test_the_verdict_names_the_command_it_ran(self, lock_dir):
        """A scrollback with several runs in it needs to say which one failed."""
        result = run_qtest("sh", "-c", "exit 1", lock_dir=lock_dir)
        assert "cmd: sh -c 'exit 1'" in self.last_line(result.stderr)

    def test_a_failure_warns_that_a_pipeline_hides_the_code(self, lock_dir):
        """The line above the verdict explains why the verdict exists."""
        failed = run_qtest("false", lock_dir=lock_dir)
        assert "pipeline" in failed.stderr
        passed = run_qtest("true", lock_dir=lock_dir)
        assert "pipeline" not in passed.stderr, "no warning on a run that passed"

    def test_the_verdict_survives_a_pipe_that_swallows_the_exit_code(self, lock_dir):
        """The motivating case, end to end.

        `qtest ... | tail -n 1` genuinely exits 0 on a failed suite -- that is
        the shell, not something qtest can fix. What it can do is put the
        answer somewhere the pipe does not reach.
        """
        pipeline = subprocess.run(
            ["sh", "-c", f'"{sys.executable}" "{QTEST}" sh -c "exit 4" | tail -n 1'],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "QTEST_LOCK_DIR": str(lock_dir)},
        )
        assert pipeline.returncode == 0, "the pipe should report tail's status"
        assert "qtest: FAIL exit=4" in pipeline.stderr

    def test_the_verdict_stays_off_stdout(self, lock_dir):
        """stdout is the command's, verbatim. Anything qtest adds there would
        land in a caller's pipeline, log file or parsed output."""
        result = run_qtest("printf", "%s", "payload", lock_dir=lock_dir)
        assert result.stdout == "payload"
        assert "qtest:" not in result.stdout

    def test_a_signal_death_is_named_as_such(self, lock_dir):
        """143 is not a test failure, and reading it as one sends you to the
        wrong place. The OOM killer and a hung suite look like this."""
        result = run_qtest("sh", "-c", "kill -TERM $$", lock_dir=lock_dir)
        assert result.returncode == 128 + signal.SIGTERM
        assert self.last_line(result.stderr).startswith(
            f"qtest: KILLED-SIGTERM exit={128 + signal.SIGTERM} "
        )

    def test_a_usage_error_is_not_a_failed_command(self, lock_dir):
        result = run_qtest(lock_dir=lock_dir)
        assert result.returncode == EXIT_USAGE
        assert self.last_line(result.stderr) == f"qtest: ERROR exit={EXIT_USAGE}"

    def test_a_missing_command_is_not_a_failed_command(self, lock_dir):
        result = run_qtest("no-such-binary-xyzzy", lock_dir=lock_dir)
        assert result.returncode == 127
        assert self.last_line(result.stderr) == "qtest: ERROR exit=127"


class TestMutualExclusion:
    def _nesting_probe(self, log: Path) -> list[str]:
        """A command that brackets its own run in a shared log file."""
        return ["sh", "-c", f'echo start >> "{log}"; sleep 0.4; echo end >> "{log}"']

    def test_one_slot_serializes_concurrent_runs(self, lock_dir, tmp_path):
        log = tmp_path / "nesting.log"
        probe = self._nesting_probe(log)
        procs = [popen_qtest(*probe, lock_dir=lock_dir) for _ in range(3)]
        for p in procs:
            assert p.wait(timeout=60) == 0

        # Well-nested means never two starts in a row: no run began before its
        # predecessor finished.
        events = log.read_text().split()
        assert events == ["start", "end"] * 3, f"runs overlapped: {events}"

    def test_announces_the_wait_up_front_then_reports_it(self, lock_dir, hold_slot):
        """Blocking on a sleep here would be a flake: on a loaded machine the
        waiter may not have reached main() yet, so `poll() is None` proves
        nothing. Reading the announcement is a real barrier."""
        holder = hold_slot()
        waiter = popen_qtest("true", lock_dir=lock_dir)

        queued = waiter.stderr.readline()
        assert "queueing" in queued, f"expected a queueing notice, got {queued!r}"

        holder.kill()
        _, err = waiter.communicate(timeout=60)
        assert waiter.returncode == 0
        assert "waiting" in err.lower()

    def test_two_slots_let_two_runs_overlap(self, lock_dir, tmp_path):
        """Without this, a qtest that serialized everything would still pass."""
        rendezvous = tmp_path / "rv"
        rendezvous.mkdir()
        # Each child announces itself, then waits up to 10s to see its peer.
        # Both can only succeed if both hold a slot at the same time.
        script = textwrap.dedent(
            f"""
            touch "{rendezvous}/$1"
            i=0
            while [ $i -lt 100 ]; do
                if [ "$(ls "{rendezvous}" | wc -l)" -ge 2 ]; then exit 0; fi
                sleep 0.1
                i=$((i + 1))
            done
            exit 1
            """
        )
        procs = [
            popen_qtest(
                "sh",
                "-c",
                script,
                "sh",
                name,
                lock_dir=lock_dir,
                env={"QTEST_SLOTS": "2"},
            )
            for name in ("a", "b")
        ]
        assert [p.wait(timeout=60) for p in procs] == [0, 0]


class TestSlotExhaustion:
    def test_gives_up_with_a_distinct_code_after_the_timeout(self, lock_dir, hold_slot):
        hold_slot()
        result = run_qtest("true", lock_dir=lock_dir, env={"QTEST_TIMEOUT": "1"})
        assert result.returncode == EXIT_NO_SLOT
        assert "timed out" in result.stderr.lower()

    def test_a_missed_slot_is_not_reported_as_a_failed_run(self, lock_dir, hold_slot):
        """75 means the command never ran, so a reader who sees FAIL here would
        go looking for a test that broke. The verdict has to say which it is."""
        hold_slot()
        result = run_qtest("true", lock_dir=lock_dir, env={"QTEST_TIMEOUT": "1"})
        assert result.stderr.strip().splitlines()[-1] == (
            f"qtest: NO-SLOT exit={EXIT_NO_SLOT} command not run"
        )
        assert "FAIL" not in result.stderr

    def test_does_not_run_the_command_when_it_cannot_get_a_slot(
        self, lock_dir, hold_slot, tmp_path
    ):
        """Giving up must mean *not running*, not running unlocked."""
        marker = tmp_path / "ran"
        hold_slot()
        result = run_qtest(
            "touch", str(marker), lock_dir=lock_dir, env={"QTEST_TIMEOUT": "1"}
        )
        # Assert the code too: without it, a qtest that failed to start at all
        # would leave the marker absent and pass.
        assert result.returncode == EXIT_NO_SLOT
        assert not marker.exists()

    def test_rejects_a_nonsense_slot_count(self, lock_dir):
        result = run_qtest("true", lock_dir=lock_dir, env={"QTEST_SLOTS": "0"})
        assert result.returncode == EXIT_USAGE
        # Name the offending setting, so an unrelated exit-2 can't pass this.
        assert "QTEST_SLOTS" in result.stderr


class TestLockRelease:
    """A lock that outlives its holder wedges every worktree on the machine."""

    def test_slot_is_released_when_the_holder_is_killed(self, lock_dir, hold_slot):
        holder = hold_slot()
        blocked = run_qtest("true", lock_dir=lock_dir, env={"QTEST_TIMEOUT": "1"})
        assert blocked.returncode == EXIT_NO_SLOT, "holder never took the slot"

        holder.kill()
        holder.wait(timeout=10)

        result = run_qtest("true", lock_dir=lock_dir, env={"QTEST_TIMEOUT": "10"})
        assert result.returncode == 0, "slot was not released when the holder died"

    def test_slot_is_released_when_the_command_fails(self, lock_dir):
        assert run_qtest("false", lock_dir=lock_dir).returncode == 1
        result = run_qtest("true", lock_dir=lock_dir, env={"QTEST_TIMEOUT": "10"})
        assert result.returncode == 0


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestTerminationTakesTheWholeTree:
    """Interrupting a run must stop the workers, not just the wrapper.

    `qtest uv run pytest` is four levels deep: qtest -> uv -> pytest -> N xdist
    workers. Signalling only the direct child leaves the workers orphaned,
    still holding every core, while qtest exits and hands the slot to the next
    queued suite -- which then runs on a machine that is still fully loaded.
    That is worse than no semaphore at all.
    """

    def _run_with_grandchild(self, lock_dir, tmp_path):
        pidfile = tmp_path / "grandchild.pid"
        # `sh` backgrounds a sleep and waits on it, so the sleep is a
        # grandchild of qtest -- structurally an xdist worker.
        proc = popen_qtest(
            "sh",
            "-c",
            f'sleep 120 & echo $! > "{pidfile}"; wait',
            lock_dir=lock_dir,
        )
        deadline = time.monotonic() + 30
        while not pidfile.exists() or not pidfile.read_text().strip():
            assert proc.poll() is None, "command exited before spawning a grandchild"
            assert time.monotonic() < deadline, "grandchild never started"
            time.sleep(0.02)
        grandchild = int(pidfile.read_text().strip())
        assert pid_alive(grandchild)
        return proc, grandchild

    def _assert_reaped(self, grandchild):
        deadline = time.monotonic() + 15
        while pid_alive(grandchild):
            if time.monotonic() > deadline:
                os.kill(grandchild, signal.SIGKILL)  # don't leak it into the suite
                raise AssertionError(
                    "grandchild outlived qtest: the worker pool would keep "
                    "running while the slot was handed to the next run"
                )
            time.sleep(0.05)

    def test_sigterm_reaps_grandchildren(self, lock_dir, tmp_path):
        proc, grandchild = self._run_with_grandchild(lock_dir, tmp_path)
        proc.terminate()
        proc.wait(timeout=30)
        self._assert_reaped(grandchild)

    def test_sigint_reaps_grandchildren(self, lock_dir, tmp_path):
        """Ctrl-C is the common case, and it must behave the same as SIGTERM."""
        proc, grandchild = self._run_with_grandchild(lock_dir, tmp_path)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=30)
        self._assert_reaped(grandchild)

    def test_reports_a_signal_death_as_128_plus_n(self, lock_dir):
        """Popen reports a signal death as a negative number; a caller reading
        `$?` expects the shell's convention, and -15 would come out as 241."""
        result = run_qtest("sh", "-c", "kill -TERM $$", lock_dir=lock_dir)
        assert result.returncode == 128 + signal.SIGTERM

    def test_the_command_does_not_share_qtest_own_process_group(
        self, lock_dir, tmp_path
    ):
        """The mechanism behind the two tests above, asserted directly.

        A child in its own group can be signalled as a unit. A child sharing
        qtest's group cannot be, without also signalling qtest and whatever
        else the caller's shell put in that group.
        """
        result = run_qtest("sh", "-c", "echo $$; ps -o pgid= -p $$", lock_dir=lock_dir)
        assert result.returncode == 0
        child_pid, child_pgid = (line.strip() for line in result.stdout.split())
        assert child_pid == child_pgid, "command should lead its own process group"
        assert int(child_pgid) != os.getpgid(0)


class TestEscapeHatch:
    def test_disable_runs_without_taking_a_slot(self, lock_dir, hold_slot):
        hold_slot()
        result = run_qtest(
            "echo",
            "ran",
            lock_dir=lock_dir,
            env={"QTEST_DISABLE": "1", "QTEST_TIMEOUT": "1"},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "ran"
