"""Tests for Talk conversation polling and task creation."""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from istota import db
from istota.config import Config, NextcloudConfig, SchedulerConfig, TalkConfig, UserConfig
from istota.talk_poller import (
    _get_participants,
    _is_multi_user,
    _participant_cache,
    _participant_names,
    clean_message_content,
    extract_attachments,
    handle_confirmation_reply,
    is_bot_mentioned,
    poll_talk_conversations,
)


@pytest.fixture
def db_path(tmp_path):
    """Create and initialize a temporary SQLite database."""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def make_config(db_path, tmp_path):
    """Create a Config object with tmp paths and test DB."""
    def _make(**overrides):
        config = Config()
        config.db_path = db_path
        config.temp_dir = tmp_path / "temp"
        config.temp_dir.mkdir(exist_ok=True)
        config.skills_dir = tmp_path / "skills"
        config.skills_dir.mkdir(exist_ok=True)
        config.talk = TalkConfig(enabled=True, bot_username="istota")
        config.nextcloud = NextcloudConfig(
            url="https://nc.test", username="istota", app_password="pass"
        )
        config.users = {"alice": UserConfig()}
        config.scheduler = SchedulerConfig()
        for key, val in overrides.items():
            setattr(config, key, val)
        return config
    return _make


def _msg(
    id=100,
    actor_id="alice",
    actor_type="users",
    message="Hello istota",
    message_type="comment",
    message_params=None,
    parent=None,
):
    """Build a Talk message dict."""
    msg = {
        "id": id,
        "actorId": actor_id,
        "actorType": actor_type,
        "message": message,
        "messageType": message_type,
        "messageParameters": message_params if message_params is not None else {},
    }
    if parent is not None:
        msg["parent"] = parent
    return msg


# =============================================================================
# TestExtractAttachments
# =============================================================================


class TestExtractAttachments:
    def test_file_attachment(self):
        msg = _msg(message_params={"file0": {"name": "photo.jpg", "type": "file"}})
        result = extract_attachments(msg)
        assert result == ["Talk/photo.jpg"]

    def test_multiple_attachments(self):
        msg = _msg(message_params={
            "file0": {"name": "a.jpg", "type": "file"},
            "file1": {"name": "b.pdf", "type": "file"},
        })
        result = extract_attachments(msg)
        assert len(result) == 2
        assert "Talk/a.jpg" in result
        assert "Talk/b.pdf" in result

    def test_no_attachments(self):
        msg = _msg(message_params={})
        result = extract_attachments(msg)
        assert result == []

    def test_empty_parameters(self):
        msg = _msg()
        msg["messageParameters"] = {}
        result = extract_attachments(msg)
        assert result == []

    def test_non_file_parameters(self):
        msg = _msg(message_params={"mention-user0": {"type": "user", "id": "alice"}})
        result = extract_attachments(msg)
        assert result == []

    def test_parameters_is_list(self):
        """messageParameters can be an empty list [] when no params exist."""
        msg = _msg()
        msg["messageParameters"] = []
        result = extract_attachments(msg)
        assert result == []


# =============================================================================
# TestCleanMessageContent
# =============================================================================


class TestCleanMessageContent:
    def test_replace_file_placeholder(self):
        msg = _msg(
            message="{file0}",
            message_params={"file0": {"name": "report.pdf"}},
        )
        result = clean_message_content(msg)
        assert result == "[report.pdf]"

    def test_multiple_placeholders(self):
        msg = _msg(
            message="Check {file0} and {file1}",
            message_params={
                "file0": {"name": "a.txt"},
                "file1": {"name": "b.txt"},
            },
        )
        result = clean_message_content(msg)
        assert result == "Check [a.txt] and [b.txt]"

    def test_no_placeholders(self):
        msg = _msg(message="Just a regular message")
        result = clean_message_content(msg)
        assert result == "Just a regular message"

    def test_parameters_is_list(self):
        """When messageParameters is an empty list, return message as-is."""
        msg = _msg(message="Hello {file0}")
        msg["messageParameters"] = []
        result = clean_message_content(msg)
        assert result == "Hello {file0}"

    def test_missing_parameter(self):
        """Placeholder without matching param is left as-is."""
        msg = _msg(
            message="Check {file0}",
            message_params={},
        )
        result = clean_message_content(msg)
        assert result == "Check {file0}"


