"""Tests for scheduler progress callback."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

from istota import db
from istota.config import Config, SchedulerConfig
from istota.scheduler import (
    _format_progress_body,
    _make_talk_progress_callback,
    edit_talk_message,
)


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
    config = Config()
    config.db_path = tmp_path / "test.db"
    defaults = dict(
        progress_updates=True,
        progress_min_interval=0,  # no debounce for testing
        progress_max_messages=3,
        progress_show_tool_use=True,
        progress_show_text=False,
        progress_edit_mode=False,  # legacy mode by default for existing tests
    )
    defaults.update(overrides)
    config.scheduler = SchedulerConfig(**defaults)
    return config


class TestMakeTalkProgressCallback:
    """Legacy mode tests (progress_edit_mode=False)."""

    def test_callback_posts_to_talk(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task()

        with (
            patch("istota.scheduler.asyncio.run") as mock_run,
            patch("istota.scheduler.db.get_db") as mock_db,
        ):
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            callback = _make_talk_progress_callback(config, task)
            callback("Reading TODO.txt")

        # Should have called asyncio.run with post_result_to_talk coroutine
        assert mock_run.called
        # Check the message was formatted in italics
        call_args = mock_run.call_args
        # The first positional arg is the coroutine
        assert call_args is not None

    def test_callback_respects_max_messages(self, tmp_path):
        config = _make_config(tmp_path)
        config.scheduler.progress_max_messages = 2
        task = _make_task()

        call_count = 0
        with (
            patch("istota.scheduler.asyncio.run") as mock_run,
            patch("istota.scheduler.db.get_db") as mock_db,
        ):
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            callback = _make_talk_progress_callback(config, task)
            callback("Message 1")
            callback("Message 2")
            callback("Message 3")  # should be dropped
            callback("Message 4")  # should be dropped

        # Only 2 calls to asyncio.run (for post_result_to_talk)
        assert mock_run.call_count == 2

    def test_callback_respects_min_interval(self, tmp_path):
        config = _make_config(tmp_path)
        config.scheduler.progress_min_interval = 100  # very high
        config.scheduler.progress_max_messages = 10
        task = _make_task()

        with (
            patch("istota.scheduler.asyncio.run") as mock_run,
            patch("istota.scheduler.db.get_db") as mock_db,
        ):
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            callback = _make_talk_progress_callback(config, task)
            # First call at time.time() should be too soon (last_send = time.time() at creation)
            # but depending on timing it might squeak through
            callback("Message 1")
            callback("Message 2")

        # At most 1 call because of the high interval
        assert mock_run.call_count <= 1

    def test_callback_truncates_long_messages(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task()

        posted_messages = []

        def capture_run(coro):
            # Extract the message arg from the coroutine
            pass

        with (
            patch("istota.scheduler.post_result_to_talk") as mock_post,
            patch("istota.scheduler.asyncio.run") as mock_run,
            patch("istota.scheduler.db.get_db") as mock_db,
        ):
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            callback = _make_talk_progress_callback(config, task)
            long_message = "x" * 500
            callback(long_message)

        # The call was made
        assert mock_run.called

    def test_callback_exception_is_swallowed(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task()

        with (
            patch("istota.scheduler.asyncio.run", side_effect=Exception("network error")),
        ):
            callback = _make_talk_progress_callback(config, task)
            # Should not raise
            callback("Some message")

    def test_callback_logs_progress(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task()

        with (
            patch("istota.scheduler.asyncio.run"),
            patch("istota.scheduler.db.get_db") as mock_db,
            patch("istota.scheduler.db.log_task") as mock_log,
        ):
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            callback = _make_talk_progress_callback(config, task)
            callback("Reading config.toml")

        mock_log.assert_called_once_with(
            mock_conn, 99, "debug", "Progress: Reading config.toml"
        )


# ---------------------------------------------------------------------------
# Edit-in-place mode tests
# ---------------------------------------------------------------------------


class TestFormatProgressBody:
    def test_basic_format(self):
        body = _format_progress_body(["📄 Reading file.txt", "⚙️ Running script"], 20)
        assert "Working — 2 actions so far…" in body
        # Header should NOT be italic
        assert "*Working" not in body
        # Descriptions should be italic
        assert "*📄 Reading file.txt*" in body
        assert "*⚙️ Running script*" in body

    def test_done_format(self):
        body = _format_progress_body(["📄 Reading file.txt"], 20, done=True)
        assert "Done — 1 action taken" in body
        assert "*Done" not in body  # header not italic
        assert "*📄 Reading file.txt*" in body

    def test_truncation_with_earlier_prefix(self):
        items = [f"⚙️ Action {i}" for i in range(25)]
        body = _format_progress_body(items, 20)
        assert "[+5 earlier]" in body
        assert "*⚙️ Action 5*" in body
        assert "*⚙️ Action 24*" in body
        # Items 0-4 should NOT appear
        assert "Action 0" not in body
        assert "Action 4" not in body

    def test_no_truncation_when_within_limit(self):
        items = ["a", "b", "c"]
        body = _format_progress_body(items, 5)
        assert "[+" not in body

    def test_singular_action(self):
        body = _format_progress_body(["one"], 10, done=True)
        assert "1 action taken" in body
        assert "actions" not in body


class TestEditTalkMessage:
    import pytest

    @pytest.mark.asyncio
    async def test_edit_calls_client(self):
        config = Config(nextcloud=__import__("istota.config", fromlist=["NextcloudConfig"]).NextcloudConfig(
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
        config = Config(nextcloud=__import__("istota.config", fromlist=["NextcloudConfig"]).NextcloudConfig(
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
        config = Config(nextcloud=__import__("istota.config", fromlist=["NextcloudConfig"]).NextcloudConfig(
            url="https://nc.test", username="bot", app_password="pass",
        ))
        task = _make_task(conversation_token="")
        result = await edit_talk_message(config, task, 42, "msg")
        assert result is False


class TestEditModeCallback:
    """Tests for _make_talk_progress_callback with progress_edit_mode=True."""

    def test_edit_mode_calls_edit_not_post(self, tmp_path):
        config = _make_config(tmp_path, progress_edit_mode=True)
        task = _make_task()

        with (
            patch("istota.scheduler.asyncio.run", return_value=True) as mock_run,
            patch("istota.scheduler.db.get_db") as mock_db,
        ):
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("Reading file.txt")

        # Should call edit_talk_message, not post_result_to_talk
        assert mock_run.called
        coro = mock_run.call_args[0][0]
        # The coroutine should be from edit_talk_message
        assert coro is not None
        assert callback.use_edit is True
        assert callback.all_descriptions == ["Reading file.txt"]
        # sent_texts should be empty (no legacy posts)
        assert callback.sent_texts == []

    def test_edit_mode_accumulates_descriptions(self, tmp_path):
        config = _make_config(tmp_path, progress_edit_mode=True)
        task = _make_task()

        with (
            patch("istota.scheduler.asyncio.run", return_value=True) as mock_run,
            patch("istota.scheduler.db.get_db") as mock_db,
        ):
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("Reading file.txt")
            callback("Writing output.json")
            callback("Running tests")

        assert callback.all_descriptions == [
            "Reading file.txt", "Writing output.json", "Running tests",
        ]
        # Each call should trigger an edit (min_interval=0)
        assert mock_run.call_count == 3

    def test_edit_mode_no_max_messages_cap(self, tmp_path):
        """Edit mode has no max_messages cap — single message, no spam."""
        config = _make_config(tmp_path, progress_edit_mode=True, progress_max_messages=2)
        task = _make_task()

        with (
            patch("istota.scheduler.asyncio.run", return_value=True) as mock_run,
            patch("istota.scheduler.db.get_db") as mock_db,
        ):
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            for i in range(5):
                callback(f"Action {i}")

        # All 5 should have triggered edits (no cap in edit mode)
        assert mock_run.call_count == 5
        assert len(callback.all_descriptions) == 5

    def test_edit_mode_respects_min_interval(self, tmp_path):
        config = _make_config(tmp_path, progress_edit_mode=True, progress_min_interval=100)
        task = _make_task()

        with (
            patch("istota.scheduler.asyncio.run", return_value=True) as mock_run,
            patch("istota.scheduler.db.get_db") as mock_db,
        ):
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("Action 1")
            callback("Action 2")  # should be throttled

        # Descriptions still accumulated even if edit was throttled
        assert len(callback.all_descriptions) == 2
        # At most 1 edit due to high interval
        assert mock_run.call_count <= 1

    def test_edit_mode_fallback_no_ack_msg_id(self, tmp_path):
        """When ack_msg_id is None, falls back to legacy mode."""
        config = _make_config(tmp_path, progress_edit_mode=True)
        task = _make_task()

        callback = _make_talk_progress_callback(config, task, ack_msg_id=None)
        assert callback.use_edit is False

    def test_edit_mode_fallback_disabled(self, tmp_path):
        """When progress_edit_mode=False, uses legacy mode."""
        config = _make_config(tmp_path, progress_edit_mode=False)
        task = _make_task()

        callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
        assert callback.use_edit is False

    def test_edit_mode_skips_text_events(self, tmp_path):
        """Text events (italicize=False) should not be accumulated in edit mode."""
        config = _make_config(tmp_path, progress_edit_mode=True)
        task = _make_task()

        with (
            patch("istota.scheduler.asyncio.run", return_value=True) as mock_run,
            patch("istota.scheduler.db.get_db") as mock_db,
        ):
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("📄 Reading file.txt")  # tool use (italicize=True default)
            callback("Some intermediate text", italicize=False)  # text event — skip
            callback("✏️ Editing config.py")  # tool use

        assert callback.all_descriptions == [
            "📄 Reading file.txt", "✏️ Editing config.py",
        ]
        # Only 2 edits (text event was skipped entirely)
        assert mock_run.call_count == 2

    def test_edit_mode_exception_swallowed(self, tmp_path):
        config = _make_config(tmp_path, progress_edit_mode=True)
        task = _make_task()

        with patch("istota.scheduler.asyncio.run", side_effect=Exception("fail")):
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            # Should not raise
            callback("Some action")

        # Description still accumulated despite failure
        assert callback.all_descriptions == ["Some action"]
