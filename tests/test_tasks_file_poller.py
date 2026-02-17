"""Tests for TASKS.md file polling and task creation."""

import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.config import Config, EmailConfig, UserConfig
from istota.db import Task, IstotaFileTask, init_db
from istota.tasks_file_poller import (
    TASKS_FILE_PATTERN,
    ParsedTask,
    compute_content_hash,
    discover_tasks_files,
    handle_tasks_file_completion,
    normalize_task_content,
    parse_tasks_file,
    poll_user_tasks_file,
    update_task_in_file,
)


# --- Fixtures ---


@pytest.fixture
def db_path(tmp_path):
    """Create and initialize a temporary SQLite database."""
    path = tmp_path / "test.db"
    init_db(path)
    return path


@pytest.fixture
def make_config(tmp_path, db_path):
    """Factory for creating Config with temp paths and optional overrides."""
    def _make(**overrides):
        defaults = dict(
            db_path=db_path,
            nextcloud_mount_path=tmp_path / "mount",
            users={"alice": UserConfig(display_name="Alice", email_addresses=["alice@example.com"])},
            email=EmailConfig(enabled=False),
        )
        defaults.update(overrides)
        return Config(**defaults)
    return _make


# --- TestNormalizeTaskContent ---


class TestNormalizeTaskContent:
    def test_basic_normalization(self):
        assert normalize_task_content("  Hello  World  ") == "hello world"

    def test_strip_timestamp_prefix(self):
        assert normalize_task_content("2025-01-26 12:34 | Send email") == "send email"

    def test_strip_result_suffix(self):
        assert normalize_task_content("Send email | Result: Email sent successfully") == "send email"

    def test_strip_error_suffix(self):
        assert normalize_task_content("Send email | Error: SMTP timeout") == "send email"

    def test_strip_trailing_ellipsis(self):
        assert normalize_task_content("Checking calendar...") == "checking calendar"

    def test_strip_all_combined(self):
        result = normalize_task_content("2025-01-26 12:34 | Send email... | Result: done")
        assert result == "send email"

    def test_empty_string(self):
        assert normalize_task_content("") == ""


# --- TestComputeContentHash ---


class TestComputeContentHash:
    def test_deterministic(self):
        h1 = compute_content_hash("Send email to Bob")
        h2 = compute_content_hash("Send email to Bob")
        assert h1 == h2

    def test_length_12(self):
        h = compute_content_hash("Some task")
        assert len(h) == 12

    def test_normalized_before_hash(self):
        # Different surface forms that normalize to same content should produce same hash
        h1 = compute_content_hash("Send email")
        h2 = compute_content_hash("2025-01-26 12:34 | Send email | Result: done")
        assert h1 == h2

    def test_different_content_different_hash(self):
        h1 = compute_content_hash("Send email")
        h2 = compute_content_hash("Read calendar")
        assert h1 != h2


# --- TestParseTasksFile ---


class TestParseTasksFile:
    def test_pending_task(self):
        content = "- [ ] Send email to Bob"
        tasks = parse_tasks_file(content)
        assert len(tasks) == 1
        assert tasks[0].status == "pending"
        assert tasks[0].normalized_content == "send email to bob"

    def test_in_progress_task(self):
        content = "- [~] Checking calendar..."
        tasks = parse_tasks_file(content)
        assert len(tasks) == 1
        assert tasks[0].status == "in_progress"

    def test_completed_task(self):
        content = "- [x] 2025-01-26 12:34 | Send email | Result: Email sent"
        tasks = parse_tasks_file(content)
        assert len(tasks) == 1
        assert tasks[0].status == "completed"

    def test_failed_task(self):
        content = "- [!] 2025-01-26 12:35 | Send email | Error: timeout"
        tasks = parse_tasks_file(content)
        assert len(tasks) == 1
        assert tasks[0].status == "failed"

    def test_multiple_tasks(self):
        content = """# Tasks

- [ ] First task
- [~] Second task...
- [x] 2025-01-26 12:34 | Third task | Result: Done
- [!] 2025-01-26 12:35 | Fourth task | Error: failed
"""
        tasks = parse_tasks_file(content)
        assert len(tasks) == 4
        assert tasks[0].status == "pending"
        assert tasks[1].status == "in_progress"
        assert tasks[2].status == "completed"
        assert tasks[3].status == "failed"

    def test_empty_content(self):
        assert parse_tasks_file("") == []

    def test_unknown_marker_skipped(self):
        content = "- [?] Unknown status task"
        tasks = parse_tasks_file(content)
        assert len(tasks) == 0

    def test_no_task_lines(self):
        content = """# Tasks

This is just a description.
No tasks here.
"""
        tasks = parse_tasks_file(content)
        assert len(tasks) == 0


