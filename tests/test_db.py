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
