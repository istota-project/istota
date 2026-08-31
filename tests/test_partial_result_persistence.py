"""The partial answer's journey out of the brain and onto the row (ISSUE-372).

`BrainResult.partial_text` is only worth having if something durable reads it.
Three hops, each a place the work was previously dropped: the executor puts it
on the task, the scheduler passes it to `update_task_status`, and that function
writes it into the `result` column it had been accepting and discarding.

The reason it travels this way rather than inside `result_text` is the property
these tests pin hardest: `result == "Cancelled by user"` is an exact-equality
match in three places in the scheduler, and a cancelled task that stopped
matching would be sent back through the retry ladder.
"""

import json
from unittest.mock import patch

from istota import db
from istota.brain._types import BrainResult
from istota.executor import execute_task
from istota.scheduler import PARTIAL_WORK_MARKER, process_one_task

from tests.test_executor_final_answer import _FakeBrain
from tests.test_executor_streaming import (
    _make_config,
    _make_task,
    _patch_executor,
    contextmanager_chain,
)


def _run_executor(tmp_path, brain_result):
    config = _make_config(tmp_path)
    config.security.sandbox_enabled = False
    task = _make_task(source_type="talk")
    patches = _patch_executor() + [
        patch("istota.executor.make_brain", return_value=_FakeBrain(brain_result)),
    ]
    with contextmanager_chain(patches):
        return task, execute_task(task, config, [])


class TestTheExecutorHandsItOn:
    def test_a_cancelled_run_leaves_its_prose_on_the_task(self, tmp_path):
        task, (success, result, _a, _t) = _run_executor(
            tmp_path,
            BrainResult(
                False, "Cancelled by user", stop_reason="cancelled",
                partial_text="I traced it to the poller's cursor.",
            ),
        )
        assert success is False
        # Untouched — three scheduler matches depend on it.
        assert result == "Cancelled by user"
        assert task.partial_result == "I traced it to the poller's cursor."

    def test_a_timed_out_run_leaves_its_prose_on_the_task(self, tmp_path):
        task, (success, _r, _a, _t) = _run_executor(
            tmp_path,
            BrainResult(
                False, "Task execution timed out after 60 minutes",
                stop_reason="timeout", partial_text="Halfway through the audit.",
            ),
        )
        assert success is False
        assert task.partial_result == "Halfway through the audit."

    def test_a_successful_run_sets_nothing(self, tmp_path):
        """The answer is `result`; a second candidate for one column is a bug."""
        task, (success, _r, _a, _t) = _run_executor(
            tmp_path,
            BrainResult(
                True, "Done: the answer is 42.", stop_reason="completed",
                partial_text="mid-flight narration",
            ),
        )
        assert success is True
        assert task.partial_result is None

    def test_a_failure_with_no_partial_text_sets_nothing(self, tmp_path):
        task, _ = _run_executor(
            tmp_path, BrainResult(False, "Cancelled by user", stop_reason="cancelled"),
        )
        assert task.partial_result is None


class TestTheColumnIsWritten:
    """`update_task_status` took a `result` and dropped it on these branches."""

    def test_cancelled_writes_the_result_column(self, tmp_path):
        path = tmp_path / "t.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            task_id = db.create_task(conn, prompt="p", user_id="u", source_type="cli")
            db.update_task_status(
                conn, task_id, "cancelled",
                result="what I had", error="Cancelled by user",
            )
            task = db.get_task(conn, task_id)
        assert task.status == "cancelled"
        assert task.result == "what I had"
        assert task.error == "Cancelled by user"

    def test_failed_writes_the_result_column(self, tmp_path):
        path = tmp_path / "t.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            task_id = db.create_task(conn, prompt="p", user_id="u", source_type="cli")
            db.update_task_status(
                conn, task_id, "failed", result="what I had", error="boom",
            )
            task = db.get_task(conn, task_id)
        assert task.result == "what I had"
        assert task.error == "boom"

    def test_a_completed_answer_survives_a_later_failure_mark(self, tmp_path):
        """The reason the write is a COALESCE and not an assignment.

        `process_one_task` re-marks a *completed* task `failed` when its email
        delivery fails, passing no `result`. With an email-only plan that column
        is the only surviving copy of the answer (ISSUE-255), so a plain write
        would blank it with the argument's `None` default.
        """
        path = tmp_path / "t.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            task_id = db.create_task(conn, prompt="p", user_id="u", source_type="cli")
            db.update_task_status(conn, task_id, "completed", result="THE ANSWER")
            db.update_task_status(
                conn, task_id, "failed", error="Email delivery failed",
            )
            task = db.get_task(conn, task_id)
        assert task.status == "failed"
        assert task.result == "THE ANSWER"
        assert task.error == "Email delivery failed"

    def test_a_failure_with_nothing_to_record_leaves_the_column_null(self, tmp_path):
        path = tmp_path / "t.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            task_id = db.create_task(conn, prompt="p", user_id="u", source_type="cli")
            db.update_task_status(conn, task_id, "failed", error="boom")
            task = db.get_task(conn, task_id)
        assert task.result is None


