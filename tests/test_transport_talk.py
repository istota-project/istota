"""Tests for TalkTransport delivery + target resolution.

poll() behaviour is covered once its body moves over (Stage 3); this file
covers the outbound surface that landed in Stage 2.
"""

from unittest.mock import AsyncMock, patch

import pytest

from istota import db
from istota.config import Config, NextcloudConfig, TalkConfig, UserConfig
from istota.transport.talk import TalkTransport


def _config(**overrides):
    return Config(
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="istota", app_password="secret",
        ),
        **overrides,
    )


def _task(**overrides):
    defaults = dict(
        id=1, status="completed", source_type="talk",
        user_id="alice", prompt="hi", conversation_token="room123",
    )
    defaults.update(overrides)
    return db.Task(**defaults)


class TestDeliver:
    @pytest.mark.asyncio
    async def test_dm_plain_post(self):
        t = TalkTransport(_config())
        task = _task(is_group_chat=False, talk_message_id=42)
        with patch("istota.transport.talk.TalkClient") as MockClient:
            inst = MockClient.return_value
            inst.send_message = AsyncMock(return_value={"ocs": {"data": {"id": 100}}})
            result = await t.deliver("room123", "Hello", task=task, threaded=True)
        assert result == 100
        inst.send_message.assert_called_once_with(
            "room123", "Hello", reply_to=None, reference_id=None,
        )

    @pytest.mark.asyncio
    async def test_group_chat_threads_and_mentions_first_part(self):
        t = TalkTransport(_config())
        task = _task(is_group_chat=True, talk_message_id=42, user_id="bob")
        with patch("istota.transport.talk.TalkClient") as MockClient:
            inst = MockClient.return_value
            inst.send_message = AsyncMock(return_value={"ocs": {"data": {"id": 200}}})
            result = await t.deliver("room123", "Sure", task=task, threaded=True)
        assert result == 200
        inst.send_message.assert_called_once_with(
            "room123", "@bob Sure", reply_to=42, reference_id=None,
        )

    @pytest.mark.asyncio
    async def test_split_only_first_part_threaded(self):
        t = TalkTransport(_config())
        task = _task(is_group_chat=True, talk_message_id=42, user_id="carol")
        with patch("istota.transport.talk.TalkClient") as MockClient, \
                patch("istota.transport.talk.split_message", return_value=["P1", "P2"]):
            inst = MockClient.return_value
            inst.send_message = AsyncMock(return_value={"ocs": {"data": {"id": 300}}})
            await t.deliver("room123", "long", task=task, threaded=True)
        calls = inst.send_message.call_args_list
        assert calls[0].args == ("room123", "@carol P1")
        assert calls[0].kwargs == {"reply_to": 42, "reference_id": None}
        assert calls[1].args == ("room123", "P2")
        assert calls[1].kwargs == {"reply_to": None, "reference_id": None}

    @pytest.mark.asyncio
    async def test_no_threading_for_progress(self):
        t = TalkTransport(_config())
        task = _task(is_group_chat=True, talk_message_id=42, user_id="eve")
        with patch("istota.transport.talk.TalkClient") as MockClient:
            inst = MockClient.return_value
            inst.send_message = AsyncMock(return_value={"ocs": {"data": {"id": 1}}})
            await t.deliver("room123", "Working...", task=task)  # threaded=False
        inst.send_message.assert_called_once_with(
            "room123", "Working...", reply_to=None, reference_id=None,
        )

    @pytest.mark.asyncio
    async def test_reference_id_passed_through(self):
        t = TalkTransport(_config())
        task = _task()
        with patch("istota.transport.talk.TalkClient") as MockClient:
            inst = MockClient.return_value
            inst.send_message = AsyncMock(return_value={"ocs": {"data": {"id": 5}}})
            await t.deliver("room123", "R", task=task, reference_id="istota:task:1:result")
        inst.send_message.assert_called_once_with(
            "room123", "R", reply_to=None, reference_id="istota:task:1:result",
        )

    @pytest.mark.asyncio
    async def test_explicit_reply_to_without_task(self):
        t = TalkTransport(_config())
        with patch("istota.transport.talk.TalkClient") as MockClient:
            inst = MockClient.return_value
            inst.send_message = AsyncMock(return_value={"ocs": {"data": {"id": 7}}})
            await t.deliver("room1", "Hi", reply_to=9)
        inst.send_message.assert_called_once_with(
            "room1", "Hi", reply_to=9, reference_id=None,
        )

    @pytest.mark.asyncio
    async def test_no_target_returns_none(self):
        t = TalkTransport(_config())
        result = await t.deliver("", "x")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_nextcloud_url_returns_none(self):
        t = TalkTransport(Config())
        result = await t.deliver("room1", "x")
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self):
        t = TalkTransport(_config())
        task = _task()
        with patch("istota.transport.talk.TalkClient") as MockClient:
            inst = MockClient.return_value
            inst.send_message = AsyncMock(side_effect=Exception("boom"))
            result = await t.deliver("room123", "x", task=task)
        assert result is None


