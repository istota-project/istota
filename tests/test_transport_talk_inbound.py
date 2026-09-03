"""Tests for Talk conversation polling and task creation."""

import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from istota import db
from istota.brain.claude_code import OPUS
from istota.config import Config, NextcloudConfig, SchedulerConfig, TalkConfig, UserConfig
from istota.transport.talk import inbound as _talk_poller_mod
from istota.transport.talk.inbound import (
    _get_participants,
    _is_multi_user,
    _participant_cache,
    _dm_token_cache,
    _participant_names,
    clean_message_content,
    extract_attachments,
    get_dm_token,
    handle_confirmation_reply,
    is_bot_mentioned,
    poll_talk_conversations,
)


@pytest.fixture(autouse=True)
def _reset_poller_caches():
    """Reset module-level caches between tests."""
    _participant_cache.clear()
    _talk_poller_mod._conversation_cache = None
    _talk_poller_mod._last_full_sweep = None
    _dm_token_cache.clear()
    yield
    _participant_cache.clear()
    _talk_poller_mod._conversation_cache = None
    _talk_poller_mod._last_full_sweep = None
    _dm_token_cache.clear()


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

    def test_path_traversal_in_filename(self):
        """Filenames with directory traversal components should be stripped."""
        msg = _msg(message_params={"file0": {"name": "../../etc/passwd", "type": "file"}})
        result = extract_attachments(msg)
        assert result == ["Talk/passwd"]

    def test_path_traversal_absolute(self):
        msg = _msg(message_params={"file0": {"name": "/etc/shadow", "type": "file"}})
        result = extract_attachments(msg)
        assert result == ["Talk/shadow"]

    def test_empty_after_sanitization(self):
        """A filename that resolves to empty after stripping should be skipped."""
        msg = _msg(message_params={"file0": {"name": "../../", "type": "file"}})
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
            patch("istota.transport.talk.inbound.get_talk_client") as MockClient,
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


class TestConfirmAndTrust:
    """Tests for 'yes trust' confirmation flow that also trusts the sender."""

    @pytest.mark.asyncio
    async def test_yes_trust_confirms_and_trusts_sender(self, make_config):
        config = make_config()

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Email from stranger@evil.com", user_id="alice",
                source_type="email", conversation_token="thread1",
            )
            db.set_task_confirmation(conn, task_id, "Email from unknown sender")
            db.mark_email_processed(
                conn, email_id="e1", sender_email="stranger@evil.com",
                subject="Hi", thread_id="t1", message_id="<m1@evil.com>",
                references=None, user_id="alice", task_id=task_id,
                routing_method="plus_address",
            )

        with (
            db.get_db(config.db_path) as conn,
            patch("istota.transport.talk.inbound.get_talk_client") as MockClient,
        ):
            mock_instance = MockClient.return_value
            mock_instance.send_message = AsyncMock()
            result = await handle_confirmation_reply(
                conn, config, "alice", "yes trust", "alerts-room"
            )

        assert result is True

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
            assert task.status == "pending"
            assert db.is_sender_trusted_in_db(conn, "alice", "stranger@evil.com") is True

    @pytest.mark.asyncio
    async def test_yes_comma_trust_variant(self, make_config):
        config = make_config()

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Email", user_id="alice",
                source_type="email", conversation_token="thread1",
            )
            db.set_task_confirmation(conn, task_id, "Confirm?")
            db.mark_email_processed(
                conn, email_id="e2", sender_email="joe@example.com",
                subject="Hi", thread_id="t2", message_id="<m2@x.com>",
                references=None, user_id="alice", task_id=task_id,
                routing_method="plus_address",
            )

        with (
            db.get_db(config.db_path) as conn,
            patch("istota.transport.talk.inbound.get_talk_client") as MockClient,
        ):
            mock_instance = MockClient.return_value
            mock_instance.send_message = AsyncMock()
            await handle_confirmation_reply(
                conn, config, "alice", "yes, trust", "alerts-room"
            )

        with db.get_db(config.db_path) as conn:
            assert db.is_sender_trusted_in_db(conn, "alice", "joe@example.com") is True

    @pytest.mark.asyncio
    async def test_plain_yes_does_not_trust(self, make_config):
        config = make_config()

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Email", user_id="alice",
                source_type="email", conversation_token="thread1",
            )
            db.set_task_confirmation(conn, task_id, "Confirm?")
            db.mark_email_processed(
                conn, email_id="e3", sender_email="stranger@evil.com",
                subject="Hi", thread_id="t3", message_id="<m3@evil.com>",
                references=None, user_id="alice", task_id=task_id,
                routing_method="plus_address",
            )

        with db.get_db(config.db_path) as conn:
            await handle_confirmation_reply(
                conn, config, "alice", "yes", "alerts-room"
            )

        with db.get_db(config.db_path) as conn:
            assert db.is_sender_trusted_in_db(conn, "alice", "stranger@evil.com") is False

    @pytest.mark.asyncio
    async def test_yes_trust_on_non_email_task_does_not_crash(self, make_config):
        """'yes trust' on a Talk task should just confirm without trusting."""
        config = make_config()

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Do something", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.set_task_confirmation(conn, task_id, "Please confirm")

        with db.get_db(config.db_path) as conn:
            result = await handle_confirmation_reply(
                conn, config, "alice", "yes trust", "room1"
            )

        assert result is True
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
            assert task.status == "pending"


