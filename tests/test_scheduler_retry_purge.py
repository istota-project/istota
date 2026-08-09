"""A re-attempt never replays the previous attempt's deferred ops.

ISSUE-074 cleared the slate in ``process_one_task``'s own retry branch, but
three other paths requeue a task under the same ``task.id`` with
``attempt_count + 1`` and no purge: ``db.fail_stuck_locked_running_tasks``
(the periodic reclaim), ``db.recover_orphaned_tasks`` (startup recovery), and
``db.claim_task``'s own inline copy of the stuck-running release. A file left
behind by attempt 1 then drains on attempt 2's success — duplicate subtasks,
duplicate ``sent_emails`` rows, replayed non-idempotent KG deletes.

The backstop is at the *start of a re-attempt* rather than at each requeue
site, because ``claim_task`` releases stuck rows inside the same statement
batch that claims the next one and offers no scheduler-side hook at all.
"""

import json
from unittest.mock import patch

from istota import db
from istota.config import Config, EmailConfig, SchedulerConfig, TalkConfig
from istota.executor import get_user_temp_dir
from istota.scheduler import process_one_task


def _config(db_path, tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    return Config(
        db_path=db_path,
        talk=TalkConfig(enabled=False),
        email=EmailConfig(enabled=False),
        scheduler=SchedulerConfig(),
        nextcloud_mount_path=mount,
        temp_dir=tmp_path / "temp",
    )


def _write_stale_subtasks(config, user_id, task_id, prompt):
    temp_dir = get_user_temp_dir(config, user_id)
    temp_dir.mkdir(parents=True, exist_ok=True)
    path = temp_dir / f"task_{task_id}_subtasks.json"
    path.write_text(json.dumps([{"prompt": prompt}]), encoding="utf-8")
    return path


def _subtask_prompts(db_path):
    with db.get_db(db_path) as conn:
        return [
            t.prompt for t in db.list_tasks(conn, limit=50)
            if t.source_type == "subtask"
        ]


def _bump_attempt(db_path, task_id):
    """Requeue as the reclaim paths do: pending, attempt charged."""
    with db.get_db(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status='pending', attempt_count = attempt_count + 1, "
            "locked_at=NULL, locked_by=NULL, started_at=NULL, last_heartbeat=NULL "
            "WHERE id = ?",
            (task_id,),
        )


class TestReattemptPurgesPriorDeferredOps:
    @patch("istota.scheduler.asyncio.run", return_value=None)
    @patch("istota.scheduler.execute_task", return_value=(True, "done", None, None))
    def test_requeued_attempt_drops_the_previous_attempt_files(
        self, mock_exec, mock_arun, db_path, tmp_path,
    ):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="parent", user_id="alice", source_type="cli",
            )
        stale = _write_stale_subtasks(config, "alice", task_id, "orphaned child")
        _bump_attempt(db_path, task_id)

        result = process_one_task(config)

        assert result == (task_id, True)
        assert not stale.exists()
        assert _subtask_prompts(db_path) == []

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_current_attempt_writes_still_drain(
        self, mock_arun, db_path, tmp_path,
    ):
        """The purge runs before execution, so it must not eat what *this*
        attempt writes. Without that ordering the fix would trade a duplicate
        subtask for a lost one.
        """
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="parent", user_id="alice", source_type="cli",
            )
        _write_stale_subtasks(config, "alice", task_id, "orphaned child")
        _bump_attempt(db_path, task_id)

        def fake_exec(task, cfg, user_resources, **kw):
            _write_stale_subtasks(cfg, task.user_id, task.id, "fresh child")
            return (True, "done", None, None)

        with patch("istota.scheduler.execute_task", side_effect=fake_exec):
            process_one_task(config)

        assert _subtask_prompts(db_path) == ["fresh child"]

    @patch("istota.scheduler.asyncio.run", return_value=None)
    @patch("istota.scheduler.execute_task", return_value=(True, "done", None, None))
    def test_first_attempt_is_untouched(
        self, mock_exec, mock_arun, db_path, tmp_path,
    ):
        """A fresh task has no prior attempt, so nothing is purged — and the
        confirmation re-run (same id, attempt_count still 0) keeps relying on
        the narrower stale-``email_output`` cleanup that already exists.
        """
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="parent", user_id="alice", source_type="cli",
            )
        held = _write_stale_subtasks(config, "alice", task_id, "held child")

        process_one_task(config)

        assert not held.exists(), "drained, not purged"
        assert _subtask_prompts(db_path) == ["held child"]


class TestRequeuePathsReachTheBackstop:
    """The three requeue paths the backstop exists for, driven end to end."""

    @patch("istota.scheduler.asyncio.run", return_value=None)
    @patch("istota.scheduler.execute_task", return_value=(True, "done", None, None))
    def test_stuck_running_reclaim(self, mock_exec, mock_arun, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="parent", user_id="alice", source_type="cli",
            )
            conn.execute(
                "UPDATE tasks SET status='running', "
                "started_at=datetime('now','-120 minutes'), "
                "last_heartbeat=datetime('now','-120 minutes') WHERE id = ?",
                (task_id,),
            )
        stale = _write_stale_subtasks(config, "alice", task_id, "orphaned child")

        with db.get_db(db_path) as conn:
            db.fail_stuck_locked_running_tasks(
                conn, max_retry_age_minutes=6000,
                stuck_running_minutes=15, heartbeat_stuck_minutes=5,
            )
            requeued = db.get_task(conn, task_id)
        assert requeued.status == "pending" and requeued.attempt_count == 1

        process_one_task(config)

        assert not stale.exists()
        assert _subtask_prompts(db_path) == []

    @patch("istota.scheduler.asyncio.run", return_value=None)
    @patch("istota.scheduler.execute_task", return_value=(True, "done", None, None))
    def test_startup_orphan_recovery(self, mock_exec, mock_arun, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="parent", user_id="alice", source_type="cli",
            )
            conn.execute(
                "UPDATE tasks SET status='running' WHERE id = ?", (task_id,),
            )
        stale = _write_stale_subtasks(config, "alice", task_id, "orphaned child")

        with db.get_db(db_path) as conn:
            recovered = db.recover_orphaned_tasks(conn, max_retry_age_minutes=6000)
        assert [r["action"] for r in recovered] == ["released"]

        process_one_task(config)

        assert not stale.exists()
        assert _subtask_prompts(db_path) == []
