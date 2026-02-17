"""Configuration loading for istota.talk module."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from istota.talk import TalkClient, split_message, truncate_message
from istota.config import Config, NextcloudConfig


@pytest.fixture
def config():
    return Config(
        nextcloud=NextcloudConfig(
            url="https://nc.test",
            username="istota",
            app_password="pass",
        )
    )


@pytest.fixture
def client(config):
    return TalkClient(config)


def _mock_httpx_client():
    """Create a mock httpx.AsyncClient that works as an async context manager."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestTalkClient:
    @pytest.mark.asyncio
    async def test_send_message(self, client):
        mock_http = _mock_httpx_client()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": {"id": 42}}}
        mock_http.post = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.send_message("room1", "Hello!")

        mock_http.post.assert_called_once()
        call_kwargs = mock_http.post.call_args
        assert "room1" in call_kwargs.args[0]
        assert call_kwargs.kwargs["json"] == {"message": "Hello!"}
        assert call_kwargs.kwargs["auth"] == ("istota", "pass")
        assert result == {"ocs": {"data": {"id": 42}}}

    @pytest.mark.asyncio
    async def test_send_message_with_reply(self, client):
        mock_http = _mock_httpx_client()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": {"id": 43}}}
        mock_http.post = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.send_message("room1", "Reply!", reply_to=10)

        call_kwargs = mock_http.post.call_args
        assert call_kwargs.kwargs["json"] == {"message": "Reply!", "replyTo": 10}

    @pytest.mark.asyncio
    async def test_list_conversations(self, client):
        mock_http = _mock_httpx_client()
        rooms = [{"token": "room1"}, {"token": "room2"}]
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": rooms}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.list_conversations()

        assert result == rooms
        call_kwargs = mock_http.get.call_args
        assert "/api/v4/room" in call_kwargs.args[0]

    @pytest.mark.asyncio
    async def test_poll_messages_new(self, client):
        """With last_known_message_id, uses lookIntoFuture=1."""
        mock_http = _mock_httpx_client()
        messages = [{"id": 11, "message": "Hi"}]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ocs": {"data": messages}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.poll_messages("room1", last_known_message_id=10, timeout=5)

        assert result == messages
        call_kwargs = mock_http.get.call_args
        params = call_kwargs.kwargs["params"]
        assert params["lookIntoFuture"] == 1
        assert params["lastKnownMessageId"] == 10
        assert params["timeout"] == 5

    @pytest.mark.asyncio
    async def test_poll_messages_304_empty(self, client):
        """304 response returns empty list."""
        mock_http = _mock_httpx_client()
        mock_response = MagicMock()
        mock_response.status_code = 304
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.poll_messages("room1", last_known_message_id=10)

        assert result == []

    @pytest.mark.asyncio
    async def test_poll_messages_history(self, client):
        """Without last_known_message_id, uses lookIntoFuture=0 and reverses."""
        mock_http = _mock_httpx_client()
        # API returns newest-first
        messages = [{"id": 3, "message": "C"}, {"id": 2, "message": "B"}, {"id": 1, "message": "A"}]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ocs": {"data": messages}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.poll_messages("room1")

        # Should be reversed to oldest-first
        assert result == [{"id": 1, "message": "A"}, {"id": 2, "message": "B"}, {"id": 3, "message": "C"}]
        call_kwargs = mock_http.get.call_args
        params = call_kwargs.kwargs["params"]
        assert params["lookIntoFuture"] == 0

    @pytest.mark.asyncio
    async def test_get_latest_message_id(self, client):
        mock_http = _mock_httpx_client()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": [{"id": 99, "message": "latest"}]}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.get_latest_message_id("room1")

        assert result == 99
        call_kwargs = mock_http.get.call_args
        assert call_kwargs.kwargs["params"] == {"lookIntoFuture": 0, "limit": 1}

    @pytest.mark.asyncio
    async def test_get_latest_message_id_empty(self, client):
        mock_http = _mock_httpx_client()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": []}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.get_latest_message_id("room1")

        assert result is None


class TestTruncateMessage:
    def test_short_unchanged(self):
        msg = "Hello world"
        assert truncate_message(msg) == msg

    def test_long_truncated(self):
        msg = "x" * 5000
        result = truncate_message(msg, max_length=100)
        assert len(result) == 100
        assert result.endswith("[Message truncated - full response available in task log]")

    def test_exact_length_unchanged(self):
        msg = "a" * 4000
        assert truncate_message(msg) == msg


class TestSplitMessage:
    def test_short_message_single_part(self):
        msg = "Hello world"
        assert split_message(msg) == ["Hello world"]

    def test_exact_limit_single_part(self):
        msg = "a" * 4000
        assert split_message(msg) == [msg]

    def test_splits_on_paragraph_boundary(self):
        para1 = "a" * 2000
        para2 = "b" * 2000
        msg = f"{para1}\n\n{para2}"
        parts = split_message(msg, max_length=2020)
        assert len(parts) == 2
        assert parts[0].startswith("a")
        assert parts[0].endswith("(1/2)")
        assert parts[1].startswith("b")
        assert parts[1].endswith("(2/2)")

    def test_splits_on_newline_when_no_paragraph_break(self):
        line1 = "a" * 2000
        line2 = "b" * 2000
        msg = f"{line1}\n{line2}"
        parts = split_message(msg, max_length=2020)
        assert len(parts) == 2

    def test_splits_on_sentence_boundary(self):
        sentence1 = "a" * 1990 + "."
        sentence2 = " " + "b" * 1990
        msg = sentence1 + sentence2
        parts = split_message(msg, max_length=2020)
        assert len(parts) == 2
        # Part content (before page indicator) should end with "."
        content = parts[0].rsplit(" (", 1)[0]
        assert content.rstrip().endswith(".")

    def test_hard_split_no_boundaries(self):
        msg = "x" * 8000
        parts = split_message(msg, max_length=4000)
        assert len(parts) >= 2
        total_content = "".join(p.rsplit(" (", 1)[0] for p in parts)
        assert "x" * 8000 == total_content

    def test_multiple_splits(self):
        msg = "\n\n".join(["a" * 1500] * 5)
        parts = split_message(msg, max_length=2000)
        assert len(parts) > 2
        for i, part in enumerate(parts):
            assert part.endswith(f"({i + 1}/{len(parts)})")

    def test_all_parts_within_limit(self):
        msg = "x" * 10000
        parts = split_message(msg, max_length=4000)
        for part in parts:
            assert len(part) <= 4000