# =============================================================================
# TestHandleConfirmationReply
# =============================================================================


class TestHandleConfirmationReply:
    @pytest.mark.asyncio
    async def test_affirmative_confirms_task(self, make_config):
        config = make_config()

        # Create a task and set it to pending_confirmation
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Do something", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.set_task_confirmation(conn, task_id, "Please confirm")

        with db.get_db(config.db_path) as conn:
            result = await handle_confirmation_reply(
                conn, config, "alice", "yes", "room1"
            )

        assert result is True

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
            assert task.status == "pending"

    @pytest.mark.asyncio
    async def test_negative_cancels_task(self, make_config):
        config = make_config()

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Do something", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.set_task_confirmation(conn, task_id, "Please confirm")

        with (
            db.get_db(config.db_path) as conn,
            patch("istota.talk_poller.TalkClient") as MockClient,
        ):
            mock_instance = MockClient.return_value
            mock_instance.send_message = AsyncMock()
            result = await handle_confirmation_reply(
                conn, config, "alice", "no", "room1"
            )

        assert result is True

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
            assert task.status == "cancelled"

    @pytest.mark.asyncio
    async def test_non_confirmation_returns_false(self, make_config):
        config = make_config()

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Do something", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.set_task_confirmation(conn, task_id, "Please confirm")

        with db.get_db(config.db_path) as conn:
            result = await handle_confirmation_reply(
                conn, config, "alice", "what do you mean?", "room1"
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_no_pending_task_returns_false(self, make_config):
        config = make_config()

        with db.get_db(config.db_path) as conn:
            result = await handle_confirmation_reply(
                conn, config, "alice", "yes", "room1"
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_wrong_user_returns_false(self, make_config):
        config = make_config()

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Do something", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.set_task_confirmation(conn, task_id, "Please confirm")

        with db.get_db(config.db_path) as conn:
            result = await handle_confirmation_reply(
                conn, config, "bob", "yes", "room1"
            )

        assert result is False

        # Task should still be pending_confirmation
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
            assert task.status == "pending_confirmation"

    @pytest.mark.asyncio
    async def test_case_insensitive(self, make_config):
        config = make_config()

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Do something", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.set_task_confirmation(conn, task_id, "Please confirm")

        with db.get_db(config.db_path) as conn:
            result = await handle_confirmation_reply(
                conn, config, "alice", "YES", "room1"
            )

        assert result is True

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
            assert task.status == "pending"


# =============================================================================
# TestPollTalkConversations
# =============================================================================


class TestPollTalkConversations:
    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self, make_config):
        config = make_config()
        config.talk = TalkConfig(enabled=False)

        result = await poll_talk_conversations(config)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_url_returns_empty(self, make_config):
        config = make_config()
        config.nextcloud = NextcloudConfig(url="", username="istota", app_password="pass")

        result = await poll_talk_conversations(config)
        assert result == []

    @pytest.mark.asyncio
    async def test_filters_system_messages(self, make_config):
        config = make_config()

        system_msg = _msg(message_type="system", actor_id="alice")

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[system_msg])

            # Pre-set poll state so we don't hit first-poll logic
            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert result == []

    @pytest.mark.asyncio
    async def test_filters_bot_messages(self, make_config):
        config = make_config()

        bot_msg = _msg(actor_id="istota")

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[bot_msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert result == []

    @pytest.mark.asyncio
    async def test_filters_unknown_users(self, make_config):
        config = make_config()

        unknown_msg = _msg(actor_id="stranger")

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[unknown_msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert result == []

    @pytest.mark.asyncio
    async def test_creates_task_for_valid_message(self, make_config):
        config = make_config()

        msg = _msg(id=101, actor_id="alice", message="Check my calendar")

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, result[0])
            assert task.user_id == "alice"
            assert task.source_type == "talk"
            assert task.prompt == "Check my calendar"
            assert task.conversation_token == "room1"
            assert task.talk_message_id == 101

    @pytest.mark.asyncio
    async def test_dm_first_poll_fetches_history(self, make_config):
        config = make_config()

        msg = _msg(id=200, actor_id="alice", message="Hello")

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "dm1", "type": 1},  # type 1 = DM
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])

            # No poll state set -> first poll
            result = await poll_talk_conversations(config)

        # DM first poll sets last_message_id=0 and polls
        assert len(result) == 1
        mock_instance.poll_messages.assert_called_once()

    @pytest.mark.asyncio
    async def test_group_first_poll_picks_up_latest_message(self, make_config):
        config = make_config()

        msg = _msg(id=500, actor_id="alice", message="Hello from new room")

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "group1", "type": 2},  # type 2 = group
            ])
            mock_instance.get_latest_message_id = AsyncMock(return_value=500)
            mock_instance.poll_messages = AsyncMock(return_value=[msg])

            # No poll state -> first poll for group room
            result = await poll_talk_conversations(config)

        # Group first poll should poll with latest_id - 1 to pick up latest message
        assert len(result) == 1
        mock_instance.get_latest_message_id.assert_called_once_with("group1")
        # poll_messages SHOULD be called with latest_id - 1
        mock_instance.poll_messages.assert_called_once()
        call_args = mock_instance.poll_messages.call_args
        assert call_args.kwargs["last_known_message_id"] == 499  # latest_id - 1

    @pytest.mark.asyncio
    async def test_group_first_poll_no_messages_yet(self, make_config):
        config = make_config()

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "group1", "type": 2},
            ])
            mock_instance.get_latest_message_id = AsyncMock(return_value=None)
            mock_instance.poll_messages = AsyncMock(return_value=[])

            result = await poll_talk_conversations(config)

        # No messages yet - should still poll with last_message_id=0
        assert result == []
        mock_instance.poll_messages.assert_called_once()
        call_args = mock_instance.poll_messages.call_args
        assert call_args.kwargs["last_known_message_id"] == 0

    @pytest.mark.asyncio
    async def test_group_first_poll_error_skips_room(self, make_config):
        config = make_config()

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "group1", "type": 2},
            ])
            mock_instance.get_latest_message_id = AsyncMock(side_effect=Exception("API error"))
            mock_instance.poll_messages = AsyncMock(return_value=[])

            result = await poll_talk_conversations(config)

        # On error, room should be skipped (continue)
        assert result == []
        mock_instance.poll_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_extracts_reply_metadata(self, make_config):
        config = make_config()

        msg = _msg(
            id=300,
            actor_id="alice",
            message="Follow up on that",
            parent={"id": 250, "message": "Original message content", "deleted": False},
        )

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, result[0])
            assert task.reply_to_talk_id == 250
            assert task.reply_to_content == "Original message content"

    @pytest.mark.asyncio
    async def test_slow_room_does_not_block_fast_room(self, make_config):
        """A quiet room long-polling should not delay processing of a room with messages."""
        config = make_config()
        config.users = {"alice": UserConfig(), "bob": UserConfig()}
        config.scheduler.talk_poll_wait = 0.5  # short wait for test

        fast_msg = _msg(id=101, actor_id="alice", message="Hello")

        async def slow_poll(token, last_known_message_id=None, timeout=30):
            """Simulate a quiet room that blocks for the full long-poll timeout."""
            await asyncio.sleep(10)  # would block for 10s without wait()
            return []

        async def fast_poll(token, last_known_message_id=None, timeout=30):
            """Simulate a room with an immediate new message."""
            return [fast_msg]

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "fast_room", "type": 1},
                {"token": "slow_room", "type": 1},
            ])

            # Route poll_messages based on conversation token
            async def route_poll(token, **kwargs):
                if token == "fast_room":
                    return await fast_poll(token, **kwargs)
                return await slow_poll(token, **kwargs)

            mock_instance.poll_messages = AsyncMock(side_effect=route_poll)

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "fast_room", 50)
                db.set_talk_poll_state(conn, "slow_room", 50)

            import time
            start = time.monotonic()
            result = await poll_talk_conversations(config)
            elapsed = time.monotonic() - start

        # Fast room's message should have been processed
        assert len(result) == 1
        # Should complete in roughly talk_poll_wait, not 10+ seconds
        assert elapsed < 3.0

    @pytest.mark.asyncio
    async def test_cancelled_slow_rooms_no_errors(self, make_config):
        """Cancelling pending slow rooms should not raise errors."""
        config = make_config()
        config.scheduler.talk_poll_wait = 0.1
        config.scheduler.talk_poll_timeout = 0.2  # short timeout so test doesn't block

        async def slow_poll(token, last_known_message_id=None, timeout=30):
            await asyncio.sleep(10)
            return []

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
                {"token": "room2", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(side_effect=slow_poll)

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)
                db.set_talk_poll_state(conn, "room2", 50)

            # Should not raise any exceptions
            result = await poll_talk_conversations(config)

        # No messages from either room
        assert result == []


