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
    UserWorker,
    WorkerPool,
    cleanup_old_claude_logs,
    download_talk_attachments,
    get_worker_id,
    strip_briefing_preamble,
    _parse_email_output,
    _load_deferred_email_output,
    _talk_poll_loop,
    _format_error_for_user,
    _strip_action_prefix,
    _execute_command_task,
    check_briefings,
    check_scheduled_jobs,
    post_result_to_email,
    process_one_task,
    _make_talk_progress_callback,
    post_result_to_talk,
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
        assert "the deep stared back" in result.lower()

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

    def test_plain_text_returns_none(self):
        msg = "Just a plain text response with no JSON at all."
        result = _parse_email_output(msg)
        assert result is None

    def test_invalid_json_returns_none(self):
        msg = '{"broken json'
        result = _parse_email_output(msg)
        assert result is None

    def test_missing_body_returns_none(self):
        # Valid JSON but missing required "body" key
        msg = '{"subject": "No body here", "format": "plain"}'
        result = _parse_email_output(msg)
        assert result is None

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

    def test_smart_quotes_normalized(self):
        # Unicode left double quote (U+201C) inside a JSON string value
        # breaks JSON parsing — Try 4 should normalize and recover
        msg = '{"subject": "Daily Notes", "body": "He said \u201chello\u201d today", "format": "plain"}'
        result = _parse_email_output(msg)
        assert result["subject"] == "Daily Notes"
        assert "hello" in result["body"]
        assert result["format"] == "plain"

    def test_smart_single_quotes_normalized(self):
        msg = '{"subject": "Test", "body": "It\u2019s a nice day", "format": "plain"}'
        result = _parse_email_output(msg)
        assert result["subject"] == "Test"
        assert "nice day" in result["body"]
        assert result["format"] == "plain"

    def test_smart_quotes_in_preamble_json(self):
        # Smart quotes in JSON with preamble text — Try 3 fails, Try 4 recovers
        msg = 'Here is the email:\n{"subject": "Notes", "body": "\u201cWise words\u201d from Dostoevsky", "format": "plain"}'
        result = _parse_email_output(msg)
        assert result["subject"] == "Notes"
        assert "Dostoevsky" in result["body"]


# ---------------------------------------------------------------------------
# TestLoadDeferredEmailOutput
# ---------------------------------------------------------------------------