# --- TestUpdateTaskInFile ---


class TestUpdateTaskInFile:
    def test_mark_in_progress(self):
        content = "- [ ] Send email to Bob"
        task_hash = compute_content_hash("Send email to Bob")
        result = update_task_in_file(content, task_hash, "in_progress")
        assert "- [~] Send email to Bob..." in result
        assert "- [ ]" not in result

    def test_mark_completed(self):
        content = "- [~] Send email to Bob..."
        task_hash = compute_content_hash("Send email to Bob")
        result = update_task_in_file(content, task_hash, "completed")
        assert "- [x]" in result
        assert "| Send email to Bob" in result
        assert "- [~]" not in result

    def test_mark_completed_with_result(self):
        content = "- [~] Send email to Bob..."
        task_hash = compute_content_hash("Send email to Bob")
        result = update_task_in_file(content, task_hash, "completed", result_summary="Email sent")
        assert "| Result: Email sent" in result

    def test_mark_failed(self):
        content = "- [~] Send email to Bob..."
        task_hash = compute_content_hash("Send email to Bob")
        result = update_task_in_file(content, task_hash, "failed")
        assert "- [!]" in result
        assert "| Send email to Bob" in result

    def test_mark_failed_with_error(self):
        content = "- [~] Send email to Bob..."
        task_hash = compute_content_hash("Send email to Bob")
        result = update_task_in_file(content, task_hash, "failed", error_message="SMTP timeout")
        assert "| Error: SMTP timeout" in result

    def test_preserves_other_lines(self):
        content = """# Tasks

- [ ] Task to update
- [ ] Other task
- [x] 2025-01-26 12:34 | Done task | Result: ok

Some footer text"""
        task_hash = compute_content_hash("Task to update")
        result = update_task_in_file(content, task_hash, "in_progress")
        assert "- [~] Task to update..." in result
        assert "- [ ] Other task" in result
        assert "- [x]" in result
        assert "Some footer text" in result


# --- TestPollUserTasksFile ---