class TestCrossConversationConfirmation:
    """Tests for reply-to-specific and cross-conversation confirmation paths."""

    @pytest.mark.asyncio
    async def test_reply_to_specific_confirmation_message(self, make_config):
        """Path A: user replies to the exact confirmation prompt message."""
        config = make_config()

        with db.get_db(config.db_path) as conn:
            t1 = db.create_task(
                conn, prompt="email task", user_id="alice",
                source_type="email", conversation_token="email_thread_hash",
            )
            db.set_task_confirmation(conn, t1, "Confirm?")
            db.update_talk_response_id(conn, t1, 42)

        with db.get_db(config.db_path) as conn:
            result = await handle_confirmation_reply(
                conn, config, "alice", "yes", "alerts_room",
                reply_to_talk_id=42,
            )

        assert result is True
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, t1)
            assert task.status == "pending"

    @pytest.mark.asyncio
    async def test_same_conversation_still_works(self, make_config):
        """Path B: existing behavior — confirmation in the same conversation."""
        config = make_config()

        with db.get_db(config.db_path) as conn:
            t1 = db.create_task(
                conn, prompt="talk task", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.set_task_confirmation(conn, t1, "Confirm?")

        with db.get_db(config.db_path) as conn:
            result = await handle_confirmation_reply(
                conn, config, "alice", "yes", "room1",
            )

        assert result is True
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, t1)
            assert task.status == "pending"

    @pytest.mark.asyncio
    async def test_cross_conversation_fallback_by_user(self, make_config):
        """Path C: user says 'yes' in alerts channel, task is in a different conversation."""
        config = make_config()

        with db.get_db(config.db_path) as conn:
            t1 = db.create_task(
                conn, prompt="email task", user_id="alice",
                source_type="email", conversation_token="email_thread_hash",
            )
            db.set_task_confirmation(conn, t1, "Confirm?")

        with db.get_db(config.db_path) as conn:
            result = await handle_confirmation_reply(
                conn, config, "alice", "yes", "alerts_room",
            )

        assert result is True
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, t1)
            assert task.status == "pending"

    @pytest.mark.asyncio
    async def test_multiple_pending_refuses_to_guess(self, make_config):
        """Path C with several pending: an unaddressed 'yes' answers none of them.

        This used to confirm the newest, which is the wrong question whenever a
        second gate arrived between the prompt the user read and the reply they
        typed — approving an untrusted email they were never shown (ISSUE-241).
        Path A (a reply to the prompt message) and Path B (same conversation)
        are still bound to a specific task and are unaffected; only the
        cross-conversation fallback is ambiguous, so only it defers.
        """
        config = make_config()
        client = MagicMock()
        client.send_message = AsyncMock(return_value=1)

        with db.get_db(config.db_path) as conn:
            t1 = db.create_task(
                conn, prompt="older", user_id="alice",
                source_type="email", conversation_token="thread1",
            )
            db.set_task_confirmation(conn, t1, "Confirm older?")
            t2 = db.create_task(
                conn, prompt="newer", user_id="alice",
                source_type="email", conversation_token="thread2",
            )
            db.set_task_confirmation(conn, t2, "Confirm newer?")

        with db.get_db(config.db_path) as conn:
            with patch(
                "istota.transport.talk.inbound.get_talk_client", return_value=client,
            ):
                result = await handle_confirmation_reply(
                    conn, config, "alice", "yes", "alerts_room",
                )

        # Handled, not passed through: falling through would turn "yes" into a
        # prompt for a new task.
        assert result is True
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, t1).status == "pending_confirmation"
            assert db.get_task(conn, t2).status == "pending_confirmation"
        posted = client.send_message.await_args[0][1]
        assert f"#{t1}" in posted and f"#{t2}" in posted

    @pytest.mark.asyncio
    async def test_reply_to_wrong_message_falls_through(self, make_config):
        """reply_to_talk_id doesn't match any pending task, falls through to path B/C."""
        config = make_config()

        with db.get_db(config.db_path) as conn:
            t1 = db.create_task(
                conn, prompt="email task", user_id="alice",
                source_type="email", conversation_token="email_thread",
            )
            db.set_task_confirmation(conn, t1, "Confirm?")
            db.update_talk_response_id(conn, t1, 42)

        with db.get_db(config.db_path) as conn:
            # reply_to_talk_id=999 doesn't match task's talk_response_id=42
            result = await handle_confirmation_reply(
                conn, config, "alice", "yes", "alerts_room",
                reply_to_talk_id=999,
            )

        assert result is True  # falls through to path C (user fallback)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, t1)
            assert task.status == "pending"

    @pytest.mark.asyncio
    async def test_no_pending_returns_false(self, make_config):
        """No pending confirmations anywhere — returns False."""
        config = make_config()

        with db.get_db(config.db_path) as conn:
            result = await handle_confirmation_reply(
                conn, config, "alice", "yes", "alerts_room",
            )

        assert result is False


# =============================================================================
# TestPollTalkConversations
# =============================================================================


class TestDmTokenCache:
    def test_get_dm_token_returns_none_when_empty(self):
        assert get_dm_token("alice") is None

    def test_get_dm_token_returns_cached_value(self):
        _dm_token_cache["alice"] = "dm_room_abc"
        assert get_dm_token("alice") == "dm_room_abc"

    @pytest.mark.asyncio
    async def test_dm_tokens_populated_during_poll(self, make_config):
        """1:1 conversations (type=1) should populate the DM token cache."""
        config = make_config()
        config.users = {"carol": UserConfig()}

        conversations = [
            {"token": "dm_carol", "type": 1, "name": "carol"},
            {"token": "group_room", "type": 2, "name": "Project Chat"},
        ]

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=conversations)
            mock_instance.poll_messages = AsyncMock(return_value=[])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "dm_carol", 50)
                db.set_talk_poll_state(conn, "group_room", 50)

            await poll_talk_conversations(config)

        assert get_dm_token("carol") == "dm_carol"
        assert get_dm_token("unknown") is None

    @pytest.mark.asyncio
    async def test_dm_cache_ignores_unknown_users(self, make_config):
        """1:1 conversations with users not in config are not cached."""
        config = make_config()
        config.users = {"carol": UserConfig()}

        conversations = [
            {"token": "dm_stranger", "type": 1, "name": "stranger"},
        ]

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=conversations)
            mock_instance.poll_messages = AsyncMock(return_value=[])

            await poll_talk_conversations(config)

        assert get_dm_token("stranger") is None


