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
