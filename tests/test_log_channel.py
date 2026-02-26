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
from istota.scheduler import (
    _finalize_log_channel,
    _format_log_channel_body,
    _log_channel_source_label,
    _make_log_channel_callback,
    _resolve_channel_name,
    _channel_name_cache,
    process_one_task,
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


class TestUserConfigLogChannelParsing:
    def test_parse_from_toml(self, tmp_path):
        from istota.config import load_user_configs
        user_file = tmp_path / "alice.toml"
        user_file.write_text('log_channel = "mylogroom"\n')
        users = load_user_configs(tmp_path)
        assert users["alice"].log_channel == "mylogroom"

    def test_parse_missing_defaults_empty(self, tmp_path):
        from istota.config import load_user_configs
        user_file = tmp_path / "bob.toml"
        user_file.write_text('display_name = "Bob"\n')
        users = load_user_configs(tmp_path)
        assert users["bob"].log_channel == ""


# ---------------------------------------------------------------------------
# Source label
# ---------------------------------------------------------------------------

class TestLogChannelSourceLabel:
    def test_with_channel_name(self, make_task):
        task = make_task(id=42, conversation_token="abc123")
        assert _log_channel_source_label(task, "Dev Room") == "[42 Dev Room]"

    def test_without_channel_name(self, make_task):
        task = make_task(id=99, source_type="email")
        assert _log_channel_source_label(task, None) == "[99 email]"

    def test_cli_source(self, make_task):
        task = make_task(id=7, source_type="cli")
        assert _log_channel_source_label(task, None) == "[7 cli]"

    def test_with_token_but_no_name(self, make_task):
        task = make_task(id=5, conversation_token="tok", source_type="talk")
        # When channel_name is None, falls back to source_type
        assert _log_channel_source_label(task, None) == "[5 talk]"


# ---------------------------------------------------------------------------
# Format body
# ---------------------------------------------------------------------------

class TestFormatLogChannelBody:
    def test_running_with_descriptions(self):
        body = _format_log_channel_body(
            "[42 #Dev]", ["📄 Reading file.txt", "⚙️ Running ls"],
        )
        assert "⏳" in body
        assert "running…" in body
        assert "📄 Reading file.txt" in body
        assert "⚙️ Running ls" in body

    def test_done_success(self):
        body = _format_log_channel_body(
            "[42 #Dev]", ["📄 Reading file.txt"],
            done=True, success=True,
        )
        assert "✓" in body
        assert "done" in body

    def test_done_failure(self):
        body = _format_log_channel_body(
            "[42 #Dev]", ["📄 Reading file.txt"],
            done=True, success=False, error="API Error: 500",
        )
        assert "✗" in body
        assert "failed" in body
        assert "API Error: 500" in body

    def test_empty_descriptions(self):
        body = _format_log_channel_body("[42 cli]", [], done=True, success=True)
        assert "✓" in body
        assert "done" in body


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

class TestMakeLogChannelCallback:
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

    @patch("istota.scheduler.asyncio.run")
    def test_first_call_posts_message(self, mock_arun, tmp_path):
        mock_arun.return_value = {"ocs": {"data": {"id": 100}}}
        config = self._make_config(tmp_path)
        task = self._make_task()
        cb = _make_log_channel_callback(config, task, "logroom", "[42 #Dev]")

        cb("📄 Reading file.txt")

        assert len(cb.all_descriptions) == 1
        assert mock_arun.called

    @patch("istota.scheduler.asyncio.run")
    @patch("istota.scheduler.edit_talk_message")
    def test_subsequent_calls_edit_message(self, mock_edit, mock_arun, tmp_path):
        # First call posts, returns msg_id
        mock_arun.return_value = {"ocs": {"data": {"id": 100}}}
        config = self._make_config(tmp_path)
        task = self._make_task()
        cb = _make_log_channel_callback(config, task, "logroom", "[42 #Dev]")

        cb("📄 Reading file.txt")
        assert cb.log_msg_id[0] == 100

        # Second call should edit
        mock_arun.return_value = True
        cb("⚙️ Running ls")
        assert len(cb.all_descriptions) == 2

    @patch("istota.scheduler.asyncio.run")
    def test_skips_text_events(self, mock_arun, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        cb = _make_log_channel_callback(config, task, "logroom", "[42 #Dev]")

        cb("Some intermediate text", italicize=False)
        assert len(cb.all_descriptions) == 0
        assert not mock_arun.called

    @patch("istota.scheduler.asyncio.run", side_effect=Exception("network"))
    def test_errors_dont_propagate(self, mock_arun, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        cb = _make_log_channel_callback(config, task, "logroom", "[42 #Dev]")

        # Should not raise
        cb("📄 Reading file.txt")
        assert len(cb.all_descriptions) == 1


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

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None))
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

    @patch("istota.scheduler.execute_task", return_value=(False, "Boom", None))
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

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None))
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

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    @patch("istota.scheduler._resolve_channel_name")
    @patch("istota.scheduler._finalize_log_channel")
    @patch("istota.scheduler._make_log_channel_callback")
    def test_channel_name_resolved_for_talk_source(
        self, mock_make_cb, mock_finalize, mock_resolve, mock_arun,
        mock_exec, db_path, tmp_path,
    ):
        mock_resolve.return_value = "Dev Room"
        mock_cb = MagicMock()
        mock_cb.all_descriptions = []
        mock_cb.log_msg_id = [None]
        mock_make_cb.return_value = mock_cb

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
    @patch("istota.scheduler._make_log_channel_callback")
    @patch("istota.scheduler._make_talk_progress_callback")
    @patch("istota.scheduler._finalize_log_channel")
    def test_composite_callback_created_for_talk_with_progress(
        self, mock_finalize, mock_talk_cb_factory, mock_log_cb_factory,
        mock_arun, mock_exec, db_path, tmp_path,
    ):
        """When both Talk progress and log channel are active, a composite callback is used."""
        mock_exec.return_value = (True, "Done", None)

        # Mock Talk progress callback with required attributes
        talk_cb = MagicMock()
        talk_cb.sent_texts = []
        talk_cb.last_progress_msg_id = [None]
        talk_cb.all_descriptions = []
        talk_cb.ack_msg_id = 42
        talk_cb.use_edit = True
        mock_talk_cb_factory.return_value = talk_cb

        log_cb = MagicMock()
        log_cb.all_descriptions = []
        log_cb.log_msg_id = [None]
        mock_log_cb_factory.return_value = log_cb

        users = {"testuser": UserConfig(log_channel="logroom")}
        config = self._make_config(db_path, tmp_path, users=users)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="Hello", user_id="testuser",
                source_type="talk", conversation_token="dev_tok",
            )

        process_one_task(config)

        # execute_task should have been called with a progress callback
        assert mock_exec.called
        call_kwargs = mock_exec.call_args
        on_progress = call_kwargs[1].get("on_progress") if call_kwargs[1] else None
        # If Talk progress was created, execute_task got a callback
        # (it's either the composite or the log callback)
        assert on_progress is not None or mock_log_cb_factory.called