class TestRoomTitleBackfill:
    """The poller proactively backfills registry room titles from Talk's
    displayName each cycle — not only when a new inbound message arrives — so a
    migrated Talk room (folded in with a NULL name) stops showing the generic
    'Talk room' in web chat once the bot next polls."""

    @pytest.mark.asyncio
    async def test_null_name_talk_room_backfilled(self, make_config):
        config = make_config()
        config.users = {"alice": UserConfig()}
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "grouptok", "alice", origin="talk", name=None)
            db.set_talk_poll_state(conn, "grouptok", 50)

        conversations = [
            {"token": "grouptok", "type": 2, "displayName": "Project X"},
        ]
        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=conversations)
            mock_instance.poll_messages = AsyncMock(return_value=[])
            await poll_talk_conversations(config)

        with db.get_db(config.db_path) as conn:
            room = db.get_room(conn, "grouptok")
        assert room.name == "Project X"

    @pytest.mark.asyncio
    async def test_talk_side_rename_flows_back(self, make_config):
        config = make_config()
        config.users = {"alice": UserConfig()}
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "grouptok", "alice", origin="talk", name="Old")
            db.set_talk_poll_state(conn, "grouptok", 50)

        conversations = [
            {"token": "grouptok", "type": 2, "displayName": "New Name"},
        ]
        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=conversations)
            mock_instance.poll_messages = AsyncMock(return_value=[])
            await poll_talk_conversations(config)

        with db.get_db(config.db_path) as conn:
            room = db.get_room(conn, "grouptok")
        assert room.name == "New Name"

    @pytest.mark.asyncio
    async def test_room_with_no_istota_participants_not_registered(self, make_config):
        """A polled room whose participants don't map to any istota user (bot-
        only or all-guest) is not registered — there's no one to show it to."""
        config = make_config()
        config.users = {"alice": UserConfig()}
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "unseen", 50)

        conversations = [
            {"token": "unseen", "type": 2, "displayName": "Some Room"},
        ]
        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=conversations)
            mock_instance.poll_messages = AsyncMock(return_value=[])
            mock_instance.get_participants = AsyncMock(return_value=[
                {"actorId": "istota", "actorType": "users"},   # the bot
                {"actorId": "guest123", "actorType": "guests"},  # not an istota user
            ])
            await poll_talk_conversations(config)

        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, "unseen") is None


