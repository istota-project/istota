"""Tests for istota.talk module."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from istota.talk import TalkClient, split_message, truncate_message, clean_message_content
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
    async def test_edit_message(self, client):
        mock_http = _mock_httpx_client()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": {}}}
        mock_http.put = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.edit_message("room1", 42, "Updated text")

        mock_http.put.assert_called_once()
        call_kwargs = mock_http.put.call_args
        assert "room1/42" in call_kwargs.args[0]
        assert call_kwargs.kwargs["json"] == {"message": "Updated text"}
        assert call_kwargs.kwargs["auth"] == ("istota", "pass")
        assert call_kwargs.kwargs["headers"]["OCS-APIRequest"] == "true"

    @pytest.mark.asyncio
    async def test_edit_message_raises_on_http_error(self, client):
        mock_http = _mock_httpx_client()
        import httpx
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(),
        )
        mock_http.put = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            with pytest.raises(httpx.HTTPStatusError):
                await client.edit_message("room1", 99, "fail")

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


class TestGetParticipants:
    @pytest.mark.asyncio
    async def test_basic_call(self, client):
        mock_http = _mock_httpx_client()
        participants = [
            {"actorId": "alice", "actorType": "users"},
            {"actorId": "istota", "actorType": "users"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": participants}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.get_participants("room1")

        assert result == participants
        call_kwargs = mock_http.get.call_args
        assert "/participants" in call_kwargs.args[0]
        assert "room1" in call_kwargs.args[0]


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


class TestSendMessageReferenceId:
    @pytest.mark.asyncio
    async def test_reference_id_included_in_body(self, client):
        mock_http = _mock_httpx_client()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": {"id": 50}}}
        mock_http.post = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            await client.send_message("room1", "Hello!", reference_id="istota:task:42:result")

        call_kwargs = mock_http.post.call_args
        assert call_kwargs.kwargs["json"] == {
            "message": "Hello!",
            "referenceId": "istota:task:42:result",
        }

    @pytest.mark.asyncio
    async def test_reference_id_omitted_when_none(self, client):
        mock_http = _mock_httpx_client()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": {"id": 51}}}
        mock_http.post = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            await client.send_message("room1", "Hello!")

        call_kwargs = mock_http.post.call_args
        assert "referenceId" not in call_kwargs.kwargs["json"]

    @pytest.mark.asyncio
    async def test_reference_id_with_reply(self, client):
        mock_http = _mock_httpx_client()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": {"id": 52}}}
        mock_http.post = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            await client.send_message(
                "room1", "Reply!", reply_to=10, reference_id="istota:task:5:ack",
            )

        call_kwargs = mock_http.post.call_args
        assert call_kwargs.kwargs["json"] == {
            "message": "Reply!",
            "replyTo": 10,
            "referenceId": "istota:task:5:ack",
        }


class TestFetchChatHistory:
    @pytest.mark.asyncio
    async def test_returns_oldest_first(self, client):
        mock_http = _mock_httpx_client()
        # API returns newest-first
        messages = [
            {"id": 3, "message": "C"},
            {"id": 2, "message": "B"},
            {"id": 1, "message": "A"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": messages}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.fetch_chat_history("room1", limit=50)

        assert [m["id"] for m in result] == [1, 2, 3]
        call_kwargs = mock_http.get.call_args
        assert call_kwargs.kwargs["params"]["lookIntoFuture"] == 0
        assert call_kwargs.kwargs["params"]["limit"] == 50

    @pytest.mark.asyncio
    async def test_empty_room(self, client):
        mock_http = _mock_httpx_client()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": []}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.fetch_chat_history("room1")

        assert result == []

    @pytest.mark.asyncio
    async def test_default_limit(self, client):
        mock_http = _mock_httpx_client()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": []}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            await client.fetch_chat_history("room1")

        call_kwargs = mock_http.get.call_args
        assert call_kwargs.kwargs["params"]["limit"] == 100


class TestGetConversationInfo:
    @pytest.mark.asyncio
    async def test_returns_room_data(self, client):
        mock_http = _mock_httpx_client()
        room_data = {"token": "room1", "displayName": "Project Planning", "type": 2}
        mock_response = MagicMock()
        mock_response.json.return_value = {"ocs": {"data": room_data}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.get_conversation_info("room1")

        assert result == room_data
        call_kwargs = mock_http.get.call_args
        assert "/api/v4/room/room1" in call_kwargs.args[0]


class TestFetchFullHistory:
    @pytest.mark.asyncio
    async def test_single_batch(self, client):
        """When all messages fit in one batch, returns oldest-first."""
        mock_http = _mock_httpx_client()
        # API returns newest-first
        messages = [{"id": 3, "message": "C"}, {"id": 2, "message": "B"}, {"id": 1, "message": "A"}]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ocs": {"data": messages}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.fetch_full_history("room1", batch_size=200)

        assert [m["id"] for m in result] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_multiple_batches(self, client):
        """Paginates backwards to get full history."""
        mock_http = _mock_httpx_client()

        # First call (no lastKnownMessageId): newest batch (full batch_size)
        batch1 = [{"id": 4, "message": "D"}, {"id": 3, "message": "C"}]
        # Second call (lastKnownMessageId=3): older batch (less than batch_size = done)
        batch2 = [{"id": 1, "message": "A"}]

        resp1 = MagicMock(status_code=200)
        resp1.json.return_value = {"ocs": {"data": batch1}}
        resp2 = MagicMock(status_code=200)
        resp2.json.return_value = {"ocs": {"data": batch2}}

        mock_http.get = AsyncMock(side_effect=[resp1, resp2])

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.fetch_full_history("room1", batch_size=2)

        assert [m["id"] for m in result] == [1, 3, 4]
        assert mock_http.get.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_room(self, client):
        mock_http = _mock_httpx_client()
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"ocs": {"data": []}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.fetch_full_history("room1")

        assert result == []

    @pytest.mark.asyncio
    async def test_304_stops_pagination(self, client):
        mock_http = _mock_httpx_client()
        batch1 = [{"id": 2, "message": "B"}, {"id": 1, "message": "A"}]
        resp1 = MagicMock(status_code=200)
        resp1.json.return_value = {"ocs": {"data": batch1}}
        resp2 = MagicMock(status_code=304)

        mock_http.get = AsyncMock(side_effect=[resp1, resp2])

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.fetch_full_history("room1", batch_size=2)

        assert [m["id"] for m in result] == [1, 2]


class TestFetchMessagesSince:
    @pytest.mark.asyncio
    async def test_single_batch(self, client):
        mock_http = _mock_httpx_client()
        messages = [{"id": 11, "message": "new1"}, {"id": 12, "message": "new2"}]
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"ocs": {"data": messages}}
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.fetch_messages_since("room1", since_id=10)

        assert [m["id"] for m in result] == [11, 12]
        call_kwargs = mock_http.get.call_args
        params = call_kwargs.kwargs["params"]
        assert params["lookIntoFuture"] == 1
        assert params["timeout"] == 0
        assert params["lastKnownMessageId"] == 10

    @pytest.mark.asyncio
    async def test_no_new_messages(self, client):
        mock_http = _mock_httpx_client()
        mock_response = MagicMock(status_code=304)
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.fetch_messages_since("room1", since_id=10)

        assert result == []

    @pytest.mark.asyncio
    async def test_multiple_batches(self, client):
        mock_http = _mock_httpx_client()
        batch1 = [{"id": 11, "message": "A"}, {"id": 12, "message": "B"}]
        batch2 = [{"id": 13, "message": "C"}]
        resp1 = MagicMock(status_code=200)
        resp1.json.return_value = {"ocs": {"data": batch1}}
        resp2 = MagicMock(status_code=200)
        resp2.json.return_value = {"ocs": {"data": batch2}}
        mock_http.get = AsyncMock(side_effect=[resp1, resp2])

        with patch("istota.talk.httpx.AsyncClient", return_value=mock_http):
            result = await client.fetch_messages_since("room1", since_id=10, batch_size=2)

        assert [m["id"] for m in result] == [11, 12, 13]


class TestCleanMessageContent:
    def test_basic_text(self):
        msg = {"message": "Hello world", "messageParameters": {}}
        assert clean_message_content(msg) == "Hello world"

    def test_file_placeholder(self):
        msg = {
            "message": "Check this {file0}",
            "messageParameters": {"file0": {"name": "photo.jpg"}},
        }
        assert clean_message_content(msg) == "Check this [photo.jpg]"

    def test_bot_mention_stripped(self):
        msg = {
            "message": "{mention-user0} do something",
            "messageParameters": {"mention-user0": {"type": "user", "id": "istota", "name": "Istota"}},
        }
        assert clean_message_content(msg, bot_username="istota") == "do something"

    def test_other_mention_replaced(self):
        msg = {
            "message": "Hey {mention-user0}",
            "messageParameters": {"mention-user0": {"type": "user", "id": "alice", "name": "Alice"}},
        }
        assert clean_message_content(msg, bot_username="istota") == "Hey @Alice"

    def test_empty_params_list(self):
        msg = {"message": "Hello {file0}", "messageParameters": []}
        assert clean_message_content(msg) == "Hello {file0}"
