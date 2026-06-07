"""Tests for the terminal REPL session + run_task_inline.

The session is driven with an injected ``input_fn`` and a fake ``execute_task``
(patched on the scheduler) so no real brain runs. These assert the wiring:
repl/stream task creation, terminal rendering via the event stream, multi-turn
token carry-forward, /clear, deferred-op drain, and the cancelled path.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from istota import db
from istota.config import (
    Config,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.repl import run_session
from istota.scheduler import run_task_inline


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return Config(
        db_path=path,
        nextcloud=NextcloudConfig(),
        talk=TalkConfig(enabled=False),
        scheduler=SchedulerConfig(event_log_enabled=True),
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig()},
    )


def _input_seq(lines):
    """An input_fn that yields the given lines then raises EOFError (Ctrl-D)."""
    it = iter(lines)

    def _fn(_prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return _fn


def _fake_execute(result="answer", *, success=True, deferred=None):
    """Build a fake execute_task that emits a progress event and returns result.

    ``deferred`` is an optional (filename_suffix, content) the fake writes into
    the user temp dir to exercise the deferred-op drain.
    """
    def _exec(task, config, user_resources, *, event_writer=None,
              workspace_dir=None, **kwargs):
        if event_writer is not None:
            event_writer.emit("progress_text", {"text": f"thinking about {task.prompt}"})
        if deferred is not None:
            from istota.executor import get_user_temp_dir
            d = get_user_temp_dir(config, task.user_id)
            d.mkdir(parents=True, exist_ok=True)
            suffix, content = deferred
            (d / f"task_{task.id}_{suffix}").write_text(content)
        return (success, result, None, None)
    return _exec


class TestRunSession:
    def test_creates_repl_stream_task(self, cfg, monkeypatch):
        monkeypatch.setattr("istota.scheduler.execute_task", _fake_execute("hi"))
        run_session(
            cfg, user_id="alice", input_fn=_input_seq(["hello"]),
            stream=io.StringIO(),
        )
        with db.get_db(cfg.db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        assert len(tasks) == 1
        t = tasks[0]
        assert t.source_type == "repl"
        assert t.output_target == "stream"
        assert t.conversation_token.startswith("repl-alice-")
        assert t.status == "completed"

    def test_terminal_renders_result(self, cfg, monkeypatch):
        monkeypatch.setattr("istota.scheduler.execute_task", _fake_execute("the answer"))
        out = io.StringIO()
        run_session(cfg, user_id="alice", input_fn=_input_seq(["q"]), stream=out)
        text = out.getvalue()
        assert "the answer" in text

    def test_two_turns_same_token(self, cfg, monkeypatch):
        monkeypatch.setattr("istota.scheduler.execute_task", _fake_execute("a"))
        run_session(
            cfg, user_id="alice", input_fn=_input_seq(["one", "two"]),
            stream=io.StringIO(),
        )
        with db.get_db(cfg.db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        assert len(tasks) == 2
        assert tasks[0].conversation_token == tasks[1].conversation_token

    def test_clear_mints_new_token(self, cfg, monkeypatch):
        monkeypatch.setattr("istota.scheduler.execute_task", _fake_execute("a"))
        run_session(
            cfg, user_id="alice",
            input_fn=_input_seq(["one", "/clear", "two"]),
            stream=io.StringIO(),
        )
        with db.get_db(cfg.db_path) as conn:
            tasks = sorted(db.list_tasks(conn, user_id="alice"), key=lambda t: t.id)
        assert len(tasks) == 2
        assert tasks[0].conversation_token != tasks[1].conversation_token

    def test_exit_command_stops(self, cfg, monkeypatch):
        monkeypatch.setattr("istota.scheduler.execute_task", _fake_execute("a"))
        # /exit before any prompt → no tasks.
        run_session(
            cfg, user_id="alice", input_fn=_input_seq(["/exit", "ignored"]),
            stream=io.StringIO(),
        )
        with db.get_db(cfg.db_path) as conn:
            assert db.list_tasks(conn, user_id="alice") == []

    def test_deferred_kv_applied_between_turns(self, cfg, monkeypatch):
        kv_op = json.dumps([{"op": "set", "namespace": "repl", "key": "k", "value": 1}])
        monkeypatch.setattr(
            "istota.scheduler.execute_task",
            _fake_execute("ok", deferred=("kv_ops.json", kv_op)),
        )
        run_session(cfg, user_id="alice", input_fn=_input_seq(["go"]), stream=io.StringIO())
        with db.get_db(cfg.db_path) as conn:
            val = db.kv_get(conn, "alice", "repl", "k")
        # The deferred-op drain ran inside run_task_inline and persisted the kv set.
        assert val is not None
        assert val["value"] in (1, "1")


class TestRunTaskInline:
    def test_completed_status_and_drain(self, cfg, monkeypatch):
        monkeypatch.setattr("istota.scheduler.execute_task", _fake_execute("done"))
        with db.get_db(cfg.db_path) as conn:
            tid = db.create_task(
                conn, prompt="x", user_id="alice", source_type="repl",
                conversation_token="repl-alice-aaaa", output_target="stream",
            )
            task = db.get_task(conn, tid)
        from istota.events import EventWriter
        writer = EventWriter(tid, str(cfg.db_path), enabled=True)
        success, result = run_task_inline(cfg, task, event_writer=writer)
        assert success is True
        assert result == "done"
        with db.get_db(cfg.db_path) as conn:
            assert db.get_task(conn, tid).status == "completed"
            events = [e["kind"] for e in db.get_task_events(conn, tid)]
        assert "result" in events and "done" in events

    def test_cancelled_status(self, cfg, monkeypatch):
        monkeypatch.setattr(
            "istota.scheduler.execute_task",
            _fake_execute("Cancelled by user", success=False),
        )
        with db.get_db(cfg.db_path) as conn:
            tid = db.create_task(
                conn, prompt="x", user_id="alice", source_type="repl",
                conversation_token="repl-alice-bbbb", output_target="stream",
            )
            task = db.get_task(conn, tid)
        from istota.events import EventWriter
        writer = EventWriter(tid, str(cfg.db_path), enabled=True)
        success, result = run_task_inline(cfg, task, event_writer=writer)
        assert success is False
        with db.get_db(cfg.db_path) as conn:
            assert db.get_task(conn, tid).status == "cancelled"
            events = [e["kind"] for e in db.get_task_events(conn, tid)]
        assert "cancelled" in events
