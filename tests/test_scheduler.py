"""Configuration loading for istota.scheduler module."""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from zoneinfo import ZoneInfo

import pytest

from istota.scheduler import (
    CONFIRMATION_PATTERN,
    PROGRESS_MESSAGES,
    WorkerPool,
    download_talk_attachments,
    get_worker_id,
    strip_briefing_preamble,
    _parse_email_output,
    _talk_poll_loop,
    _format_error_for_user,
    _strip_action_prefix,
    _execute_command_task,
    check_briefings,
    check_scheduled_jobs,
    post_result_to_email,
    process_one_task,
    _make_talk_progress_callback,
)
from istota.config import (
    Config,
    SchedulerConfig,
    TalkConfig,
    NextcloudConfig,
    UserConfig,
    BriefingConfig,
    EmailConfig,
)
from istota import db


# ---------------------------------------------------------------------------
# TestConfirmationPattern
# ---------------------------------------------------------------------------


class TestConfirmationPattern:
    def test_matches_i_need_your_confirmation(self):
        assert CONFIRMATION_PATTERN.search("I need your confirmation before proceeding.")

    def test_matches_please_confirm(self):
        assert CONFIRMATION_PATTERN.search("Please confirm that you want to delete this file.")

    def test_matches_reply_yes(self):
        assert CONFIRMATION_PATTERN.search('Reply "yes" to continue.')

    def test_matches_should_i_proceed(self):
        assert CONFIRMATION_PATTERN.search("Should I proceed with the deletion?")

    def test_matches_can_you_confirm(self):
        assert CONFIRMATION_PATTERN.search("Can you confirm this action?")

    def test_matches_do_you_want_me_to_proceed(self):
        assert CONFIRMATION_PATTERN.search("Do you want me to proceed?")

    def test_no_match_regular_text(self):
        assert CONFIRMATION_PATTERN.search("Here is the weather forecast.") is None
        assert CONFIRMATION_PATTERN.search("Task completed successfully.") is None

    def test_case_insensitive(self):
        assert CONFIRMATION_PATTERN.search("PLEASE CONFIRM this action")
        assert CONFIRMATION_PATTERN.search("i need your confirmation")
        assert CONFIRMATION_PATTERN.search("Reply Yes or No")


# ---------------------------------------------------------------------------
# TestFormatErrorForUser
# ---------------------------------------------------------------------------


class TestFormatErrorForUser:
    def test_formats_500_error(self):
        error = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"},"request_id":"req_abc"}'
        result = _format_error_for_user(error)
        assert "mothership" in result.lower()
        assert "API Error" not in result
        assert "req_abc" not in result

    def test_formats_503_error(self):
        error = 'API Error: 503 {"type":"error","error":{"type":"overloaded_error","message":"Service unavailable"}}'
        result = _format_error_for_user(error)
        assert "mothership" in result.lower()

    def test_formats_529_error(self):
        error = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"API overloaded"}}'
        result = _format_error_for_user(error)
        assert "mothership" in result.lower()

    def test_formats_429_error(self):
        error = 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}'
        result = _format_error_for_user(error)
        assert "throttled" in result.lower()
        assert "chatty" in result.lower()

    def test_formats_401_error(self):
        error = 'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Invalid API key"}}'
        result = _format_error_for_user(error)
        assert "authenticate" in result.lower()
        assert "locked out" in result.lower()

    def test_formats_403_error(self):
        error = 'API Error: 403 {"type":"error","error":{"type":"permission_error","message":"Forbidden"}}'
        result = _format_error_for_user(error)
        assert "authenticate" in result.lower()

    def test_formats_other_api_error(self):
        error = 'API Error: 422 {"type":"error","error":{"type":"invalid_request_error","message":"Bad request"}}'
        result = _format_error_for_user(error)
        assert "void stared back" in result.lower()

    def test_formats_oom_error(self):
        error = "Claude Code was killed (likely out of memory)"
        result = _format_error_for_user(error)
        assert "memory" in result.lower()
        assert "simpler" in result.lower()

    def test_formats_timeout_error(self):
        error = "Task execution timed out after 10 minutes"
        result = _format_error_for_user(error)
        assert "timed out" in result.lower()

    def test_formats_generic_error(self):
        error = "Something completely unexpected happened in the system"
        result = _format_error_for_user(error)
        assert "sideways" in result.lower()
        assert "try again" in result.lower()
        # Should not expose the raw error
        assert "unexpected happened" not in result

    def test_hides_request_id(self):
        error = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"},"request_id":"req_011CXoLgXCH9oBmyPDc1SuMQ"}'
        result = _format_error_for_user(error)
        assert "req_011CXoLgXCH9oBmyPDc1SuMQ" not in result

    def test_hides_raw_json(self):
        error = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"}}'
        result = _format_error_for_user(error)
        assert "{" not in result
        assert "}" not in result


# ---------------------------------------------------------------------------
# TestParseEmailOutput
# ---------------------------------------------------------------------------


class TestParseEmailOutput:
    def test_valid_json(self):
        msg = '{"subject": "Hello", "body": "World", "format": "plain"}'
        result = _parse_email_output(msg)
        assert result["subject"] == "Hello"
        assert result["body"] == "World"
        assert result["format"] == "plain"

    def test_json_in_code_fence(self):
        msg = 'Here is the response:\n```json\n{"subject": "Re: Test", "body": "Got it", "format": "html"}\n```'
        result = _parse_email_output(msg)
        assert result["subject"] == "Re: Test"
        assert result["body"] == "Got it"
        assert result["format"] == "html"

    def test_json_with_preamble(self):
        msg = 'I have composed the reply:\n{"subject": "Update", "body": "Details here", "format": "plain"}'
        result = _parse_email_output(msg)
        assert result["subject"] == "Update"
        assert result["body"] == "Details here"

    def test_plain_text_fallback(self):
        msg = "Just a plain text response with no JSON at all."
        result = _parse_email_output(msg)
        assert result["body"] == msg
        assert result["format"] == "plain"
        assert result["subject"] is None

    def test_invalid_json_fallback(self):
        msg = '{"broken json'
        result = _parse_email_output(msg)
        assert result["body"] == msg
        assert result["format"] == "plain"

    def test_missing_body_fallback(self):
        # Valid JSON but missing required "body" key
        msg = '{"subject": "No body here", "format": "plain"}'
        result = _parse_email_output(msg)
        # Falls back because "body" is required
        assert result["body"] == msg
        assert result["format"] == "plain"

    def test_invalid_format_normalized(self):
        msg = '{"subject": "Test", "body": "Content", "format": "markdown"}'
        result = _parse_email_output(msg)
        assert result["body"] == "Content"
        assert result["format"] == "plain"  # invalid format normalized to plain

    def test_subject_optional(self):
        msg = '{"body": "Just body", "format": "html"}'
        result = _parse_email_output(msg)
        assert result["subject"] is None
        assert result["body"] == "Just body"
        assert result["format"] == "html"