class TestLoadDeferredEmailOutput:
    def _make_task(self, task_id=42, user_id="alice"):
        return db.Task(
            id=task_id, status="completed", source_type="email",
            user_id=user_id, prompt="test",
        )

    def test_loads_valid_file(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        data = {"subject": "Hello", "body": "World", "format": "plain"}
        (user_dir / "task_42_email_output.json").write_text(json.dumps(data))

        result = _load_deferred_email_output(config, self._make_task())
        assert result == {"subject": "Hello", "body": "World", "format": "plain"}
        # File should be deleted after loading
        assert not (user_dir / "task_42_email_output.json").exists()

    def test_returns_none_when_no_file(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        (tmp_path / "alice").mkdir()
        result = _load_deferred_email_output(config, self._make_task())
        assert result is None

    def test_handles_invalid_json(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        (user_dir / "task_42_email_output.json").write_text("not json")

        result = _load_deferred_email_output(config, self._make_task())
        assert result is None
        # File should be cleaned up
        assert not (user_dir / "task_42_email_output.json").exists()

    def test_handles_missing_body(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        data = {"subject": "Hello", "format": "plain"}
        (user_dir / "task_42_email_output.json").write_text(json.dumps(data))

        result = _load_deferred_email_output(config, self._make_task())
        assert result is None

    def test_normalizes_invalid_format(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        data = {"subject": "S", "body": "B", "format": "markdown"}
        (user_dir / "task_42_email_output.json").write_text(json.dumps(data))

        result = _load_deferred_email_output(config, self._make_task())
        assert result["format"] == "plain"

    def test_html_format_preserved(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        data = {"subject": "S", "body": "<p>Hi</p>", "format": "html"}
        (user_dir / "task_42_email_output.json").write_text(json.dumps(data))

        result = _load_deferred_email_output(config, self._make_task())
        assert result["format"] == "html"
        assert result["body"] == "<p>Hi</p>"

    def test_null_subject(self, tmp_path):
        config = Config(temp_dir=tmp_path)
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        data = {"body": "Reply text", "format": "plain"}
        (user_dir / "task_42_email_output.json").write_text(json.dumps(data))

        result = _load_deferred_email_output(config, self._make_task())
        assert result["subject"] is None
        assert result["body"] == "Reply text"


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
            scheduler=SchedulerConfig(worker_idle_timeout=1, poll_interval=1),
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
            scheduler=SchedulerConfig(max_foreground_workers=1, worker_idle_timeout=1, poll_interval=1),
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
            # Only 1 fg worker due to max_foreground_workers=1
            assert pool.active_count == 1

        pool.shutdown()

    def test_no_dispatch_when_empty(self, db_path, tmp_path):
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(),
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
            scheduler=SchedulerConfig(user_max_foreground_workers=1, worker_idle_timeout=2, poll_interval=1),
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
        """Foreground tasks should use full fg worker cap."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=3,
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
            # All 3 foreground users should get workers (fg cap=3)
            assert pool.active_count == 3

        pool.shutdown()

    def test_background_capped_by_max_background_workers(self, db_path, tmp_path):
        """Background tasks should be capped by max_background_workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_background_workers=1,
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
            # Background cap = 1, so only 1 worker
            assert pool.active_count == 1

        pool.shutdown()

    def test_foreground_prioritized_over_background(self, db_path, tmp_path):
        """Foreground user should get a worker even when background fills cap."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=3, max_background_workers=1,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        # bg_cap=1, fg_cap=3: foreground should still get workers
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="bg1", user_id="bg-user", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="fg1", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="fg2", user_id="bob", source_type="email", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # 2 foreground + 1 background
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
            scheduler=SchedulerConfig(max_foreground_workers=6, max_background_workers=6, worker_idle_timeout=1, poll_interval=1),
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
            scheduler=SchedulerConfig(max_foreground_workers=6, max_background_workers=6, worker_idle_timeout=1, poll_interval=1),
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
            scheduler=SchedulerConfig(max_foreground_workers=6, max_background_workers=6, worker_idle_timeout=1, poll_interval=1),
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
        """Calling dispatch twice doesn't duplicate workers for the same (user, queue) when per-user cap is 1."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(max_foreground_workers=6, max_background_workers=6, user_max_foreground_workers=1, worker_idle_timeout=2, poll_interval=1),
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

    def test_worker_pool_respects_per_queue_caps(self, db_path, tmp_path):
        """Workers capped independently by max_foreground_workers and max_background_workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=2, max_background_workers=1,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            # 2 users × 2 queues = 4 potential workers, but fg cap=2, bg cap=1
            db.create_task(conn, prompt="fg", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="bg", user_id="alice", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="fg", user_id="bob", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="bg", user_id="bob", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            fg_count = sum(1 for (_, qt, _) in pool._workers if qt == "foreground")
            bg_count = sum(1 for (_, qt, _) in pool._workers if qt == "background")
            assert fg_count <= 2
            assert bg_count <= 1
            assert pool.active_count <= 3  # 2 fg + 1 bg

        pool.shutdown()

    def test_worker_pool_fg_prioritized_over_bg(self, db_path, tmp_path):
        """Foreground workers spawned independently from background workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=2, max_background_workers=0,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            # 2 users with fg tasks + 1 user with bg task, bg cap=0
            db.create_task(conn, prompt="fg1", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="fg2", user_id="bob", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="bg1", user_id="carol", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Both fg workers should get slots, bg should be capped out (bg cap=0)
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


# ---------------------------------------------------------------------------
# TestOnceJobAutoRemoval
# ---------------------------------------------------------------------------


class TestOnceJobAutoRemoval:
    """Tests for automatic removal of once=true scheduled jobs after success."""

    def _make_config(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="secret"),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    @patch("istota.scheduler.execute_task", return_value=(True, "Reminder sent", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_once_job_removed_on_success(self, mock_arun, mock_exec, db_path, tmp_path):
        """Successful once job should be removed from DB."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "reminder-123", "30 14 17 2 *", "Send reminder"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='reminder-123'").fetchone()[0]

            db.create_task(
                conn,
                prompt="Send reminder",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "reminder-123")
            assert job is None, "Once job should be deleted from DB after success"

    @patch("istota.scheduler.execute_task", return_value=(False, "Task failed", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_once_job_not_removed_on_failure(self, mock_arun, mock_exec, db_path, tmp_path):
        """Failed once job should NOT be removed (stays for retry)."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "reminder-456", "0 9 18 2 *", "Reminder"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='reminder-456'").fetchone()[0]

            task_id = db.create_task(
                conn,
                prompt="Reminder",
                user_id="alice",
                source_type="scheduled",
                scheduled_job_id=job_id,
            )
            # Set attempts to max so failure is permanent
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "reminder-456")
            assert job is not None, "Once job should NOT be deleted on failure"

    @patch("istota.scheduler.execute_task", return_value=(True, "Done", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_once_job_also_removed_from_cron_md(self, mock_arun, mock_exec, db_path, tmp_path):
        """Successful once job should also be removed from CRON.md file."""
        from istota.cron_loader import load_cron_jobs
        from istota.storage import get_user_cron_path

        config = self._make_config(db_path, tmp_path)
        mount = config.nextcloud_mount_path

        # Write CRON.md with the once job and a regular job
        cron_path = mount / get_user_cron_path("alice", "istota").lstrip("/")
        cron_path.parent.mkdir(parents=True, exist_ok=True)
        cron_path.write_text("""\
# Scheduled Jobs

```toml
[[jobs]]
name = "keep-this"
cron = "0 9 * * *"
prompt = "daily check"

[[jobs]]
name = "reminder-789"
cron = "0 15 20 2 *"
prompt = "One-time reminder"
once = true
```
""")

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "reminder-789", "0 15 20 2 *", "One-time reminder"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='reminder-789'").fetchone()[0]

            db.create_task(
                conn,
                prompt="One-time reminder",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        process_one_task(config)

        # CRON.md should only have the keep-this job
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].name == "keep-this"

    @patch("istota.scheduler.execute_task", return_value=(True, "Regular success", None))
    @patch("istota.scheduler.asyncio.run", return_value=42)
    def test_non_once_job_not_removed(self, mock_arun, mock_exec, db_path, tmp_path):
        """Regular (non-once) job should NOT be removed on success."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 0)""",
                ("alice", "daily-job", "0 9 * * *", "Do stuff"),
            )
            job_id = conn.execute("SELECT id FROM scheduled_jobs WHERE name='daily-job'").fetchone()[0]

            db.create_task(
                conn,
                prompt="Do stuff",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room1",
                scheduled_job_id=job_id,
            )

        process_one_task(config)

        with db.get_db(db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "daily-job")
            assert job is not None, "Regular job should NOT be deleted on success"


# ---------------------------------------------------------------------------
# TestCleanupOldClaudeLogs
# ---------------------------------------------------------------------------


class TestCleanupOldClaudeLogs:
    """Tests for cleanup_old_claude_logs()."""

    def test_deletes_old_jsonl_files(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        projects = claude_dir / "projects" / "some-project"
        projects.mkdir(parents=True)

        import time
        old_file = projects / "old-session.jsonl"
        old_file.write_text("{}")
        recent_file = projects / "recent-session.jsonl"
        recent_file.write_text("{}")

        # Make old file actually old
        import os
        old_mtime = time.time() - (10 * 24 * 60 * 60)
        os.utime(old_file, (old_mtime, old_mtime))

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            deleted = cleanup_old_claude_logs(retention_days=7)

        assert deleted == 1
        assert not old_file.exists()
        assert recent_file.exists()

    def test_deletes_old_debug_files(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        debug = claude_dir / "debug"
        debug.mkdir(parents=True)

        import time, os
        old_file = debug / "debug-2026-01-01.txt"
        old_file.write_text("log")
        old_mtime = time.time() - (10 * 24 * 60 * 60)
        os.utime(old_file, (old_mtime, old_mtime))

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            deleted = cleanup_old_claude_logs(retention_days=7)

        assert deleted == 1
        assert not old_file.exists()

    def test_deletes_old_todo_files(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        todos = claude_dir / "todos"
        todos.mkdir(parents=True)

        import time, os
        old_file = todos / "tasks.json"
        old_file.write_text("[]")
        old_mtime = time.time() - (10 * 24 * 60 * 60)
        os.utime(old_file, (old_mtime, old_mtime))

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            deleted = cleanup_old_claude_logs(retention_days=7)

        assert deleted == 1

    def test_removes_empty_subdirectories(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        subdir = claude_dir / "projects" / "empty-project"
        subdir.mkdir(parents=True)

        import time, os
        old_file = subdir / "session.jsonl"
        old_file.write_text("{}")
        old_mtime = time.time() - (10 * 24 * 60 * 60)
        os.utime(old_file, (old_mtime, old_mtime))

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            cleanup_old_claude_logs(retention_days=7)

        assert not subdir.exists(), "Empty subdirectory should be removed"

    def test_missing_claude_dir_returns_zero(self, tmp_path):
        import os
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            deleted = cleanup_old_claude_logs(retention_days=7)
        assert deleted == 0

    def test_keeps_recent_files(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        projects = claude_dir / "projects" / "active"
        projects.mkdir(parents=True)

        recent = projects / "today.jsonl"
        recent.write_text("{}")

        import os
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            deleted = cleanup_old_claude_logs(retention_days=7)

        assert deleted == 0
        assert recent.exists()


# ---------------------------------------------------------------------------
# TestPostResultToTalk
# ---------------------------------------------------------------------------


class TestPostResultToTalk:
    """Tests for post_result_to_talk() — reply threading and @mentions in group chats."""

    def _make_config(self):
        return Config(
            nextcloud=NextcloudConfig(
                url="https://nc.example.com",
                username="istota",
                app_password="secret",
            ),
        )

    def _make_task(self, *, is_group_chat=False, talk_message_id=None, user_id="alice"):
        return db.Task(
            id=1,
            prompt="hello",
            user_id=user_id,
            source_type="talk",
            status="completed",
            conversation_token="room123",
            is_group_chat=is_group_chat,
            talk_message_id=talk_message_id,
        )

    @pytest.mark.asyncio
    async def test_dm_no_reply_to_no_mention(self):
        """DM messages should not use reply_to or @mention."""
        config = self._make_config()
        task = self._make_task(is_group_chat=False, talk_message_id=42)

        with patch("istota.scheduler.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.send_message = AsyncMock(
                return_value={"ocs": {"data": {"id": 100}}}
            )
            result = await post_result_to_talk(
                config, task, "Hello there", use_reply_threading=True,
            )

        mock_instance.send_message.assert_called_once_with(
            "room123", "Hello there", reply_to=None, reference_id=None,
        )
        assert result == 100

    @pytest.mark.asyncio
    async def test_group_chat_reply_to_and_mention(self):
        """Group chat messages should reply to original and @mention the user."""
        config = self._make_config()
        task = self._make_task(is_group_chat=True, talk_message_id=42, user_id="bob")

        with patch("istota.scheduler.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.send_message = AsyncMock(
                return_value={"ocs": {"data": {"id": 200}}}
            )
            result = await post_result_to_talk(
                config, task, "Sure thing", use_reply_threading=True,
            )

        mock_instance.send_message.assert_called_once_with(
            "room123", "@bob Sure thing", reply_to=42, reference_id=None,
        )
        assert result == 200

    @pytest.mark.asyncio
    async def test_group_chat_split_message_only_first_part_gets_reply(self):
        """When a message is split, only the first part should get reply_to and @mention."""
        config = self._make_config()
        task = self._make_task(is_group_chat=True, talk_message_id=42, user_id="carol")

        with patch("istota.scheduler.TalkClient") as MockClient, \
             patch("istota.scheduler.split_message", return_value=["Part 1", "Part 2"]):
            mock_instance = MockClient.return_value
            mock_instance.send_message = AsyncMock(
                return_value={"ocs": {"data": {"id": 300}}}
            )
            result = await post_result_to_talk(
                config, task, "Long message", use_reply_threading=True,
            )

        calls = mock_instance.send_message.call_args_list
        assert len(calls) == 2
        # First part: reply_to + @mention
        assert calls[0].args == ("room123", "@carol Part 1")
        assert calls[0].kwargs == {"reply_to": 42, "reference_id": None}
        # Second part: no reply_to, no @mention
        assert calls[1].args == ("room123", "Part 2")
        assert calls[1].kwargs == {"reply_to": None, "reference_id": None}

    @pytest.mark.asyncio
    async def test_group_chat_no_talk_message_id(self):
        """Group chat without talk_message_id should still @mention but reply_to is None."""
        config = self._make_config()
        task = self._make_task(is_group_chat=True, talk_message_id=None, user_id="dave")

        with patch("istota.scheduler.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.send_message = AsyncMock(
                return_value={"ocs": {"data": {"id": 400}}}
            )
            result = await post_result_to_talk(
                config, task, "Response", use_reply_threading=True,
            )

        mock_instance.send_message.assert_called_once_with(
            "room123", "@dave Response", reply_to=None, reference_id=None,
        )

    @pytest.mark.asyncio
    async def test_group_chat_no_threading_for_progress_updates(self):
        """Progress updates (use_reply_threading=False) should not get reply_to or @mention."""
        config = self._make_config()
        task = self._make_task(is_group_chat=True, talk_message_id=42, user_id="eve")

        with patch("istota.scheduler.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.send_message = AsyncMock(
                return_value={"ocs": {"data": {"id": 500}}}
            )
            # Default use_reply_threading=False (progress/ack messages)
            result = await post_result_to_talk(config, task, "Working on it...")

        mock_instance.send_message.assert_called_once_with(
            "room123", "Working on it...", reply_to=None, reference_id=None,
        )

    @pytest.mark.asyncio
    async def test_reference_id_passed_through(self):
        """reference_id should be passed to send_message for each part."""
        config = self._make_config()
        task = self._make_task(is_group_chat=False, talk_message_id=42)

        with patch("istota.scheduler.TalkClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.send_message = AsyncMock(
                return_value={"ocs": {"data": {"id": 100}}}
            )
            await post_result_to_talk(
                config, task, "Result", reference_id="istota:task:1:result",
            )

        mock_instance.send_message.assert_called_once_with(
            "room123", "Result", reply_to=None,
            reference_id="istota:task:1:result",
        )


class TestWorkerPoolConcurrencyCaps:
    """Test the three-tier concurrency control in WorkerPool.dispatch()."""

    def test_dispatch_respects_instance_fg_cap(self, db_path, tmp_path):
        """Foreground workers capped at max_foreground_workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=2, max_background_workers=3,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="fg1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="fg2", user_id="bob", queue="foreground")
            db.create_task(conn, prompt="fg3", user_id="carol", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Only 2 fg workers despite 3 users, because max_foreground_workers=2
            fg_count = sum(1 for (_, qt, _) in pool._workers if qt == "foreground")
            assert fg_count <= 2

        pool.shutdown()

    def test_dispatch_respects_instance_bg_cap(self, db_path, tmp_path):
        """Background workers capped at max_background_workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5, max_background_workers=1,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="bg1", user_id="alice", queue="background")
            db.create_task(conn, prompt="bg2", user_id="bob", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            bg_count = sum(1 for (_, qt, _) in pool._workers if qt == "background")
            assert bg_count <= 1

        pool.shutdown()

    def test_dispatch_separate_fg_bg_caps(self, db_path, tmp_path):
        """Separate fg and bg caps work independently."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=4, max_background_workers=3,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="fg1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="bg1", user_id="bob", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 2

        pool.shutdown()


class TestMultiWorkerPerUser:
    """Tests for per-user multi-worker support (multiple fg/bg workers per user)."""

    def test_dispatch_multiple_fg_workers_same_user(self, db_path, tmp_path):
        """User with 2 pending fg tasks and per-user cap of 2 gets 2 fg workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 2

        pool.shutdown()

    def test_dispatch_respects_per_user_fg_cap(self, db_path, tmp_path):
        """User with 3 pending fg tasks but per-user cap of 2 gets only 2 workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t3", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 2

        pool.shutdown()

    def test_dispatch_per_user_bg_cap(self, db_path, tmp_path):
        """Background workers also respect per-user caps."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_background_workers=5,
                user_max_background_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="t2", user_id="alice", source_type="scheduled", queue="background")
            db.create_task(conn, prompt="t3", user_id="alice", source_type="scheduled", queue="background")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 2

        pool.shutdown()

    def test_dispatch_instance_cap_limits_per_user(self, db_path, tmp_path):
        """Instance cap of 2 overrides per-user cap of 3."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=2,
                user_max_foreground_workers=3,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t3", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 2

        pool.shutdown()

    def test_dispatch_doesnt_spawn_excess_workers_for_few_tasks(self, db_path, tmp_path):
        """Don't spawn 3 workers if user only has 1 pending task."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=3,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 1

        pool.shutdown()

    def test_dispatch_multiple_users_with_multi_workers(self, db_path, tmp_path):
        """Multiple users each get their per-user cap of workers."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=10,
                user_max_foreground_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t3", user_id="bob", queue="foreground")
            db.create_task(conn, prompt="t4", user_id="bob", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # alice: 2 workers, bob: 2 workers
            assert pool.active_count == 4

        pool.shutdown()

    def test_worker_key_is_three_tuple(self, db_path, tmp_path):
        """Worker keys should be (user_id, queue_type, slot) 3-tuples."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            for key in pool._workers:
                assert len(key) == 3, f"Expected 3-tuple key, got {key}"
                user_id, queue_type, slot = key
                assert isinstance(slot, int)

        pool.shutdown()

    def test_redispatch_doesnt_duplicate_existing_slots(self, db_path, tmp_path):
        """Calling dispatch twice doesn't create duplicate workers for same slots."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=2,
                worker_idle_timeout=2, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")
            db.create_task(conn, prompt="t2", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            count_after_first = pool.active_count
            pool.dispatch()
            assert pool.active_count == count_after_first

        pool.shutdown()

    def test_new_worker_spawned_while_existing_worker_busy(self, db_path, tmp_path):
        """A pending task should get a new worker even if another worker is busy.

        Scenario: user posts in Room A, worker 0 claims it (now running).
        User posts in Room B. Task B is pending, worker 0 is busy.
        Dispatch should spawn worker 1 for the pending task.
        """
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=2,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        # Task A is claimed and running (simulates worker 0 busy)
        with db.get_db(db_path) as conn:
            task_a = db.create_task(conn, prompt="room A", user_id="alice", queue="foreground")
            db.update_task_status(conn, task_a, "running")
            # Task B is pending (user just posted in another room)
            db.create_task(conn, prompt="room B", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        # Simulate worker 0 already in the pool (busy with task A)
        busy_worker = MagicMock(spec=UserWorker)
        pool._workers[("alice", "foreground", 0)] = busy_worker

        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Should have 2 workers: slot 0 (busy) + slot 1 (new for task B)
            assert pool.active_count == 2

        pool.shutdown()

    def test_slot_assignment_handles_gaps(self, db_path, tmp_path):
        """Slot assignment should work even if lower slots have exited."""
        config = Config(
            db_path=db_path,
            scheduler=SchedulerConfig(
                max_foreground_workers=5,
                user_max_foreground_workers=3,
                worker_idle_timeout=1, poll_interval=1,
            ),
            nextcloud_mount_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "mount").mkdir(exist_ok=True)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice", queue="foreground")

        pool = WorkerPool(config)
        # Simulate: slot 0 exited, slot 1 still running
        busy_worker = MagicMock(spec=UserWorker)
        pool._workers[("alice", "foreground", 1)] = busy_worker

        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            # Should pick slot 0 (gap) rather than colliding with slot 1
            keys = list(pool._workers.keys())
            slots = sorted(s for (uid, qt, s) in keys if uid == "alice" and qt == "foreground")
            assert 0 in slots, f"Expected slot 0 to be used, got slots {slots}"
            assert 1 in slots, f"Expected slot 1 to still exist, got slots {slots}"
            # No duplicate keys
            assert len(keys) == len(set(keys))

        pool.shutdown()


# ---------------------------------------------------------------------------
# TestApiErrorInSuccessResult
# ---------------------------------------------------------------------------


class TestApiErrorInSuccessResult:
    """Test that process_one_task detects API errors in 'successful' results."""

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

    @patch("istota.scheduler.execute_task")
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_api_error_in_result_flips_to_failure(self, mock_arun, mock_exec, db_path, tmp_path):
        """When execute_task returns success=True but result contains API error, treat as failure."""
        api_error = 'API Error: 500 {"error": {"message": "Internal server error"}, "request_id": "req_abc"}'
        mock_exec.return_value = (True, api_error, None)
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Briefing", user_id="testuser", source_type="briefing")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        # Should be pending for retry (first attempt)
        assert task.status == "pending"
        assert task.attempt_count == 1

    @patch("istota.scheduler.execute_task", return_value=(True, "Here is your morning briefing...", None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_normal_result_not_affected(self, mock_arun, mock_exec, db_path, tmp_path):
        """Normal successful results are not falsely detected as API errors."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="Briefing", user_id="testuser", source_type="briefing")

        result = process_one_task(config)
        assert result is not None
        task_id, success = result
        assert success is True

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "completed"


# ---------------------------------------------------------------------------
# TestBriefingFailureSuppression
# ---------------------------------------------------------------------------


class TestBriefingFailureSuppression:
    """Test that briefing/scheduled task failures don't send error notifications to users."""

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

    @patch("istota.scheduler.execute_task", return_value=(False, "Fatal error", None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_briefing_failure_no_talk_notification(self, mock_arun, mock_exec, db_path, tmp_path):
        """Failed briefing tasks should not send error messages to Talk."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Morning briefing", user_id="testuser",
                source_type="briefing", conversation_token="room1",
            )
            # Exhaust retries so it fails permanently
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"

        # asyncio.run should NOT be called for Talk error notification
        assert mock_arun.call_count == 0

    @patch("istota.scheduler.execute_task", return_value=(False, "Fatal error", None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_scheduled_failure_no_talk_notification(self, mock_arun, mock_exec, db_path, tmp_path):
        """Failed scheduled tasks should not send error messages to Talk."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Daily check", user_id="testuser",
                source_type="scheduled", conversation_token="room1",
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"

        # No Talk notification for scheduled failures
        assert mock_arun.call_count == 0

    @patch("istota.scheduler.execute_task", return_value=(False, "Fatal error", None))
    @patch("istota.scheduler.asyncio.run", return_value=None)
    def test_interactive_failure_still_notifies(self, mock_arun, mock_exec, db_path, tmp_path):
        """Interactive (Talk) task failures should still send error messages."""
        config = self._make_config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Help me", user_id="testuser",
                source_type="talk", conversation_token="room1",
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (task_id,))

        result = process_one_task(config)
        assert result is not None
        _, success = result
        assert success is False

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.status == "failed"

        # Should have Talk notification with error
        assert mock_arun.call_count >= 1