class TestEdit:
    @pytest.mark.asyncio
    async def test_edit_calls_client(self):
        t = TalkTransport(_config())
        with patch("istota.transport.talk.TalkClient") as MockClient:
            inst = MockClient.return_value
            inst.edit_message = AsyncMock()
            await t.edit("room123", 42, "Updated")
        inst.edit_message.assert_awaited_once_with("room123", 42, "Updated")

    @pytest.mark.asyncio
    async def test_edit_no_target_noop(self):
        t = TalkTransport(_config())
        with patch("istota.transport.talk.TalkClient") as MockClient:
            await t.edit("", 1, "x")
            MockClient.assert_not_called()


def _talk_msg(**overrides):
    defaults = dict(
        id=100, actorId="alice", actorType="users",
        message="Hello", messageType="comment", messageParameters={},
    )
    defaults.update(overrides)
    return defaults


class TestPoll:
    @pytest.mark.asyncio
    async def test_poll_self_creates_and_returns_empty(self):
        # Talk self-creates inside poll_talk_conversations (atomic with the
        # poll-state advance), so poll() returns an empty IncomingMessage list.
        t = TalkTransport(_config())
        with patch(
            "istota.transport.talk.poll_talk_conversations",
            new=AsyncMock(return_value=[7, 8]),
        ) as mock_poll:
            result = await t.poll()
        assert result == []
        mock_poll.assert_awaited_once_with(t._config)

    @pytest.mark.asyncio
    async def test_poll_creates_task_atomically(self, tmp_path):
        path = tmp_path / "t.db"
        db.init_db(path)
        config = _config(
            db_path=path,
            talk=TalkConfig(enabled=True, bot_username="istota"),
            users={"alice": UserConfig()},
        )
        t = TalkTransport(config)
        with patch("istota.transport.talk.inbound.TalkClient") as MockClient:
            inst = MockClient.return_value
            inst.list_conversations = AsyncMock(return_value=[{"token": "room1", "type": 1}])
            inst.poll_messages = AsyncMock(return_value=[
                _talk_msg(id=101, actorId="alice", message="Check my calendar"),
            ])
            with db.get_db(path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)
            result = await t.poll()
        # poll() returns [] (self-creating); the task exists in the DB.
        assert result == []
        with db.get_db(path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        assert len(tasks) == 1
        assert tasks[0].source_type == "talk"
        assert tasks[0].prompt == "Check my calendar"
        assert tasks[0].conversation_token == "room1"
        assert tasks[0].talk_message_id == 101


class TestResolveTarget:
    def test_prefers_delivery_token(self):
        t = TalkTransport(_config())
        task = _task(talk_delivery_token="real_room", conversation_token="other")
        assert t.resolve_target(task) == "real_room"

    def test_falls_back_to_conversation_token_for_talk(self):
        t = TalkTransport(_config())
        task = _task(source_type="talk", conversation_token="room9")
        assert t.resolve_target(task) == "room9"

    def test_synthetic_email_token_resolves_to_user_channel(self):
        config = _config(users={"alice": UserConfig(alerts_channel="alerts1")})
        t = TalkTransport(config)
        # 16-char lowercase hex = synthetic email-thread token
        task = _task(source_type="email", conversation_token="0123456789abcdef")
        assert t.resolve_target(task) == "alerts1"