# ---------------------------------------------------------------------------
# TestDownloadTalkAttachments
# ---------------------------------------------------------------------------


class TestDownloadTalkAttachments:
    def test_mount_path_exists(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        talk_dir = mount / "Talk"
        talk_dir.mkdir()
        (talk_dir / "photo.jpg").write_bytes(b"fake image")

        config = Config(nextcloud_mount_path=mount)
        result = download_talk_attachments(config, ["Talk/photo.jpg"])
        assert len(result) == 1
        assert result[0] == str(mount / "Talk" / "photo.jpg")

    def test_mount_path_not_exists(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        # No Talk/file.jpg on disk
        config = Config(nextcloud_mount_path=mount)
        result = download_talk_attachments(config, ["Talk/missing.jpg"])
        assert len(result) == 1
        # Falls back to original path
        assert result[0] == "Talk/missing.jpg"

    @patch("istota.scheduler.subprocess.run")
    def test_rclone_download(self, mock_run, tmp_path):
        temp_dir = tmp_path / "temp"
        temp_dir.mkdir()
        # Simulate rclone creating the file
        (temp_dir / "doc.pdf").write_bytes(b"pdf content")
        mock_run.return_value = MagicMock(returncode=0)

        config = Config(
            nextcloud_mount_path=None,
            rclone_remote="nc",
            temp_dir=temp_dir,
        )
        result = download_talk_attachments(config, ["Talk/doc.pdf"])
        assert len(result) == 1
        assert result[0] == str(temp_dir / "doc.pdf")
        mock_run.assert_called_once()

    def test_non_talk_path_unchanged(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        result = download_talk_attachments(config, ["/some/other/path.txt"])
        assert result == ["/some/other/path.txt"]

    def test_empty_list(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        result = download_talk_attachments(config, [])
        assert result == []


# ---------------------------------------------------------------------------
# TestCheckBriefings
# ---------------------------------------------------------------------------


class TestCheckBriefings:
    def test_no_briefings(self, db_path):
        config = Config(db_path=db_path, users={})
        with db.get_db(db_path) as conn:
            result = check_briefings(conn, config)
        assert result == []

    @patch("istota.scheduler.build_briefing_prompt", return_value="Test briefing prompt")
    def test_cron_triggers_briefing(self, mock_build, db_path):
        # Briefing at 6 AM UTC, we pretend it is 6:05 AM UTC
        briefing = BriefingConfig(
            name="morning",
            cron="0 6 * * *",
            conversation_token="room1",
            components={"calendar": True},
        )
        user = UserConfig(
            display_name="Test",
            timezone="UTC",
            briefings=[briefing],
        )
        config = Config(db_path=db_path, users={"alice": user})

        # Set last run to yesterday
        with db.get_db(db_path) as conn:
            yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
            conn.execute(
                "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
                ("alice", "morning", yesterday),
            )

        with db.get_db(db_path) as conn:
            result = check_briefings(conn, config)

        assert len(result) == 1
        mock_build.assert_called_once()

    @patch("istota.scheduler.build_briefing_prompt", return_value="Test prompt")
    def test_cron_not_yet_due(self, mock_build, db_path):
        briefing = BriefingConfig(
            name="morning",
            cron="0 6 * * *",
            conversation_token="room1",
            components={},
        )
        user = UserConfig(
            display_name="Test",
            timezone="UTC",
            briefings=[briefing],
        )
        config = Config(db_path=db_path, users={"alice": user})

        # Set last run to just now (so next cron is tomorrow)
        with db.get_db(db_path) as conn:
            now = datetime.now(ZoneInfo("UTC")).isoformat()
            conn.execute(
                "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
                ("alice", "morning", now),
            )

        with db.get_db(db_path) as conn:
            result = check_briefings(conn, config)

        assert result == []

    @patch("istota.scheduler.build_briefing_prompt", return_value="Test prompt")
    @patch("istota.scheduler._now")
    def test_first_run_past_scheduled(self, mock_now, mock_build, db_path):
        """First run: no last_run_at, and we are past the cron time today."""
        # Cron at 6am, mock current time to 14:00 so it's reliably in the past
        mock_now.return_value = datetime(2026, 6, 15, 14, 0, 0, tzinfo=ZoneInfo("UTC"))

        briefing = BriefingConfig(
            name="past",
            cron="0 6 * * *",
            conversation_token="room1",
            components={},
        )
        user = UserConfig(timezone="UTC", briefings=[briefing])
        config = Config(db_path=db_path, users={"alice": user})

        with db.get_db(db_path) as conn:
            result = check_briefings(conn, config)
        assert len(result) == 1

    @patch("istota.scheduler.build_briefing_prompt", return_value="Test prompt")
    def test_first_run_before_scheduled(self, mock_build, db_path):
        """First run: no last_run_at, but cron time is in the future today."""
        # Use a cron at 23:59 so we haven't reached it yet (unless it is 23:59)
        briefing = BriefingConfig(
            name="late",
            cron="59 23 * * *",
            conversation_token="room1",
            components={},
        )
        user = UserConfig(timezone="UTC", briefings=[briefing])
        config = Config(db_path=db_path, users={"alice": user})

        now = datetime.now(ZoneInfo("UTC"))
        if now.hour < 23 or (now.hour == 23 and now.minute < 59):
            with db.get_db(db_path) as conn:
                result = check_briefings(conn, config)
            assert result == []

    def test_missing_conversation_token_skipped_for_talk(self, db_path):
        briefing = BriefingConfig(
            name="no_token",
            cron="0 6 * * *",
            conversation_token="",  # empty token, output defaults to "talk"
            components={},
        )
        user = UserConfig(timezone="UTC", briefings=[briefing])
        config = Config(db_path=db_path, users={"alice": user})

        with db.get_db(db_path) as conn:
            result = check_briefings(conn, config)
        assert result == []

    @patch("istota.scheduler.build_briefing_prompt", return_value="Test briefing prompt")
    def test_email_briefing_without_conversation_token(self, mock_build, db_path):
        briefing = BriefingConfig(
            name="morning",
            cron="0 6 * * *",
            conversation_token="",
            output="email",
            components={"calendar": True},
        )
        user = UserConfig(timezone="UTC", briefings=[briefing])
        config = Config(db_path=db_path, users={"alice": user})

        # Set last run to yesterday so cron evaluates as due
        with db.get_db(db_path) as conn:
            yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
            conn.execute(
                "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
                ("alice", "morning", yesterday),
            )

        with db.get_db(db_path) as conn:
            result = check_briefings(conn, config)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# TestCheckScheduledJobs
# ---------------------------------------------------------------------------


class TestCheckScheduledJobs:
    @patch("istota.scheduler._sync_cron_files")
    def test_no_jobs(self, mock_sync, db_path):
        config = Config(db_path=db_path, users={})
        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)
        assert result == []

    @patch("istota.scheduler._sync_cron_files")
    def test_job_triggers(self, mock_sync, db_path):
        """A job whose cron has passed since last_run should trigger."""
        user = UserConfig(timezone="UTC")
        config = Config(db_path=db_path, users={"alice": user})

        yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token, enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "daily-check", "0 0 * * *", "Run daily check", "room1", 1, yesterday, yesterday),
            )

        now = datetime.now(ZoneInfo("UTC"))
        if now.hour > 0:
            with db.get_db(db_path) as conn:
                result = check_scheduled_jobs(conn, config)
            assert len(result) == 1

    @patch("istota.scheduler._sync_cron_files")
    def test_job_not_yet_due(self, mock_sync, db_path):
        """A job that just ran should not trigger again."""
        user = UserConfig(timezone="UTC")
        config = Config(db_path=db_path, users={"alice": user})

        now = datetime.now(ZoneInfo("UTC")).isoformat()
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token, enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "hourly", "0 * * * *", "Hourly task", "room1", 1, now, now),
            )

        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)
        assert result == []

    @patch("istota.scheduler._sync_cron_files")
    def test_first_run_uses_created_at(self, mock_sync, db_path):
        """When last_run_at is NULL, created_at is used as base for cron evaluation."""
        user = UserConfig(timezone="UTC")
        config = Config(db_path=db_path, users={"alice": user})

        # Created yesterday, cron every minute -- should be due
        yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token, enabled, last_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "every-min", "* * * * *", "Frequent task", "room1", 1, None, yesterday),
            )

        with db.get_db(db_path) as conn:
            result = check_scheduled_jobs(conn, config)
        assert len(result) == 1

    def test_sync_called_before_evaluation(self, db_path):
        """_sync_cron_files should be called at the start of check_scheduled_jobs."""
        user = UserConfig(timezone="UTC")
        config = Config(db_path=db_path, users={"alice": user})
        with patch("istota.scheduler._sync_cron_files") as mock_sync:
            with db.get_db(db_path) as conn:
                check_scheduled_jobs(conn, config)
            mock_sync.assert_called_once_with(conn, config)