# =============================================================================
# TestIsBotMentioned
# =============================================================================


class TestIsBotMentioned:
    def test_direct_mention(self):
        msg = _msg(message_params={
            "mention-user0": {"type": "user", "id": "istota", "name": "Istota"},
        })
        assert is_bot_mentioned(msg, "istota") is True

    def test_no_mention(self):
        msg = _msg(message_params={})
        assert is_bot_mentioned(msg, "istota") is False

    def test_other_user_mentioned(self):
        msg = _msg(message_params={
            "mention-user0": {"type": "user", "id": "alice", "name": "Alice"},
        })
        assert is_bot_mentioned(msg, "istota") is False

    def test_mention_call_excluded(self):
        """@all mentions should not count as bot mention."""
        msg = _msg(message_params={
            "mention-call0": {"type": "call", "id": "room1", "name": "All"},
        })
        assert is_bot_mentioned(msg, "istota") is False

    def test_multiple_mentions_bot_present(self):
        msg = _msg(message_params={
            "mention-user0": {"type": "user", "id": "alice", "name": "Alice"},
            "mention-user1": {"type": "user", "id": "istota", "name": "Istota"},
        })
        assert is_bot_mentioned(msg, "istota") is True

    def test_empty_params_list(self):
        """messageParameters can be an empty list."""
        msg = _msg()
        msg["messageParameters"] = []
        assert is_bot_mentioned(msg, "istota") is False

    def test_federated_user_mention(self):
        msg = _msg(message_params={
            "mention-federated-user0": {"type": "user", "id": "istota", "name": "Istota"},
        })
        assert is_bot_mentioned(msg, "istota") is True


