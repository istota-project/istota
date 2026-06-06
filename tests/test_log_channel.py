"""Tests for the per-user log channel feature."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota import db
from istota.config import (
    Config,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.consumers import LogChannelSubscriber
from istota.events import TaskEvent
from istota.scheduler import (
    _finalize_log_channel,
    _format_log_channel_body,
    _log_channel_source_label,
    _resolve_channel_name,
    _channel_name_cache,
    process_one_task,
)


def _tool_start(desc: str, seq: int = 1) -> TaskEvent:
    return TaskEvent(
        task_id=42, seq=seq, kind="tool_start",
        payload={"tool_name": "Read", "description": desc, "tool_call_id": ""},
        created_at="2026-06-06T00:00:00.000Z",
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestUserConfigLogChannel:
    def test_default_empty(self):
        cfg = UserConfig()
        assert cfg.log_channel == ""

    def test_set_value(self):
        cfg = UserConfig(log_channel="logroom42")
        assert cfg.log_channel == "logroom42"


# ---------------------------------------------------------------------------
# Source label
# ---------------------------------------------------------------------------

class TestLogChannelSourceLabel:
    def test_with_channel_name(self, make_task):
        task = make_task(id=42, conversation_token="abc123")
        assert _log_channel_source_label(task, "Dev Room") == ("**[#42]**", "Dev Room")

    def test_without_channel_name(self, make_task):
        task = make_task(id=99, source_type="email")
        assert _log_channel_source_label(task, None) == ("**[#99]**", "email")

    def test_cli_source(self, make_task):
        task = make_task(id=7, source_type="cli")
        assert _log_channel_source_label(task, None) == ("**[#7]**", "cli")

    def test_with_token_but_no_name(self, make_task):
        task = make_task(id=5, conversation_token="tok", source_type="talk")
        # When channel_name is None, falls back to source_type
        assert _log_channel_source_label(task, None) == ("**[#5]**", "talk")


# ---------------------------------------------------------------------------
# Format body
# ---------------------------------------------------------------------------

class TestFormatLogChannelBody:
    def test_running_with_descriptions(self):
        body = _format_log_channel_body(
            ("**[#42]**", "Dev"), ["📄 Reading file.txt", "⚙️ Running ls"],
        )
        assert "⏳ Running (2 actions) - Dev" in body
        assert "📄 Reading file.txt" in body
        assert "⚙️ Running ls" in body

    def test_done_success(self):
        body = _format_log_channel_body(
            ("**[#42]**", "Dev"), ["📄 Reading file.txt"],
            done=True, success=True,
        )
        assert "✅ Done (1 action) - Dev" in body

    def test_done_failure(self):
        body = _format_log_channel_body(
            ("**[#42]**", "Dev"), ["📄 Reading file.txt"],
            done=True, success=False, error="API Error: 500",
        )
        assert "❌ Failed (1 action) - Dev" in body
        assert "API Error: 500" in body

    def test_empty_descriptions(self):
        body = _format_log_channel_body(("**[#42]**", "cli"), [], done=True, success=True)
        assert "✅ Done (no tool calls) - cli" in body

    def test_string_prefix_compat(self):
        body = _format_log_channel_body("**[#42]**", [], done=True, success=True)
        assert "✅ Done (no tool calls)" in body
        assert "- " not in body

    def test_skills_included_in_body(self):
        body = _format_log_channel_body(
            ("**[#42]**", "Dev"), ["📄 Reading file.txt"],
            done=True, success=True, skills=["calendar", "email", "files"],
        )
        assert "Skills: calendar, email, files" in body
        # Skills line should come before tool descriptions
        lines = body.split("\n")
        skills_idx = next(i for i, l in enumerate(lines) if "Skills:" in l)
        tool_idx = next(i for i, l in enumerate(lines) if "Reading file.txt" in l)
        assert skills_idx < tool_idx

    def test_no_skills_line_when_none(self):
        body = _format_log_channel_body(
            ("**[#42]**", "Dev"), ["📄 Reading file.txt"],
            done=True, success=True,
        )
        assert "Skills:" not in body

    def test_no_skills_line_when_empty(self):
        body = _format_log_channel_body(
            ("**[#42]**", "Dev"), ["📄 Reading file.txt"],
            done=True, success=True, skills=[],
        )
        assert "Skills:" not in body

    def test_model_and_effort_inlined_in_header(self):
        body = _format_log_channel_body(
            ("**[#42]**", "Dev"), ["📄 Reading file.txt"],
            done=True, success=True,
            model="claude-sonnet-4-6", effort="low",
        )
        # Spec appended to header line
        first_line = body.split("\n")[0]
        assert "(claude-sonnet-4-6 low)" in first_line

    def test_model_only_in_header(self):
        body = _format_log_channel_body(
            ("**[#42]**", "Dev"), [],
            done=True, success=True,
            model="claude-opus-4-7",
        )
        first_line = body.split("\n")[0]
        assert "(claude-opus-4-7)" in first_line

    def test_effort_only_in_header(self):
        body = _format_log_channel_body(
            ("**[#42]**", "Dev"), [],
            done=True, success=True,
            effort="high",
        )
        first_line = body.split("\n")[0]
        assert "(high)" in first_line

    def test_no_spec_when_unset(self):
        body = _format_log_channel_body(
            ("**[#42]**", "Dev"), ["📄 Reading file.txt"],
            done=True, success=True,
        )
        first_line = body.split("\n")[0]
        # No parens after the source
        assert first_line.endswith("- Dev")

    def test_deduplicates_consecutive_descriptions(self):
        body = _format_log_channel_body(
            ("**[#42]**", "Dev"),
            ["📄 Reading _ISSUES.md", "📄 Reading _ISSUES.md", "✏️ Editing _ISSUES.md"],
            done=True, success=True,
        )
        assert "📄 Reading _ISSUES.md ×2" in body
        assert "✏️ Editing _ISSUES.md" in body
        assert body.count("📄 Reading _ISSUES.md") == 1


# ---------------------------------------------------------------------------
# Channel name resolution
# ---------------------------------------------------------------------------

class TestResolveChannelName:
    def setup_method(self):
        _channel_name_cache.clear()

    def teardown_method(self):
        _channel_name_cache.clear()

    @pytest.mark.asyncio
    async def test_resolves_display_name(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="bot", app_password="pw"),
        )
        mock_info = {"displayName": "Dev Room", "token": "abc123"}
        with patch("istota.scheduler.TalkClient") as MockClient:
            instance = MockClient.return_value
            instance.get_conversation_info = AsyncMock(return_value=mock_info)
            name = await _resolve_channel_name(config, "abc123")
        assert name == "Dev Room"
        assert _channel_name_cache["abc123"] == "Dev Room"

    @pytest.mark.asyncio
    async def test_caches_result(self):
        _channel_name_cache["cached_tok"] = "Cached Room"
        config = Config()
        name = await _resolve_channel_name(config, "cached_tok")
        assert name == "Cached Room"

    @pytest.mark.asyncio
    async def test_fallback_on_error(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="bot", app_password="pw"),
        )
        with patch("istota.scheduler.TalkClient") as MockClient:
            instance = MockClient.return_value
            instance.get_conversation_info = AsyncMock(side_effect=Exception("network error"))
            name = await _resolve_channel_name(config, "fail_tok")
        assert name == "fail_tok"
        assert _channel_name_cache["fail_tok"] == "fail_tok"


# ---------------------------------------------------------------------------
# Log channel callback
# ---------------------------------------------------------------------------

class TestLogChannelSubscriber:
    def _make_config(self, tmp_path):
        return Config(
            db_path=tmp_path / "test.db",
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="bot", app_password="pw"),
            temp_dir=tmp_path / "temp",
        )

    def _make_task(self, **overrides):
        defaults = dict(
            id=42, status="running", source_type="talk",
            user_id="testuser", prompt="test",
            conversation_token="work_room",
        )
        defaults.update(overrides)
        return db.Task(**defaults)

    @patch("istota.consumers.log_channel.asyncio.run")
    def test_first_event_posts_message(self, mock_arun, tmp_path):
        # The first send now goes through TalkTransport.deliver, which returns
        # the posted message id directly (not the raw OCS dict).
        mock_arun.return_value = 100
        sub = LogChannelSubscriber(self._make_config(tmp_path), self._make_task(), "logroom", "[42 #Dev]")

        sub.on_event(_tool_start("📄 Reading file.txt"))

        assert len(sub.all_descriptions) == 1
        assert sub.log_msg_id[0] == 100
        assert mock_arun.called

    @patch("istota.scheduler.edit_talk_message", new_callable=MagicMock)
    @patch("istota.consumers.log_channel.asyncio.run")
    def test_subsequent_events_edit_message(self, mock_arun, mock_edit, tmp_path):
        mock_arun.return_value = 100
        sub = LogChannelSubscriber(self._make_config(tmp_path), self._make_task(), "logroom", "[42 #Dev]")

        sub.on_event(_tool_start("📄 Reading file.txt", seq=1))
        assert sub.log_msg_id[0] == 100

        mock_arun.return_value = True
        sub.on_event(_tool_start("⚙️ Running ls", seq=2))
        assert len(sub.all_descriptions) == 2

    @patch("istota.consumers.log_channel.asyncio.run")
    def test_ignores_non_tool_events(self, mock_arun, tmp_path):
        sub = LogChannelSubscriber(self._make_config(tmp_path), self._make_task(), "logroom", "[42 #Dev]")

        sub.on_event(TaskEvent(
            task_id=42, seq=1, kind="progress_text",
            payload={"text": "Some intermediate text"},
            created_at="2026-06-06T00:00:00.000Z",
        ))
        assert len(sub.all_descriptions) == 0
        assert not mock_arun.called

    @patch("istota.consumers.log_channel.asyncio.run", side_effect=Exception("network"))
    def test_errors_dont_propagate(self, mock_arun, tmp_path):
        sub = LogChannelSubscriber(self._make_config(tmp_path), self._make_task(), "logroom", "[42 #Dev]")

        # Should not raise — but the description is still recorded.
        sub.on_event(_tool_start("📄 Reading file.txt"))
        assert len(sub.all_descriptions) == 1


# ---------------------------------------------------------------------------
# Finalize log channel
# ---------------------------------------------------------------------------

class TestFinalizeLogChannel:
    def _make_config(self, tmp_path):
        return Config(
            db_path=tmp_path / "test.db",
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="bot", app_password="pw"),
            temp_dir=tmp_path / "temp",
        )

    def _make_task(self, **overrides):
        defaults = dict(
            id=42, status="running", source_type="talk",
            user_id="testuser", prompt="test",
        )
        defaults.update(overrides)
        return db.Task(**defaults)

    @patch("istota.scheduler.asyncio.run")
    def test_edits_existing_message_on_success(self, mock_arun, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        cb = MagicMock()
        cb.all_descriptions = ["📄 Reading file.txt"]
        cb.log_msg_id = [100]

        _finalize_log_channel(config, task, "logroom", "[42 #Dev]", cb, True)
        mock_arun.assert_called()

    @patch("istota.scheduler.asyncio.run")
    def test_posts_one_liner_when_no_tool_calls(self, mock_arun, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()

        _finalize_log_channel(config, task, "logroom", "[42 #Dev]", None, True)
        mock_arun.assert_called()
        # Should have been called with a send_message (no msg to edit)
        call_args = mock_arun.call_args
        assert call_args is not None

    @patch("istota.scheduler.TalkClient")
    def test_one_liner_unpacks_tuple_prefix(self, mock_talk_cls, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        mock_client = MagicMock()
        mock_talk_cls.return_value = mock_client
        mock_client.send_message = AsyncMock(return_value=200)

        _finalize_log_channel(
            config, task, "logroom", ("**[#42]**", "#istota"), None, True,
        )
        mock_client.send_message.assert_called_once()
        msg = mock_client.send_message.call_args[0][1]
        assert "**[#42]**" in msg
        assert "('#" not in msg  # no raw tuple
        assert "#istota" in msg

    @patch("istota.scheduler.asyncio.run")
    def test_includes_error_on_failure(self, mock_arun, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        cb = MagicMock()
        cb.all_descriptions = ["📄 Reading file.txt"]
        cb.log_msg_id = [100]

        _finalize_log_channel(
            config, task, "logroom", "[42 #Dev]", cb, False,
            error="API Error: 500",
        )
        mock_arun.assert_called()

    @patch("istota.scheduler.asyncio.run", side_effect=Exception("network"))
    def test_errors_dont_propagate(self, mock_arun, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        # Should not raise
        _finalize_log_channel(config, task, "logroom", "[42 #Dev]", None, True)


# ---------------------------------------------------------------------------
# Integration: process_one_task with log channel
# ---------------------------------------------------------------------------

class TestProcessOneTaskLogChannel:
    def _make_config(self, db_path, tmp_path, users=None):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
            users=users or {},
        )

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    @patch("istota.scheduler._finalize_log_channel")
    def test_log_channel_finalized_on_success(
        self, mock_finalize, mock_arun, mock_exec, db_path, tmp_path,
    ):
        users = {"testuser": UserConfig(log_channel="logroom")}
        config = self._make_config(db_path, tmp_path, users=users)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Hello", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is True
        mock_finalize.assert_called_once()
        # Verify success=True in the finalize call (config, task, log_channel, prefix, log_callback, success)
        assert mock_finalize.call_args[0][5] is True

    @patch("istota.scheduler.execute_task", return_value=(False, "Boom", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    @patch("istota.scheduler._finalize_log_channel")
    def test_log_channel_finalized_on_failure(
        self, mock_finalize, mock_arun, mock_exec, db_path, tmp_path,
    ):
        users = {"testuser": UserConfig(log_channel="logroom")}
        config = self._make_config(db_path, tmp_path, users=users)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Doom", user_id="testuser", source_type="cli")
            # Exhaust retries so it fails permanently
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False
        mock_finalize.assert_called_once()
        # Verify success=False and error passed (config, task, log_channel, prefix, log_callback, success)
        assert mock_finalize.call_args[0][5] is False

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    @patch("istota.scheduler._finalize_log_channel")
    def test_skip_log_channel_suppresses_logging(
        self, mock_finalize, mock_arun, mock_exec, db_path, tmp_path,
    ):
        users = {"testuser": UserConfig(log_channel="logroom")}
        config = self._make_config(db_path, tmp_path, users=users)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="Hello", user_id="testuser",
                source_type="scheduled", skip_log_channel=True,
            )

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is True
        mock_finalize.assert_not_called()

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    @patch("istota.scheduler._finalize_log_channel")
    def test_no_log_channel_when_unconfigured(
        self, mock_finalize, mock_arun, mock_exec, db_path, tmp_path,
    ):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Hello", user_id="testuser", source_type="cli")

        process_one_task(config)
        mock_finalize.assert_not_called()

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None, None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    @patch("istota.scheduler._resolve_channel_name")
    @patch("istota.scheduler._finalize_log_channel")
    def test_channel_name_resolved_for_talk_source(
        self, mock_finalize, mock_resolve, mock_arun, mock_exec, db_path, tmp_path,
    ):
        mock_resolve.return_value = "Dev Room"

        users = {"testuser": UserConfig(log_channel="logroom")}
        config = self._make_config(db_path, tmp_path, users=users)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="Hello", user_id="testuser",
                source_type="talk", conversation_token="dev_tok",
            )

        process_one_task(config)
        # Channel name should have been resolved
        mock_resolve.assert_called()

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=42)
    @patch("istota.scheduler._finalize_log_channel")
    def test_subscribers_wired_for_talk_with_progress(
        self, mock_finalize, mock_arun, mock_exec, db_path, tmp_path,
    ):
        """When both Talk progress and log channel are active, the executor
        receives an EventWriter with both subscribers registered."""
        mock_exec.return_value = (True, "Done", None, None)

        users = {"testuser": UserConfig(log_channel="logroom")}
        config = self._make_config(db_path, tmp_path, users=users)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="Hello", user_id="testuser",
                source_type="talk", conversation_token="dev_tok",
            )

        process_one_task(config)

        assert mock_exec.called
        # execute_task is called with event_writer=<EventWriter> carrying the
        # Talk + log channel subscribers (push is gated to talk source too).
        _, kwargs = mock_exec.call_args
        writer = kwargs["event_writer"]
        sub_types = {type(s).__name__ for s in writer.subscribers}
        assert "TalkEventSubscriber" in sub_types
        assert "LogChannelSubscriber" in sub_types
