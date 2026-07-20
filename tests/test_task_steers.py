"""Tests for the `task_steers` control channel (Stage 1 of the !steer spec)."""

import sqlite3

import pytest

from istota import db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def _task(conn, user_id="alice"):
    return db.create_task(conn, prompt="do a thing", user_id=user_id, source_type="talk")


class TestAddAndCount:
    def test_add_returns_id_and_counts_pending(self, db_path):
        with db.get_db(db_path) as conn:
            tid = _task(conn)
            assert db.count_pending_steers(conn, tid) == 0
            sid = db.add_task_steer(conn, tid, "check the auth module", "alice", "talk")
            assert isinstance(sid, int) and sid > 0
            assert db.count_pending_steers(conn, tid) == 1

    def test_seq_is_per_task_monotonic(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = _task(conn)
            t2 = _task(conn)
            db.add_task_steer(conn, t1, "a", "alice", "talk")
            db.add_task_steer(conn, t1, "b", "alice", "talk")
            db.add_task_steer(conn, t2, "c", "alice", "web")
            rows = conn.execute(
                "SELECT task_id, seq FROM task_steers ORDER BY id"
            ).fetchall()
            by_task: dict[int, list[int]] = {}
            for r in rows:
                by_task.setdefault(r["task_id"], []).append(r["seq"])
            assert by_task[t1] == [1, 2]
            assert by_task[t2] == [1]

    def test_unique_task_seq(self, db_path):
        with db.get_db(db_path) as conn:
            tid = _task(conn)
            db.add_task_steer(conn, tid, "a", "alice", "talk")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO task_steers (task_id, seq, text, user_id, source) "
                    "VALUES (?, 1, 'dup', 'alice', 'talk')",
                    (tid,),
                )


class TestClaim:
    def test_claim_returns_ordered_and_marks_consumed(self, db_path):
        with db.get_db(db_path) as conn:
            tid = _task(conn)
            db.add_task_steer(conn, tid, "first", "alice", "talk")
            db.add_task_steer(conn, tid, "second", "alice", "talk")
            claimed = db.claim_pending_steers(conn, tid)
            assert [s.text for s in claimed] == ["first", "second"]
            assert all(s.status == "consumed" for s in claimed)
            assert all(s.consumed_at for s in claimed)
            # Now nothing pending.
            assert db.count_pending_steers(conn, tid) == 0

    def test_claim_does_not_double_deliver(self, db_path):
        with db.get_db(db_path) as conn:
            tid = _task(conn)
            db.add_task_steer(conn, tid, "only", "alice", "talk")
            first = db.claim_pending_steers(conn, tid)
            second = db.claim_pending_steers(conn, tid)
            assert len(first) == 1
            assert second == []

    def test_claim_scoped_to_task(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = _task(conn)
            t2 = _task(conn)
            db.add_task_steer(conn, t1, "for-t1", "alice", "talk")
            db.add_task_steer(conn, t2, "for-t2", "alice", "talk")
            claimed = db.claim_pending_steers(conn, t1)
            assert [s.text for s in claimed] == ["for-t1"]
            assert db.count_pending_steers(conn, t2) == 1

    def test_empty_when_none_pending(self, db_path):
        with db.get_db(db_path) as conn:
            tid = _task(conn)
            assert db.claim_pending_steers(conn, tid) == []


class TestDrop:
    def test_drop_marks_pending_dropped(self, db_path):
        with db.get_db(db_path) as conn:
            tid = _task(conn)
            db.add_task_steer(conn, tid, "a", "alice", "talk")
            db.add_task_steer(conn, tid, "b", "alice", "talk")
            n = db.drop_pending_steers(conn, tid)
            assert n == 2
            assert db.count_pending_steers(conn, tid) == 0
            statuses = [
                r["status"]
                for r in conn.execute(
                    "SELECT status FROM task_steers WHERE task_id = ?", (tid,)
                ).fetchall()
            ]
            assert set(statuses) == {"dropped"}

    def test_drop_leaves_consumed_alone(self, db_path):
        with db.get_db(db_path) as conn:
            tid = _task(conn)
            db.add_task_steer(conn, tid, "a", "alice", "talk")
            db.claim_pending_steers(conn, tid)  # -> consumed
            db.add_task_steer(conn, tid, "b", "alice", "talk")  # -> pending
            n = db.drop_pending_steers(conn, tid)
            assert n == 1
            statuses = sorted(
                r["status"]
                for r in conn.execute(
                    "SELECT status FROM task_steers WHERE task_id = ?", (tid,)
                ).fetchall()
            )
            assert statuses == ["consumed", "dropped"]


class TestAppendTaskEvent:
    def test_append_assigns_next_seq(self, db_path):
        with db.get_db(db_path) as conn:
            tid = _task(conn)
            seq1 = db.append_task_event(conn, tid, "progress_text", {"text": "hi"})
            seq2 = db.append_task_event(conn, tid, "progress_text", {"text": "again"})
            assert seq1 == 1
            assert seq2 == 2
            assert db.get_max_task_event_seq(conn, tid) == 2

    def test_append_resumes_above_existing(self, db_path):
        with db.get_db(db_path) as conn:
            tid = _task(conn)
            conn.execute(
                "INSERT INTO task_events (task_id, seq, kind, payload) "
                "VALUES (?, 7, 'tool_start', '{}')",
                (tid,),
            )
            conn.commit()
            seq = db.append_task_event(conn, tid, "progress_text", {"text": "x"})
            assert seq == 8
