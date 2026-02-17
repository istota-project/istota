"""Tests for email polling and task creation."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config, EmailConfig as AppEmailConfig, UserConfig
from istota.email_poller import (
    cleanup_old_emails,
    compute_thread_id,
    get_email_config,
    normalize_subject,
    poll_emails,
)
from istota.skills.email import Email, EmailConfig, EmailEnvelope


@pytest.fixture
def db_path(tmp_path):
    """Create and initialize a temporary SQLite database."""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def make_config(db_path, tmp_path):
    """Create a Config object with tmp paths and test DB."""
    def _make(**overrides):
        config = Config()
        config.db_path = db_path
        config.temp_dir = tmp_path / "temp"
        config.temp_dir.mkdir(exist_ok=True)
        config.skills_dir = tmp_path / "skills"
        config.skills_dir.mkdir(exist_ok=True)
        for key, val in overrides.items():
            setattr(config, key, val)
        return config
    return _make


def _email_config():
    """Return a standard test AppEmailConfig."""
    return AppEmailConfig(
        enabled=True,
        imap_host="imap.test",
        imap_port=993,
        imap_user="user",
        imap_password="pass",
        smtp_host="smtp.test",
        smtp_port=587,
        bot_email="bot@test.com",
    )


def _envelope(id="1", subject="Hello", sender="alice@test.com", date="Mon, 01 Jan 2026 10:00:00 +0000"):
    return EmailEnvelope(id=id, subject=subject, sender=sender, date=date, is_read=False)


def _email(id="1", subject="Hello", sender="alice@test.com", body="Hi there"):
    return Email(
        id=id, subject=subject, sender=sender,
        date="Mon, 01 Jan 2026 10:00:00 +0000",
        body=body, attachments=[],
        message_id="<msg1@test.com>", references=None,
    )


# =============================================================================
# TestNormalizeSubject
# =============================================================================


class TestNormalizeSubject:
    def test_basic(self):
        assert normalize_subject("Hello World") == "hello world"

    def test_strip_re_prefix(self):
        assert normalize_subject("Re: Hello") == "hello"

    def test_strip_fwd_prefix(self):
        assert normalize_subject("Fwd: Hello") == "hello"

    def test_strip_multiple_prefixes(self):
        assert normalize_subject("Re: Fwd: Re: Hello") == "hello"

    def test_case_insensitive(self):
        assert normalize_subject("RE: FWD: Hello") == "hello"
        assert normalize_subject("Fw: Hello") == "hello"

    def test_normalize_whitespace(self):
        assert normalize_subject("  Hello   World  ") == "hello world"

    def test_lowercase(self):
        assert normalize_subject("IMPORTANT Meeting") == "important meeting"


# =============================================================================
# TestComputeThreadId
# =============================================================================


class TestComputeThreadId:
    def test_deterministic(self):
        id1 = compute_thread_id("Hello", ["a@test.com", "b@test.com"])
        id2 = compute_thread_id("Hello", ["a@test.com", "b@test.com"])
        assert id1 == id2

    def test_length_16(self):
        result = compute_thread_id("Hello", ["a@test.com"])
        assert len(result) == 16

    def test_sorted_participants(self):
        id1 = compute_thread_id("Hello", ["b@test.com", "a@test.com"])
        id2 = compute_thread_id("Hello", ["a@test.com", "b@test.com"])
        assert id1 == id2

    def test_normalized_subject(self):
        id1 = compute_thread_id("Re: Hello", ["a@test.com"])
        id2 = compute_thread_id("Hello", ["a@test.com"])
        assert id1 == id2

    def test_different_subjects_different_ids(self):
        id1 = compute_thread_id("Hello", ["a@test.com"])
        id2 = compute_thread_id("Goodbye", ["a@test.com"])
        assert id1 != id2


# =============================================================================
# TestPollEmails
# =============================================================================


class TestPollEmails:
    def test_creates_task_for_known_sender(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope()
        email = _email()

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
            patch("istota.email_poller.download_attachments", return_value=[]),
            patch("istota.email_poller.ensure_user_directories_v2"),
            patch("istota.email_poller.upload_file_to_inbox_v2"),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        # Verify the task was created in the database
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task is not None
            assert task.user_id == "alice"
            assert task.source_type == "email"
            assert "alice@test.com" in task.prompt

    def test_skips_processed_email(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope()

        # Pre-mark the email as processed
        with db.get_db(config.db_path) as conn:
            db.mark_email_processed(conn, email_id="1", sender_email="alice@test.com", subject="Hello")

        with patch("istota.email_poller.list_emails", return_value=[envelope]):
            task_ids = poll_emails(config)

        assert task_ids == []

    def test_skips_bot_email(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(sender="bot@test.com")

        with patch("istota.email_poller.list_emails", return_value=[envelope]):
            task_ids = poll_emails(config)

        assert task_ids == []

        # Verify marked as processed
        with db.get_db(config.db_path) as conn:
            assert db.is_email_processed(conn, "1")

    def test_skips_unknown_sender(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(sender="stranger@unknown.com")

        with patch("istota.email_poller.list_emails", return_value=[envelope]):
            task_ids = poll_emails(config)

        assert task_ids == []

        # Verify marked as processed (but no task created)
        with db.get_db(config.db_path) as conn:
            assert db.is_email_processed(conn, "1")

    def test_disabled_returns_empty(self, make_config):
        config = make_config()
        config.email = AppEmailConfig(enabled=False)

        task_ids = poll_emails(config)
        assert task_ids == []

    def test_handles_list_error(self, make_config):
        config = make_config()
        config.email = _email_config()

        with patch("istota.email_poller.list_emails", side_effect=Exception("IMAP connection failed")):
            task_ids = poll_emails(config)

        assert task_ids == []


# =============================================================================
# TestCleanupOldEmails
# =============================================================================


class TestCleanupOldEmails:
    def test_disabled_returns_zero(self, make_config):
        config = make_config()
        config.email = AppEmailConfig(enabled=False)

        result = cleanup_old_emails(config, days=7)
        assert result == 0

    def test_zero_days_returns_zero(self, make_config):
        config = make_config()
        config.email = _email_config()

        result = cleanup_old_emails(config, days=0)
        assert result == 0

    def test_deletes_old_emails(self, make_config):
        config = make_config()
        config.email = _email_config()

        # An old email (date well in the past)
        old_envelope = _envelope(
            id="old1",
            date="Mon, 01 Jan 2020 10:00:00 +0000",
        )

        with (
            patch("istota.email_poller.list_emails", return_value=[old_envelope]),
            patch("istota.email_poller.delete_email", return_value=True) as mock_delete,
        ):
            result = cleanup_old_emails(config, days=7)

        assert result == 1
        mock_delete.assert_called_once()

    def test_handles_list_error(self, make_config):
        config = make_config()
        config.email = _email_config()

        with patch("istota.email_poller.list_emails", side_effect=Exception("IMAP error")):
            result = cleanup_old_emails(config, days=7)

        assert result == 0


# =============================================================================
# TestGetEmailConfig
# =============================================================================


class TestGetEmailConfig:
    def test_converts_config(self, make_config):
        config = make_config()
        config.email = _email_config()

        email_config = get_email_config(config)

        assert isinstance(email_config, EmailConfig)
        assert email_config.imap_host == "imap.test"
        assert email_config.imap_port == 993
        assert email_config.smtp_host == "smtp.test"
        assert email_config.smtp_port == 587
        assert email_config.bot_email == "bot@test.com"
