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
        with patch("istota.talk_poller.get_dm_token", return_value="dm_room_abc"):
            assert resolve_conversation_token(config, "alice") == "dm_room_abc"

    def test_dm_token_not_used_when_alerts_channel_set(self):
        config = Config(users={
            "alice": UserConfig(alerts_channel="alerts_room"),
        })
        with patch("istota.talk_poller.get_dm_token", return_value="dm_room_abc"):
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
        with patch("istota.transport.talk.TalkClient") as MockClient:
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
        with patch("istota.transport.talk.TalkClient") as MockClient:
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
        with patch("istota.transport.talk.TalkClient") as MockClient:
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
        with patch("istota.transport.talk.TalkClient") as MockClient:
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
        with patch("istota.transport.talk.TalkClient") as MockClient:
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
    @patch("istota.email_poller.get_email_config")
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


class TestSendNtfy:
    """ntfy is per-user — settings live in the encrypted secrets table.

    These tests stub `secrets_store.get_service_secrets` so we don't need a real DB.
    """

    @patch("istota.notifications.httpx")
    @patch("istota.secrets_store.get_service_secrets")
    def test_sends_to_user_topic(self, mock_get_secrets, mock_httpx):
        mock_get_secrets.side_effect = _ntfy_secrets(topic="alice-topic", server_url="https://ntfy.sh")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(users={"alice": UserConfig()})
        result = _send_ntfy(config, "alice", "Hello world")
        assert result is True
        mock_httpx.post.assert_called_once()
        call_args = mock_httpx.post.call_args
        assert call_args[0][0] == "https://ntfy.sh/alice-topic"
        assert call_args[1]["content"] == "Hello world"

    @patch("istota.notifications.httpx")
    @patch("istota.secrets_store.get_service_secrets")
    def test_default_server_when_unset(self, mock_get_secrets, mock_httpx):
        # Topic only — server_url falls back to the public ntfy.sh.
        mock_get_secrets.side_effect = _ntfy_secrets(topic="t")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(users={"alice": UserConfig()})
        _send_ntfy(config, "alice", "msg")
        assert mock_httpx.post.call_args[0][0] == "https://ntfy.sh/t"

    @patch("istota.notifications.httpx")
    @patch("istota.secrets_store.get_service_secrets")
    def test_includes_token_auth_header(self, mock_get_secrets, mock_httpx):
        mock_get_secrets.side_effect = _ntfy_secrets(topic="t", token="secret")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(users={"alice": UserConfig()})
        _send_ntfy(config, "alice", "msg")
        headers = mock_httpx.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer secret"

    @patch("istota.notifications.httpx")
    @patch("istota.secrets_store.get_service_secrets")
    def test_includes_title_priority_tags(self, mock_get_secrets, mock_httpx):
        mock_get_secrets.side_effect = _ntfy_secrets(topic="t")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(users={"alice": UserConfig()})
        _send_ntfy(config, "alice", "msg", title="Alert!", priority=5, tags="warning")
        headers = mock_httpx.post.call_args[1]["headers"]
        assert headers["Title"] == "Alert!"
        assert headers["Priority"] == "5"
        assert headers["Tags"] == "warning"

    @patch("istota.notifications.httpx")
    @patch("istota.secrets_store.get_service_secrets")
    def test_default_priority_is_3(self, mock_get_secrets, mock_httpx):
        mock_get_secrets.side_effect = _ntfy_secrets(topic="t")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(users={"alice": UserConfig()})
        _send_ntfy(config, "alice", "msg")
        assert mock_httpx.post.call_args[1]["headers"]["Priority"] == "3"

    @patch("istota.notifications.httpx")
    @patch("istota.secrets_store.get_service_secrets")
    def test_strips_newlines_from_title_and_tags(self, mock_get_secrets, mock_httpx):
        """CRLF in title/tags must be stripped to prevent header injection."""
        mock_get_secrets.side_effect = _ntfy_secrets(topic="t")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(users={"alice": UserConfig()})
        _send_ntfy(
            config, "alice", "msg",
            title="Alert\r\nX-Injected: evil",
            tags="warn\nsmuggled",
        )
        headers = mock_httpx.post.call_args[1]["headers"]
        assert "\r" not in headers["Title"]
        assert "\n" not in headers["Title"]
        assert "\r" not in headers["Tags"]
        assert "\n" not in headers["Tags"]
        assert "Alert" in headers["Title"]
        assert "warn" in headers["Tags"]

    @patch("istota.secrets_store.get_service_secrets")
    def test_returns_false_when_topic_unset(self, mock_get_secrets):
        # No topic configured — ntfy is opt-in per user, so this is a no-op.
        mock_get_secrets.return_value = {}
        config = Config(users={"alice": UserConfig()})
        result = _send_ntfy(config, "alice", "msg")
        assert result is False

    @patch("istota.notifications.httpx")
    @patch("istota.secrets_store.get_service_secrets")
    def test_basic_auth(self, mock_get_secrets, mock_httpx):
        mock_get_secrets.side_effect = _ntfy_secrets(topic="t", username="user", password="pass")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(users={"alice": UserConfig()})
        _send_ntfy(config, "alice", "msg")
        headers = mock_httpx.post.call_args[1]["headers"]
        import base64
        expected = base64.b64encode(b"user:pass").decode()
        assert headers["Authorization"] == f"Basic {expected}"

    @patch("istota.notifications.httpx")
    @patch("istota.secrets_store.get_service_secrets")
    def test_token_auth_takes_precedence_over_basic(self, mock_get_secrets, mock_httpx):
        mock_get_secrets.side_effect = _ntfy_secrets(
            topic="t", token="tok", username="user", password="pass",
        )
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(users={"alice": UserConfig()})
        _send_ntfy(config, "alice", "msg")
        headers = mock_httpx.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer tok"

    @patch("istota.notifications.httpx")
    @patch("istota.secrets_store.get_service_secrets")
    def test_returns_false_on_error(self, mock_get_secrets, mock_httpx):
        mock_get_secrets.side_effect = _ntfy_secrets(topic="t")
        mock_httpx.post.side_effect = Exception("connection refused")
        config = Config(users={"alice": UserConfig()})
        result = _send_ntfy(config, "alice", "msg")
        assert result is False


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
