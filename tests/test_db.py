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


class TestClaimTaskChannelGate:
    def test_claim_skips_channel_blocked_fg_tasks(self, db_path):
        """claim_task should not return a fg task if another fg task in the
        same channel is already running/locked."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="do thing 1", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )
            db.update_task_status(conn, t1, "running")

            db.create_task(
                conn, prompt="do thing 2", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )
            t3 = db.create_task(
                conn, prompt="do thing 3", user_id="user1",
                source_type="talk", conversation_token="room2",
                queue="foreground",
            )

            claimed = db.claim_task(conn, "worker-1", queue="foreground")
            assert claimed is not None
            assert claimed.id == t3

    def test_claim_unblocks_after_channel_task_completes(self, db_path):
        """Once the active task completes, the next one in the same channel
        becomes claimable."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="do thing 1", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )
            db.update_task_status(conn, t1, "running")

            t2 = db.create_task(
                conn, prompt="do thing 2", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )

            claimed = db.claim_task(
                conn, "worker-1", queue="foreground", user_id="user1",
            )
            assert claimed is None

            db.update_task_status(conn, t1, "completed", result="ok")

            claimed = db.claim_task(
                conn, "worker-2", queue="foreground", user_id="user1",
            )
            assert claimed is not None
            assert claimed.id == t2

    def test_channel_gate_ignores_background_queue(self, db_path):
        """Background tasks are not subject to the per-channel gate."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="fg thing", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )
            db.update_task_status(conn, t1, "running")

            t2 = db.create_task(
                conn, prompt="bg thing", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="background",
            )

            claimed = db.claim_task(conn, "worker-1", queue="background")
            assert claimed is not None
            assert claimed.id == t2

    def test_channel_gate_ignores_null_token(self, db_path):
        """Tasks without a conversation_token (email, cron) are never
        channel-blocked."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="cron thing", user_id="user1",
                source_type="cron", conversation_token=None,
                queue="foreground",
            )
            db.update_task_status(conn, t1, "running")

            t2 = db.create_task(
                conn, prompt="email thing", user_id="user1",
                source_type="email", conversation_token=None,
                queue="foreground",
            )

            claimed = db.claim_task(conn, "worker-1", queue="foreground")
            assert claimed is not None
            assert claimed.id == t2

    def test_cancelled_task_does_not_block(self, db_path):
        """A cancel_requested task in the channel should not block the next."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="do thing 1", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )
            db.update_task_status(conn, t1, "running")
            conn.execute(
                "UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (t1,),
            )
            conn.commit()

            t2 = db.create_task(
                conn, prompt="do thing 2", user_id="user1",
                source_type="talk", conversation_token="room1",
                queue="foreground",
            )

            claimed = db.claim_task(conn, "worker-1", queue="foreground")
            assert claimed is not None
            assert claimed.id == t2


class TestCreateTaskTalkDedup:
    def test_duplicate_talk_message_id_returns_existing(self, db_path):
        with db.get_db(db_path) as conn:
            task1 = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", talk_message_id=999,
            )
            task2 = db.create_task(
                conn, prompt="hello again", user_id="alice",
                conversation_token="room1", talk_message_id=999,
            )
            assert task2 == task1

    def test_same_message_id_different_conversation_creates_new(self, db_path):
        with db.get_db(db_path) as conn:
            task1 = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", talk_message_id=999,
            )
            task2 = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room2", talk_message_id=999,
            )
            assert task2 != task1

    def test_null_talk_message_id_allows_duplicates(self, db_path):
        with db.get_db(db_path) as conn:
            task1 = db.create_task(
                conn, prompt="job1", user_id="alice",
            )
            task2 = db.create_task(
                conn, prompt="job2", user_id="alice",
            )
            assert task2 != task1


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


# =============================================================================
# TestSentEmails
# =============================================================================


class TestSentEmails:
    def test_record_and_find_by_message_id(self, db_path):
        with db.get_db(db_path) as conn:
            rid = db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<abc123@example.com>",
                to_addr="bob@example.com",
                subject="Meeting request",
                task_id=None,
                conversation_token="room42",
            )
            assert rid > 0

            found = db.find_sent_email_by_message_id(conn, "<abc123@example.com>")
            assert found is not None
            assert found.user_id == "stefan"
            assert found.to_addr == "bob@example.com"
            assert found.subject == "Meeting request"
            assert found.conversation_token == "room42"

    def test_find_by_message_id_not_found(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.find_sent_email_by_message_id(conn, "<nope@nope>") is None

    def test_find_by_references(self, db_path):
        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<msg1@example.com>",
                to_addr="alice@example.com",
                subject="Hello",
                conversation_token="room1",
            )
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<msg2@example.com>",
                to_addr="bob@example.com",
                subject="Other",
                conversation_token="room2",
            )

            # References list containing one of our sent message IDs
            found = db.find_sent_email_by_references(
                conn, ["<unknown@x.com>", "<msg1@example.com>"]
            )
            assert found is not None
            assert found.message_id == "<msg1@example.com>"

    def test_find_by_references_empty_list(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.find_sent_email_by_references(conn, []) is None

    def test_find_by_references_no_match(self, db_path):
        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<msg1@example.com>",
                to_addr="alice@example.com",
                subject="Hello",
            )
            assert db.find_sent_email_by_references(conn, ["<other@x.com>"]) is None

    def test_find_by_references_returns_most_recent(self, db_path):
        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<old@example.com>",
                to_addr="alice@example.com",
                subject="Old",
                conversation_token="room1",
            )
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<new@example.com>",
                to_addr="alice@example.com",
                subject="New",
                conversation_token="room2",
            )

            # Both match — should return the more recent one
            found = db.find_sent_email_by_references(
                conn, ["<old@example.com>", "<new@example.com>"]
            )
            assert found is not None
            assert found.message_id == "<new@example.com>"

    def test_record_with_all_fields(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="send email", user_id="stefan")
            rid = db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<full@example.com>",
                to_addr="bob@example.com",
                subject="Re: Meeting",
                task_id=task_id,
                thread_id="abc123",
                in_reply_to="<original@example.com>",
                references="<original@example.com>",
                conversation_token="room5",
            )

            found = db.find_sent_email_by_message_id(conn, "<full@example.com>")
            assert found.task_id == task_id
            assert found.thread_id == "abc123"
            assert found.in_reply_to == "<original@example.com>"
            assert found.references == "<original@example.com>"


# =============================================================================
# TestConfirmedAt
# =============================================================================


class TestConfirmedAt:
    def test_confirmed_at_roundtrip(self, db_path):
        """confirm_task sets confirmed_at, get_task reads it back."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Do something", user_id="alice",
                conversation_token="room1",
            )
            # Set to pending_confirmation first (confirm_task requires this status)
            db.set_task_confirmation(conn, task_id, "Should I proceed?")
            task = db.get_task(conn, task_id)
            assert task.confirmed_at is None
            assert task.confirmation_prompt == "Should I proceed?"

            # Confirm it
            db.confirm_task(conn, task_id)
            task = db.get_task(conn, task_id)
            assert task.confirmed_at is not None
            assert task.status == "pending"

    def test_unconfirmed_task_has_none(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Normal", user_id="alice")
            task = db.get_task(conn, task_id)
            assert task.confirmed_at is None


# =============================================================================
# TestCancelPendingConfirmations
# =============================================================================


class TestCancelPendingConfirmations:
    def test_cancels_pending_confirmation_in_conversation(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Draft email", user_id="alice",
                conversation_token="room1",
            )
            db.set_task_confirmation(conn, task_id, "Send this?")

            count = db.cancel_pending_confirmations(conn, "room1", "alice")
            assert count == 1

            task = db.get_task(conn, task_id)
            assert task.status == "cancelled"

    def test_does_not_cancel_other_users(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="Alice draft", user_id="alice",
                conversation_token="room1",
            )
            db.set_task_confirmation(conn, t1, "Send?")

            t2 = db.create_task(
                conn, prompt="Bob draft", user_id="bob",
                conversation_token="room1",
            )
            db.set_task_confirmation(conn, t2, "Send?")

            count = db.cancel_pending_confirmations(conn, "room1", "alice")
            assert count == 1

            # Alice's cancelled, Bob's still pending
            assert db.get_task(conn, t1).status == "cancelled"
            assert db.get_task(conn, t2).status == "pending_confirmation"

    def test_does_not_cancel_other_conversations(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="Draft", user_id="alice",
                conversation_token="room1",
            )
            db.set_task_confirmation(conn, t1, "Send?")

            t2 = db.create_task(
                conn, prompt="Other draft", user_id="alice",
                conversation_token="room2",
            )
            db.set_task_confirmation(conn, t2, "Send?")

            count = db.cancel_pending_confirmations(conn, "room1", "alice")
            assert count == 1

            assert db.get_task(conn, t1).status == "cancelled"
            assert db.get_task(conn, t2).status == "pending_confirmation"

    def test_returns_zero_when_nothing_to_cancel(self, db_path):
        with db.get_db(db_path) as conn:
            count = db.cancel_pending_confirmations(conn, "room1", "alice")
            assert count == 0


class TestGetPendingConfirmationForUser:
    def test_returns_newest_pending_confirmation(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="older", user_id="alice", conversation_token="thread1",
            )
            db.set_task_confirmation(conn, t1, "Confirm older?")
            t2 = db.create_task(
                conn, prompt="newer", user_id="alice", conversation_token="thread2",
            )
            db.set_task_confirmation(conn, t2, "Confirm newer?")

            result = db.get_pending_confirmation_for_user(conn, "alice")
            assert result is not None
            assert result.id == t2

    def test_returns_none_when_no_pending(self, db_path):
        with db.get_db(db_path) as conn:
            result = db.get_pending_confirmation_for_user(conn, "alice")
            assert result is None

    def test_ignores_other_users(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="bob's task", user_id="bob", conversation_token="room1",
            )
            db.set_task_confirmation(conn, t1, "Confirm?")

            result = db.get_pending_confirmation_for_user(conn, "alice")
            assert result is None

    def test_ignores_non_confirmation_statuses(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="pending task", user_id="alice", conversation_token="room1",
            )
            # Task stays in 'pending' status, not 'pending_confirmation'
            result = db.get_pending_confirmation_for_user(conn, "alice")
            assert result is None


class TestGetPendingConfirmationByResponseId:
    def test_returns_matching_task(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="gated email", user_id="alice", conversation_token="thread1",
            )
            db.set_task_confirmation(conn, t1, "Confirm?")
            db.update_talk_response_id(conn, t1, 42)

            result = db.get_pending_confirmation_by_response_id(conn, 42)
            assert result is not None
            assert result.id == t1

    def test_returns_none_when_no_match(self, db_path):
        with db.get_db(db_path) as conn:
            result = db.get_pending_confirmation_by_response_id(conn, 999)
            assert result is None

    def test_ignores_non_confirmation_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="completed task", user_id="alice", conversation_token="room1",
            )
            db.update_talk_response_id(conn, t1, 42)
            # Task is 'pending', not 'pending_confirmation'
            result = db.get_pending_confirmation_by_response_id(conn, 42)
            assert result is None


