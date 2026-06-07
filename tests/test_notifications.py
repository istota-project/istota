"""Configuration loading for istota.notifications module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota.config import (
    BriefingConfig,
    Config,
    EmailConfig,
    NextcloudConfig,
    UserConfig,
)
from istota.notifications import (
    _send_email,
    _send_ntfy,
    _send_talk,
    resolve_conversation_token,
    send_notification,
)


def _ntfy_secrets(**values: str):
    """Build a fake ``secrets_store.get_service_secrets`` returning the given values.

    Patched onto ``istota.secrets_store.get_service_secrets`` (the bulk
    helper) — a single SELECT is the production path, so tests mirror it.
    Empty values are dropped to mirror the real "row missing" behaviour.
    """
    table = {k: v for k, v in values.items() if v}

    def fake_get_service_secrets(db_path, user_id, service):
        return dict(table) if service == "ntfy" else {}

    return fake_get_service_secrets


class TestResolveConversationToken:
    def test_returns_briefing_token(self):
        config = Config(users={
            "alice": UserConfig(
                briefings=[BriefingConfig(name="morning", cron="0 6 * * *", conversation_token="room2")],
            ),
        })
        assert resolve_conversation_token(config, "alice") == "room2"

    def test_returns_none_for_unknown_user(self):
        config = Config()
        assert resolve_conversation_token(config, "unknown") is None

    def test_returns_none_when_no_tokens(self):
        config = Config(users={"alice": UserConfig()})
        assert resolve_conversation_token(config, "alice") is None


    def test_falls_back_to_dm_token(self):
        config = Config(users={"alice": UserConfig()})
        with patch("istota.transport.talk.get_dm_token", return_value="dm_room_abc"):
            assert resolve_conversation_token(config, "alice") == "dm_room_abc"

    def test_dm_token_not_used_when_alerts_channel_set(self):
        config = Config(users={
            "alice": UserConfig(alerts_channel="alerts_room"),
        })
        with patch("istota.transport.talk.get_dm_token", return_value="dm_room_abc"):
            assert resolve_conversation_token(config, "alice") == "alerts_room"

    def test_prefers_alerts_channel(self):
        config = Config(users={
            "alice": UserConfig(
                alerts_channel="alerts_room",
                briefings=[BriefingConfig(name="morning", cron="0 6 * * *", conversation_token="briefing_room")],
            ),
        })
        assert resolve_conversation_token(config, "alice") == "alerts_room"

    def test_falls_back_to_briefing_when_no_alerts_channel(self):
        config = Config(users={
            "alice": UserConfig(
                alerts_channel="",
                briefings=[BriefingConfig(name="morning", cron="0 6 * * *", conversation_token="briefing_room")],
            ),
        })
        assert resolve_conversation_token(config, "alice") == "briefing_room"


class TestSendTalk:
    @pytest.mark.asyncio
    async def test_sends_with_explicit_token(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig()},
        )
        with patch("istota.transport.talk.get_talk_client") as MockClient:
            mock_client = AsyncMock()
            mock_client.send_message.return_value = {"ocs": {"data": {"id": 10}}}
            MockClient.return_value = mock_client
            result = await _send_talk(config, "alice", "hello", conversation_token="room1")
        assert result == 10
        # _send_talk now delegates to TalkTransport.deliver, which always passes
        # reply_to / reference_id (both None here). Behaviour at the wire is
        # identical — TalkClient.send_message no-ops on falsy values.
        mock_client.send_message.assert_called_once_with(
            "room1", "hello", reply_to=None, reference_id=None,
        )

    @pytest.mark.asyncio
    async def test_resolves_token_from_user(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig(
                briefings=[BriefingConfig(name="morning", cron="0 6 * * *", conversation_token="room2")],
            )},
        )
        with patch("istota.transport.talk.get_talk_client") as MockClient:
            mock_client = AsyncMock()
            mock_client.send_message.return_value = {"ocs": {"data": {"id": 11}}}
            MockClient.return_value = mock_client
            result = await _send_talk(config, "alice", "hello")
        assert result == 11
        mock_client.send_message.assert_called_once_with(
            "room2", "hello", reply_to=None, reference_id=None,
        )

    @pytest.mark.asyncio
    async def test_returns_none_without_token(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig()},
        )
        result = await _send_talk(config, "alice", "hello")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_without_nextcloud(self):
        config = Config(users={"alice": UserConfig(
            briefings=[BriefingConfig(name="morning", cron="0 6 * * *", conversation_token="room1")],
        )})
        result = await _send_talk(config, "alice", "hello")
        assert result is None


    @pytest.mark.asyncio
    async def test_returns_message_id_on_success(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig()},
        )
        with patch("istota.transport.talk.get_talk_client") as MockClient:
            mock_client = AsyncMock()
            mock_client.send_message.return_value = {"ocs": {"data": {"id": 42}}}
            MockClient.return_value = mock_client
            result = await _send_talk(config, "alice", "hello", conversation_token="room1")
        assert result == 42

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig()},
        )
        with patch("istota.transport.talk.get_talk_client") as MockClient:
            mock_client = AsyncMock()
            mock_client.send_message.side_effect = Exception("fail")
            MockClient.return_value = mock_client
            result = await _send_talk(config, "alice", "hello", conversation_token="room1")
        assert result is None


class TestSendTalkConfirmation:
    def test_returns_message_id(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig()},
        )
        with patch("istota.transport.talk.get_talk_client") as MockClient:
            mock_client = AsyncMock()
            mock_client.send_message.return_value = {"ocs": {"data": {"id": 99}}}
            MockClient.return_value = mock_client
            from istota.notifications import send_talk_confirmation
            result = send_talk_confirmation(config, "alice", "Confirm?", conversation_token="room1")
        assert result == 99

    def test_returns_none_without_token(self):
        config = Config(users={"alice": UserConfig()})
        from istota.notifications import send_talk_confirmation
        result = send_talk_confirmation(config, "alice", "Confirm?")
        assert result is None


class TestSendEmail:
    @patch("istota.skills.email.send_email")
    @patch("istota.email_support.get_email_config")
    def test_sends_email(self, mock_get_config, mock_send):
        config = Config(
            email=EmailConfig(enabled=True, bot_email="bot@test.com"),
            users={"alice": UserConfig(email_addresses=["alice@test.com"])},
        )
        result = _send_email(config, "alice", "Subject", "Body")
        assert result is True
        mock_send.assert_called_once()

    def test_returns_false_without_email_addresses(self):
        config = Config(
            email=EmailConfig(enabled=True),
            users={"alice": UserConfig()},
        )
        result = _send_email(config, "alice", "Subject", "Body")
        assert result is False

    def test_returns_false_when_email_disabled(self):
        config = Config(
            email=EmailConfig(enabled=False),
            users={"alice": UserConfig(email_addresses=["alice@test.com"])},
        )
        result = _send_email(config, "alice", "Subject", "Body")
        assert result is False


class TestSendNtfyShim:
    """_send_ntfy is now a thin sync shim over the ntfy transport (the POST
    detail tests live in test_ntfy_transport.py). These verify the shim
    delegates and adapts the sync signature correctly."""

    def test_delegates_to_transport_with_options(self):
        config = Config(users={"alice": UserConfig()})
        with patch("istota.transport.ntfy.send_ntfy_async") as mock_send:
            async def _ok(*a, **k):
                return True
            mock_send.side_effect = _ok
            result = _send_ntfy(config, "alice", "msg", title="T", priority=5, tags="x")
        assert result is True
        call = mock_send.call_args
        assert call[0][0] is config
        assert call[0][1] == "alice"
        assert call[0][2] == "msg"
        opts = call[0][3]
        assert opts.title == "T" and opts.priority == 5 and opts.tags == "x"

    @patch("istota.secrets_store.get_service_secrets")
    def test_returns_false_when_topic_unset(self, mock_get_secrets):
        mock_get_secrets.return_value = {}
        config = Config(users={"alice": UserConfig()})
        assert _send_ntfy(config, "alice", "msg") is False


class TestSendNotification:
    @patch("istota.notifications._send_talk")
    def test_talk_surface(self, mock_talk):
        mock_talk.return_value = True
        config = Config(users={"alice": UserConfig()})
        result = send_notification(config, "alice", "msg", surface="talk")
        assert result is True
        mock_talk.assert_called_once()

    @patch("istota.notifications._send_email")
    def test_email_surface(self, mock_email):
        mock_email.return_value = True
        config = Config(users={"alice": UserConfig()})
        result = send_notification(config, "alice", "msg", surface="email", title="Sub")
        assert result is True
        mock_email.assert_called_once()

    @patch("istota.notifications._send_email")
    @patch("istota.notifications._send_talk")
    def test_both_surface(self, mock_talk, mock_email):
        mock_talk.return_value = True
        mock_email.return_value = True
        config = Config(users={"alice": UserConfig()})
        result = send_notification(config, "alice", "msg", surface="both", title="Sub")
        assert result is True
        mock_talk.assert_called_once()
        mock_email.assert_called_once()

    @patch("istota.notifications._send_ntfy")
    def test_ntfy_surface(self, mock_ntfy):
        mock_ntfy.return_value = True
        config = Config(users={"alice": UserConfig()})
        result = send_notification(config, "alice", "msg", surface="ntfy", title="T")
        assert result is True
        mock_ntfy.assert_called_once()

    @patch("istota.notifications._send_ntfy")
    @patch("istota.notifications._send_email")
    @patch("istota.notifications._send_talk")
    def test_all_surface(self, mock_talk, mock_email, mock_ntfy):
        mock_talk.return_value = True
        mock_email.return_value = True
        mock_ntfy.return_value = True
        config = Config(users={"alice": UserConfig()})
        result = send_notification(config, "alice", "msg", surface="all", title="T")
        assert result is True
        mock_talk.assert_called_once()
        mock_email.assert_called_once()
        mock_ntfy.assert_called_once()

    @patch("istota.notifications._send_talk")
    def test_returns_false_when_delivery_fails(self, mock_talk):
        mock_talk.return_value = False
        config = Config(users={"alice": UserConfig()})
        result = send_notification(config, "alice", "msg", surface="talk")
        assert result is False

    @patch("istota.notifications._send_talk")
    def test_passes_conversation_token(self, mock_talk):
        mock_talk.return_value = True
        config = Config(users={"alice": UserConfig()})
        send_notification(config, "alice", "msg", surface="talk", conversation_token="room1")
        _, kwargs = mock_talk.call_args
        # conversation_token is passed as positional arg to _send_talk
        assert mock_talk.call_args[0][3] == "room1" or "room1" in str(mock_talk.call_args)

    @patch("istota.notifications._send_ntfy")
    def test_passes_priority_and_tags(self, mock_ntfy):
        mock_ntfy.return_value = True
        config = Config(users={"alice": UserConfig()})
        send_notification(
            config, "alice", "msg", surface="ntfy",
            title="T", priority=5, tags="urgent",
        )
        mock_ntfy.assert_called_once_with(
            config, "alice", "msg", title="T", priority=5, tags="urgent",
        )