class TestPollRoomRegistration:
    """A Talk room the bot is in surfaces in web chat on the next poll, even
    when no one has messaged the bot in it (the #sysadmin case) — registration
    is seeded from the human participants, not from task history."""

    @pytest.mark.asyncio
    async def test_group_room_registered_and_members_seeded(self, make_config):
        config = make_config()
        config.users = {"alice": UserConfig(), "bob": UserConfig()}
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "grp", 50)

        conversations = [{"token": "grp", "type": 2, "displayName": "#sysadmin"}]
        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=conversations)
            mock_instance.poll_messages = AsyncMock(return_value=[])
            mock_instance.get_participants = AsyncMock(return_value=[
                {"actorId": "istota", "actorType": "users"},
                {"actorId": "alice", "actorType": "users"},
                {"actorId": "bob", "actorType": "users"},
            ])
            await poll_talk_conversations(config)

        with db.get_db(config.db_path) as conn:
            room = db.get_room(conn, "grp")
            assert room is not None and room.origin == "talk"
            assert room.name == "#sysadmin"
            assert sorted(db.list_room_members(conn, "grp")) == ["alice", "bob"]
            assert {r.token for r in db.list_member_rooms(conn, "alice")} == {"grp"}
            assert {r.token for r in db.list_member_rooms(conn, "bob")} == {"grp"}

    @pytest.mark.asyncio
    async def test_changelog_room_not_registered(self, make_config):
        """A type-4 'Talk updates' changelog room is a system room and must not
        surface in web chat."""
        config = make_config()
        config.users = {"alice": UserConfig()}
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "changelog", 50)

        conversations = [{"token": "changelog", "type": 4, "displayName": "Talk updates"}]
        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=conversations)
            mock_instance.poll_messages = AsyncMock(return_value=[])
            mock_instance.get_participants = AsyncMock(return_value=[
                {"actorId": "alice", "actorType": "users"},
            ])
            await poll_talk_conversations(config)

        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, "changelog") is None

    @pytest.mark.asyncio
    async def test_dm_registered_with_other_party(self, make_config):
        config = make_config()
        config.users = {"alice": UserConfig()}
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "dmtok", 50)

        conversations = [{"token": "dmtok", "type": 1, "name": "alice"}]
        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=conversations)
            mock_instance.poll_messages = AsyncMock(return_value=[])
            await poll_talk_conversations(config)

        with db.get_db(config.db_path) as conn:
            room = db.get_room(conn, "dmtok")
            assert room is not None and room.origin == "talk"
            assert db.list_room_members(conn, "dmtok") == ["alice"]

    @pytest.mark.asyncio
    async def test_hidden_existing_room_not_resurfaced_by_poll(self, make_config):
        """A polled room that already exists isn't re-seeded, so a hide (member
        dropped + tombstone) stays hidden across polls with no message."""
        config = make_config()
        config.users = {"alice": UserConfig()}
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "grp", "alice", origin="talk", name="#sysadmin")
            db.add_room_binding(conn, "grp", "talk", "grp")
            db.remove_room_member(conn, "grp", "alice")
            db.dismiss_room(conn, "grp", "alice")
            db.set_talk_poll_state(conn, "grp", 50)

        conversations = [{"token": "grp", "type": 2, "displayName": "#sysadmin"}]
        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=conversations)
            mock_instance.poll_messages = AsyncMock(return_value=[])
            mock_instance.get_participants = AsyncMock(return_value=[
                {"actorId": "alice", "actorType": "users"},
            ])
            await poll_talk_conversations(config)

        with db.get_db(config.db_path) as conn:
            assert not db.is_room_member(conn, "grp", "alice")
            assert db.is_room_dismissed(conn, "grp", "alice")
            assert db.list_member_rooms(conn, "alice") == []

    @pytest.mark.asyncio
    async def test_promoted_web_room_not_duplicated(self, make_config):
        """A promoted web room's canonical token is its web token (Talk token
        lives only in a binding). Polling the Talk token must NOT create a
        phantom duplicate origin='talk' room."""
        config = make_config()
        config.users = {"alice": UserConfig()}
        web_token, talk_token = "web-alice-uuid", "nc_talk_xyz"
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, web_token, "alice", origin="web", name="My Room")
            db.add_room_binding(conn, web_token, "talk", talk_token)
            db.set_talk_poll_state(conn, talk_token, 50)

        conversations = [{"token": talk_token, "type": 2, "displayName": "My Room"}]
        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=conversations)
            mock_instance.poll_messages = AsyncMock(return_value=[])
            mock_instance.get_participants = AsyncMock(return_value=[
                {"actorId": "alice", "actorType": "users"},
            ])
            await poll_talk_conversations(config)

        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, talk_token) is None  # no phantom
            assert {r.token for r in db.list_member_rooms(conn, "alice")} == {web_token}

    @pytest.mark.asyncio
    async def test_non_mention_post_unhides_in_multiuser_room(self, make_config):
        """Re-engagement un-hides even when the post doesn't @mention the bot
        (so record_inbound is never reached): the poll message loop clears the
        sender's tombstone and re-adds their membership."""
        config = make_config()
        config.users = {"alice": UserConfig(), "bob": UserConfig()}
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "grp", "alice", origin="talk", name="#sysadmin")
            db.add_room_binding(conn, "grp", "talk", "grp")
            db.add_room_member(conn, "grp", "bob")
            db.remove_room_member(conn, "grp", "alice")
            db.dismiss_room(conn, "grp", "alice")
            db.set_talk_poll_state(conn, "grp", 50)

        # A plain (no-@mention) message from alice in a 3-participant room.
        msg = _msg(actor_id="alice", message="status update, no mention")
        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "grp", "type": 2, "displayName": "#sysadmin"},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])
            mock_instance.get_participants = AsyncMock(return_value=[
                {"actorId": "istota", "actorType": "users"},
                {"actorId": "alice", "actorType": "users"},
                {"actorId": "bob", "actorType": "users"},
            ])
            await poll_talk_conversations(config)

        with db.get_db(config.db_path) as conn:
            assert not db.is_room_dismissed(conn, "grp", "alice")  # un-hidden
            assert db.is_room_member(conn, "grp", "alice")
            assert {r.token for r in db.list_member_rooms(conn, "alice")} == {"grp"}

    @pytest.mark.asyncio
    async def test_web_origin_room_name_not_overwritten(self, make_config):
        """A web-origin room's user-set name wins over Talk's displayName."""
        config = make_config()
        config.users = {"alice": UserConfig()}
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "webtok", "alice", origin="web", name="My Room")
            db.add_room_binding(conn, "webtok", "talk", "webtok")
            db.set_talk_poll_state(conn, "webtok", 50)

        conversations = [
            {"token": "webtok", "type": 2, "displayName": "Talk Title"},
        ]
        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=conversations)
            mock_instance.poll_messages = AsyncMock(return_value=[])
            await poll_talk_conversations(config)

        with db.get_db(config.db_path) as conn:
            room = db.get_room(conn, "webtok")
        assert room.name == "My Room"


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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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
    async def test_create_failure_does_not_advance_poll_state(self, make_config):
        # Atomicity regression guard: task creation shares the db.get_db
        # transaction with set_talk_poll_state, so if create_task raises the
        # whole batch (including the poll-cursor advance) rolls back and the
        # message is re-polled next cycle rather than silently lost.
        config = make_config()
        msg = _msg(id=101, actor_id="alice", message="Do the thing")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            with patch("istota.transport.talk.inbound.ingest_message", side_effect=RuntimeError("db locked")):
                with pytest.raises(RuntimeError):
                    await poll_talk_conversations(config)

        # Poll cursor must NOT have advanced past 50 — the message is re-pollable.
        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "room1") == 50
            # And no orphaned task was committed.
            assert db.list_tasks(conn, user_id="alice") == []

    @pytest.mark.asyncio
    async def test_model_prefix_overrides_task_model_and_effort(self, make_config):
        config = make_config()

        msg = _msg(id=102, actor_id="alice", message="!model opus:high draft a spec for X")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])
            mock_instance.send_message = AsyncMock()

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, result[0])
            assert task.prompt == "draft a spec for X"
            assert task.model == OPUS
            assert task.effort == "high"

    @pytest.mark.asyncio
    async def test_model_prefix_default_alias_clears_overrides(self, make_config):
        config = make_config()

        msg = _msg(id=103, actor_id="alice", message="!model default just do it")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])
            mock_instance.send_message = AsyncMock()

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, result[0])
            assert task.prompt == "just do it"
            assert task.model is None
            assert task.effort is None

    @pytest.mark.asyncio
    async def test_model_prefix_unknown_alias_posts_usage_and_skips_task(self, make_config):
        config = make_config()

        msg = _msg(id=104, actor_id="alice", message="!model gpt-4 draft a spec")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])
            mock_instance.send_message = AsyncMock()

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert result == []
        # Usage message posted to the conversation
        assert mock_instance.send_message.await_count == 1
        args, _ = mock_instance.send_message.await_args
        assert args[0] == "room1"
        assert "Aliases:" in args[1]

    @pytest.mark.asyncio
    async def test_model_prefix_alias_only_posts_usage_and_skips_task(self, make_config):
        config = make_config()

        msg = _msg(id=105, actor_id="alice", message="!model opus")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])
            mock_instance.send_message = AsyncMock()

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert result == []
        assert mock_instance.send_message.await_count == 1

    @pytest.mark.asyncio
    async def test_dm_first_poll_fetches_history(self, make_config):
        config = make_config()

        msg = _msg(id=200, actor_id="alice", message="Hello")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

    def test_no_bot_username_resolves_mentions(self):
        """Without bot_username we can't single out the bot's own mention to
        strip, but mentions still resolve to @name rather than leaking the
        literal placeholder (ISSUE-132 — the cache/is-bot display path)."""
        msg = _msg(
            message="{mention-user0} hello",
            message_params={
                "mention-user0": {"type": "user", "id": "istota", "name": "Istota"},
            },
        )
        result = clean_message_content(msg)
        assert result == "@Istota hello"

    def test_mention_call_becomes_at_all(self):
        """A call mention resolves to @all — Nextcloud's semantics for a
        {mention-call} / call-type rich object (ISSUE-132)."""
        msg = _msg(
            message="{mention-call0} meeting in 5 mins",
            message_params={
                "mention-call0": {"type": "call", "id": "room1", "name": "Engineering"},
            },
        )
        result = clean_message_content(msg, bot_username="istota")
        assert result == "@all meeting in 5 mins"


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

        config = make_config()
        config.users = {"alice": UserConfig(), "bob": UserConfig()}

        msg = _msg(id=101, actor_id="alice", message="Just chatting")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        config = make_config()

        msg = _msg(id=103, actor_id="alice", message="Hello there")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        config = make_config()

        msg = _msg(id=104, actor_id="alice", message="Hello")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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
    """Per-channel gate: queue gated messages instead of discarding them."""

    @pytest.mark.asyncio
    async def test_channel_gate_queues_when_active_task(self, make_config):
        """When an active fg task exists, send 'still working' AND create a task."""
        config = make_config()

        # Pre-create an active foreground task for room1
        with db.get_db(config.db_path) as conn:
            db.create_task(
                conn, prompt="previous request", user_id="alice",
                source_type="talk", conversation_token="room1", queue="foreground",
            )

        msg = _msg(id=200, actor_id="alice", message="Another request")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])
            mock_instance.send_message = AsyncMock()

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        # Task should be created (queued, not discarded)
        assert len(result) == 1
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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
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

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1


