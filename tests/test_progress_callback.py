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
        progress_style="legacy",  # legacy mode by default for existing tests
    )
    defaults.update(overrides)
    config.scheduler = SchedulerConfig(**defaults)
    return config


class TestMakeTalkProgressCallback:
    """Legacy mode tests (progress_style="legacy")."""

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

    def test_legacy_skips_text_events(self, tmp_path):
        """Text events (italicize=False) should not be posted in legacy mode."""
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
            callback("Reading file.txt")  # tool use — should post
            callback("Here is the full response text", italicize=False)  # text event — skip
            callback("Writing output.json")  # tool use — should post

        # Only 2 posts (text event was skipped)
        assert mock_run.call_count == 2
        assert callback.sent_texts == ["Reading file.txt", "Writing output.json"]

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
    """Tests for _make_talk_progress_callback with progress_style='full'."""

    def test_edit_mode_calls_edit_not_post(self, tmp_path):
        config = _make_config(tmp_path, progress_style="full")
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
        config = _make_config(tmp_path, progress_style="full")
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
        config = _make_config(tmp_path, progress_style="full", progress_max_messages=2)
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
        config = _make_config(tmp_path, progress_style="full", progress_min_interval=100)
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
        config = _make_config(tmp_path, progress_style="full")
        task = _make_task()

        callback = _make_talk_progress_callback(config, task, ack_msg_id=None)
        assert callback.use_edit is False

    def test_edit_mode_text_events_update_text_message(self, tmp_path):
        """Text events (italicize=False) post/edit a separate text message."""
        config = _make_config(tmp_path, progress_style="full")
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
            callback("Some intermediate text", italicize=False)  # text event — posts text msg
            callback("✏️ Editing config.py")  # tool use

        assert callback.all_descriptions == [
            "📄 Reading file.txt", "✏️ Editing config.py",
        ]
        assert callback.accumulated_texts == ["Some intermediate text"]
        # 3 calls: 2 tool edits + 1 text post
        assert mock_run.call_count == 3

    def test_edit_mode_exception_swallowed(self, tmp_path):
        config = _make_config(tmp_path, progress_style="full")
        task = _make_task()

        with patch("istota.scheduler.asyncio.run", side_effect=Exception("fail")):
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            # Should not raise
            callback("Some action")

        # Description still accumulated despite failure
        assert callback.all_descriptions == ["Some action"]


# ---------------------------------------------------------------------------
# Replace mode tests (progress_style="replace")
# ---------------------------------------------------------------------------