# =============================================================================
# TestCleanMessageContentMentions
# =============================================================================


class TestCleanMessageContentMentions:
    def test_bot_mention_stripped(self):
        msg = _msg(
            message="{mention-user0} what's the weather?",
            message_params={
                "mention-user0": {"type": "user", "id": "istota", "name": "Istota"},
            },
        )
        result = clean_message_content(msg, bot_username="istota")
        assert result == "what's the weather?"

    def test_other_mention_replaced_with_display_name(self):
        msg = _msg(
            message="{mention-user0} can you ask {mention-user1} about the meeting?",
            message_params={
                "mention-user0": {"type": "user", "id": "istota", "name": "Istota"},
                "mention-user1": {"type": "user", "id": "alice", "name": "Alice"},
            },
        )
        result = clean_message_content(msg, bot_username="istota")
        assert "Istota" not in result
        assert "@Alice" in result
        assert "about the meeting?" in result

    def test_no_bot_username_preserves_all(self):
        """When bot_username is None, mention placeholders are not processed."""
        msg = _msg(
            message="{mention-user0} hello",
            message_params={
                "mention-user0": {"type": "user", "id": "istota", "name": "Istota"},
            },
        )
        result = clean_message_content(msg)
        assert result == "{mention-user0} hello"

    def test_mention_call_preserved(self):
        """@all mentions are replaced with display name, not stripped."""
        msg = _msg(
            message="{mention-call0} meeting in 5 mins",
            message_params={
                "mention-call0": {"type": "call", "id": "room1", "name": "Engineering"},
            },
        )
        result = clean_message_content(msg, bot_username="istota")
        assert "@Engineering" in result