# =============================================================================
# TestTalkMessageCacheIntegration
# =============================================================================


class TestTalkMessageCacheIntegration:
    """Tests for talk message cache storage and backfill in the poller."""

    @pytest.mark.asyncio
    async def test_poll_stores_messages_in_cache(self, make_config):
        """Polled messages are stored in the talk_messages cache."""
        config = make_config()

        msg = _msg(id=100, actor_id="alice", message="Hello")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)
                # Seed cache so backfill doesn't trigger
                db.upsert_talk_messages(conn, "room1", [_msg(id=50)])

            await poll_talk_conversations(config)

        # Verify the polled message was stored
        with db.get_db(config.db_path) as conn:
            cached = db.get_cached_talk_messages(conn, "room1")
            ids = [m["id"] for m in cached]
            assert 100 in ids

    @pytest.mark.asyncio
    async def test_backfill_on_first_encounter(self, make_config):
        """When no cache exists, fetch_chat_history is called for backfill."""
        config = make_config()

        backfill_msgs = [
            _msg(id=i, actor_id="alice", message=f"msg {i}")
            for i in range(1, 6)
        ]

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[])
            mock_instance.fetch_chat_history = AsyncMock(return_value=backfill_msgs)

            # No poll state yet — will be initialized as DM (last_message_id=0)
            await poll_talk_conversations(config)

            # fetch_chat_history should have been called for backfill
            mock_instance.fetch_chat_history.assert_called_once_with(
                "room1", limit=config.conversation.talk_context_limit,
            )

        with db.get_db(config.db_path) as conn:
            cached = db.get_cached_talk_messages(conn, "room1")
            assert len(cached) == 5

    @pytest.mark.asyncio
    async def test_skip_backfill_when_cache_exists(self, make_config):
        """When cache already has messages, no backfill fetch_chat_history call."""
        config = make_config()

        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "room1", 50)
            db.upsert_talk_messages(conn, "room1", [_msg(id=50)])

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[])

            await poll_talk_conversations(config)

            # fetch_chat_history should NOT have been called
            mock_instance.fetch_chat_history.assert_not_called()


class TestConversationListCache:
    """Tests for the conversation list caching in poll_talk_conversations."""

    @pytest.mark.asyncio
    async def test_cached_list_avoids_api_call(self, make_config):
        """Second poll cycle uses cached conversation list.

        Only where the `lastMessage` gate is off. The gate reads `lastMessage`
        out of this payload, so a gated deployment must refetch every cycle or
        it gates on a snapshot up to a TTL old — see
        `TestThePerRoomFetchIsGatedOnLastMessage`. `talk_poll_full_sweep_interval
        = 0` is what turns the gate off, and it is the shape this TTL still
        serves (ISSUE-399).
        """
        config = make_config()
        config.scheduler.talk_poll_full_sweep_interval = 0

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[])
            mock_instance.fetch_chat_history = AsyncMock(return_value=[])

            # First call populates cache
            await poll_talk_conversations(config)
            assert mock_instance.list_conversations.call_count == 1

            # Second call within TTL uses cache
            await poll_talk_conversations(config)
            assert mock_instance.list_conversations.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self, make_config):
        """Conversation list is refreshed after TTL expires.

        Gate off, for the reason its sibling above says: with the gate on,
        `cache_valid` is False whatever the timestamp holds, so this test would
        pass against a deleted TTL check (ISSUE-399).
        """
        config = make_config()
        config.scheduler.talk_poll_full_sweep_interval = 0

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[])
            mock_instance.fetch_chat_history = AsyncMock(return_value=[])

            await poll_talk_conversations(config)
            assert mock_instance.list_conversations.call_count == 1

            # Expire the cache
            _talk_poller_mod._conversation_cache = (
                _talk_poller_mod._conversation_cache[0],
                time.monotonic() - 120,  # well past TTL
            )

            await poll_talk_conversations(config)
            assert mock_instance.list_conversations.call_count == 2

    @pytest.mark.asyncio
    async def test_fallback_to_cache_on_error(self, make_config):
        """If list_conversations fails, use cached list instead of returning empty."""
        config = make_config()

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            rooms = [{"token": "room1", "type": 1}]
            mock_instance.list_conversations = AsyncMock(return_value=rooms)
            mock_instance.poll_messages = AsyncMock(return_value=[])
            mock_instance.fetch_chat_history = AsyncMock(return_value=[])

            # Populate cache
            await poll_talk_conversations(config)

            # Expire cache and make API fail
            _talk_poller_mod._conversation_cache = (
                _talk_poller_mod._conversation_cache[0],
                time.monotonic() - 120,
            )
            mock_instance.list_conversations = AsyncMock(
                side_effect=Exception("ReadTimeout")
            )

            # Should still work using cached rooms
            result = await poll_talk_conversations(config)
            # No crash, poll proceeded with cached list
            assert result == []  # no messages, but didn't abort

    @pytest.mark.asyncio
    async def test_no_cache_on_first_error(self, make_config):
        """If list_conversations fails with no cache, returns empty."""
        config = make_config()

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(
                side_effect=Exception("ReadTimeout")
            )

            result = await poll_talk_conversations(config)
            assert result == []


