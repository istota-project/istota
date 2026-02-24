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


class TestTalkMessageCache:
    """Tests for the talk_messages cache DB functions."""

    def _make_msg(self, id, actor_id="alice", message="hello", timestamp=1000,
                  message_params=None, deleted=False, parent_id=None,
                  reference_id=None, actor_display_name="Alice",
                  actor_type="users", message_type="comment"):
        msg = {
            "id": id,
            "actorId": actor_id,
            "actorDisplayName": actor_display_name,
            "actorType": actor_type,
            "message": message,
            "messageType": message_type,
            "messageParameters": message_params if message_params is not None else {},
            "timestamp": timestamp,
            "referenceId": reference_id,
            "deleted": deleted,
        }
        if parent_id is not None:
            msg["parent"] = {"id": parent_id}
        return msg

    def test_upsert_and_retrieve(self, db_path):
        with db.get_db(db_path) as conn:
            msgs = [
                self._make_msg(1, timestamp=100, message="first"),
                self._make_msg(2, timestamp=200, message="second"),
            ]
            count = db.upsert_talk_messages(conn, "room1", msgs)
            assert count == 2

            result = db.get_cached_talk_messages(conn, "room1")
            assert len(result) == 2
            # Oldest first
            assert result[0]["id"] == 1
            assert result[0]["message"] == "first"
            assert result[1]["id"] == 2
            assert result[1]["message"] == "second"

    def test_upsert_replaces_on_conflict(self, db_path):
        with db.get_db(db_path) as conn:
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, message="original"),
            ])
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, message="updated"),
            ])
            result = db.get_cached_talk_messages(conn, "room1")
            assert len(result) == 1
            assert result[0]["message"] == "updated"

    def test_upsert_preserves_result_reference_id(self, db_path):
        """Poller upserts should not overwrite :result tags set by scheduler."""
        with db.get_db(db_path) as conn:
            # Scheduler caches a result message
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, message="Done!", reference_id="istota:task:5:result"),
            ])
            # Poller later upserts the same message with :progress tag
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, message="Done!", reference_id="istota:task:5:progress"),
            ])
            result = db.get_cached_talk_messages(conn, "room1")
            assert len(result) == 1
            # :result tag should be preserved
            assert result[0]["referenceId"] == "istota:task:5:result"

    def test_upsert_updates_non_result_reference_id(self, db_path):
        """Upserts should update reference_id when existing is not :result."""
        with db.get_db(db_path) as conn:
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, reference_id="istota:task:5:progress"),
            ])
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, reference_id="istota:task:5:result"),
            ])
            result = db.get_cached_talk_messages(conn, "room1")
            assert result[0]["referenceId"] == "istota:task:5:result"

    def test_get_cached_limit_and_order(self, db_path):
        with db.get_db(db_path) as conn:
            msgs = [self._make_msg(i, timestamp=i * 100) for i in range(1, 21)]
            db.upsert_talk_messages(conn, "room1", msgs)

            result = db.get_cached_talk_messages(conn, "room1", limit=10)
            assert len(result) == 10
            # Should be the 10 most recent, in oldest-first order
            assert result[0]["id"] == 11
            assert result[-1]["id"] == 20

    def test_reconstructed_dict_format(self, db_path):
        """Verify returned dicts match raw API format for build_talk_context()."""
        with db.get_db(db_path) as conn:
            msg = self._make_msg(
                42,
                actor_id="bob",
                actor_display_name="Bob",
                message="test msg",
                timestamp=1700000000,
                reference_id="istota:task:5:result",
                message_params={"file0": {"name": "photo.jpg", "type": "file"}},
                parent_id=40,
                deleted=False,
            )
            db.upsert_talk_messages(conn, "room1", [msg])

            result = db.get_cached_talk_messages(conn, "room1")
            assert len(result) == 1
            r = result[0]
            assert r["id"] == 42
            assert r["actorId"] == "bob"
            assert r["actorDisplayName"] == "Bob"
            assert r["message"] == "test msg"
            assert r["timestamp"] == 1700000000
            assert r["referenceId"] == "istota:task:5:result"
            assert r["messageParameters"] == {"file0": {"name": "photo.jpg", "type": "file"}}
            assert r["parent"] == {"id": 40}
            assert r["deleted"] is False

    def test_has_cached_talk_messages(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.has_cached_talk_messages(conn, "room1") is False
            db.upsert_talk_messages(conn, "room1", [self._make_msg(1)])
            assert db.has_cached_talk_messages(conn, "room1") is True
            # Different room still empty
            assert db.has_cached_talk_messages(conn, "room2") is False

    def test_cleanup_old_messages(self, db_path):
        with db.get_db(db_path) as conn:
            # Insert 5 messages for room1
            msgs = [self._make_msg(i, timestamp=i * 100) for i in range(1, 6)]
            db.upsert_talk_messages(conn, "room1", msgs)

            # Cap at 3 per conversation — should delete the 2 oldest
            deleted = db.cleanup_old_talk_messages(conn, max_per_conversation=3)
            assert deleted == 2

            result = db.get_cached_talk_messages(conn, "room1")
            assert len(result) == 3
            assert result[0]["id"] == 3
            assert result[1]["id"] == 4
            assert result[2]["id"] == 5

    def test_cleanup_per_conversation_independent(self, db_path):
        with db.get_db(db_path) as conn:
            # 4 messages in room1, 2 in room2
            db.upsert_talk_messages(conn, "room1",
                [self._make_msg(i, timestamp=i * 100) for i in range(1, 5)])
            db.upsert_talk_messages(conn, "room2",
                [self._make_msg(10 + i, timestamp=i * 100) for i in range(1, 3)])

            # Cap at 2 per conversation
            deleted = db.cleanup_old_talk_messages(conn, max_per_conversation=2)
            assert deleted == 2  # only room1 has excess

            r1 = db.get_cached_talk_messages(conn, "room1")
            assert len(r1) == 2
            assert r1[0]["id"] == 3
            assert r1[1]["id"] == 4

            r2 = db.get_cached_talk_messages(conn, "room2")
            assert len(r2) == 2  # unchanged

    def test_message_parameters_json_roundtrip(self, db_path):
        """Both dict and list messageParameters survive serialization."""
        with db.get_db(db_path) as conn:
            # Dict params
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, message_params={"key": "value"}),
            ])
            # List params (Talk API can return empty list)
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(2, message_params=[]),
            ])

            result = db.get_cached_talk_messages(conn, "room1")
            assert result[0]["messageParameters"] == {"key": "value"}
            assert result[1]["messageParameters"] == []

    def test_upsert_empty_list_returns_zero(self, db_path):
        with db.get_db(db_path) as conn:
            count = db.upsert_talk_messages(conn, "room1", [])
            assert count == 0

    def test_deleted_message_flag(self, db_path):
        with db.get_db(db_path) as conn:
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1, deleted=True),
            ])
            result = db.get_cached_talk_messages(conn, "room1")
            assert result[0]["deleted"] is True

    def test_no_parent_omits_key(self, db_path):
        with db.get_db(db_path) as conn:
            db.upsert_talk_messages(conn, "room1", [
                self._make_msg(1),  # No parent_id
            ])
            result = db.get_cached_talk_messages(conn, "room1")
            assert "parent" not in result[0]