class TestTheSchedulerPersistsIt:
    def _config(self, db_path, tmp_path):
        from tests.test_scheduler import TestProcessOneTask

        return TestProcessOneTask()._make_config(db_path, tmp_path)

    def _exec_returning(self, result_text, partial):
        """A stand-in for `execute_task` that also stamps the task, as it does."""

        def _fake(task, *args, **kwargs):
            task.partial_result = partial
            return (False, result_text, None, None)

        return _fake

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_a_cancel_keeps_the_work_on_the_row(self, _arun, db_path, tmp_path):
        config = self._config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="investigate", user_id="testuser",
                source_type="web", conversation_token="webtok", output_target="web",
            )

        with patch(
            "istota.scheduler.execute_task",
            side_effect=self._exec_returning(
                "Cancelled by user", "I traced it to the poller's cursor.",
            ),
        ):
            task_id, success = process_one_task(config)

        assert success is False
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "cancelled"
        # The classification is unchanged; the work is beside it, not inside it.
        assert task.error == "Cancelled by user"
        assert task.result == "I traced it to the poller's cursor."

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_a_permanent_failure_keeps_the_work_on_the_row(
        self, _arun, db_path, tmp_path,
    ):
        config = self._config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="investigate", user_id="testuser", source_type="cli",
            )
            conn.execute(
                "UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,)
            )

        with patch(
            "istota.scheduler.execute_task",
            side_effect=self._exec_returning(
                "Task execution timed out after 60 minutes", "Halfway through.",
            ),
        ):
            process_one_task(config)

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"
        assert task.result == "Halfway through."

    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_a_retryable_failure_writes_nothing_yet(self, _arun, db_path, tmp_path):
        """A task that will run again has not finished, so there is no answer."""
        config = self._config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="investigate", user_id="testuser", source_type="cli",
            )

        with patch(
            "istota.scheduler.execute_task",
            side_effect=self._exec_returning(
                "Task execution timed out after 60 minutes", "Halfway through.",
            ),
        ):
            process_one_task(config)

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "pending"
        assert task.result is None


class TestTheUserIsToldWhatSurvived:
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_the_talk_failure_notice_carries_the_partial_work(
        self, _arun, db_path, tmp_path,
    ):
        from tests.test_scheduler import TestProcessOneTask

        config = TestProcessOneTask()._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="investigate", user_id="testuser",
                source_type="talk", conversation_token="room1",
            )
            conn.execute(
                "UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,)
            )

        posted: list[str] = []

        def _capture(config_, task_, message, **kwargs):
            posted.append(message)
            return None

        def _fake(task, *args, **kwargs):
            task.partial_result = "The leak is in the poller's cursor."
            return (False, "Task execution timed out after 60 minutes", None, None)

        with patch("istota.scheduler.execute_task", side_effect=_fake), patch(
            "istota.scheduler.post_result_to_talk", side_effect=_capture,
        ):
            process_one_task(config)

        assert posted, "no Talk message was composed for a permanent failure"
        # posted[0] is the ack; the failure notice is the last thing posted.
        body = posted[-1]
        assert PARTIAL_WORK_MARKER in body
        assert "The leak is in the poller's cursor." in body
        # The failure still leads.
        assert body.index(PARTIAL_WORK_MARKER) > 0


class TestTheCancelStringIsStillExact:
    """The reason `partial_text` is a separate field at all."""

    def test_the_native_brain_still_returns_it_byte_for_byte(self):
        from istota.brain.native import NativeBrain

        result = NativeBrain._build_result(
            "aborted", "", "", None, None, None, "m",
            partial_text="lots of prose",
        )
        assert result.result_text == "Cancelled by user"
        assert result.partial_text == "lots of prose"

    def test_json_round_trip_of_a_trace_is_unaffected(self):
        from istota.brain.native import NativeBrain

        result = NativeBrain._build_result(
            "aborted", "", "", [{"type": "text", "text": "x"}], ["a"], None, "m",
        )
        assert json.loads(result.execution_trace) == [{"type": "text", "text": "x"}]
        assert result.partial_text is None
