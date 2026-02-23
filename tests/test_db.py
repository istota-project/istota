"""Tests for db module functions."""

import pytest

from istota import db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


class TestHasActiveForegroundTaskForChannel:
    def test_true_when_pending_fg_task_exists(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            assert db.has_active_foreground_task_for_channel(conn, "room1") is True

    def test_false_when_no_active_fg_task(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.has_active_foreground_task_for_channel(conn, "room1") is False

    def test_ignores_completed_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            db.update_task_status(conn, task_id, "completed", result="done")
            assert db.has_active_foreground_task_for_channel(conn, "room1") is False

    def test_ignores_background_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="cron job", user_id="alice",
                conversation_token="room1", queue="background",
            )
            assert db.has_active_foreground_task_for_channel(conn, "room1") is False

    def test_true_for_locked_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            db.update_task_status(conn, task_id, "locked")
            assert db.has_active_foreground_task_for_channel(conn, "room1") is True

    def test_true_for_running_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            db.update_task_status(conn, task_id, "running")
            assert db.has_active_foreground_task_for_channel(conn, "room1") is True

    def test_different_channel_not_counted(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            assert db.has_active_foreground_task_for_channel(conn, "room2") is False

    def test_false_when_cancel_requested(self, db_path):
        """A running task with cancel_requested should not block new messages."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            db.update_task_status(conn, task_id, "running")
            conn.execute(
                "UPDATE tasks SET cancel_requested = 1 WHERE id = ?",
                (task_id,),
            )
            conn.commit()
            assert db.has_active_foreground_task_for_channel(conn, "room1") is False


class TestCountPendingTasksForUserQueue:
    def test_counts_pending_fg_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 2

    def test_ignores_other_user(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="bob", queue="foreground")
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 1

    def test_ignores_other_queue(self, db_path):
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="background")
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 1

    def test_ignores_completed_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.update_task_status(conn, task_id, "completed", result="done")
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 0

    def test_zero_when_no_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.count_pending_tasks_for_user_queue(conn, "alice", "foreground") == 0


class TestGetPreviousTasks:
    """Tests for get_previous_tasks (returns last N tasks unfiltered by source_type)."""

    def _create_completed(self, conn, prompt, token="room1", source_type="talk"):
        task_id = db.create_task(
            conn, prompt=prompt, user_id="alice",
            conversation_token=token, source_type=source_type,
        )
        db.update_task_status(conn, task_id, "completed", result=f"result-{task_id}")
        return task_id

    def test_returns_empty_list_when_no_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            result = db.get_previous_tasks(conn, "room1")
            assert result == []

    def test_returns_tasks_in_oldest_first_order(self, db_path):
        with db.get_db(db_path) as conn:
            id1 = self._create_completed(conn, "first")
            id2 = self._create_completed(conn, "second")
            id3 = self._create_completed(conn, "third")
            result = db.get_previous_tasks(conn, "room1", limit=3)
            assert [m.id for m in result] == [id1, id2, id3]

    def test_respects_limit(self, db_path):
        with db.get_db(db_path) as conn:
            self._create_completed(conn, "first")
            id2 = self._create_completed(conn, "second")
            id3 = self._create_completed(conn, "third")
            result = db.get_previous_tasks(conn, "room1", limit=2)
            assert [m.id for m in result] == [id2, id3]

    def test_respects_exclude_task_id(self, db_path):
        with db.get_db(db_path) as conn:
            id1 = self._create_completed(conn, "first")
            id2 = self._create_completed(conn, "second")
            id3 = self._create_completed(conn, "third")
            result = db.get_previous_tasks(conn, "room1", exclude_task_id=id3, limit=3)
            assert [m.id for m in result] == [id1, id2]

    def test_scoped_by_conversation_token(self, db_path):
        with db.get_db(db_path) as conn:
            self._create_completed(conn, "other room", token="room2")
            id2 = self._create_completed(conn, "this room")
            result = db.get_previous_tasks(conn, "room1", limit=3)
            assert [m.id for m in result] == [id2]

    def test_includes_scheduled_and_briefing_source_types(self, db_path):
        with db.get_db(db_path) as conn:
            id1 = self._create_completed(conn, "scheduled", source_type="scheduled")
            id2 = self._create_completed(conn, "briefing", source_type="briefing")
            id3 = self._create_completed(conn, "talk", source_type="talk")
            result = db.get_previous_tasks(conn, "room1", limit=3)
            assert [m.id for m in result] == [id1, id2, id3]

    def test_returns_fewer_than_limit_when_not_enough(self, db_path):
        with db.get_db(db_path) as conn:
            id1 = self._create_completed(conn, "only one")
            result = db.get_previous_tasks(conn, "room1", limit=3)
            assert [m.id for m in result] == [id1]

    def test_default_limit_is_three(self, db_path):
        with db.get_db(db_path) as conn:
            self._create_completed(conn, "t1")
            self._create_completed(conn, "t2")
            id3 = self._create_completed(conn, "t3")
            id4 = self._create_completed(conn, "t4")
            id5 = self._create_completed(conn, "t5")
            # Default limit=3 should return the last 3
            result = db.get_previous_tasks(conn, "room1")
            assert [m.id for m in result] == [id3, id4, id5]