class TestPollUserTasksFile:
    @patch("istota.tasks_file_poller.write_text")
    @patch("istota.tasks_file_poller.read_text")
    def test_creates_tasks_for_pending(self, mock_read, mock_write, make_config):
        config = make_config()
        mock_read.return_value = "- [ ] Send email\n- [ ] Read calendar"
        mock_write.return_value = None

        task_ids = poll_user_tasks_file(config, "alice", "/Users/alice/istota/config/TASKS.md")

        assert len(task_ids) == 2
        # Verify tasks exist in DB
        conn = sqlite3.connect(config.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM tasks WHERE source_type = 'istota_file'").fetchall()
        assert len(rows) == 2
        conn.close()
        # Verify write was called to update file with in-progress markers
        mock_write.assert_called_once()

    @patch("istota.tasks_file_poller.write_text")
    @patch("istota.tasks_file_poller.read_text")
    def test_skips_tracked_tasks(self, mock_read, mock_write, make_config):
        config = make_config()
        mock_read.return_value = "- [ ] Send email"

        # First poll creates the task
        task_ids_1 = poll_user_tasks_file(config, "alice", "/Users/alice/istota/config/TASKS.md")
        assert len(task_ids_1) == 1

        # Second poll should skip (already tracked)
        task_ids_2 = poll_user_tasks_file(config, "alice", "/Users/alice/istota/config/TASKS.md")
        assert len(task_ids_2) == 0

    @patch("istota.tasks_file_poller.write_text")
    @patch("istota.tasks_file_poller.read_text")
    def test_skips_non_pending(self, mock_read, mock_write, make_config):
        config = make_config()
        mock_read.return_value = "- [~] In progress...\n- [x] 2025-01-26 12:34 | Done | Result: ok"

        task_ids = poll_user_tasks_file(config, "alice", "/Users/alice/istota/config/TASKS.md")
        assert len(task_ids) == 0
        mock_write.assert_not_called()

    @patch("istota.tasks_file_poller.read_text")
    def test_handles_read_error(self, mock_read, make_config):
        config = make_config()
        mock_read.side_effect = OSError("File not found")

        task_ids = poll_user_tasks_file(config, "alice", "/Users/alice/istota/config/TASKS.md")
        assert task_ids == []

    @patch("istota.tasks_file_poller.write_text")
    @patch("istota.tasks_file_poller.read_text")
    def test_updates_file_with_in_progress(self, mock_read, mock_write, make_config):
        config = make_config()
        mock_read.return_value = "- [ ] Send email"

        poll_user_tasks_file(config, "alice", "/Users/alice/istota/config/TASKS.md")

        mock_write.assert_called_once()
        written_content = mock_write.call_args[0][2]  # positional arg: config, path, content
        assert "- [~] Send email..." in written_content
        assert "- [ ]" not in written_content


# --- TestHandleTasksFileCompletion ---


class TestHandleTasksFileCompletion:
    def _setup_task_and_istota_entry(self, config):
        """Create a task and its corresponding istota_file_tasks entry."""
        conn = sqlite3.connect(config.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row

        # Create the main task
        cursor = conn.execute(
            """INSERT INTO tasks (prompt, user_id, source_type, status)
               VALUES (?, ?, ?, ?) RETURNING id""",
            ("send email", "alice", "istota_file", "running"),
        )
        task_id = cursor.fetchone()[0]

        # Create istota_file_tasks entry
        conn.execute(
            """INSERT INTO istota_file_tasks
               (user_id, content_hash, original_line, normalized_content,
                file_path, task_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("alice", compute_content_hash("Send email"),
             "- [~] Send email...", "send email",
             "/Users/alice/istota/config/TASKS.md", task_id, "pending"),
        )
        conn.commit()

        task = Task(
            id=task_id, status="running", source_type="istota_file",
            user_id="alice", prompt="send email",
        )
        conn.close()
        return task

    @patch("istota.tasks_file_poller.write_text")
    @patch("istota.tasks_file_poller.read_text")
    def test_success_updates_file_and_db(self, mock_read, mock_write, make_config):
        config = make_config()
        task = self._setup_task_and_istota_entry(config)
        mock_read.return_value = "- [~] Send email..."

        handle_tasks_file_completion(config, task, success=True, result="Email sent successfully")

        # Verify file was updated
        mock_write.assert_called_once()
        written = mock_write.call_args[0][2]
        assert "- [x]" in written
        assert "Send email" in written

        # Verify DB status updated
        conn = sqlite3.connect(config.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, result_summary FROM istota_file_tasks WHERE task_id = ?",
            (task.id,),
        ).fetchone()
        assert row["status"] == "completed"
        assert row["result_summary"] == "Email sent successfully"
        conn.close()

    @patch("istota.tasks_file_poller.write_text")
    @patch("istota.tasks_file_poller.read_text")
    def test_failure_updates_file_and_db(self, mock_read, mock_write, make_config):
        config = make_config()
        task = self._setup_task_and_istota_entry(config)
        mock_read.return_value = "- [~] Send email..."

        handle_tasks_file_completion(config, task, success=False, result="SMTP timeout")

        mock_write.assert_called_once()
        written = mock_write.call_args[0][2]
        assert "- [!]" in written

        conn = sqlite3.connect(config.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, error_message FROM istota_file_tasks WHERE task_id = ?",
            (task.id,),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_message"] == "SMTP timeout"
        conn.close()

    @patch("istota.tasks_file_poller.write_text")
    @patch("istota.tasks_file_poller.read_text")
    def test_sends_email_notification(self, mock_read, mock_write, make_config):
        config = make_config(
            email=EmailConfig(enabled=True, bot_email="istota@example.com",
                              smtp_host="smtp.example.com", smtp_port=587,
                              imap_host="imap.example.com",
                              imap_user="istota", imap_password="pass"),
        )
        task = self._setup_task_and_istota_entry(config)
        mock_read.return_value = "- [~] Send email..."

        with patch("istota.tasks_file_poller.send_email", create=True) as mock_send, \
             patch("istota.skills.email.send_email") as mock_skill_send:
            # The function imports send_email inside the try block
            handle_tasks_file_completion(config, task, success=True, result="Done")

            # send_email is called via from .skills.email import send_email
            mock_skill_send.assert_called_once()
            call_kwargs = mock_skill_send.call_args
            assert call_kwargs.kwargs.get("to") == "alice@example.com" or \
                   call_kwargs[1].get("to") == "alice@example.com"

    @patch("istota.tasks_file_poller.write_text")
    @patch("istota.tasks_file_poller.read_text")
    def test_skips_email_when_not_configured(self, mock_read, mock_write, make_config):
        config = make_config(email=EmailConfig(enabled=False))
        task = self._setup_task_and_istota_entry(config)
        mock_read.return_value = "- [~] Send email..."

        with patch("istota.skills.email.send_email") as mock_send:
            handle_tasks_file_completion(config, task, success=True, result="Done")
            mock_send.assert_not_called()

    @patch("istota.tasks_file_poller.write_text")
    @patch("istota.tasks_file_poller.read_text")
    def test_no_istota_task_found(self, mock_read, mock_write, make_config):
        config = make_config()
        # Task with no corresponding istota_file_tasks entry
        task = Task(
            id=9999, status="running", source_type="istota_file",
            user_id="alice", prompt="orphan task",
        )
        # Should return without error
        handle_tasks_file_completion(config, task, success=True, result="Done")
        mock_read.assert_not_called()
        mock_write.assert_not_called()


# --- TestDiscoverTasksFiles ---


class TestDiscoverTasksFiles:
    @patch("istota.tasks_file_poller.list_files")
    def test_discovers_tasks_md(self, mock_list_files, make_config):
        config = make_config()
        mock_list_files.return_value = [
            {"name": "TASKS.md", "is_dir": False},
            {"name": "notes.txt", "is_dir": False},
        ]
        discovered = discover_tasks_files(config)
        assert len(discovered) == 1
        assert discovered[0].owner_id == "alice"
        assert discovered[0].file_path == "/Users/alice/istota/config/TASKS.md"
        # Verify it scans istota/config/ not user root
        mock_list_files.assert_called_once_with(config, "/Users/alice/istota/config")

    @patch("istota.tasks_file_poller.list_files")
    def test_skips_directories(self, mock_list_files, make_config):
        config = make_config()
        mock_list_files.return_value = [
            {"name": "TASKS.md", "is_dir": True},
        ]
        discovered = discover_tasks_files(config)
        assert len(discovered) == 0

    @patch("istota.tasks_file_poller.list_files")
    def test_handles_missing_user_dir(self, mock_list_files, make_config):
        config = make_config()
        mock_list_files.side_effect = FileNotFoundError("No user directory")
        discovered = discover_tasks_files(config)
        assert len(discovered) == 0


# --- TestTasksFilePattern ---


class TestTasksFilePattern:
    def test_matches_tasks_md(self):
        assert TASKS_FILE_PATTERN.match("TASKS.md")

    def test_matches_case_insensitive(self):
        assert TASKS_FILE_PATTERN.match("tasks.md")
        assert TASKS_FILE_PATTERN.match("Tasks.md")

    def test_no_match_other_files(self):
        assert not TASKS_FILE_PATTERN.match("README.md")
        assert not TASKS_FILE_PATTERN.match("notes.txt")
        assert not TASKS_FILE_PATTERN.match("TASKS.txt")
        assert not TASKS_FILE_PATTERN.match("ZORG.md")
        assert not TASKS_FILE_PATTERN.match("_TASKS.md")