# =============================================================================
# TestIsMultiUserRoom
# =============================================================================


class TestGetParticipantsAndMultiUser:
    @pytest.mark.asyncio
    async def test_type_1_returns_empty(self):
        client = MagicMock()
        result = await _get_participants(client, "dm1", 1)
        assert result == []
        client.get_participants.assert_not_called()

    @pytest.mark.asyncio
    async def test_type_2_with_2_participants(self):
        _participant_cache.clear()
        client = MagicMock()
        client.get_participants = AsyncMock(return_value=[
            {"actorId": "alice", "displayName": "Alice"},
            {"actorId": "istota", "displayName": "Istota"},
        ])
        participants = await _get_participants(client, "room1", 2)
        assert len(participants) == 2
        assert _is_multi_user(participants) is False

    @pytest.mark.asyncio
    async def test_type_2_with_3_participants(self):
        _participant_cache.clear()
        client = MagicMock()
        client.get_participants = AsyncMock(return_value=[
            {"actorId": "alice", "displayName": "Alice"},
            {"actorId": "bob", "displayName": "Bob"},
            {"actorId": "istota", "displayName": "Istota"},
        ])
        participants = await _get_participants(client, "room2", 2)
        assert _is_multi_user(participants) is True

    @pytest.mark.asyncio
    async def test_caching(self):
        _participant_cache.clear()
        client = MagicMock()
        client.get_participants = AsyncMock(return_value=[
            {"actorId": "alice", "displayName": "Alice"},
            {"actorId": "bob", "displayName": "Bob"},
            {"actorId": "istota", "displayName": "Istota"},
        ])
        # First call
        p1 = await _get_participants(client, "room3", 2)
        assert _is_multi_user(p1) is True
        assert client.get_participants.call_count == 1

        # Second call should use cache
        p2 = await _get_participants(client, "room3", 2)
        assert _is_multi_user(p2) is True
        assert client.get_participants.call_count == 1  # still 1

    @pytest.mark.asyncio
    async def test_api_error_falls_back_to_empty(self):
        _participant_cache.clear()
        client = MagicMock()
        client.get_participants = AsyncMock(side_effect=Exception("API error"))
        participants = await _get_participants(client, "room4", 2)
        assert participants == []
        assert _is_multi_user(participants) is False


class TestParticipantNames:
    def test_extracts_display_names(self):
        participants = [
            {"actorId": "alice", "displayName": "Alice"},
            {"actorId": "bob", "displayName": "Bob"},
        ]
        assert _participant_names(participants) == ["Alice", "Bob"]

    def test_excludes_actor(self):
        participants = [
            {"actorId": "alice", "displayName": "Alice"},
            {"actorId": "istota", "displayName": "Istota"},
        ]
        assert _participant_names(participants, exclude="istota") == ["Alice"]

    def test_falls_back_to_actor_id(self):
        participants = [{"actorId": "alice", "displayName": ""}]
        assert _participant_names(participants) == ["alice"]


# =============================================================================
# TestPollTalkConversationsGroupRoom
# =============================================================================