# =============================================================================
# TestCancelPendingConfirmationsOnNewMessage
# =============================================================================


class TestCancelPendingConfirmationsOnNewMessage:
    @pytest.mark.asyncio
    async def test_new_message_cancels_pending_confirmation(self, make_config):
        """When a user sends a new message, pending confirmations are cancelled."""
        config = make_config()

        # Create a pending confirmation task in room1
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Draft email", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.set_task_confirmation(conn, task_id, "Should I send this?")

        # User sends a new message (not yes/no)
        msg = _msg(id=101, actor_id="alice", message="Actually, change the subject")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        # New task created
        assert len(result) == 1

        # Old confirmation cancelled
        with db.get_db(config.db_path) as conn:
            old_task = db.get_task(conn, task_id)
            assert old_task.status == "cancelled"

    @pytest.mark.asyncio
    async def test_yes_reply_does_not_cancel_confirmation(self, make_config):
        """A 'yes' reply confirms (not cancels) the pending task."""
        config = make_config()

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Draft email", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.set_task_confirmation(conn, task_id, "Should I send this?")

        msg = _msg(id=101, actor_id="alice", message="yes")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = AsyncMock(return_value=[msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        # No new task created (confirmation handled it)
        assert result == []

        # Task should be confirmed (pending), not cancelled
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
            assert task.status == "pending"
            assert task.confirmed_at is not None


# =============================================================================
# TestPollUnderPersistentRuntime (Stage 5)
# =============================================================================


class TestPollUnderPersistentRuntime:
    """The daemon drives the poll cycle via run_coro on the shared persistent
    loop (not a fresh asyncio.run loop per cycle). Verify a full cycle —
    long-poll, filtering, atomic task creation — works under the runtime."""

    def test_run_coro_poll_cycle_creates_task(self, make_config, db_path):
        from istota.async_runtime import (
            reset_async_runtime,
            reset_talk_client,
            run_coro,
        )

        config = make_config()
        try:
            with patch(
                "istota.transport.talk.inbound.get_talk_client"
            ) as MockClient:
                inst = MockClient.return_value
                inst.list_conversations = AsyncMock(
                    return_value=[{"token": "room1", "type": 1}]
                )
                inst.poll_messages = AsyncMock(
                    return_value=[
                        _msg(id=101, actor_id="alice", message="Check my calendar"),
                    ]
                )
                with db.get_db(db_path) as conn:
                    db.set_talk_poll_state(conn, "room1", 50)

                created = run_coro(poll_talk_conversations(config))

            assert isinstance(created, list)
            with db.get_db(db_path) as conn:
                tasks = db.list_tasks(conn, user_id="alice")
            assert len(tasks) == 1
            assert tasks[0].prompt == "Check my calendar"
            assert tasks[0].conversation_token == "room1"
            assert tasks[0].talk_message_id == 101
        finally:
            reset_talk_client()
            reset_async_runtime()


class TestThePollGateOutlastsTheServerLongPoll:
    """`talk_poll_timeout` is two different durations, and they must not be equal.

    `poll_messages` sends it to Nextcloud as the `timeout` query parameter, so
    it is how long the *server* holds the request open. `poll_talk_conversations`
    then passes the same number to `asyncio.wait` as how long the *client* waits
    for an answer. An answer to a request the server holds for N seconds cannot
    arrive before N seconds have passed — it is late by construction, by a
    network hop and Nextcloud's own dispatch — so a gate of exactly N expires
    first and the room is cancelled.

    `results = [t.result() for t in done]` reads only the finished tasks, so a
    cancelled room's messages are dropped: the poll cursor does not advance and
    the message is refetched a `talk_poll_interval` later. The `talk_poll_wait`
    grace does not cover it, because `if done and pending` requires some room to
    have answered *before* the gate — and on a cycle where every room is running
    the same server-side timer, none has.

    Found while investigating ISSUE-399, where the deployment had set
    `talk_poll_timeout = 1` to stop holding a Nextcloud PHP worker per room. At
    30 the skew is a fraction of the window; at 1 it is the whole of it, so
    every cycle became a full round of connections opened and abandoned.
    """

    @staticmethod
    def _answers_after(delay: float, messages: list[dict]):
        """A `poll_messages` that answers `delay` seconds late, like the server."""
        async def _poll(*args, **kwargs):
            await asyncio.sleep(delay)
            return list(messages)
        return _poll

    @pytest.mark.asyncio
    async def test_a_late_answer_is_collected_rather_than_cancelled(self, make_config):
        config = make_config()
        config.scheduler.talk_poll_timeout = 1
        config.scheduler.talk_poll_wait = 2.0

        msg = _msg(id=101, actor_id="alice", message="answered just after the gate")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
            ])
            mock_instance.poll_messages = self._answers_after(1.15, [msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1, (
            "the room answered 0.15s after a 1s server-side long-poll — the "
            "earliest it could — and the cycle threw the answer away"
        )

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, result[0]).talk_message_id == 101

    @pytest.mark.asyncio
    async def test_the_grace_window_applies_when_no_room_beat_the_gate(
        self, make_config,
    ):
        """Two rooms on the same server-side timer, neither answering early.

        This is the ordinary quiet cycle: every room was started together and
        carries the same `timeout`, so they all answer at once and all of them
        answer late. `if done and pending` is False for want of an early
        responder, so the grace never runs and both rooms are cancelled — the
        case a single-room test cannot distinguish from a gate that is merely
        too short.
        """
        config = make_config()
        config.scheduler.talk_poll_timeout = 1
        config.scheduler.talk_poll_wait = 2.0

        async def _poll(token, *args, **kwargs):
            await asyncio.sleep(1.15)
            return [_msg(id=200 if token == "room1" else 300, actor_id="alice")]

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.list_conversations = AsyncMock(return_value=[
                {"token": "room1", "type": 1},
                {"token": "room2", "type": 1},
            ])
            mock_instance.poll_messages = _poll

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)
                db.set_talk_poll_state(conn, "room2", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 2, (
            "both rooms answered and both were cancelled: the grace window is "
            "guarded on a room having beaten the gate, and none can"
        )


class TestThePerRoomFetchIsGatedOnLastMessage:
    """Only poll a room the room list already says has something new.

    ISSUE-399. The poll loop opened one long-poll per room per cycle and held
    each one on a Nextcloud PHP-FPM worker for `talk_poll_timeout` seconds,
    around the clock, for every room the bot is in — 41 rows against 6 or 7 live
    rooms on the production deployment. A day's nginx log showed 95,703 such
    requests, 92,678 of them (97%) ending in a 499: opened, held, and abandoned
    client-side having carried nothing.

    `/api/v4/room` — the call that enumerates the rooms — already returns a
    `lastMessage` object per room, and `talk_poll_state` already holds the
    cursor. Comparing the two says whether a room can possibly have news before
    a single long-poll is opened, so the quiet case, which is nearly every case,
    costs one short request for the whole cycle instead of N held ones.

    The gate fails toward fetching: a missing `lastMessage`, an unexpected
    shape, or a room with no cursor yet is polled. A gate that guesses wrong in
    the other direction loses a message permanently, where the behaviour it
    replaces merely re-fetched a cycle later.
    """

    @staticmethod
    def _client(conversations, poll):
        mock_instance = MagicMock()
        mock_instance.list_conversations = AsyncMock(return_value=conversations)
        mock_instance.poll_messages = poll
        mock_instance.fetch_chat_history = AsyncMock(return_value=[])
        mock_instance.get_latest_message_id = AsyncMock(return_value=0)
        return mock_instance

    @staticmethod
    def _room(token, last_message_id, *, conv_type=1):
        room = {"token": token, "type": conv_type}
        if last_message_id is not None:
            room["lastMessage"] = {"id": last_message_id}
        return room

    @staticmethod
    def _swept_just_now():
        """Put the cycle under test *between* sweeps.

        `_last_full_sweep` is None in a fresh process, and the first cycle after
        a restart is deliberately a full sweep — the gate has no history to
        reason from at that point. A test that calls the poller once therefore
        measures a sweep, not the gate, and would pass against no gate at all.
        """
        _talk_poller_mod._last_full_sweep = time.monotonic()

    @pytest.mark.asyncio
    async def test_a_quiet_room_is_not_polled(self, make_config):
        config = make_config()
        poll = AsyncMock(return_value=[])

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            MockClient.return_value = self._client(
                [self._room("quiet", 100)], poll,
            )
            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "quiet", 100)
            self._swept_just_now()
            await poll_talk_conversations(config)

        assert poll.await_count == 0, (
            "the room list said the newest message is the one we already have, "
            "and a long-poll was opened and held anyway"
        )

    @pytest.mark.asyncio
    async def test_a_room_with_a_newer_last_message_is_polled(self, make_config):
        config = make_config()
        poll = AsyncMock(return_value=[])

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            MockClient.return_value = self._client(
                [self._room("quiet", 100), self._room("busy", 205)], poll,
            )
            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "quiet", 100)
                db.set_talk_poll_state(conn, "busy", 200)
            self._swept_just_now()
            await poll_talk_conversations(config)

        polled = {c.args[0] for c in poll.await_args_list}
        assert polled == {"busy"}, f"expected only the busy room, polled {polled}"

    @pytest.mark.parametrize("last_message", [
        None,
        "not-a-dict",
        {},
        {"id": None},
        {"id": "12"},
    ])
    @pytest.mark.asyncio
    async def test_an_unfamiliar_shape_is_polled_rather_than_skipped(
        self, make_config, last_message,
    ):
        """Fail toward fetching. A skipped room's message is lost for good."""
        config = make_config()
        poll = AsyncMock(return_value=[])
        room = {"token": "odd", "type": 1}
        if last_message is not None:
            room["lastMessage"] = last_message

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            MockClient.return_value = self._client([room], poll)
            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "odd", 100)
            self._swept_just_now()
            await poll_talk_conversations(config)

        assert poll.await_count == 1, (
            f"lastMessage={last_message!r} was read as 'nothing new' — an "
            f"unrecognised shape must fail toward fetching"
        )

    @pytest.mark.asyncio
    async def test_the_full_sweep_polls_a_quiet_room(self, make_config):
        """The gate's safety net: every room is polled on a fixed cadence.

        A gate that never opens loses a message permanently, where a dropped
        cycle self-heals. The sweep bounds that to the sweep interval.
        """
        config = make_config()
        config.scheduler.talk_poll_full_sweep_interval = 300
        poll = AsyncMock(return_value=[])

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            MockClient.return_value = self._client(
                [self._room("quiet", 100)], poll,
            )
            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "quiet", 100)

            _talk_poller_mod._last_full_sweep = time.monotonic() - 301
            await poll_talk_conversations(config)
            assert poll.await_count == 1, "the overdue sweep did not run"

            poll.reset_mock()
            await poll_talk_conversations(config)
            assert poll.await_count == 0, (
                "every cycle swept; the sweep timestamp is not being recorded"
            )

    @pytest.mark.asyncio
    async def test_a_zero_sweep_interval_turns_the_gate_off(self, make_config):
        """The operator's escape hatch, with an honest meaning at every value.

        `0` means every cycle is a full sweep, which is exactly the behaviour
        the gate replaced — so a deployment that finds the gate wrong for it can
        say so without a second boolean and a second code path.
        """
        config = make_config()
        config.scheduler.talk_poll_full_sweep_interval = 0
        poll = AsyncMock(return_value=[])

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            MockClient.return_value = self._client(
                [self._room("quiet", 100)], poll,
            )
            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "quiet", 100)
            await poll_talk_conversations(config)
            await poll_talk_conversations(config)

        assert poll.await_count == 2

    @pytest.mark.asyncio
    async def test_the_room_list_is_refetched_every_cycle_while_gating(
        self, make_config,
    ):
        """The gate reads `lastMessage`, so a cached room list is a stale gate.

        The issue assumed `/api/v4/room` was fetched every cycle. It was not:
        `_CONVERSATION_CACHE_TTL` is 60 seconds, so with a 10-second poll
        interval six consecutive cycles read one snapshot. Gating on a
        `lastMessage` up to a minute old would hold a real message for that
        minute — worse inbound latency than the long-poll it replaces, and the
        one outcome that would make the gate not worth having.

        One short request per cycle against N held ones is still the trade the
        issue asked for.
        """
        config = make_config()
        poll = AsyncMock(return_value=[])
        client = self._client([self._room("quiet", 100)], poll)

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            MockClient.return_value = client
            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "quiet", 100)
            await poll_talk_conversations(config)
            await poll_talk_conversations(config)

        assert client.list_conversations.await_count == 2, (
            "the second cycle gated on a cached room list"
        )

    @pytest.mark.asyncio
    async def test_a_stale_list_still_serves_when_the_fetch_fails(
        self, make_config,
    ):
        """Refetching every cycle must not cost the failure fallback.

        The cache stops being a TTL and becomes what it is now only useful for:
        the last known room list, for a cycle where Nextcloud did not answer.
        """
        config = make_config()
        poll = AsyncMock(return_value=[])
        client = self._client([self._room("busy", 205)], poll)

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            MockClient.return_value = client
            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "busy", 200)
            await poll_talk_conversations(config)
            assert poll.await_count == 1

            poll.reset_mock()
            client.list_conversations.side_effect = RuntimeError("nextcloud down")
            await poll_talk_conversations(config)

        assert poll.await_count == 1, (
            "the cached room list was not used when list_conversations failed"
        )

    @pytest.mark.asyncio
    async def test_a_stale_list_ungates_rather_than_gating_on_frozen_evidence(
        self, make_config,
    ):
        """A room list the server did not just hand us is not evidence of news.

        The defect this pins is a *permanent* hold, not a delayed one, and it is
        the one case where the gate is worse than what it replaced. On the
        fallback path `lastMessage` is frozen at the last successful listing
        while the cursor keeps advancing from whatever the polls return. Once
        the cursor reaches that frozen id, `_has_news` is False for that room on
        every subsequent cycle — so for as long as `/api/v4/room` keeps failing,
        inbound runs at one full sweep per `talk_poll_full_sweep_interval`
        instead of every cycle. Before the gate existed the same outage cost
        nothing, because the cached list was only ever used to enumerate rooms.

        The first version of the test beside this one could not see it: it
        mocked the poll to return no messages, so the cursor never advanced to
        meet the frozen id and the gate stayed open by construction. Letting the
        first cycle actually deliver message 205 is the whole difference.
        """
        config = make_config()
        poll = AsyncMock(return_value=[_msg(id=205, actor_id="alice")])
        client = self._client([self._room("busy", 205)], poll)

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            MockClient.return_value = client
            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "busy", 200)

            # Cycle one succeeds and advances the cursor to the id the room
            # list reports, which is what arms the trap.
            await poll_talk_conversations(config)
            with db.get_db(config.db_path) as conn:
                assert db.get_talk_poll_state(conn, "busy") == 205

            # Cycle two: the listing fails, so the frozen list comes back — and
            # its lastMessage now equals the cursor.
            poll.reset_mock()
            poll.return_value = []
            client.list_conversations.side_effect = RuntimeError("nextcloud down")
            self._swept_just_now()
            await poll_talk_conversations(config)

        assert poll.await_count == 1, (
            "the room was gated on a frozen lastMessage: while the listing "
            "keeps failing this room is only ever polled on a full sweep"
        )

    @pytest.mark.asyncio
    async def test_the_initialised_cursor_is_persisted(self, make_config):
        """Otherwise a room whose polls come back empty is never gated.

        The only other writer of `talk_poll_state` is the message loop, which
        fires solely for a message a poll actually returned. A room initialised
        from `get_latest_message_id` whose long-poll then returns nothing wrote
        no cursor at all, so `known_cursor` stayed None and the gate was
        bypassed on every cycle for ever — the dormant-room shape the issue
        counted (41 rows against 6-7 live).
        """
        config = make_config()
        poll = AsyncMock(return_value=[])
        client = self._client([self._room("fresh", 500, conv_type=2)], poll)
        client.get_latest_message_id = AsyncMock(return_value=500)

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            MockClient.return_value = client
            await poll_talk_conversations(config)

        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "fresh") == 499, (
                "the initialised cursor was not written, so the next cycle "
                "re-initialises and the gate can never apply"
            )

    @pytest.mark.asyncio
    async def test_a_failed_listing_does_not_spend_the_sweep_credit(
        self, make_config,
    ):
        """The sweep is the gate's only safety net, so a cycle that swept
        nothing must not consume its interval.

        Stamped beside the decision rather than after the work, a cycle that
        died on the listing fetch still counted as the sweep, deferring the real
        one by a whole `talk_poll_full_sweep_interval`.
        """
        config = make_config()
        poll = AsyncMock(return_value=[])
        client = self._client([self._room("quiet", 100)], poll)
        client.list_conversations.side_effect = RuntimeError("nextcloud down")

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            MockClient.return_value = client
            # No cached list yet, so this cycle returns having done nothing.
            await poll_talk_conversations(config)

        assert _talk_poller_mod._last_full_sweep is None, (
            "a cycle that never reached the room loop recorded a full sweep"
        )
