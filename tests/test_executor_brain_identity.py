"""The task identity the executor hands the brain.

`BrainRequest` grew six fields naming the task attempt a request *is*, so that
NativeBrain can name and head its per-attempt session log. This file pins the
mapping, because the values come off the task row and nothing else asserts on
them — `tests/native/test_session_log_integration.py` builds its own requests
by hand and would stay green against any executor mapping at all.
"""

import dataclasses
from unittest.mock import patch

from istota import db
from istota.brain import BrainRequest, BrainResult
from istota.config import Config, SecurityConfig


class _RecordingBrain:
    """Stands in for whatever `make_brain` would have returned."""

    model_namespace = "anthropic"
    supports_steering = False

    def __init__(self):
        self.requests: list[BrainRequest] = []

    def resolve_model_name(self, name):
        return name

    def validate_alias_override(self, name, target):
        return []

    def execute(self, req: BrainRequest) -> BrainResult:
        self.requests.append(req)
        return BrainResult(success=True, result_text="ok", stop_reason="completed")


def _config(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    skills_dir = tmp_path / "config" / "skills"
    skills_dir.mkdir(parents=True)
    return Config(
        db_path=db_path,
        skills_dir=skills_dir,
        bundled_skills_dir=tmp_path / "_empty_bundled",
        temp_dir=tmp_path / "temp",
        model="claude-sonnet-4-6",
        security=SecurityConfig(skill_proxy_enabled=False),
    )


def _run(tmp_path, **task_kwargs) -> BrainRequest:
    config = _config(tmp_path)
    (tmp_path / "temp" / "alice").mkdir(parents=True)
    brain = _RecordingBrain()
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(
            conn,
            prompt="do the thing",
            user_id="alice",
            source_type="talk",
            conversation_token="a1b2c3d4",
            **task_kwargs,
        )
        task = db.get_task(conn, task_id)
        with patch("istota.executor.make_brain", return_value=brain):
            from istota.executor import execute_task

            execute_task(task, config, [], conn=conn)
    assert brain.requests, "the brain was never called"
    return brain.requests[-1]


class TestTheIdentityReachesTheBrain:
    def test_the_task_row_is_copied_onto_the_request(self, tmp_path):
        req = _run(tmp_path)
        assert req.task_id > 0
        assert req.user_id == "alice"
        assert req.source_type == "talk"
        assert req.conversation_token == "a1b2c3d4"
        assert req.is_group_chat is False

    def test_a_group_chat_is_carried(self, tmp_path):
        assert _run(tmp_path, is_group_chat=True).is_group_chat is True

    def test_the_first_run_is_attempt_one(self, tmp_path):
        """`attempt_count` counts *prior* attempts, so a first run carries 0.

        The session log's numbering is 1-based — `task_usage.attempt_seq` is
        `MAX(...) + 1` — so a first run has to be attempt 1 and a retry 2.
        Passing the raw counter would name the first file `task-N-0`.
        """
        assert _run(tmp_path).attempt == 1

    def test_a_retry_is_attempt_two(self, tmp_path):
        config = _config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        brain = _RecordingBrain()
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="do the thing", user_id="alice", source_type="talk"
            )
            conn.execute(
                "UPDATE tasks SET attempt_count = 1 WHERE id = ?", (task_id,)
            )
            conn.commit()
            task = db.get_task(conn, task_id)
            assert task.attempt_count == 1
            with patch("istota.executor.make_brain", return_value=brain):
                from istota.executor import execute_task

                execute_task(task, config, [], conn=conn)
        assert brain.requests[-1].attempt == 2


class TestTheIdentitySurvivesAFallbackReroute:
    def test_replace_preserves_every_identity_field(self, tmp_path):
        """`_run_fallback` copies a request with `dataclasses.replace(req,
        model=…, effort=…, advisor=…)`, which names no other field.

        That is what makes a fallback native run's session log a *second* file
        for the same attempt — collision-suffixed — rather than a nameless one,
        so it is worth an assertion rather than a comment.
        """
        req = _run(tmp_path)
        copied = dataclasses.replace(req, model="other", effort="high", advisor="")
        for field in (
            "task_id",
            "attempt",
            "user_id",
            "source_type",
            "conversation_token",
            "is_group_chat",
        ):
            assert getattr(copied, field) == getattr(req, field), field