class TestPollTalkConversationsGroupRoom:
    @pytest.mark.asyncio
    async def test_group_room_skips_without_mention(self, make_config):
        """In a 3+ person room, messages without @mention are skipped."""
        _participant_cache.clear()
        config = make_config()
        config.users = {"alice": UserConfig(), "bob": UserConfig()}

        msg = _msg(id=101, actor_id="alice", message="Just chatting")

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "group1", "type": 2},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])
            mock_instance.get_participants = AsyncMock(return_value=[
                {"actorId": "alice", "displayName": "Alice"},
                {"actorId": "bob", "displayName": "Bob"},
                {"actorId": "istota", "displayName": "Istota"},
            ])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "group1", 50)

            result = await poll_talk_conversations(config)

        assert result == []

    @pytest.mark.asyncio
    async def test_group_room_processes_with_mention(self, make_config):
        """In a 3+ person room, messages with @mention are processed."""
        _participant_cache.clear()
        config = make_config()
        config.users = {"alice": UserConfig(), "bob": UserConfig()}

        msg = _msg(
            id=102,
            actor_id="alice",
            message="{mention-user0} check my calendar",
            message_params={
                "mention-user0": {"type": "user", "id": "istota", "name": "Istota"},
            },
        )

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "group1", "type": 2},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])
            mock_instance.get_participants = AsyncMock(return_value=[
                {"actorId": "alice", "displayName": "Alice"},
                {"actorId": "bob", "displayName": "Bob"},
                {"actorId": "istota", "displayName": "Istota"},
            ])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "group1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, result[0])
            assert task.is_group_chat is True
            # Bot mention should be stripped from prompt
            assert "istota" not in task.prompt.lower()
            assert "check my calendar" in task.prompt
            # Participant names should be in the prompt
            assert "Alice" in task.prompt
            assert "Bob" in task.prompt

    @pytest.mark.asyncio
    async def test_two_person_group_acts_like_dm(self, make_config):
        """A type-2 room with only 2 participants doesn't require mention."""
        _participant_cache.clear()
        config = make_config()

        msg = _msg(id=103, actor_id="alice", message="Hello there")

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 2},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])
            mock_instance.get_participants = AsyncMock(return_value=[
                {"actorId": "alice", "displayName": "Alice"},
                {"actorId": "istota", "displayName": "Istota"},
            ])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, result[0])
            assert task.is_group_chat is False
            # No participant context for DM-like rooms
            assert "[Room participants:" not in task.prompt

    @pytest.mark.asyncio
    async def test_dm_unchanged(self, make_config):
        """Type-1 DM always processes without mention."""
        _participant_cache.clear()
        config = make_config()

        msg = _msg(id=104, actor_id="alice", message="Hello")

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "dm1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "dm1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1
        # get_participants should not be called for type 1
        mock_instance.get_participants.assert_not_called()


class TestChannelGate:
    """Per-channel gate: reject duplicate foreground tasks."""

    @pytest.mark.asyncio
    async def test_channel_gate_rejects_when_active_task(self, make_config):
        """When an active fg task exists for the channel, reject and send 'still working'."""
        config = make_config()

        # Pre-create an active foreground task for room1
        with db.get_db(config.db_path) as conn:
            db.create_task(
                conn, prompt="previous request", user_id="alice",
                source_type="talk", conversation_token="room1", queue="foreground",
            )

        msg = _msg(id=200, actor_id="alice", message="Another request")

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])
            mock_instance.send_message = AsyncMock()

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        # No new task should be created
        assert result == []
        # Bot should have sent "still working" message
        mock_instance.send_message.assert_called_once()
        call_args = mock_instance.send_message.call_args
        assert "room1" == call_args[0][0]
        assert "still working" in call_args[0][1].lower() or "previous request" in call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_channel_gate_allows_when_no_active_task(self, make_config):
        """When no active fg task exists, message is processed normally."""
        config = make_config()

        msg = _msg(id=200, actor_id="alice", message="New request")

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_channel_gate_allows_after_task_completes(self, make_config):
        """Completed tasks don't block new ones."""
        config = make_config()

        # Create and complete a task
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="old request", user_id="alice",
                source_type="talk", conversation_token="room1", queue="foreground",
            )
            db.update_task_status(conn, task_id, "completed", result="done")

        msg = _msg(id=200, actor_id="alice", message="New request")

        with patch("istota.talk_poller.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1
