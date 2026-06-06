"""Tests for the Talk event subscriber (replaces the old progress callback)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota import db
from istota.config import Config, NextcloudConfig
from istota.consumers import TalkEventSubscriber
from istota.events import TaskEvent
from istota.scheduler import edit_talk_message


def _make_task(**kwargs):
    defaults = dict(
        id=99,
        prompt="test",
        user_id="testuser",
        source_type="talk",
        status="running",
        conversation_token="room123",
    )
    defaults.update(kwargs)
    return db.Task(**defaults)


def _make_config(tmp_path, **overrides):
    config = Config(
        db_path=tmp_path / "test.db",
        nextcloud=NextcloudConfig(url="https://nc.test", username="bot", app_password="pw"),
    )
    return config


def _ev(kind, payload, seq=1):
    return TaskEvent(
        task_id=99, seq=seq, kind=kind, payload=payload,
        created_at="2026-06-06T00:00:00.000Z",
    )


# ---------------------------------------------------------------------------
# TalkEventSubscriber
# ---------------------------------------------------------------------------


class TestTalkEventSubscriber:
    @patch("istota.consumers.talk.asyncio.run", side_effect=lambda x: x)
    @patch("istota.scheduler.edit_talk_message", new_callable=MagicMock)
    def test_tool_start_edits_ack(self, mock_edit, mock_run, tmp_path):
        sub = TalkEventSubscriber(_make_config(tmp_path), _make_task(), ack_msg_id=100)
        sub.on_event(_ev("tool_start", {
            "tool_name": "Read", "description": "📄 Reading x.txt", "tool_call_id": "t1",
        }))
        mock_edit.assert_called_once()
        args = mock_edit.call_args.args
        assert args[2] == 100  # ack_msg_id
        assert "📄 Reading x.txt" in args[3]
        assert "s)" in args[3]  # elapsed seconds annotation
        assert sub.descriptions == ["📄 Reading x.txt"]

    @patch("istota.consumers.talk.asyncio.run", side_effect=lambda x: x)
    @patch("istota.scheduler.edit_talk_message", new_callable=MagicMock)
    def test_tool_end_annotates_with_duration(self, mock_edit, mock_run, tmp_path):
        sub = TalkEventSubscriber(_make_config(tmp_path), _make_task(), ack_msg_id=100)
        sub.on_event(_ev("tool_start", {"description": "📄 Reading x.txt"}, seq=1))
        sub.on_event(_ev("tool_end", {
            "tool_name": "Read", "tool_call_id": "t1", "success": True, "duration_ms": 180,
        }, seq=2))
        body = mock_edit.call_args.args[3]
        assert "✓" in body
        assert "180ms" in body

    @patch("istota.consumers.talk.asyncio.run", side_effect=lambda x: x)
    @patch("istota.scheduler.edit_talk_message", new_callable=MagicMock)
    def test_tool_end_failure_marks_cross(self, mock_edit, mock_run, tmp_path):
        sub = TalkEventSubscriber(_make_config(tmp_path), _make_task(), ack_msg_id=100)
        sub.on_event(_ev("tool_start", {"description": "⚙️ build"}, seq=1))
        sub.on_event(_ev("tool_end", {
            "tool_name": "Bash", "tool_call_id": "t1", "success": False, "duration_ms": 5,
        }, seq=2))
        assert "✗" in mock_edit.call_args.args[3]

    @patch("istota.consumers.talk.asyncio.run", side_effect=lambda x: x)
    @patch("istota.scheduler.edit_talk_message", new_callable=MagicMock)
    def test_no_ack_msg_id_means_no_edits(self, mock_edit, mock_run, tmp_path):
        sub = TalkEventSubscriber(_make_config(tmp_path), _make_task(), ack_msg_id=None)
        sub.on_event(_ev("tool_start", {"description": "📄 Reading x.txt"}))
        sub.on_event(_ev("error", {"message": "boom"}, seq=2))
        mock_edit.assert_not_called()
        # descriptions are still accumulated.
        assert sub.descriptions == ["📄 Reading x.txt"]

    @patch("istota.consumers.talk.asyncio.run", side_effect=lambda x: x)
    @patch("istota.scheduler.post_result_to_talk", new_callable=MagicMock)
    @patch("istota.scheduler.edit_talk_message", new_callable=MagicMock)
    def test_progress_text_posts_then_edits(self, mock_edit, mock_post, mock_run, tmp_path):
        mock_post.return_value = 555
        sub = TalkEventSubscriber(_make_config(tmp_path), _make_task(), ack_msg_id=100)
        sub.on_event(_ev("progress_text", {"text": "Working on it"}, seq=1))
        mock_post.assert_called_once()
        assert sub._text_msg_id == 555
        # Second text event edits the existing message rather than posting.
        sub.on_event(_ev("progress_text", {"text": "Still working"}, seq=2))
        mock_edit.assert_called_once()
        body = mock_edit.call_args.args[3]
        assert "Working on it" in body and "Still working" in body

    @patch("istota.consumers.talk.asyncio.run", side_effect=lambda x: x)
    @patch("istota.scheduler.edit_talk_message", new_callable=MagicMock)
    def test_result_edits_ack_with_done_summary(self, mock_edit, mock_run, tmp_path):
        sub = TalkEventSubscriber(_make_config(tmp_path), _make_task(), ack_msg_id=100)
        sub.on_event(_ev("tool_start", {"description": "📄 Reading a"}, seq=1))
        sub.on_event(_ev("tool_start", {"description": "📄 Reading b"}, seq=2))
        sub.on_event(_ev("result", {"text": "done", "truncated": False}, seq=3))
        body = mock_edit.call_args.args[3]
        assert "✅ Done" in body
        assert "2 actions" in body  # N-actions summary retained
        assert "#99" in body

    @patch("istota.consumers.talk.asyncio.run", side_effect=lambda x: x)
    @patch("istota.scheduler.edit_talk_message", new_callable=MagicMock)
    def test_result_summary_singular_and_zero(self, mock_edit, mock_run, tmp_path):
        sub = TalkEventSubscriber(_make_config(tmp_path), _make_task(), ack_msg_id=100)
        sub.on_event(_ev("result", {"text": "done"}, seq=1))
        body = mock_edit.call_args.args[3]
        assert "✅ Done" in body
        assert "action" not in body  # zero actions → no "— N actions" clause

    @patch("istota.consumers.talk.asyncio.run", side_effect=lambda x: x)
    @patch("istota.scheduler.edit_talk_message", new_callable=MagicMock)
    def test_error_edits_ack_with_failed_summary(self, mock_edit, mock_run, tmp_path):
        sub = TalkEventSubscriber(_make_config(tmp_path), _make_task(), ack_msg_id=100)
        sub.on_event(_ev("error", {"message": "Something broke"}))
        assert "❌ Failed" in mock_edit.call_args.args[3]

    @patch("istota.consumers.talk.asyncio.run", side_effect=lambda x: x)
    @patch("istota.scheduler.edit_talk_message", new_callable=MagicMock)
    def test_cancelled_edits_ack(self, mock_edit, mock_run, tmp_path):
        sub = TalkEventSubscriber(_make_config(tmp_path), _make_task(), ack_msg_id=100)
        sub.on_event(_ev("cancelled", {}))
        assert "Cancelled" in mock_edit.call_args.args[3]

    @patch("istota.consumers.talk.asyncio.run", side_effect=Exception("network"))
    @patch("istota.scheduler.edit_talk_message", new_callable=MagicMock)
    def test_edit_exception_swallowed(self, mock_edit, mock_run, tmp_path):
        sub = TalkEventSubscriber(_make_config(tmp_path), _make_task(), ack_msg_id=100)
        # Must not raise.
        sub.on_event(_ev("tool_start", {"description": "📄 Reading x.txt"}))

    @patch("istota.consumers.talk.asyncio.run", side_effect=lambda x: x)
    @patch("istota.scheduler.edit_talk_message", new_callable=MagicMock)
    def test_on_finish_is_noop(self, mock_edit, mock_run, tmp_path):
        sub = TalkEventSubscriber(_make_config(tmp_path), _make_task(), ack_msg_id=100)
        sub.on_finish()  # no result delivery here — scheduler owns that
        mock_edit.assert_not_called()


# ---------------------------------------------------------------------------
# edit_talk_message (unchanged scheduler helper)
# ---------------------------------------------------------------------------


class TestEditTalkMessage:
    @pytest.mark.asyncio
    async def test_edit_calls_client(self):
        config = Config(nextcloud=NextcloudConfig(
            url="https://nc.test", username="bot", app_password="pass",
        ))
        task = _make_task()
        with patch("istota.scheduler.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.edit_message = AsyncMock()
            result = await edit_talk_message(config, task, 42, "Updated")
        assert result is True
        mock_instance.edit_message.assert_awaited_once_with("room123", 42, "Updated")

    @pytest.mark.asyncio
    async def test_edit_returns_false_on_failure(self):
        config = Config(nextcloud=NextcloudConfig(
            url="https://nc.test", username="bot", app_password="pass",
        ))
        task = _make_task()
        with patch("istota.scheduler.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.edit_message = AsyncMock(side_effect=Exception("404"))
            result = await edit_talk_message(config, task, 42, "Updated")
        assert result is False

    @pytest.mark.asyncio
    async def test_edit_returns_false_no_url(self):
        config = Config()  # no nextcloud URL
        task = _make_task()
        result = await edit_talk_message(config, task, 42, "msg")
        assert result is False

    @pytest.mark.asyncio
    async def test_edit_returns_false_no_conversation_token(self):
        config = Config(nextcloud=NextcloudConfig(
            url="https://nc.test", username="bot", app_password="pass",
        ))
        task = _make_task(conversation_token="")
        result = await edit_talk_message(config, task, 42, "msg")
        assert result is False