class TestSyncCronFiles:
    """Tests for _sync_cron_files edge cases."""

    def test_empty_file_with_db_jobs_triggers_migration(self, db_path, tmp_path):
        """When CRON.md exists but is empty and DB has jobs, migrate to file."""
        from istota.scheduler import _sync_cron_files

        mount = tmp_path / "mount"
        mount.mkdir()
        user = UserConfig(timezone="UTC")
        config = Config(
            db_path=db_path, users={"alice": user},
            nextcloud_mount_path=mount,
        )

        # Create empty CRON.md (like seeded template)
        from istota.storage import get_user_cron_path
        cron_path = mount / get_user_cron_path("alice", "istota").lstrip("/")
        cron_path.parent.mkdir(parents=True, exist_ok=True)
        cron_path.write_text("# Scheduled Jobs\n\n```toml\n```\n")

        # Add a DB job
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "daily-check", "0 9 * * *", "Run check"),
            )

        # Sync should migrate DB jobs to file, not delete them
        with db.get_db(db_path) as conn:
            _sync_cron_files(conn, config)
            jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert len(jobs) == 1
        assert jobs[0].name == "daily-check"
        # File should now contain the job
        content = cron_path.read_text()
        assert 'name = "daily-check"' in content

    def test_empty_file_no_db_jobs_is_noop(self, db_path, tmp_path):
        """When CRON.md is empty and DB has no jobs, nothing happens."""
        from istota.scheduler import _sync_cron_files

        mount = tmp_path / "mount"
        mount.mkdir()
        user = UserConfig(timezone="UTC")
        config = Config(
            db_path=db_path, users={"alice": user},
            nextcloud_mount_path=mount,
        )

        from istota.storage import get_user_cron_path
        cron_path = mount / get_user_cron_path("alice", "istota").lstrip("/")
        cron_path.parent.mkdir(parents=True, exist_ok=True)
        cron_path.write_text("# Scheduled Jobs\n\n```toml\n```\n")

        with db.get_db(db_path) as conn:
            _sync_cron_files(conn, config)
            jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert len(jobs) == 0


# ---------------------------------------------------------------------------
# TestProcessOneTask
# ---------------------------------------------------------------------------