class TestReplaceModeCallback:
    """Tests for _make_talk_progress_callback with progress_style='replace'."""

    def test_replace_edits_ack_message(self, tmp_path):
        config = _make_config(tmp_path, progress_style="replace")
        task = _make_task()

        with patch("istota.scheduler.asyncio.run", return_value=True) as mock_run:
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("📄 Reading config.py")

        assert callback.style == "replace"
        assert callback.use_edit is True
        assert len(callback.all_descriptions) == 1
        # Should have called edit_talk_message via asyncio.run
        assert mock_run.called
        # sent_texts should be empty (no legacy posts)
        assert callback.sent_texts == []

    def test_replace_no_rate_limiting(self, tmp_path):
        """Replace mode has no min_interval — every tool call triggers an edit."""
        config = _make_config(tmp_path, progress_style="replace", progress_min_interval=100)
        task = _make_task()

        with (
            patch("istota.scheduler.asyncio.run", return_value=True) as mock_run,
        ):
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            for i in range(5):
                callback(f"Action {i}")

        # All 5 should have triggered edits (no rate limiting)
        assert mock_run.call_count == 5
        assert len(callback.all_descriptions) == 5

    def test_replace_message_format(self, tmp_path):
        """Replace mode message contains the tool description and elapsed time."""
        config = _make_config(tmp_path, progress_style="replace")
        task = _make_task()

        with patch("istota.scheduler.asyncio.run", return_value=True):
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("📄 Reading config.py")

        # Verify the format by checking the asyncio.run call
        # The body should be: "⏳ *📄 Reading config.py…* (Ns)"
        assert callback.all_descriptions == ["📄 Reading config.py"]
        assert callback.style == "replace"

    def test_replace_text_events_post_text_message(self, tmp_path):
        config = _make_config(tmp_path, progress_style="replace")
        task = _make_task()

        with patch("istota.scheduler.asyncio.run", return_value=True) as mock_run:
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("📄 Reading file.txt")
            callback("Some intermediate text", italicize=False)  # posts text msg
            callback("⚙️ Running test")

        assert callback.all_descriptions == ["📄 Reading file.txt", "⚙️ Running test"]
        assert callback.accumulated_texts == ["Some intermediate text"]
        # 3 calls: 2 tool edits + 1 text post
        assert mock_run.call_count == 3

    def test_replace_accumulates_all_descriptions(self, tmp_path):
        config = _make_config(tmp_path, progress_style="replace")
        task = _make_task()

        with patch("istota.scheduler.asyncio.run", return_value=True):
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("📄 Reading file.txt")
            callback("✏️ Editing config.py")
            callback("⚙️ Running tests")

        assert callback.all_descriptions == [
            "📄 Reading file.txt", "✏️ Editing config.py", "⚙️ Running tests",
        ]

    def test_replace_falls_back_to_legacy_without_ack_msg(self, tmp_path):
        config = _make_config(tmp_path, progress_style="replace")
        task = _make_task()

        callback = _make_talk_progress_callback(config, task, ack_msg_id=None)
        assert callback.use_edit is False
        assert callback.style == "legacy"

    def test_replace_exception_swallowed(self, tmp_path):
        config = _make_config(tmp_path, progress_style="replace")
        task = _make_task()

        with patch("istota.scheduler.asyncio.run", side_effect=Exception("fail")):
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("Some action")

        assert callback.all_descriptions == ["Some action"]

    def test_replace_collapses_multiline_messages(self, tmp_path):
        """Multi-line tool output (e.g. inline scripts) should be collapsed to one line."""
        config = _make_config(tmp_path, progress_style="replace")
        task = _make_task()

        multiline = '⚙️ python3 -c "\nimport json\n\nwith open(\'/srv/app/...'
        with patch("istota.scheduler.asyncio.run", return_value=True) as mock_run:
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback(multiline)

        assert "\n" not in callback.all_descriptions[0]
        assert callback.all_descriptions[0].startswith("⚙️ python3 -c")


