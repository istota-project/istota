"""Configuration loading for istota.notifications module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota.config import (
    BriefingConfig,
    Config,
    EmailConfig,
    NextcloudConfig,
    NtfyConfig,
    UserConfig,
)
from istota.notifications import (
    _send_email,
    _send_ntfy,
    _send_talk,
    resolve_conversation_token,
    send_notification,
)


class TestResolveConversationToken:
    def test_returns_invoicing_token(self):
        config = Config(users={
            "alice": UserConfig(invoicing_conversation_token="room1"),
        })
        assert resolve_conversation_token(config, "alice") == "room1"

    def test_falls_back_to_briefing_token(self):
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


class TestSendTalk:
    @pytest.mark.asyncio
    async def test_sends_with_explicit_token(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig()},
        )
        with patch("istota.talk.TalkClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value = mock_client
            result = await _send_talk(config, "alice", "hello", conversation_token="room1")
        assert result is True
        mock_client.send_message.assert_called_once_with("room1", "hello")

    @pytest.mark.asyncio
    async def test_resolves_token_from_user(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig(invoicing_conversation_token="room2")},
        )
        with patch("istota.talk.TalkClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value = mock_client
            result = await _send_talk(config, "alice", "hello")
        assert result is True
        mock_client.send_message.assert_called_once_with("room2", "hello")

    @pytest.mark.asyncio
    async def test_returns_false_without_token(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig()},
        )
        result = await _send_talk(config, "alice", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_without_nextcloud(self):
        config = Config(users={"alice": UserConfig(invoicing_conversation_token="room1")})
        result = await _send_talk(config, "alice", "hello")
        assert result is False


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
    @patch("istota.notifications.httpx")
    def test_sends_to_global_topic(self, mock_httpx):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(
            ntfy=NtfyConfig(enabled=True, server_url="https://ntfy.sh", topic="test-topic"),
            users={"alice": UserConfig()},
        )
        result = _send_ntfy(config, "alice", "Hello world")
        assert result is True
        mock_httpx.post.assert_called_once()
        call_args = mock_httpx.post.call_args
        assert call_args[0][0] == "https://ntfy.sh/test-topic"
        assert call_args[1]["content"] == "Hello world"

    @patch("istota.notifications.httpx")
    def test_sends_to_user_topic_override(self, mock_httpx):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(
            ntfy=NtfyConfig(enabled=True, server_url="https://ntfy.sh", topic="global"),
            users={"alice": UserConfig(ntfy_topic="alice-topic")},
        )
        result = _send_ntfy(config, "alice", "Hello")
        assert result is True
        assert mock_httpx.post.call_args[0][0] == "https://ntfy.sh/alice-topic"

    @patch("istota.notifications.httpx")
    def test_includes_auth_header(self, mock_httpx):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(
            ntfy=NtfyConfig(enabled=True, topic="t", token="secret"),
            users={"alice": UserConfig()},
        )
        _send_ntfy(config, "alice", "msg")
        headers = mock_httpx.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer secret"

    @patch("istota.notifications.httpx")
    def test_includes_title_and_priority(self, mock_httpx):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(
            ntfy=NtfyConfig(enabled=True, topic="t"),
            users={"alice": UserConfig()},
        )
        _send_ntfy(config, "alice", "msg", title="Alert!", priority=5, tags="warning")
        headers = mock_httpx.post.call_args[1]["headers"]
        assert headers["Title"] == "Alert!"
        assert headers["Priority"] == "5"
        assert headers["Tags"] == "warning"

    def test_returns_false_when_disabled(self):
        config = Config(
            ntfy=NtfyConfig(enabled=False),
            users={"alice": UserConfig()},
        )
        result = _send_ntfy(config, "alice", "msg")
        assert result is False

    def test_returns_false_without_topic(self):
        config = Config(
            ntfy=NtfyConfig(enabled=True, topic=""),
            users={"alice": UserConfig()},
        )
        result = _send_ntfy(config, "alice", "msg")
        assert result is False

    @patch("istota.notifications.httpx")
    def test_basic_auth(self, mock_httpx):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(
            ntfy=NtfyConfig(enabled=True, topic="t", username="user", password="pass"),
            users={"alice": UserConfig()},
        )
        _send_ntfy(config, "alice", "msg")
        headers = mock_httpx.post.call_args[1]["headers"]
        import base64
        expected = base64.b64encode(b"user:pass").decode()
        assert headers["Authorization"] == f"Basic {expected}"

    @patch("istota.notifications.httpx")
    def test_token_auth_takes_precedence_over_basic(self, mock_httpx):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(
            ntfy=NtfyConfig(enabled=True, topic="t", token="tok", username="user", password="pass"),
            users={"alice": UserConfig()},
        )
        _send_ntfy(config, "alice", "msg")
        headers = mock_httpx.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer tok"

    @patch("istota.notifications.httpx")
    def test_explicit_topic_overrides_user_and_global(self, mock_httpx):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        config = Config(
            ntfy=NtfyConfig(enabled=True, server_url="https://ntfy.sh", topic="global"),
            users={"alice": UserConfig(ntfy_topic="user-topic")},
        )
        _send_ntfy(config, "alice", "msg", ntfy_topic="explicit-topic")
        assert mock_httpx.post.call_args[0][0] == "https://ntfy.sh/explicit-topic"

    @patch("istota.notifications.httpx")
    def test_returns_false_on_error(self, mock_httpx):
        mock_httpx.post.side_effect = Exception("connection refused")
        config = Config(
            ntfy=NtfyConfig(enabled=True, topic="t"),
            users={"alice": UserConfig()},
        )
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
            config, "alice", "msg", title="T", priority=5, tags="urgent", ntfy_topic=None,
        )

    @patch("istota.notifications._send_ntfy")
    def test_passes_ntfy_topic(self, mock_ntfy):
        mock_ntfy.return_value = True
        config = Config(users={"alice": UserConfig()})
        send_notification(
            config, "alice", "msg", surface="ntfy", ntfy_topic="custom-topic",
        )
        assert mock_ntfy.call_args[1]["ntfy_topic"] == "custom-topic"
