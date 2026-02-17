"""Integration tests for Talk API connectivity with real Nextcloud server.

These tests verify actual connectivity and data flow against the configured
Nextcloud instance. They are skipped by default and only run when explicitly
requested via: pytest -m integration

Prerequisites:
  - bot user must be a participant in the test room (token: hy8u77nc)
  - config/config.toml must have valid [nextcloud] and [talk] sections
"""

import asyncio
import time
import uuid
from pathlib import Path

import pytest

from istota.config import load_config
from istota.talk import TalkClient, truncate_message

# Load config from project root — skip all tests if missing or misconfigured
_CONFIG_PATHS = [
    Path(__file__).parent.parent / "config" / "config.toml",
    Path.home() / ".config" / "istota" / "config.toml",
]

_config = None
for p in _CONFIG_PATHS:
    if p.exists():
        try:
            _config = load_config(p)
            break
        except Exception:
            pass

_skip_reason = None
if _config is None:
    _skip_reason = "No config.toml found"
elif not _config.nextcloud.url:
    _skip_reason = "nextcloud.url not configured"
elif not _config.nextcloud.app_password:
    _skip_reason = "nextcloud.app_password not configured"
elif not _config.talk.enabled:
    _skip_reason = "talk not enabled in config"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_skip_reason is not None, reason=_skip_reason or ""),
]

TEST_ROOM = "hy8u77nc"


@pytest.fixture
def client():
    return TalkClient(_config)


@pytest.fixture
def run(client):
    """Helper to run async methods synchronously."""
    def _run(coro):
        return asyncio.run(coro)
    return _run


class TestAuthentication:
    """Verify credentials and API access."""

    def test_can_list_conversations(self, client, run):
        """Bot user can authenticate and list conversations."""
        conversations = run(client.list_conversations())
        assert isinstance(conversations, list)
        # Should have at least one conversation (the test room)
        assert len(conversations) > 0

    def test_conversations_have_expected_fields(self, client, run):
        """Conversation objects have the fields we rely on."""
        conversations = run(client.list_conversations())
        conv = conversations[0]
        assert "token" in conv
        assert "type" in conv

    def test_test_room_visible(self, client, run):
        """The test room is in the bot's conversation list."""
        conversations = run(client.list_conversations())
        tokens = [c["token"] for c in conversations]
        assert TEST_ROOM in tokens, (
            f"Test room {TEST_ROOM} not found. "
            f"Make sure the bot user is added to the room. "
            f"Available tokens: {tokens}"
        )

    def test_test_room_metadata(self, client, run):
        """Test room has expected conversation type and fields."""
        conversations = run(client.list_conversations())
        test_conv = next(c for c in conversations if c["token"] == TEST_ROOM)
        # type 2 = group, type 3 = public — both are valid for a test room
        assert test_conv["type"] in (2, 3), f"Unexpected room type: {test_conv['type']}"


class TestMessageSending:
    """Verify message sending to the test room."""

    def test_send_message(self, client, run):
        """Can send a message and get back a valid response."""
        tag = uuid.uuid4().hex[:8]
        message = f"[integration test] send_message {tag}"
        response = run(client.send_message(TEST_ROOM, message))

        assert isinstance(response, dict)
        ocs = response.get("ocs", {})
        assert "data" in ocs
        data = ocs["data"]
        assert "id" in data
        assert isinstance(data["id"], int)

    def test_sent_message_has_correct_actor(self, client, run):
        """Sent message is attributed to the bot user."""
        tag = uuid.uuid4().hex[:8]
        message = f"[integration test] actor_check {tag}"
        response = run(client.send_message(TEST_ROOM, message))
        data = response["ocs"]["data"]
        assert data["actorId"] == _config.talk.bot_username
        assert data["actorType"] == "users"

    def test_sent_message_content_preserved(self, client, run):
        """Message content survives the round-trip."""
        tag = uuid.uuid4().hex[:8]
        message = f"[integration test] content_check {tag}"
        response = run(client.send_message(TEST_ROOM, message))
        data = response["ocs"]["data"]
        assert data["message"] == message

    def test_send_message_returns_id(self, client, run):
        """Response includes a message ID we can use for reply tracking."""
        tag = uuid.uuid4().hex[:8]
        response = run(client.send_message(TEST_ROOM, f"[integration test] id_check {tag}"))
        msg_id = response.get("ocs", {}).get("data", {}).get("id")
        assert msg_id is not None
        assert isinstance(msg_id, int)
        assert msg_id > 0


