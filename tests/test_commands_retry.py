"""ISSUE-189: `!retry` / `!resume` — user-initiated re-run of failed/cancelled tasks."""

import json

import pytest

from istota import db
from istota.commands import (
    CommandContext,
    _build_resume_prompt,
    _render_prior_progress,
    cmd_resume,
    cmd_retry,
)
from istota.config import (
    BrainConfig,
    Config,
    NativeBrainConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def make_config(db_path):
    def _make():
        config = Config()
        config.db_path = db_path
        config.talk = TalkConfig(enabled=True, bot_username="istota")
        config.users = {"alice": UserConfig(), "bob": UserConfig()}
        config.scheduler = SchedulerConfig()
        config.brain = BrainConfig(kind="native", native=NativeBrainConfig())
        return config

    return _make


def _ctx(config, conn, *, user_id="alice", token="room1", args="", surface="talk"):
    return CommandContext(
        config=config, conn=conn, user_id=user_id,
        conversation_token=token, args=args, surface=surface,
    )


def _make_task(
    conn, *, user_id="alice", token="room1", source_type="talk",
    status="failed", prompt="do the thing", trace=None, model=None, effort=None,
):
    tid = db.create_task(
        conn, prompt=prompt, user_id=user_id, source_type=source_type,
        conversation_token=token, model=model, effort=effort,
    )
    db.update_task_status(conn, tid, status)
    if trace is not None:
        conn.execute(
            "UPDATE tasks SET execution_trace = ? WHERE id = ?",
            (json.dumps(trace), tid),
        )
        conn.commit()
    return tid


class TestTargetResolution:
    async def test_no_failed_task_in_room(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            out = await cmd_retry(_ctx(config, conn))
        assert "No failed or cancelled task" in out

    async def test_last_failed_in_room(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            old = _make_task(conn, prompt="first")
            new = _make_task(conn, prompt="second")
            out = await cmd_retry(_ctx(config, conn))
            # Picks the most recent (second), not the first.
            assert f"#{new}" in out
            assert "second" in out
            assert f"#{old}" not in out.split(":")[0]

    async def test_cancelled_is_retryable(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            tid = _make_task(conn, status="cancelled")
            out = await cmd_retry(_ctx(config, conn))
        assert f"Retrying task #{tid}" in out

    async def test_room_scoped(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _make_task(conn, token="room2")  # failed, other room
            out = await cmd_retry(_ctx(config, conn, token="room1"))
        assert "No failed or cancelled task" in out

    async def test_only_interactive_source_types(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _make_task(conn, source_type="scheduled")  # not interactive
            out = await cmd_retry(_ctx(config, conn))
        assert "No failed or cancelled task" in out

    async def test_explicit_id(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            tid = _make_task(conn)
            out = await cmd_retry(_ctx(config, conn, args=f"#{tid}"))
        assert f"Retrying task #{tid}" in out

    async def test_explicit_id_not_found(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            out = await cmd_retry(_ctx(config, conn, args="#9999"))
        assert "not found" in out

    async def test_explicit_id_non_numeric(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            out = await cmd_retry(_ctx(config, conn, args="#abc"))
        assert "Usage" in out

    async def test_another_users_task_rejected(self, make_config):
        config = make_config()
        config.admin_users = {"someone_else"}  # non-empty → alice is not admin
        with db.get_db(config.db_path) as conn:
            tid = _make_task(conn, user_id="bob")
            out = await cmd_retry(_ctx(config, conn, user_id="alice", args=f"#{tid}"))
        assert "another user" in out

    async def test_admin_can_retry_others(self, make_config):
        config = make_config()
        config.admin_users = {"alice"}
        with db.get_db(config.db_path) as conn:
            tid = _make_task(conn, user_id="bob")
            out = await cmd_retry(_ctx(config, conn, user_id="alice", args=f"#{tid}"))
        assert f"Retrying task #{tid}" in out

    async def test_running_task_rejected(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            tid = _make_task(conn, status="running")
            out = await cmd_retry(_ctx(config, conn, args=f"#{tid}"))
        assert "!stop" in out

    async def test_completed_task_rejected(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            tid = _make_task(conn, status="completed")
            out = await cmd_retry(_ctx(config, conn, args=f"#{tid}"))
        assert "nothing to retry" in out

    async def test_pending_confirmation_rejected(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            tid = _make_task(conn, status="pending_confirmation")
            out = await cmd_retry(_ctx(config, conn, args=f"#{tid}"))
        assert "confirmation" in out


class TestNewTaskCreation:
    async def test_creates_new_task_with_parent_link(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            orig = _make_task(conn, prompt="research X", model="opus", effort="high")
            await cmd_retry(_ctx(config, conn))
            new = conn.execute(
                "SELECT * FROM tasks WHERE parent_task_id = ?", (orig,),
            ).fetchone()
        assert new is not None
        assert new["prompt"] == "research X"
        assert new["model"] == "opus"
        assert new["effort"] == "high"
        assert new["status"] == "pending"
        assert new["source_type"] == "talk"

    async def test_original_task_left_intact(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            orig = _make_task(conn, status="failed")
            await cmd_retry(_ctx(config, conn))
            row = db.get_task(conn, orig)
        assert row.status == "failed"

    async def test_delivery_fields_copied(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            orig = _make_task(conn)
            conn.execute(
                "UPDATE tasks SET talk_delivery_token = ?, output_target = ? "
                "WHERE id = ?",
                ("real-room", "talk", orig),
            )
            conn.commit()
            await cmd_retry(_ctx(config, conn))
            new = conn.execute(
                "SELECT * FROM tasks WHERE parent_task_id = ?", (orig,),
            ).fetchone()
        assert new["talk_delivery_token"] == "real-room"
        assert new["output_target"] == "talk"

    async def test_transcript_user_row_written(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk", name="Room 1")
            orig = _make_task(conn, prompt="clean prompt")
            await cmd_retry(_ctx(config, conn, surface="talk"))
            new = conn.execute(
                "SELECT id FROM tasks WHERE parent_task_id = ?", (orig,),
            ).fetchone()["id"]
            row = conn.execute(
                "SELECT body, role FROM messages WHERE room_token = ? AND task_id = ?",
                ("room1", new),
            ).fetchone()
        assert row is not None
        assert row["role"] == "user"
        assert row["body"] == "clean prompt"

    async def test_no_transcript_row_without_room(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            # No room registered — write is skipped, command still succeeds.
            _make_task(conn, prompt="p")
            out = await cmd_retry(_ctx(config, conn))
            count = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        assert "Retrying" in out
        assert count == 0


class TestResume:
    _TRACE = [
        {"type": "tool", "text": "Read file A", "raw": "cat A"},
        {"type": "text", "text": "found the config"},
        {"type": "tool", "text": "Browse site B", "raw": "curl B"},
    ]

    async def test_resume_injects_prior_progress(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            orig = _make_task(conn, prompt="find the answer", trace=self._TRACE)
            out = await cmd_resume(_ctx(config, conn))
            new = conn.execute(
                "SELECT prompt FROM tasks WHERE parent_task_id = ?", (orig,),
            ).fetchone()
        assert "Resuming task" in out
        prompt = new["prompt"]
        assert "cat A" in prompt
        assert "curl B" in prompt
        assert "found the config" in prompt
        assert "find the answer" in prompt
        assert prompt.rstrip().endswith("find the answer")

    async def test_resume_reports_step_count(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _make_task(conn, trace=self._TRACE)
            out = await cmd_resume(_ctx(config, conn))
        assert "3 prior step" in out

    async def test_resume_transcript_row_is_clean_prompt(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk", name="Room 1")
            orig = _make_task(conn, prompt="original ask", trace=self._TRACE)
            await cmd_resume(_ctx(config, conn, surface="talk"))
            new = conn.execute(
                "SELECT id FROM tasks WHERE parent_task_id = ?", (orig,),
            ).fetchone()["id"]
            body = conn.execute(
                "SELECT body FROM messages WHERE task_id = ?", (new,),
            ).fetchone()["body"]
        # The transcript shows the clean prompt, not the trace-injected version.
        assert body == "original ask"

    async def test_resume_degrades_without_trace(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            orig = _make_task(conn, prompt="no trace here", trace=None)
            out = await cmd_resume(_ctx(config, conn))
            new = conn.execute(
                "SELECT prompt FROM tasks WHERE parent_task_id = ?", (orig,),
            ).fetchone()
        assert "No prior progress" in out
        assert new["prompt"] == "no trace here"

    async def test_resume_degrades_on_corrupt_trace(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            tid = _make_task(conn, prompt="p")
            conn.execute(
                "UPDATE tasks SET execution_trace = ? WHERE id = ?",
                ("{not json", tid),
            )
            conn.commit()
            out = await cmd_resume(_ctx(config, conn))
        assert "No prior progress" in out

    async def test_resume_degrades_on_empty_trace(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _make_task(conn, prompt="p", trace=[])
            out = await cmd_resume(_ctx(config, conn))
        assert "No prior progress" in out


class TestRenderHelpers:
    def test_render_prefers_raw(self):
        trace = [{"type": "tool", "text": "Run something", "raw": "ls -la"}]
        assert _render_prior_progress(_task_with(trace)) == "- ran: ls -la"

    def test_render_falls_back_to_desc(self):
        trace = [{"type": "tool", "text": "Read file"}]
        assert _render_prior_progress(_task_with(trace)) == "- Read file"

    def test_render_none_for_no_trace(self):
        assert _render_prior_progress(_task_with(None)) is None

    def test_build_resume_prompt_shape(self):
        out = _build_resume_prompt("original", "- ran: x")
        assert "continue from where you left off" in out
        assert out.endswith("original")


def _task_with(trace):
    return db.Task(
        id=1, status="failed", source_type="talk", user_id="alice",
        prompt="p", execution_trace=json.dumps(trace) if trace is not None else None,
    )
