"""Tests for TalkClient bearer-auth mode + mark_conversation_read (Stage 2 of
the user-scoped Nextcloud OAuth spec)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from istota.config import Config, NextcloudConfig
from istota.talk import TalkClient


def _config() -> Config:
    return Config(
        nextcloud=NextcloudConfig(
            url="https://nc.example.com",
            username="bot",
            app_password="secret",
        )
    )


def _response(status=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_body if json_body is not None else {}
    return resp


def _wired_client(bearer_token=None, json_body=None):
    client = TalkClient(_config(), bearer_token=bearer_token)
    mock_http = AsyncMock()
    mock_http.post.return_value = _response(json_body=json_body)
    mock_http.get.return_value = _response(json_body=json_body)
    mock_http.put.return_value = _response(json_body=json_body)
    client._client = mock_http
    return client, mock_http


class TestBearerMode:
    def test_send_message_uses_bearer_header(self):
        client, http = _wired_client(bearer_token="tok123")

        asyncio.run(client.send_message("room1", "hello"))

        _, kwargs = http.post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer tok123"
        assert kwargs["auth"] is None

    def test_basic_auth_mode_unchanged(self):
        client, http = _wired_client()

        asyncio.run(client.send_message("room1", "hello"))

        _, kwargs = http.post.call_args
        assert kwargs["auth"] == ("bot", "secret")
        assert "Authorization" not in kwargs["headers"]

    def test_bearer_reference_id_passthrough(self):
        client, http = _wired_client(bearer_token="tok123")

        asyncio.run(client.send_message(
            "room1", "hello", reference_id="istota:webmirror:42",
        ))

        _, kwargs = http.post.call_args
        assert kwargs["json"]["referenceId"] == "istota:webmirror:42"

    def test_list_conversations_bearer(self):
        convs = [{"token": "t1", "unreadMessages": 3, "lastReadMessage": 100}]
        client, http = _wired_client(
            bearer_token="tok123",
            json_body={"ocs": {"data": convs}},
        )

        out = asyncio.run(client.list_conversations())

        # Read-state fields survive the parse (the raw dicts are returned).
        assert out[0]["unreadMessages"] == 3
        assert out[0]["lastReadMessage"] == 100
        _, kwargs = http.get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer tok123"


class TestMarkConversationRead:
    def test_posts_to_read_endpoint(self):
        client, http = _wired_client(bearer_token="tok123")

        ok = asyncio.run(client.mark_conversation_read("roomX"))

        assert ok is True
        args, kwargs = http.post.call_args
        assert args[0] == (
            "https://nc.example.com/ocs/v2.php/apps/spreed/api/v1/chat/roomX/read"
        )
        assert kwargs["headers"]["Authorization"] == "Bearer tok123"

    def test_returns_false_on_error(self):
        client, http = _wired_client(bearer_token="tok123")
        http.post.side_effect = RuntimeError("boom")

        ok = asyncio.run(client.mark_conversation_read("roomX"))

        assert ok is False

    def test_returns_false_on_http_error_status(self):
        client, http = _wired_client(bearer_token="tok123")
        resp = _response(status=403)
        resp.raise_for_status.side_effect = RuntimeError("403")
        http.post.return_value = resp

        ok = asyncio.run(client.mark_conversation_read("roomX"))

        assert ok is False

    def test_works_with_basic_auth_too(self):
        client, http = _wired_client()

        ok = asyncio.run(client.mark_conversation_read("roomX"))

        assert ok is True
        _, kwargs = http.post.call_args
        assert kwargs["auth"] == ("bot", "secret")
