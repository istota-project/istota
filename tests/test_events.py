"""Tests for the task-event-streaming infrastructure (events.py + db helpers)."""

import json

import pytest

from istota import db
from istota.events import PAYLOAD_MAX_BYTES, EventWriter, TaskEvent


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "events.db"
    db.init_db(p)
    return p


class _Recorder:
    def __init__(self):
        self.events = []
        self.finished = 0

    def on_event(self, event):
        self.events.append(event)

    def on_finish(self):
        self.finished += 1


class _Exploder:
    def on_event(self, event):
        raise RuntimeError("boom")

    def on_finish(self):
        raise RuntimeError("boom-finish")


# ---------------------------------------------------------------------------
# EventWriter
# ---------------------------------------------------------------------------


class TestEventWriterSeq:
    def test_seq_is_monotonic_from_one(self, db_path):
        w = EventWriter(1, str(db_path))
        e1 = w.emit("task_started")
        e2 = w.emit("tool_start", {"description": "x"})
        e3 = w.emit("done", {"stop_reason": "completed"})
        assert (e1.seq, e2.seq, e3.seq) == (1, 2, 3)
        assert e1.kind == "task_started"

    def test_events_persisted_and_readable(self, db_path):
        w = EventWriter(7, str(db_path))
        w.emit("task_started")
        w.emit("tool_start", {"tool_name": "Read", "description": "📄 Reading f"})
        rows = []
        with db.get_db(db_path) as conn:
            rows = db.get_task_events(conn, 7)
        assert [r["seq"] for r in rows] == [1, 2]
        assert rows[1]["kind"] == "tool_start"
        # payload is decoded back to a dict
        assert rows[1]["payload"]["description"] == "📄 Reading f"

    def test_elapsed_is_nonnegative(self, db_path):
        w = EventWriter(1, str(db_path))
        assert w.elapsed_seconds() >= 0


class TestPayloadTruncation:
    def test_oversized_payload_is_truncated(self, db_path):
        w = EventWriter(1, str(db_path))
        big = "x" * (PAYLOAD_MAX_BYTES * 2)
        event = w.emit("result", {"text": big, "truncated": False})
        assert event.payload["_truncated"] is True
        assert event.payload["text"].endswith("… [truncated]")
        assert len(event.payload["text"]) < len(big)

    def test_small_payload_untouched(self, db_path):
        w = EventWriter(1, str(db_path))
        event = w.emit("result", {"text": "short"})
        assert "_truncated" not in event.payload
        assert event.payload["text"] == "short"


class TestKillSwitch:
    def test_disabled_skips_db_but_notifies_subscribers(self, db_path):
        rec = _Recorder()
        w = EventWriter(1, str(db_path), enabled=False)
        w.subscribe(rec)
        w.emit("task_started")
        # Subscriber still saw it…
        assert len(rec.events) == 1
        # …but nothing was persisted.
        with db.get_db(db_path) as conn:
            assert db.get_task_events(conn, 1) == []


class TestSubscriberRobustness:
    def test_subscriber_exception_does_not_break_emit(self, db_path):
        rec = _Recorder()
        w = EventWriter(1, str(db_path))
        w.subscribe(_Exploder())
        w.subscribe(rec)
        # Must not raise; the healthy subscriber still runs and the row persists.
        w.emit("task_started")
        assert len(rec.events) == 1
        with db.get_db(db_path) as conn:
            assert len(db.get_task_events(conn, 1)) == 1

    def test_finish_calls_on_finish_and_swallows_errors(self, db_path):
        rec = _Recorder()
        w = EventWriter(1, str(db_path))
        w.subscribe(_Exploder())
        w.subscribe(rec)
        w.finish()  # must not raise
        assert rec.finished == 1


# ---------------------------------------------------------------------------
# db helpers
# ---------------------------------------------------------------------------


class TestGetTaskEvents:
    def test_since_seq_filters_and_orders(self, db_path):
        w = EventWriter(3, str(db_path))
        for i in range(5):
            w.emit("tool_start", {"i": i})
        with db.get_db(db_path) as conn:
            rows = db.get_task_events(conn, 3, since_seq=2)
        assert [r["seq"] for r in rows] == [3, 4, 5]

    def test_isolated_per_task(self, db_path):
        EventWriter(1, str(db_path)).emit("task_started")
        EventWriter(2, str(db_path)).emit("task_started")
        with db.get_db(db_path) as conn:
            assert len(db.get_task_events(conn, 1)) == 1
            assert len(db.get_task_events(conn, 2)) == 1

    def test_limit(self, db_path):
        w = EventWriter(1, str(db_path))
        for _ in range(10):
            w.emit("tool_start", {})
        with db.get_db(db_path) as conn:
            assert len(db.get_task_events(conn, 1, limit=3)) == 3


class TestDeleteTaskEvents:
    def test_delete_clears_rows(self, db_path):
        w = EventWriter(5, str(db_path))
        w.emit("task_started")
        w.emit("tool_start", {})
        with db.get_db(db_path) as conn:
            assert db.delete_task_events(conn, 5) == 2
            assert db.get_task_events(conn, 5) == []

    def test_retry_restart_no_collision(self, db_path):
        # Attempt 1 writes seq 1..3, then we clear and a fresh writer (seq from
        # 1) must not collide on UNIQUE(task_id, seq).
        w1 = EventWriter(9, str(db_path))
        w1.emit("task_started")
        w1.emit("tool_start", {})
        w1.emit("error", {"message": "fail"})
        with db.get_db(db_path) as conn:
            db.delete_task_events(conn, 9)
        w2 = EventWriter(9, str(db_path))
        w2.emit("task_started")  # seq=1 again — would collide without the delete
        w2.emit("result", {"text": "ok"})
        with db.get_db(db_path) as conn:
            rows = db.get_task_events(conn, 9)
        assert [r["seq"] for r in rows] == [1, 2]
        assert rows[1]["payload"]["text"] == "ok"  # only the retry's events survive


class TestCleanupDeletesEvents:
    def test_cleanup_old_tasks_removes_events(self, db_path):
        with db.get_db(db_path) as conn:
            tid = db.create_task(conn, prompt="x", user_id="u", source_type="cli")
            db.update_task_status(conn, tid, "completed", result="done")
            # Backdate completion beyond the retention window.
            conn.execute(
                "UPDATE tasks SET completed_at = datetime('now', '-30 days') WHERE id = ?",
                (tid,),
            )
        EventWriter(tid, str(db_path)).emit("done", {"stop_reason": "completed"})
        with db.get_db(db_path) as conn:
            assert len(db.get_task_events(conn, tid)) == 1
            db.cleanup_old_tasks(conn, retention_days=7)
            assert db.get_task_events(conn, tid) == []