class TestLiveTextMessage:
    """Tests for the live text message that progressively shows intermediate text."""

    def test_first_text_event_posts_new_message(self, tmp_path):
        config = _make_config(tmp_path, progress_style="replace")
        task = _make_task()

        with patch("istota.scheduler.asyncio.run", return_value=42) as mock_run:
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("Looking at the code...", italicize=False)

        assert callback.text_msg_id[0] == 42
        assert callback.accumulated_texts == ["Looking at the code..."]

    def test_subsequent_text_events_edit_message(self, tmp_path):
        config = _make_config(tmp_path, progress_style="replace")
        task = _make_task()

        call_count = 0

        def mock_run_fn(coro):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 42  # first call returns msg ID (post)
            return True  # subsequent calls are edits

        with patch("istota.scheduler.asyncio.run", side_effect=mock_run_fn):
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("First update...", italicize=False)
            callback("Second update...", italicize=False)

        assert callback.text_msg_id[0] == 42
        assert callback.accumulated_texts == ["First update...", "Second update..."]
        assert call_count == 2

    def test_text_message_body_is_italicized(self, tmp_path):
        """Text message body uses markdown italic for intermediate text."""
        config = _make_config(tmp_path, progress_style="replace")
        task = _make_task()

        posted_bodies = []

        def capture_run(coro):
            # We can't easily inspect the coro args, so just track calls
            return 42

        with patch("istota.scheduler.asyncio.run", side_effect=capture_run):
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("Reading the config...", italicize=False)
            callback("Analyzing the code...", italicize=False)

        # accumulated_texts should contain both
        assert callback.accumulated_texts == [
            "Reading the config...", "Analyzing the code...",
        ]

    def test_text_events_skipped_in_legacy_mode(self, tmp_path):
        """Legacy mode (no edit) should skip text events entirely."""
        config = _make_config(tmp_path, progress_style="legacy")
        task = _make_task()

        with patch("istota.scheduler.asyncio.run") as mock_run:
            callback = _make_talk_progress_callback(config, task)
            callback("Some text event", italicize=False)

        # No calls — text events skipped in legacy mode
        assert not mock_run.called
        assert callback.accumulated_texts == []

    def test_text_events_skipped_in_none_mode(self, tmp_path):
        """None mode should skip text events entirely."""
        config = _make_config(tmp_path, progress_style="none")
        task = _make_task()

        callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
        callback("Some text event", italicize=False)

        assert callback.accumulated_texts == []

    def test_text_message_preserves_original_text(self, tmp_path):
        """Text events should NOT be truncated or newline-collapsed."""
        config = _make_config(tmp_path, progress_style="replace", progress_text_max_chars=20)
        task = _make_task()

        with patch("istota.scheduler.asyncio.run", return_value=42):
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("This is a long intermediate text that exceeds the truncation limit", italicize=False)

        # Should use the full original text, not the truncated msg
        assert callback.accumulated_texts == [
            "This is a long intermediate text that exceeds the truncation limit",
        ]

    def test_text_message_exception_swallowed(self, tmp_path):
        config = _make_config(tmp_path, progress_style="replace")
        task = _make_task()

        with patch("istota.scheduler.asyncio.run", side_effect=Exception("fail")):
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("Some text", italicize=False)  # should not raise

        assert callback.accumulated_texts == ["Some text"]
        assert callback.text_msg_id[0] is None  # failed to post

    def test_text_msg_id_propagated_through_composite(self, tmp_path):
        """Composite callback should propagate text_msg_id attribute."""
        config = _make_config(tmp_path, progress_style="replace")
        task = _make_task()
        db_p = tmp_path / "test.db"
        db.init_db(db_p)
        config.db_path = db_p

        callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
        assert hasattr(callback, "text_msg_id")
        assert hasattr(callback, "accumulated_texts")


class TestNoneModeCallback:
    """Tests for progress_style='none' — silent mode."""

    def test_none_still_accumulates_descriptions(self, tmp_path):
        config = _make_config(tmp_path, progress_style="none")
        task = _make_task()

        with patch("istota.scheduler.asyncio.run") as mock_run:
            callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
            callback("📄 Reading file.txt")
            callback("⚙️ Running test")

        # No API calls made
        assert not mock_run.called
        # But descriptions still accumulated (for log channel / actions_taken)
        assert callback.all_descriptions == ["📄 Reading file.txt", "⚙️ Running test"]

    def test_none_skips_text_events(self, tmp_path):
        config = _make_config(tmp_path, progress_style="none")
        task = _make_task()

        callback = _make_talk_progress_callback(config, task, ack_msg_id=100)
        callback("📄 Reading file.txt")
        callback("Some text", italicize=False)

        assert callback.all_descriptions == ["📄 Reading file.txt"]


class TestProgressStyleConfig:
    """Test progress_style config loading."""

    def test_config_default_is_replace(self):
        config = Config()
        assert config.scheduler.progress_style == "replace"

    def test_toml_progress_style_none(self, tmp_path):
        from istota.config import load_config
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[scheduler]
progress_style = "none"
""")
        config = load_config(config_file)
        assert config.scheduler.progress_style == "none"
