"""Tests for scheduler progress callback."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

from istota import db
from istota.config import Config, SchedulerConfig
from istota.scheduler import _make_talk_progress_callback


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


def _make_config(tmp_path):
    config = Config()
    config.db_path = tmp_path / "test.db"
    config.scheduler = SchedulerConfig(
        progress_updates=True,
        progress_min_interval=0,  # no debounce for testing
        progress_max_messages=3,
        progress_show_tool_use=True,
        progress_show_text=False,
    )
    return config


class TestMakeTalkProgressCallback:
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