class TestMessagePolling:
    """Verify message polling and retrieval."""

    def test_poll_history(self, client, run):
        """Can fetch message history (lookIntoFuture=0)."""
        messages = run(client.poll_messages(TEST_ROOM, last_known_message_id=None))
        assert isinstance(messages, list)
        # Should have at least the messages we sent in TestMessageSending
        # (if running in sequence), but even a fresh room should have system messages

    def test_history_messages_have_fields(self, client, run):
        """Historical messages have the fields the poller relies on."""
        messages = run(client.poll_messages(TEST_ROOM, last_known_message_id=None))
        if not messages:
            pytest.skip("No messages in test room history")
        msg = messages[0]
        assert "id" in msg
        assert "actorId" in msg
        assert "actorType" in msg
        assert "message" in msg
        assert "messageType" in msg

    def test_history_ordered_oldest_first(self, client, run):
        """History fetch returns messages in oldest-first order."""
        messages = run(client.poll_messages(TEST_ROOM, last_known_message_id=None))
        if len(messages) < 2:
            pytest.skip("Need at least 2 messages to check ordering")
        ids = [m["id"] for m in messages]
        assert ids == sorted(ids), "History should be oldest-first"

    def test_poll_with_known_id_returns_newer(self, client, run):
        """Polling with a known message ID returns only newer messages."""
        # First, send a marker message
        tag = uuid.uuid4().hex[:8]
        response = run(client.send_message(TEST_ROOM, f"[integration test] poll_marker {tag}"))
        marker_id = response["ocs"]["data"]["id"]

        # Send another message after the marker
        tag2 = uuid.uuid4().hex[:8]
        after_msg = f"[integration test] poll_after {tag2}"
        run(client.send_message(TEST_ROOM, after_msg))

        # Poll from the marker — should only get messages after it
        # Use a short timeout to avoid blocking
        messages = run(client.poll_messages(
            TEST_ROOM,
            last_known_message_id=marker_id,
            timeout=5,
        ))
        assert len(messages) >= 1
        # All returned messages should be newer than the marker
        for msg in messages:
            assert msg["id"] > marker_id

    def test_get_latest_message_id(self, client, run):
        """Can retrieve the latest message ID for state initialization."""
        latest_id = run(client.get_latest_message_id(TEST_ROOM))
        assert latest_id is not None
        assert isinstance(latest_id, int)
        assert latest_id > 0

    def test_latest_id_advances_after_send(self, client, run):
        """Latest message ID increases after sending a new message."""
        id_before = run(client.get_latest_message_id(TEST_ROOM))
        tag = uuid.uuid4().hex[:8]
        run(client.send_message(TEST_ROOM, f"[integration test] advance_check {tag}"))
        id_after = run(client.get_latest_message_id(TEST_ROOM))
        assert id_after > id_before


class TestMessageTypes:
    """Verify handling of different message types."""

    def test_system_messages_identifiable(self, client, run):
        """System messages have messageType='system' for filtering."""
        messages = run(client.poll_messages(TEST_ROOM, last_known_message_id=None, limit=100))
        system_msgs = [m for m in messages if m.get("messageType") == "system"]
        # A group room should have at least one system message (user added, etc.)
        # If not, that's fine — we just verify the field exists on all messages
        for msg in messages:
            assert "messageType" in msg

    def test_user_messages_identifiable(self, client, run):
        """User messages have actorType='users' and meaningful actorId."""
        tag = uuid.uuid4().hex[:8]
        run(client.send_message(TEST_ROOM, f"[integration test] type_check {tag}"))
        messages = run(client.poll_messages(TEST_ROOM, last_known_message_id=None, limit=100))
        user_msgs = [m for m in messages if m.get("actorType") == "users"]
        assert len(user_msgs) > 0
        for msg in user_msgs:
            assert msg["actorId"], "User messages should have actorId"


