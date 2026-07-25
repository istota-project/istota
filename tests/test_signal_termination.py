"""Signal-death classification and shutdown-aware recovery (ISSUE-191).

A `claude` CLI subprocess killed by a signal other than SIGKILL used to fall
through to the generic stream-parse catch-all ("Stream parsing failed
(rc=-15, N lines)") and be retried — or, on the last attempt, fail permanently
— with no record of what happened. The common cause is systemd's default
`KillMode=control-group`: `systemctl restart` SIGTERMs every process in the
cgroup, including an in-flight task's subprocess, while the daemon's own
handler shuts down gracefully and records the corpse as an ordinary failure.

Covers three layers:
  * the brain classifies any signal death (SIGKILL keeps its OOM wording),
  * the DB clears `worker_pid` on every transition out of `running`,
  * the scheduler releases a signal-killed task instead of failing it when the
    daemon is shutting down.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota import db, scheduler
from istota.brain import BrainRequest
from istota.brain.claude_code import ClaudeCodeBrain, is_signal_termination


def _req(tmp_path: Path, **kw) -> BrainRequest:
    defaults = dict(
        prompt="hi",
        allowed_tools=["Read"],
        cwd=tmp_path,
        env={},
        timeout_seconds=60,
        model="",
        effort="",
    )
    defaults.update(kw)
    return BrainRequest(**defaults)


def _mock_process(stdout_lines, returncode, stderr_lines=None):
    mock = MagicMock()
    mock.stdout = iter(stdout_lines)
    mock.stderr = iter(stderr_lines or [])
    mock.returncode = returncode
    mock.wait.return_value = returncode
    return mock


class TestBrainSignalClassification:
    """ClaudeCodeBrain labels a signal death by name instead of by symptom."""

    def test_streaming_sigterm_is_classified(self, tmp_path):
        lines = [json.dumps({"type": "system", "subtype": "init"}) + "\n"]
        proc = _mock_process(lines, returncode=-15)
        with patch("istota.brain.claude_code.subprocess.Popen", return_value=proc):
            result = ClaudeCodeBrain()._execute_streaming_once(["claude"], _req(tmp_path))

        assert result.success is False
        assert result.stop_reason == "terminated"
        assert "SIGTERM" in result.result_text
        assert "Stream parsing failed" not in result.result_text

    def test_streaming_sigterm_with_no_output_is_still_classified(self, tmp_path):
        # A process killed before emitting a single frame took the
        # "produced no output (rc=-15)" branch, which reads like a CLI bug.
        proc = _mock_process([], returncode=-15)
        with patch("istota.brain.claude_code.subprocess.Popen", return_value=proc):
            result = ClaudeCodeBrain()._execute_streaming_once(["claude"], _req(tmp_path))

        assert result.stop_reason == "terminated"
        assert "SIGTERM" in result.result_text

    def test_streaming_sigkill_keeps_oom_wording(self, tmp_path):
        # SIGKILL is the OOM killer's / systemd-oomd's signature; the
        # established message and stop_reason must not shift under it.
        proc = _mock_process([], returncode=-9)
        with patch("istota.brain.claude_code.subprocess.Popen", return_value=proc):
            result = ClaudeCodeBrain()._execute_streaming_once(["claude"], _req(tmp_path))

        assert result.stop_reason == "oom"
        assert "out of memory" in result.result_text

    def test_streaming_signal_death_keeps_execution_trace(self, tmp_path):
        # The tools that ran before the kill are the only diagnostic we get
        # (ISSUE-183) — a signal death must not drop them.
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash",
                     "input": {"command": "echo hi"}},
                ]},
            }) + "\n",
        ]
        proc = _mock_process(lines, returncode=-15)
        with patch("istota.brain.claude_code.subprocess.Popen", return_value=proc):
            result = ClaudeCodeBrain()._execute_streaming_once(["claude"], _req(tmp_path))

        assert result.execution_trace is not None
        assert "Bash" in result.execution_trace or "echo hi" in result.execution_trace

    def test_simple_path_sigterm_is_classified(self, tmp_path):
        completed = MagicMock()
        completed.returncode = -15
        completed.stdout = ""
        completed.stderr = ""
        with patch("istota.brain.claude_code.subprocess.run", return_value=completed):
            result = ClaudeCodeBrain()._execute_simple_once(["claude"], _req(tmp_path))

        assert result.stop_reason == "terminated"
        assert "SIGTERM" in result.result_text

    def test_unusual_signal_is_named_by_number_at_least(self, tmp_path):
        proc = _mock_process([], returncode=-6)  # SIGABRT
        with patch("istota.brain.claude_code.subprocess.Popen", return_value=proc):
            result = ClaudeCodeBrain()._execute_streaming_once(["claude"], _req(tmp_path))

        assert result.stop_reason == "terminated"
        assert "6" in result.result_text

    def test_cancellation_still_wins_over_signal_classification(self, tmp_path):
        # !stop SIGTERMs the subprocess; that is a cancellation, not a
        # mystery termination, and must keep reporting as one.
        proc = _mock_process([], returncode=-15)
        with patch("istota.brain.claude_code.subprocess.Popen", return_value=proc):
            result = ClaudeCodeBrain()._execute_streaming_once(
                ["claude"], _req(tmp_path, cancel_check=lambda: True),
            )

        assert result.stop_reason == "cancelled"


class TestIsSignalTermination:
    def test_matches_the_brain_message(self):
        assert is_signal_termination("Claude Code was terminated by SIGTERM (signal 15)")

    def test_does_not_match_oom_or_ordinary_errors(self):
        assert not is_signal_termination("Claude Code was killed (likely out of memory)")
        assert not is_signal_termination("Stream parsing failed (rc=1, 12 lines)")
        assert not is_signal_termination("")


class TestWorkerPidLifecycle:
    """A dead attempt's PID must not stay on the row — `!stop` and the web
    cancel endpoint both `os.kill` whatever `worker_pid` holds, so a stale
    one can SIGTERM an unrelated process once the OS recycles the number."""

    @pytest.fixture
    def conn(self, tmp_path):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        with db.get_db(db_path) as c:
            yield c

    def _task_with_pid(self, conn) -> int:
        task_id = db.create_task(conn, prompt="p", user_id="u")
        db.update_task_pid(conn, task_id, 4242)
        return task_id

    def _row(self, conn, task_id):
        # worker_pid / locked_by / last_heartbeat are columns, not Task fields.
        return conn.execute(
            "SELECT worker_pid, locked_by, last_heartbeat, started_at "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()

    def test_pending_retry_clears_worker_pid(self, conn):
        task_id = self._task_with_pid(conn)
        db.set_task_pending_retry(conn, task_id, "boom", 1)
        assert self._row(conn, task_id)["worker_pid"] is None

    def test_terminal_status_clears_worker_pid(self, conn):
        for status in ("completed", "failed", "cancelled"):
            task_id = self._task_with_pid(conn)
            db.update_task_status(conn, task_id, status)
            assert self._row(conn, task_id)["worker_pid"] is None, status

    def test_release_for_restart_resets_liveness_without_burning_an_attempt(self, conn):
        task_id = self._task_with_pid(conn)
        db.update_task_status(conn, task_id, "running")
        before = db.get_task(conn, task_id).attempt_count

        db.release_task_for_restart(conn, task_id, "terminated during shutdown")

        task = db.get_task(conn, task_id)
        row = self._row(conn, task_id)
        assert task.status == "pending"
        assert row["worker_pid"] is None
        assert row["locked_by"] is None
        assert row["last_heartbeat"] is None
        assert row["started_at"] is None
        assert task.attempt_count == before


class TestShutdownReleasesSignalKilledTasks:
    """A task whose subprocess died from the daemon's own shutdown signal is
    infrastructure collateral, not a task failure — it goes back on the queue."""

    @pytest.fixture(autouse=True)
    def _clear_shutdown_flag(self):
        scheduler._shutdown_requested = False
        yield
        scheduler._shutdown_requested = False

    @pytest.fixture
    def config(self, tmp_path):
        from istota.config import Config
        cfg = Config()
        cfg.db_path = tmp_path / "istota.db"
        cfg.temp_dir = tmp_path / "temp"
        cfg.temp_dir.mkdir(parents=True, exist_ok=True)
        cfg.nextcloud_mount_path = tmp_path / "mount"
        cfg.talk.enabled = False
        db.init_db(cfg.db_path)
        return cfg

    _TERMINATED = "Claude Code was terminated by SIGTERM (signal 15)"

    def _run(self, config, error_text, source_type="talk"):
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="p", user_id="u", source_type=source_type,
            )
        with patch("istota.scheduler.execute_task",
                   return_value=(False, error_text, None, None)), \
             patch("istota.scheduler.asyncio.run", return_value=None):
            scheduler.process_one_task(config)
        with db.get_db(config.db_path) as conn:
            return db.get_task(conn, task_id)

    def test_released_when_shutting_down(self, config):
        scheduler._shutdown_requested = True
        task = self._run(config, self._TERMINATED)
        assert task.status == "pending"
        assert task.attempt_count == 0  # the aborted attempt isn't charged
        assert task.scheduled_for is None  # no backoff — retry as soon as we're back

    def test_last_attempt_is_still_released_when_shutting_down(self, config):
        # The whole point: the task that prompted this died on its terminal
        # attempt and was marked permanently failed for an event that had
        # nothing to do with it.
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="p", user_id="u", source_type="talk")
            conn.execute(
                "UPDATE tasks SET attempt_count = 2, max_attempts = 3 WHERE id = ?",
                (task_id,),
            )
        scheduler._shutdown_requested = True
        with patch("istota.scheduler.execute_task",
                   return_value=(False, self._TERMINATED, None, None)), \
             patch("istota.scheduler.asyncio.run", return_value=None):
            scheduler.process_one_task(config)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "pending"
        assert task.attempt_count == 2

    def test_signal_death_outside_shutdown_takes_the_normal_retry_path(self, config):
        task = self._run(config, self._TERMINATED)
        assert task.status == "pending"
        assert task.attempt_count == 1  # charged, and backoff applies
        assert task.scheduled_for is not None

    def test_ordinary_failure_during_shutdown_is_untouched(self, config):
        scheduler._shutdown_requested = True
        task = self._run(config, "The model refused to answer")
        assert task.attempt_count == 1
        assert task.scheduled_for is not None
