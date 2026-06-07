"""Tests for transport.ntfy — the single ntfy delivery code path.

The POST-detail tests (URL, auth, header injection) moved here from
test_notifications.py when the httpx POST was folded into the transport; they
patch ``istota.transport.ntfy.httpx`` (the new home).
"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import MagicMock, patch

from istota import db
from istota.config import Config, UserConfig
from istota.transport._types import DeliveryOptions
from istota.transport.ntfy import (
    NtfyTransport,
    _post_ntfy_blocking,
    ntfy_settings,
    send_ntfy_async,
)


def _settings(**overrides):
    base = {
        "topic": "alice-topic",
        "server_url": "https://ntfy.sh",
        "token": "",
        "username": "",
        "password": "",
    }
    base.update(overrides)
    return base


def _ntfy_secrets(**values):
    table = {k: v for k, v in values.items() if v}

    def fake_get_service_secrets(db_path, user_id, service):
        return dict(table) if service == "ntfy" else {}

    return fake_get_service_secrets


def _task(user_id="alice"):
    return db.Task(
        id=1, status="completed", source_type="scheduled", user_id=user_id,
        prompt="x", conversation_token=None, priority=5,
        attempt_count=0, max_attempts=3,
    )


class TestPostNtfyBlocking:
    @patch("istota.transport.ntfy.httpx")
    def test_url_and_content(self, mock_httpx):
        mock_httpx.post.return_value = MagicMock(raise_for_status=MagicMock())
        ok = _post_ntfy_blocking(_settings(topic="t"), "Hello", DeliveryOptions())
        assert ok is True
        assert mock_httpx.post.call_args[0][0] == "https://ntfy.sh/t"
        assert mock_httpx.post.call_args[1]["content"] == "Hello"

    @patch("istota.transport.ntfy.httpx")
    def test_token_auth(self, mock_httpx):
        mock_httpx.post.return_value = MagicMock(raise_for_status=MagicMock())
        _post_ntfy_blocking(_settings(token="secret"), "m", DeliveryOptions())
        assert mock_httpx.post.call_args[1]["headers"]["Authorization"] == "Bearer secret"

    @patch("istota.transport.ntfy.httpx")
    def test_basic_auth(self, mock_httpx):
        mock_httpx.post.return_value = MagicMock(raise_for_status=MagicMock())
        _post_ntfy_blocking(
            _settings(username="user", password="pass"), "m", DeliveryOptions(),
        )
        expected = base64.b64encode(b"user:pass").decode()
        assert mock_httpx.post.call_args[1]["headers"]["Authorization"] == f"Basic {expected}"

    @patch("istota.transport.ntfy.httpx")
    def test_token_takes_precedence_over_basic(self, mock_httpx):
        mock_httpx.post.return_value = MagicMock(raise_for_status=MagicMock())
        _post_ntfy_blocking(
            _settings(token="tok", username="user", password="pass"),
            "m", DeliveryOptions(),
        )
        assert mock_httpx.post.call_args[1]["headers"]["Authorization"] == "Bearer tok"

    @patch("istota.transport.ntfy.httpx")
    def test_title_priority_tags(self, mock_httpx):
        mock_httpx.post.return_value = MagicMock(raise_for_status=MagicMock())
        _post_ntfy_blocking(
            _settings(), "m",
            DeliveryOptions(title="Alert!", priority=5, tags="warning"),
        )
        headers = mock_httpx.post.call_args[1]["headers"]
        assert headers["Title"] == "Alert!"
        assert headers["Priority"] == "5"
        assert headers["Tags"] == "warning"

    @patch("istota.transport.ntfy.httpx")
    def test_default_priority_is_3(self, mock_httpx):
        mock_httpx.post.return_value = MagicMock(raise_for_status=MagicMock())
        _post_ntfy_blocking(_settings(), "m", DeliveryOptions())
        assert mock_httpx.post.call_args[1]["headers"]["Priority"] == "3"

    @patch("istota.transport.ntfy.httpx")
    def test_strips_header_injection(self, mock_httpx):
        mock_httpx.post.return_value = MagicMock(raise_for_status=MagicMock())
        _post_ntfy_blocking(
            _settings(), "m",
            DeliveryOptions(title="Alert\r\nX-Injected: evil", tags="warn\nsmuggled"),
        )
        headers = mock_httpx.post.call_args[1]["headers"]
        assert "\r" not in headers["Title"] and "\n" not in headers["Title"]
        assert "\r" not in headers["Tags"] and "\n" not in headers["Tags"]

    @patch("istota.transport.ntfy.httpx")
    def test_returns_false_on_error(self, mock_httpx):
        mock_httpx.post.side_effect = Exception("connection refused")
        assert _post_ntfy_blocking(_settings(), "m", DeliveryOptions()) is False


class TestNtfySettings:
    @patch("istota.secrets_store.get_service_secrets")
    def test_returns_none_when_topic_unset(self, mock_secrets):
        mock_secrets.return_value = {}
        config = Config(users={"alice": UserConfig()})
        assert ntfy_settings(config, "alice") is None

    @patch("istota.secrets_store.get_service_secrets")
    def test_default_server_when_unset(self, mock_secrets):
        mock_secrets.side_effect = _ntfy_secrets(topic="t")
        config = Config(users={"alice": UserConfig()})
        settings = ntfy_settings(config, "alice")
        assert settings["server_url"] == "https://ntfy.sh"
        assert settings["topic"] == "t"


class TestSendNtfyAsync:
    @patch("istota.transport.ntfy.httpx")
    @patch("istota.secrets_store.get_service_secrets")
    def test_sends_when_configured(self, mock_secrets, mock_httpx):
        mock_secrets.side_effect = _ntfy_secrets(topic="t")
        mock_httpx.post.return_value = MagicMock(raise_for_status=MagicMock())
        config = Config(users={"alice": UserConfig()})
        ok = asyncio.run(send_ntfy_async(config, "alice", "hi", DeliveryOptions()))
        assert ok is True
        assert mock_httpx.post.call_args[0][0] == "https://ntfy.sh/t"

    @patch("istota.secrets_store.get_service_secrets")
    def test_no_op_when_unconfigured(self, mock_secrets):
        mock_secrets.return_value = {}
        config = Config(users={"alice": UserConfig()})
        assert asyncio.run(send_ntfy_async(config, "alice", "hi")) is False


class TestNtfyTransport:
    def test_capabilities_push_no_io_on_init(self):
        config = Config(users={"alice": UserConfig()})
        t = NtfyTransport(config)
        assert t.name == "ntfy"
        assert t.capabilities.surface_class == "push"
        assert t.capabilities.supports_edit is False

    @patch("istota.secrets_store.get_service_secrets")
    def test_resolve_target_returns_topic(self, mock_secrets):
        mock_secrets.side_effect = _ntfy_secrets(topic="mytopic")
        config = Config(users={"alice": UserConfig()})
        assert NtfyTransport(config).resolve_target(_task()) == "mytopic"

    @patch("istota.secrets_store.get_service_secrets")
    def test_resolve_target_none_when_unconfigured(self, mock_secrets):
        mock_secrets.return_value = {}
        config = Config(users={"alice": UserConfig()})
        assert NtfyTransport(config).resolve_target(_task()) is None

    def test_deliver_none_without_task(self):
        config = Config(users={"alice": UserConfig()})
        assert asyncio.run(NtfyTransport(config).deliver("t", "m")) is None

    @patch("istota.transport.ntfy.httpx")
    @patch("istota.secrets_store.get_service_secrets")
    def test_deliver_posts_with_options(self, mock_secrets, mock_httpx):
        mock_secrets.side_effect = _ntfy_secrets(topic="t")
        mock_httpx.post.return_value = MagicMock(raise_for_status=MagicMock())
        config = Config(users={"alice": UserConfig()})
        asyncio.run(NtfyTransport(config).deliver(
            "t", "body", task=_task(), options=DeliveryOptions(title="T"),
        ))
        assert mock_httpx.post.call_args[1]["headers"]["Title"] == "T"

    def test_poll_returns_empty(self):
        config = Config(users={"alice": UserConfig()})
        assert asyncio.run(NtfyTransport(config).poll()) == []