class TestFailStuckLockedRunningTasks:
    def test_fails_old_locked_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            # Simulate a task locked 60+ minutes ago, created 90+ minutes ago
            conn.execute(
                """UPDATE tasks SET status = 'locked',
                   locked_at = datetime('now', '-45 minutes'),
                   created_at = datetime('now', '-90 minutes')
                WHERE id = ?""",
                (task_id,),
            )
            conn.commit()

            failed = db.fail_stuck_locked_running_tasks(conn)
            assert len(failed) == 1
            assert failed[0]["id"] == task_id
            assert db.get_task(conn, task_id).status == "failed"

    def test_releases_recent_locked_task_for_retry(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            # Locked 45 min ago but created recently (within max_retry_age)
            conn.execute(
                """UPDATE tasks SET status = 'locked',
                   locked_at = datetime('now', '-45 minutes'),
                   created_at = datetime('now', '-30 minutes')
                WHERE id = ?""",
                (task_id,),
            )
            conn.commit()

            failed = db.fail_stuck_locked_running_tasks(conn)
            assert len(failed) == 0  # released, not failed
            assert db.get_task(conn, task_id).status == "pending"

    def test_fails_old_running_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            conn.execute(
                """UPDATE tasks SET status = 'running',
                   started_at = datetime('now', '-20 minutes'),
                   created_at = datetime('now', '-90 minutes')
                WHERE id = ?""",
                (task_id,),
            )
            conn.commit()

            failed = db.fail_stuck_locked_running_tasks(conn)
            assert len(failed) == 1
            task = db.get_task(conn, task_id)
            assert task.status == "failed"

    def test_no_stuck_tasks_returns_empty(self, db_path):
        with db.get_db(db_path) as conn:
            # Create a normal pending task
            db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            failed = db.fail_stuck_locked_running_tasks(conn)
            assert failed == []

    def test_unblocks_channel_gate(self, db_path):
        """After recovering a stuck task, the channel gate should be clear."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hello", user_id="alice",
                conversation_token="room1", queue="foreground",
            )
            conn.execute(
                """UPDATE tasks SET status = 'running',
                   started_at = datetime('now', '-20 minutes'),
                   created_at = datetime('now', '-90 minutes')
                WHERE id = ?""",
                (task_id,),
            )
            conn.commit()

            assert db.has_active_foreground_task_for_channel(conn, "room1") is True
            db.fail_stuck_locked_running_tasks(conn)
            assert db.has_active_foreground_task_for_channel(conn, "room1") is False


class TestTrustedEmailSendersDB:
    def test_add_trusted_sender(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.add_trusted_sender(conn, "alice", "joe@example.com") is True

    def test_add_duplicate_returns_false(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "joe@example.com")
            assert db.add_trusted_sender(conn, "alice", "joe@example.com") is False

    def test_add_is_case_insensitive(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "Joe@Example.COM")
            assert db.is_sender_trusted_in_db(conn, "alice", "joe@example.com") is True

    def test_remove_trusted_sender(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "joe@example.com")
            assert db.remove_trusted_sender(conn, "alice", "joe@example.com") is True
            assert db.is_sender_trusted_in_db(conn, "alice", "joe@example.com") is False

    def test_remove_nonexistent_returns_false(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.remove_trusted_sender(conn, "alice", "nobody@example.com") is False

    def test_list_trusted_senders(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "bob@example.com")
            db.add_trusted_sender(conn, "alice", "alice@example.com")
            senders = db.list_trusted_senders(conn, "alice")
            assert len(senders) == 2
            assert senders[0]["sender_email"] == "alice@example.com"  # sorted
            assert senders[1]["sender_email"] == "bob@example.com"

    def test_list_empty(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.list_trusted_senders(conn, "alice") == []

    def test_is_sender_trusted_in_db(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.is_sender_trusted_in_db(conn, "alice", "joe@example.com") is False
            db.add_trusted_sender(conn, "alice", "joe@example.com")
            assert db.is_sender_trusted_in_db(conn, "alice", "joe@example.com") is True

    def test_user_isolation(self, db_path):
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "joe@example.com")
            assert db.is_sender_trusted_in_db(conn, "bob", "joe@example.com") is False


class TestSaveAndGetRecentConversationSkills:
    def test_empty_when_no_prior_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            result = db.get_recent_conversation_skills(conn, "room1")
            assert result == set()

    def test_returns_skills_from_recent_completed_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="list gmail", user_id="alice",
                conversation_token="room1",
            )
            db.update_task_status(conn, task_id, "completed", result="done")
            db.save_task_selected_skills(conn, task_id, ["files", "google_workspace"])
            result = db.get_recent_conversation_skills(conn, "room1")
            assert result == {"files", "google_workspace"}

    def test_excludes_current_task(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="list gmail", user_id="alice",
                conversation_token="room1",
            )
            db.update_task_status(conn, task_id, "completed", result="done")
            db.save_task_selected_skills(conn, task_id, ["google_workspace"])
            result = db.get_recent_conversation_skills(
                conn, "room1", exclude_task_id=task_id,
            )
            assert result == set()

    def test_respects_max_age(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="old task", user_id="alice",
                conversation_token="room1",
            )
            db.update_task_status(conn, task_id, "completed", result="done")
            db.save_task_selected_skills(conn, task_id, ["google_workspace"])
            # Backdate the task
            conn.execute(
                "UPDATE tasks SET created_at = datetime('now', '-60 minutes') WHERE id = ?",
                (task_id,),
            )
            result = db.get_recent_conversation_skills(
                conn, "room1", max_age_minutes=30,
            )
            assert result == set()

    def test_respects_limit(self, db_path):
        with db.get_db(db_path) as conn:
            for i in range(3):
                tid = db.create_task(
                    conn, prompt=f"task {i}", user_id="alice",
                    conversation_token="room1",
                )
                db.update_task_status(conn, tid, "completed", result="done")
                db.save_task_selected_skills(conn, tid, [f"skill_{i}"])
            # limit=1 should only get the most recent
            result = db.get_recent_conversation_skills(conn, "room1", limit=1)
            assert result == {"skill_2"}

    def test_unions_skills_from_multiple_tasks(self, db_path):
        with db.get_db(db_path) as conn:
            for skills in [["calendar"], ["google_workspace", "email"]]:
                tid = db.create_task(
                    conn, prompt="test", user_id="alice",
                    conversation_token="room1",
                )
                db.update_task_status(conn, tid, "completed", result="done")
                db.save_task_selected_skills(conn, tid, skills)
            result = db.get_recent_conversation_skills(conn, "room1")
            assert result == {"calendar", "google_workspace", "email"}

    def test_ignores_null_selected_skills(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="test", user_id="alice",
                conversation_token="room1",
            )
            db.update_task_status(conn, task_id, "completed", result="done")
            # No save_task_selected_skills — column stays NULL
            result = db.get_recent_conversation_skills(conn, "room1")
            assert result == set()

    def test_ignores_different_conversation(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="test", user_id="alice",
                conversation_token="room2",
            )
            db.update_task_status(conn, task_id, "completed", result="done")
            db.save_task_selected_skills(conn, task_id, ["google_workspace"])
            result = db.get_recent_conversation_skills(conn, "room1")
            assert result == set()