class TestReplyTracking:
    """Verify reply/threading metadata."""

    def test_reply_to_message(self, client, run):
        """Replying to a message includes parent reference."""
        # Send original message
        tag = uuid.uuid4().hex[:8]
        original = run(client.send_message(TEST_ROOM, f"[integration test] original {tag}"))
        original_id = original["ocs"]["data"]["id"]

        # Reply to it
        reply = run(client.send_message(
            TEST_ROOM,
            f"[integration test] reply {tag}",
            reply_to=original_id,
        ))
        reply_data = reply["ocs"]["data"]

        # The reply should reference the parent
        parent = reply_data.get("parent")
        assert parent is not None, "Reply should have parent field"
        assert parent.get("id") == original_id

    def test_non_reply_has_no_parent(self, client, run):
        """Regular messages don't have a parent reference."""
        tag = uuid.uuid4().hex[:8]
        response = run(client.send_message(TEST_ROOM, f"[integration test] no_parent {tag}"))
        data = response["ocs"]["data"]
        # parent should be absent or empty for non-replies
        parent = data.get("parent")
        # Nextcloud may omit parent entirely or set it to empty
        if parent is not None:
            assert parent.get("id") is None or parent.get("id") == 0


class TestMessageParameters:
    """Verify messageParameters structure for attachment handling."""

    def test_text_message_parameters(self, client, run):
        """Text messages have messageParameters (may be empty dict or list)."""
        tag = uuid.uuid4().hex[:8]
        response = run(client.send_message(TEST_ROOM, f"[integration test] params {tag}"))
        data = response["ocs"]["data"]
        # messageParameters should exist (our code handles both dict and list)
        assert "messageParameters" in data


class TestPollingRoundTrip:
    """End-to-end polling flow matching what the scheduler does."""

    def test_send_then_poll_picks_up_message(self, client, run):
        """Full round-trip: send a message, then poll and find it."""
        # Get current latest ID
        latest_before = run(client.get_latest_message_id(TEST_ROOM))

        # Send a uniquely tagged message
        tag = uuid.uuid4().hex[:8]
        expected_content = f"[integration test] roundtrip {tag}"
        send_response = run(client.send_message(TEST_ROOM, expected_content))
        sent_id = send_response["ocs"]["data"]["id"]

        # Poll from before — should pick up our message
        messages = run(client.poll_messages(
            TEST_ROOM,
            last_known_message_id=latest_before,
            timeout=5,
        ))

        found = [m for m in messages if m.get("message") == expected_content]
        assert len(found) == 1, f"Expected to find our message in poll results. Got {len(found)} matches."
        assert found[0]["id"] == sent_id
        assert found[0]["actorId"] == _config.talk.bot_username

    def test_poll_state_continuity(self, client, run):
        """Simulates scheduler poll state: poll, update state, poll again."""
        # Initialize state
        state_id = run(client.get_latest_message_id(TEST_ROOM))

        # Send first message
        tag1 = uuid.uuid4().hex[:8]
        run(client.send_message(TEST_ROOM, f"[integration test] state1 {tag1}"))

        # First poll — should get the new message
        batch1 = run(client.poll_messages(TEST_ROOM, last_known_message_id=state_id, timeout=5))
        assert len(batch1) >= 1
        # Update state to latest from this batch
        state_id = max(m["id"] for m in batch1)

        # Send second message
        tag2 = uuid.uuid4().hex[:8]
        expected = f"[integration test] state2 {tag2}"
        run(client.send_message(TEST_ROOM, expected))

        # Second poll from updated state — should only get second message
        batch2 = run(client.poll_messages(TEST_ROOM, last_known_message_id=state_id, timeout=5))
        assert len(batch2) >= 1
        contents = [m.get("message") for m in batch2]
        assert expected in contents
        # First message should NOT appear in second batch
        first_content = f"[integration test] state1 {tag1}"
        assert first_content not in contents

    def test_poll_timeout_returns_empty(self, client, run):
        """Long-polling with no new messages returns empty after timeout."""
        # Get latest ID — no new messages should arrive after this
        latest = run(client.get_latest_message_id(TEST_ROOM))

        # Poll with very short timeout — should return empty (304)
        start = time.time()
        messages = run(client.poll_messages(
            TEST_ROOM,
            last_known_message_id=latest,
            timeout=3,
        ))
        elapsed = time.time() - start

        assert messages == []
        # Should have waited roughly the timeout period
        assert elapsed >= 2.0, f"Poll returned too quickly ({elapsed:.1f}s)"