class TestProcessOneTask:
    def _make_config(self, db_path, tmp_path, **kwargs):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
            **kwargs,
        )

    def test_no_tasks_returns_none(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        result = process_one_task(config)
        assert result is None

    @patch("istota.scheduler.execute_task", return_value=(True, "All done", None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_success_completes_task(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Hello", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "completed"
        assert task.result == "All done"

    @patch("istota.scheduler.execute_task", return_value=(True, "All done", '["📄 Reading file"]'))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_actions_taken_stored_on_success(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Hello", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.actions_taken == '["📄 Reading file"]'

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_actions_taken_none_when_not_streaming(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Hello", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        task_id, _ = result

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.actions_taken is None

    @patch("istota.scheduler.execute_task", return_value=(False, "Something broke", None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_failure_retries_task(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Fail me", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        # Should be pending for retry (attempt_count 0 < max_attempts 3)
        assert task.status == "pending"
        assert task.attempt_count == 1

    @patch("istota.scheduler.execute_task", return_value=(False, "Fatal error", None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_failure_after_max_retries(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Doomed", user_id="testuser", source_type="cli")
            # Set attempt_count to max_attempts - 1 so next failure is permanent
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"

    @patch("istota.scheduler.execute_task", return_value=(False, "Process killed (likely out of memory)", None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_oom_skips_retry(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="OOM task", user_id="testuser", source_type="cli")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        # OOM should fail immediately, no retry
        assert task.status == "failed"

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_confirmation_detected(self, mock_arun, mock_exec, db_path, tmp_path):
        mock_exec.return_value = (True, "I need your confirmation before deleting the file. Reply yes or no.", None)
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="Delete file", user_id="testuser",
                source_type="talk", conversation_token="room1",
            )

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "pending_confirmation"
        assert task.confirmation_prompt is not None

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_talk_sends_ack_message(self, mock_arun, mock_exec, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="Talk task", user_id="testuser",
                source_type="talk", conversation_token="room1",
            )

        process_one_task(config)

        # asyncio.run should be called at least for the ack message and the result
        assert mock_arun.call_count >= 2

    @patch("istota.scheduler.execute_task", return_value=(True, "Confirmed result", None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_talk_rerun_skips_ack(self, mock_arun, mock_exec, db_path, tmp_path):
        """A task being rerun after confirmation should not send another ack."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Confirmed task", user_id="testuser",
                source_type="talk", conversation_token="room1",
            )
            # Simulate confirmed rerun: set confirmation_prompt and attempt_count
            conn.execute(
                "UPDATE tasks SET confirmation_prompt = ?, attempt_count = 1 WHERE id = ?",
                ("Please confirm", task_id),
            )

        process_one_task(config)

        # Should be called only once for the result (no ack for rerun)
        assert mock_arun.call_count == 1

    @patch("istota.scheduler.execute_task", return_value=(True, '{"body": "reply", "format": "plain"}', None))
    @patch("istota.scheduler.post_result_to_email", new_callable=AsyncMock, return_value=False)
    def test_email_send_failure_marks_task_failed(self, mock_post_email, mock_exec, db_path, tmp_path):
        """When email delivery fails, the task should be marked as failed."""
        config = self._make_config(db_path, tmp_path)
        user = UserConfig(
            display_name="Test",
            timezone="UTC",
            email_addresses=["test@example.com"],
        )
        config = self._make_config(db_path, tmp_path, users={"testuser": user})

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Send email", user_id="testuser",
                source_type="email",
            )

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True  # execute_task succeeded

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"
        assert task.error == "Email delivery failed"


# ---------------------------------------------------------------------------
# TestProgressMessages
# ---------------------------------------------------------------------------


class TestProgressMessages:
    def test_progress_messages_not_empty(self):
        assert len(PROGRESS_MESSAGES) > 0

    def test_all_messages_are_italic(self):
        for msg in PROGRESS_MESSAGES:
            assert msg.startswith("*") and msg.endswith("*"), f"Message not italic: {msg}"


# ---------------------------------------------------------------------------
# TestMakeTalkProgressCallback (additional coverage beyond test_progress_callback.py)
# ---------------------------------------------------------------------------


class TestMakeTalkProgressCallbackExtra:
    def test_emoji_prefix_separated(self, tmp_path):
        """Emoji prefix should be kept outside italic formatting."""
        db_p = tmp_path / "test.db"
        db.init_db(db_p)

        config = Config(
            db_path=db_p,
            scheduler=SchedulerConfig(
                progress_updates=True,
                progress_min_interval=0,
                progress_max_messages=5,
            ),
        )
        task = db.Task(
            id=1, status="running", source_type="talk",
            user_id="testuser", prompt="test", conversation_token="room1",
        )

        with patch("istota.scheduler.asyncio.run") as mock_run, \
             patch("istota.scheduler.db.get_db") as mock_db:
            mock_conn = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            callback = _make_talk_progress_callback(config, task)
            # Use a message with emoji prefix
            callback("\U0001f4c4 Reading file.txt")

        # Should have been called
        assert mock_run.called


# ---------------------------------------------------------------------------
# TestTalkPollThread
# ---------------------------------------------------------------------------


class TestTalkPollThread:
    def test_calls_poll_and_sleeps(self):
        """_talk_poll_loop calls poll_talk_conversations and sleeps between polls."""
        config = Config(scheduler=SchedulerConfig(talk_poll_interval=0))
        call_count = 0

        def stop_after_one(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Stop the loop after first poll
            import istota.scheduler as sched_mod
            sched_mod._shutdown_requested = True
            return []

        with patch("istota.scheduler.asyncio.run", side_effect=stop_after_one) as mock_run, \
             patch("istota.scheduler._shutdown_requested", False):
            # _shutdown_requested is checked at loop top, so we set it inside the poll
            _talk_poll_loop(config)

        assert call_count == 1

    def test_shutdown_flag_stops_loop(self):
        """Loop exits when _shutdown_requested is True."""
        config = Config(scheduler=SchedulerConfig(talk_poll_interval=0))

        with patch("istota.scheduler._shutdown_requested", True):
            # Should return immediately without calling anything
            with patch("istota.scheduler.asyncio.run") as mock_run:
                _talk_poll_loop(config)
            mock_run.assert_not_called()

    def test_exception_does_not_crash(self):
        """Exceptions in polling are caught and loop continues."""
        config = Config(scheduler=SchedulerConfig(talk_poll_interval=0))
        call_count = 0

        def fail_then_stop(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("network down")
            import istota.scheduler as sched_mod
            sched_mod._shutdown_requested = True
            return []

        with patch("istota.scheduler.asyncio.run", side_effect=fail_then_stop), \
             patch("istota.scheduler._shutdown_requested", False):
            _talk_poll_loop(config)

        assert call_count == 2


# ---------------------------------------------------------------------------
# TestGetWorkerId
# ---------------------------------------------------------------------------


class TestGetWorkerId:
    def test_without_user_id(self):
        wid = get_worker_id()
        assert "-" in wid
        # Should be hostname-pid format
        parts = wid.rsplit("-", 1)
        assert len(parts) == 2
        assert parts[1].isdigit()

    def test_with_user_id(self):
        wid = get_worker_id(user_id="alice")
        assert wid.endswith("-alice")
        # Should be hostname-pid-alice
        parts = wid.split("-")
        assert len(parts) >= 3
        assert parts[-1] == "alice"

    def test_none_user_id_same_as_no_arg(self):
        assert get_worker_id(None) == get_worker_id()


# ---------------------------------------------------------------------------
# TestWorkerPool
# ---------------------------------------------------------------------------


class TestWorkerPool:
    def test_dispatch_creates_worker(self, db_path, tmp_path):
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_total_workers=5, worker_idle_timeout=1, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        # Create a pending task
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="test", user_id="alice")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Worker should have been spawned for alice
            assert pool.active_count >= 1

        pool.shutdown()

    def test_respects_max_workers(self, db_path, tmp_path):
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_total_workers=1, worker_idle_timeout=1, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")
            db.create_task(conn, prompt="t2", user_id="bob")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Only 1 worker due to cap
            assert pool.active_count == 1

        pool.shutdown()

    def test_no_dispatch_when_empty(self, db_path, tmp_path):
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_total_workers=5),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        pool = WorkerPool(config)
        pool.dispatch()
        assert pool.active_count == 0

    def test_no_duplicate_workers_for_same_user(self, db_path, tmp_path):
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_total_workers=5, worker_idle_timeout=2, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        # Mock process_one_task to block briefly so worker stays alive
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            count_after_first = pool.active_count
            pool.dispatch()  # should not create a duplicate
            assert pool.active_count == count_after_first

        pool.shutdown()


# ---------------------------------------------------------------------------
# TestProcessHeartbeatTask
# ---------------------------------------------------------------------------


class TestProcessHeartbeatTask:
    def _make_config(self, db_path, tmp_path, **kwargs):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
            **kwargs,
        )



# ---------------------------------------------------------------------------
# TestStripActionPrefix
# ---------------------------------------------------------------------------


class TestStripActionPrefix:
    def test_action_at_start(self):
        should_post, text = _strip_action_prefix("ACTION: Something happened")
        assert should_post is True
        assert text == "Something happened"

    def test_action_after_newline(self):
        should_post, text = _strip_action_prefix("Explanation\nACTION: Did stuff")
        assert should_post is True
        assert text == "Did stuff"

    def test_no_action(self):
        should_post, _ = _strip_action_prefix("NO_ACTION: All good")
        assert should_post is False

    def test_no_action_in_middle(self):
        should_post, _ = _strip_action_prefix("Some text\nNO_ACTION: Fine")
        assert should_post is False

    def test_no_prefix_fails_safe(self):
        should_post, text = _strip_action_prefix("Just a normal result")
        assert should_post is True
        assert text == "Just a normal result"


class TestStripBriefingPreamble:
    def test_no_preamble_unchanged(self):
        text = "📰 NEWS\nSome news here"
        assert strip_briefing_preamble(text) == text

    def test_strips_thinking_preamble(self):
        text = "Now I have all the data. Let me compose the briefing.\n\n📰 NEWS\nSome news"
        result = strip_briefing_preamble(text)
        assert result.startswith("📰 NEWS")
        assert "Let me compose" not in result

    def test_strips_multiline_preamble(self):
        text = "Here's my analysis:\n\nI'll organize this into sections.\n\n📈 MARKETS\nS&P 500: +0.5%"
        result = strip_briefing_preamble(text)
        assert result.startswith("📈 MARKETS")

    def test_no_emoji_returns_unchanged(self):
        text = "Just plain text with no emoji headers"
        assert strip_briefing_preamble(text) == text

    def test_empty_string(self):
        assert strip_briefing_preamble("") == ""

    def test_emoji_at_start_no_strip(self):
        text = "📅 CALENDAR\n- 9:00 AM: Meeting"
        assert strip_briefing_preamble(text) == text


# ---------------------------------------------------------------------------
# TestSilentScheduledJob
# ---------------------------------------------------------------------------


class TestSilentScheduledJob:
    def _make_config(self, db_path, tmp_path, **kwargs):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
            **kwargs,
        )

    @patch("istota.scheduler.execute_task", return_value=(True, "ACTION: Found something important", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_silent_scheduled_action_posts(self, mock_arun, mock_exec, db_path, tmp_path):
        """Silent scheduled job with ACTION: should post result."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn,
                prompt="Check stuff",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                heartbeat_silent=True,
            )

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is True

        # Should post (ACTION: found)
        assert mock_arun.call_count >= 1

    @patch("istota.scheduler.execute_task", return_value=(True, "NO_ACTION: Nothing to report", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_silent_scheduled_no_action_suppressed(self, mock_arun, mock_exec, db_path, tmp_path):
        """Silent scheduled job with NO_ACTION: should suppress output."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn,
                prompt="Check stuff",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                heartbeat_silent=True,
            )

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is True

        # Should NOT post (NO_ACTION)
        assert mock_arun.call_count == 0

    @patch("istota.scheduler.execute_task", return_value=(True, "Just a result", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_silent_scheduled_no_prefix_posts(self, mock_arun, mock_exec, db_path, tmp_path):
        """Silent scheduled job without prefix should post as fail-safe."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn,
                prompt="Check stuff",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                heartbeat_silent=True,
            )

        result = process_one_task(config)
        assert result is not None
        assert mock_arun.call_count >= 1


# ---------------------------------------------------------------------------
# TestScheduledJobFailureTracking
# ---------------------------------------------------------------------------


class TestScheduledJobFailureTracking:
    def _make_config(self, db_path, tmp_path, max_failures=5):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(scheduled_job_max_consecutive_failures=max_failures),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_success_resets_failures(self, mock_arun, mock_exec, db_path, tmp_path):
        """Successful task should reset scheduled job failure count."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "test-job", "0 0 * * *", "do stuff"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='test-job'").fetchone()[0]
            db.increment_scheduled_job_failures(conn, job_id, "prev error")

            db.create_task(
                conn,
                prompt="do stuff",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "test-job")
            assert job.consecutive_failures == 0
            assert job.last_error is None
            assert job.last_success_at is not None

    @patch("istota.scheduler.execute_task", return_value=(False, "Task failed", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_failure_increments_count(self, mock_arun, mock_exec, db_path, tmp_path):
        """Failed task should increment scheduled job failure count."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "fail-job", "0 0 * * *", "do stuff"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='fail-job'").fetchone()[0]

            task_id = db.create_task(
                conn,
                prompt="do stuff",
                user_id="alice",
                source_type="scheduled",
                scheduled_job_id=job_id,
            )
            # Set attempts to max so failure is permanent
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "fail-job")
            assert job.consecutive_failures == 1
            assert "Task failed" in job.last_error

    @patch("istota.scheduler.execute_task", return_value=(False, "boom", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_auto_disable_after_max_failures(self, mock_arun, mock_exec, db_path, tmp_path):
        """Job should be auto-disabled after max consecutive failures."""
        config = self._make_config(db_path, tmp_path, max_failures=2)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled, consecutive_failures)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "flaky-job", "0 0 * * *", "do stuff"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='flaky-job'").fetchone()[0]

            task_id = db.create_task(
                conn,
                prompt="do stuff",
                user_id="alice",
                source_type="scheduled",
                scheduled_job_id=job_id,
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "flaky-job")
            assert job.enabled is False
            assert job.consecutive_failures == 2

    @patch("istota.scheduler.execute_task", return_value=(False, "boom", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_auto_disable_disabled_when_zero(self, mock_arun, mock_exec, db_path, tmp_path):
        """Auto-disable should not trigger when max_failures=0."""
        config = self._make_config(db_path, tmp_path, max_failures=0)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled, consecutive_failures)
                   VALUES (?, ?, ?, ?, 1, 99)""",
                ("alice", "persistent", "0 0 * * *", "do stuff"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='persistent'").fetchone()[0]

            task_id = db.create_task(
                conn,
                prompt="do stuff",
                user_id="alice",
                source_type="scheduled",
                scheduled_job_id=job_id,
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "persistent")
            assert job.enabled is True  # Not disabled despite 100 failures


# ---------------------------------------------------------------------------
# TestWorkerPoolIsolation
# ---------------------------------------------------------------------------


class TestWorkerPoolIsolation:
    def test_foreground_gets_full_cap(self, db_path, tmp_path):
        """Foreground tasks should use full worker cap."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_total_workers=3, reserved_interactive_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="bob", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="t3", user_id="carol", source_type="talk", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # All 3 foreground users should get workers (full cap=3)
            assert pool.active_count == 3

        pool.shutdown()

    def test_background_capped_by_reserved(self, db_path, tmp_path):
        """Background tasks should be capped by max - reserved."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_total_workers=3, reserved_interactive_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="t2", user_id="bob", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="t3", user_id="carol", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Background cap = max(1, 3-2) = 1, so only 1 worker
            assert pool.active_count == 1

        pool.shutdown()

    def test_foreground_prioritized_over_background(self, db_path, tmp_path):
        """Foreground user should get a worker even when background fills cap."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_total_workers=3, reserved_interactive_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        # bg_cap = max(1, 3-2) = 1, but foreground should still get workers
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="bg1", user_id="bg-user", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="fg1", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="fg2", user_id="bob", source_type="email", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # 2 foreground + 1 background (bg_cap=1 allows it)
            assert pool.active_count == 3

        pool.shutdown()


# ---------------------------------------------------------------------------
# TestExecuteCommandTask
# ---------------------------------------------------------------------------


class TestExecuteCommandTask:
    def _make_config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        temp = tmp_path / "temp"
        temp.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            temp_dir=temp,
            scheduler=SchedulerConfig(task_timeout_minutes=1),
        )

    def _make_task(self, **kwargs):
        defaults = dict(
            id=1,
            status="running",
            source_type="scheduled",
            user_id="alice",
            prompt="",
            command="echo hello",
        )
        defaults.update(kwargs)
        return db.Task(**defaults)

    def test_success_returns_stdout(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="echo hello world")
        success, result = _execute_command_task(task, config)
        assert success is True
        assert result == "hello world"

    def test_failure_returns_stderr(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="echo oops >&2 && exit 1")
        success, result = _execute_command_task(task, config)
        assert success is False
        assert "oops" in result

    def test_failure_exit_code_when_no_stderr(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="exit 42")
        success, result = _execute_command_task(task, config)
        assert success is False
        assert "42" in result

    def test_timeout(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        config = Config(
            db_path=db_path,
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
            scheduler=SchedulerConfig(task_timeout_minutes=0),  # 0 seconds
        )
        task = self._make_task(command="sleep 10")
        success, result = _execute_command_task(task, config)
        assert success is False
        assert "timed out" in result.lower()

    def test_env_vars_passed(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(
            id=42,
            user_id="bob",
            conversation_token="room99",
            command="echo $ISTOTA_TASK_ID:$ISTOTA_USER_ID:$ISTOTA_CONVERSATION_TOKEN",
        )
        success, result = _execute_command_task(task, config)
        assert success is True
        assert "42:bob:room99" in result

    def test_db_path_in_env(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="echo $ISTOTA_DB_PATH")
        success, result = _execute_command_task(task, config)
        assert success is True
        assert str(db_path) in result

    def test_no_output_shows_placeholder(self, db_path, tmp_path):
        config = self._make_config(db_path, tmp_path)
        task = self._make_task(command="true")
        success, result = _execute_command_task(task, config)
        assert success is True
        assert result == "(no output)"

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_command_task_flows_through_process_one_task(self, mock_arun, mock_exec, db_path, tmp_path):
        """Command task should bypass execute_task and use _execute_command_task."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn,
                prompt="",
                user_id="alice",
                source_type="scheduled",
                command="echo from-command",
            )
        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True
        # execute_task should NOT have been called
        mock_exec.assert_not_called()

        # Verify the task was completed in DB
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "completed"
        assert "from-command" in task.result

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_prompt_task_still_uses_execute_task(self, mock_arun, mock_exec, db_path, tmp_path):
        """Non-command task should still use execute_task."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(
                conn,
                prompt="Do stuff",
                user_id="alice",
                source_type="scheduled",
            )
        process_one_task(config)
        mock_exec.assert_called_once()

    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_command_task_failure_tracks_scheduled_job(self, mock_arun, db_path, tmp_path):
        """Command task failure should increment scheduled job failures."""
        config = self._make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "cmd-job", "0 0 * * *", ""),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='cmd-job'").fetchone()[0]
            task_id = db.create_task(
                conn,
                prompt="",
                user_id="alice",
                source_type="scheduled",
                command="exit 1",
                scheduled_job_id=job_id,
                conversation_token="room1",
            )
            # Set max_attempts=1 so failure is permanent on first try
            conn.execute("UPDATE tasks SET max_attempts = 1 WHERE id = ?", (task_id,))

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "cmd-job")
        assert job.consecutive_failures == 1


# ---------------------------------------------------------------------------
# TestDualWorkerQueue
# ---------------------------------------------------------------------------


class TestDualWorkerQueue:
    """Tests for the dual foreground/background worker queue model."""

    def test_worker_pool_spawns_both_fg_and_bg_for_same_user(self, db_path, tmp_path):
        """A user with both fg and bg tasks should get two workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_total_workers=6, worker_idle_timeout=1, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="chat", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="cron", user_id="alice", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Alice should have 2 workers: one fg, one bg
            assert pool.active_count == 2

        pool.shutdown()

    def test_worker_pool_fg_only(self, db_path, tmp_path):
        """A user with only foreground tasks gets one fg worker."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_total_workers=6, worker_idle_timeout=1, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="chat", user_id="alice", source_type="talk", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 1

        pool.shutdown()

    def test_worker_pool_bg_only(self, db_path, tmp_path):
        """A user with only background tasks gets one bg worker."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_total_workers=6, worker_idle_timeout=1, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="cron", user_id="alice", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 1

        pool.shutdown()

    def test_worker_pool_no_duplicate_workers_per_queue(self, db_path, tmp_path):
        """Calling dispatch twice doesn't duplicate workers for the same (user, queue)."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_total_workers=6, worker_idle_timeout=2, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="chat", user_id="alice", source_type="talk", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            count_after_first = pool.active_count
            pool.dispatch()
            assert pool.active_count == count_after_first

        pool.shutdown()

    def test_worker_pool_respects_max_workers(self, db_path, tmp_path):
        """Total workers across all users/queues capped by max_total_workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_total_workers=3, worker_idle_timeout=1, poll_interval=1),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            # 2 users × 2 queues = 4 potential workers, but cap is 3
            db.create_task(conn, prompt="fg", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="bg", user_id="alice", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="fg", user_id="bob", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="bg", user_id="bob", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count <= 3

        pool.shutdown()

    def test_worker_pool_fg_prioritized_over_bg(self, db_path, tmp_path):
        """Foreground workers should be spawned before background workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_total_workers=2, reserved_interactive_workers=1,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            # 2 users with fg tasks + 1 user with bg task, cap=2
            db.create_task(conn, prompt="fg1", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="fg2", user_id="bob", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="bg1", user_id="carol", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Both fg workers should get slots, bg should be capped out
            assert pool.active_count == 2

        pool.shutdown()

    def test_process_one_task_with_queue(self, db_path, tmp_path):
        """process_one_task should filter by queue when provided."""
        config = Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)
        (tmp_path / "temp").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="bg task", user_id="alice", source_type="scheduled", queue="background")

        # Trying to process a foreground task should find nothing
        result = process_one_task(config, user_id="alice", queue="foreground")
        assert result is None

        # Trying to process a background task should find the task
        with patch("istota.scheduler.execute_task", return_value=(True, "done", None)):
            with patch("istota.scheduler.post_result_to_talk", new_callable=AsyncMock):
                result = process_one_task(config, user_id="alice", queue="background")
        assert result is not None
        task_id, success = result
        assert success is True


# ---------------------------------------------------------------------------
# TestDeferredOperations
# ---------------------------------------------------------------------------


class TestDeferredOperations:
    """Tests for deferred DB operations (subtasks + transaction tracking)."""

    def _make_config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    def test_process_deferred_subtasks_creates_tasks(self, db_path, tmp_path):
        """Subtask JSON file should create tasks in DB with correct parent/queue."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        # Create parent task
        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
                conversation_token="room1", queue="background",
            )
            parent = db.get_task(conn, parent_id)

        # Write deferred subtasks file
        subtasks = [
            {"prompt": "Do X", "conversation_token": "room1", "priority": 3},
            {"prompt": "Do Y"},
        ]
        (user_temp / f"task_{parent_id}_subtasks.json").write_text(json.dumps(subtasks))

        count = _process_deferred_subtasks(config, parent, user_temp)
        assert count == 2

        # Verify tasks created in DB
        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        # parent + 2 subtasks
        subtask_list = [t for t in tasks if t.source_type == "subtask"]
        assert len(subtask_list) == 2
        assert subtask_list[0].parent_task_id == parent_id
        assert subtask_list[0].queue == "background"  # inherits from parent
        assert subtask_list[0].user_id == "alice"

        # File should be deleted
        assert not (user_temp / f"task_{parent_id}_subtasks.json").exists()

    def test_process_deferred_subtasks_admin_only(self, db_path, tmp_path):
        """Non-admin users should have deferred subtasks ignored."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        config = Config(
            **{**config.__dict__, "admin_users": {"bob"}},  # alice is NOT admin
        )
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
            )
            parent = db.get_task(conn, parent_id)

        subtasks = [{"prompt": "sneaky"}]
        (user_temp / f"task_{parent_id}_subtasks.json").write_text(json.dumps(subtasks))

        count = _process_deferred_subtasks(config, parent, user_temp)
        assert count == 0

        # No subtasks created
        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        assert all(t.source_type != "subtask" for t in tasks)

        # File should still be deleted (cleaned up)
        assert not (user_temp / f"task_{parent_id}_subtasks.json").exists()

    def test_process_deferred_subtasks_no_file(self, db_path, tmp_path):
        """No file means no-op, returns 0."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
            )
            parent = db.get_task(conn, parent_id)

        count = _process_deferred_subtasks(config, parent, user_temp)
        assert count == 0

    def test_process_deferred_subtasks_bad_json(self, db_path, tmp_path):
        """Malformed JSON should be handled gracefully (logged, file deleted)."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
            )
            parent = db.get_task(conn, parent_id)

        (user_temp / f"task_{parent_id}_subtasks.json").write_text("{bad json")

        count = _process_deferred_subtasks(config, parent, user_temp)
        assert count == 0
        # File cleaned up
        assert not (user_temp / f"task_{parent_id}_subtasks.json").exists()

    def test_process_deferred_subtasks_inherits_queue(self, db_path, tmp_path):
        """Subtasks should inherit the parent's queue."""
        from istota.scheduler import _process_deferred_subtasks
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Parent", user_id="alice", source_type="talk",
                queue="background",
            )
            parent = db.get_task(conn, parent_id)

        subtasks = [{"prompt": "bg subtask"}]
        (user_temp / f"task_{parent_id}_subtasks.json").write_text(json.dumps(subtasks))

        _process_deferred_subtasks(config, parent, user_temp)

        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="alice")
        subtask = [t for t in tasks if t.source_type == "subtask"][0]
        assert subtask.queue == "background"

    def test_process_deferred_tracking_monarch(self, db_path, tmp_path):
        """Monarch synced entries should be tracked in DB."""
        from istota.scheduler import _process_deferred_tracking
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Sync", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)

        tracking = {
            "monarch_synced": [
                {"id": "txn_123", "amount": 42.50, "merchant": "Acme",
                 "posted_account": "Assets:Bank", "txn_date": "2026-01-15",
                 "content_hash": "abc123", "tags_json": "[]"},
            ],
            "csv_imported": [],
            "monarch_recategorized": [],
        }
        (user_temp / f"task_{task_id}_tracked_transactions.json").write_text(json.dumps(tracking))

        count = _process_deferred_tracking(config, task, user_temp)
        assert count == 1

        # Verify in DB
        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM monarch_synced_transactions WHERE user_id = ?", ("alice",)
            ).fetchone()
        assert row is not None
        assert row["monarch_transaction_id"] == "txn_123"

        # File cleaned up
        assert not (user_temp / f"task_{task_id}_tracked_transactions.json").exists()

    def test_process_deferred_tracking_csv(self, db_path, tmp_path):
        """CSV imported entries should be tracked in DB."""
        from istota.scheduler import _process_deferred_tracking
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Import", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)

        tracking = {
            "monarch_synced": [],
            "csv_imported": [{"content_hash": "hash1", "source_file": "bank.csv"}],
            "monarch_recategorized": [],
        }
        (user_temp / f"task_{task_id}_tracked_transactions.json").write_text(json.dumps(tracking))

        count = _process_deferred_tracking(config, task, user_temp)
        assert count == 1

        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM csv_imported_transactions WHERE user_id = ?", ("alice",)
            ).fetchone()
        assert row is not None
        assert row["content_hash"] == "hash1"

    def test_process_deferred_tracking_recategorized(self, db_path, tmp_path):
        """Recategorized monarch transactions should be processed."""
        from istota.scheduler import _process_deferred_tracking
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Sync", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)
            # Pre-seed a synced transaction to recategorize
            db.track_monarch_transactions_batch(conn, "alice", [
                {"id": "txn_789", "amount": 10.0, "merchant": "Shop",
                 "posted_account": "Assets:Bank", "txn_date": "2026-01-10",
                 "content_hash": "xyz", "tags_json": "[]"},
            ])

        tracking = {
            "monarch_synced": [],
            "csv_imported": [],
            "monarch_recategorized": ["txn_789"],
        }
        (user_temp / f"task_{task_id}_tracked_transactions.json").write_text(json.dumps(tracking))

        count = _process_deferred_tracking(config, task, user_temp)
        assert count == 1

        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT recategorized_at FROM monarch_synced_transactions WHERE monarch_transaction_id = ?",
                ("txn_789",),
            ).fetchone()
        assert row["recategorized_at"] is not None

    def test_process_deferred_tracking_category_updates(self, db_path, tmp_path):
        """Category updates should update posted_account in DB."""
        from istota.scheduler import _process_deferred_tracking
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Sync", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)
            # Pre-seed a synced transaction to update
            db.track_monarch_transactions_batch(conn, "alice", [
                {"id": "txn_500", "amount": 504.0, "merchant": "PayPal",
                 "posted_account": "Expenses:Office-Supplies", "txn_date": "2026-02-10",
                 "content_hash": "abc", "tags_json": "[]"},
            ])

        tracking = {
            "monarch_synced": [],
            "csv_imported": [],
            "monarch_recategorized": [],
            "monarch_category_updates": [
                {"monarch_transaction_id": "txn_500", "posted_account": "Expenses:Software:Subscriptions"},
            ],
        }
        (user_temp / f"task_{task_id}_tracked_transactions.json").write_text(json.dumps(tracking))

        count = _process_deferred_tracking(config, task, user_temp)
        assert count == 1

        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT posted_account FROM monarch_synced_transactions WHERE monarch_transaction_id = ?",
                ("txn_500",),
            ).fetchone()
        assert row["posted_account"] == "Expenses:Software:Subscriptions"

    def test_process_deferred_tracking_no_file(self, db_path, tmp_path):
        """No file means no-op."""
        from istota.scheduler import _process_deferred_tracking
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Noop", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)

        count = _process_deferred_tracking(config, task, user_temp)
        assert count == 0

    @patch("istota.scheduler.execute_task", return_value=(False, "Something broke", None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_deferred_ops_skipped_on_failure(self, mock_arun, mock_exec, db_path, tmp_path):
        """Deferred files should NOT be processed when task fails."""
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "testuser"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Fail me", user_id="testuser", source_type="cli",
            )

        # Write deferred files that should NOT be processed
        subtasks = [{"prompt": "Should not exist"}]
        (user_temp / f"task_{task_id}_subtasks.json").write_text(json.dumps(subtasks))

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        # Deferred files should still exist (not processed)
        assert (user_temp / f"task_{task_id}_subtasks.json").exists()

        # No subtasks created
        with db.get_db(db_path) as conn:
            tasks = db.list_tasks(conn, user_id="testuser")
        assert all(t.source_type != "subtask" for t in tasks)
