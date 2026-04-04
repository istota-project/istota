"""Tests for email polling and task creation."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config, EmailConfig as AppEmailConfig, UserConfig
from istota.email_poller import (
    _extract_user_from_recipient,
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


def _email(id="1", subject="Hello", sender="alice@test.com", body="Hi there",
           to=("bot@test.com",), cc=()):
    return Email(
        id=id, subject=subject, sender=sender,
        date="Mon, 01 Jan 2026 10:00:00 +0000",
        body=body, attachments=[],
        message_id="<msg1@test.com>", references=None,
        to=to, cc=cc,
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
        email = _email(sender="stranger@unknown.com")

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
        ):
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


# =============================================================================
# TestSendEmailReturnsMessageId
# =============================================================================


class TestSendEmailReturnsMessageId:
    def test_send_email_returns_message_id(self):
        config = EmailConfig(
            imap_host="imap.test", imap_port=993,
            imap_user="u", imap_password="p",
            smtp_host="smtp.test", smtp_port=587,
            bot_email="bot@test.com",
        )
        from istota.skills.email import send_email
        with patch("istota.skills.email._send_smtp"):
            result = send_email(
                to="alice@test.com",
                subject="Hello",
                body="Hi",
                config=config,
            )
        assert result.startswith("<") and result.endswith(">")
        assert "@test.com>" in result

    def test_reply_to_email_returns_message_id(self):
        config = EmailConfig(
            imap_host="imap.test", imap_port=993,
            imap_user="u", imap_password="p",
            smtp_host="smtp.test", smtp_port=587,
            bot_email="bot@test.com",
        )
        from istota.skills.email import reply_to_email
        with patch("istota.skills.email._send_smtp"):
            result = reply_to_email(
                to_addr="alice@test.com",
                subject="Hello",
                body="Reply",
                config=config,
                in_reply_to="<orig@test.com>",
            )
        assert result.startswith("<") and result.endswith(">")


# =============================================================================
# TestDeferredSentEmail
# =============================================================================


class TestDeferredSentEmail:
    def test_write_deferred_sent_email(self, tmp_path):
        from istota.skills.email import _write_deferred_sent_email

        env = {
            "ISTOTA_TASK_ID": "42",
            "ISTOTA_DEFERRED_DIR": str(tmp_path),
            "ISTOTA_CONVERSATION_TOKEN": "room1",
            "ISTOTA_USER_ID": "stefan",
        }
        with patch.dict("os.environ", env, clear=False):
            _write_deferred_sent_email("<msg@test.com>", "bob@x.com", "Hello")

        path = tmp_path / "task_42_sent_emails.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["message_id"] == "<msg@test.com>"
        assert data[0]["to_addr"] == "bob@x.com"
        assert data[0]["subject"] == "Hello"
        assert data[0]["conversation_token"] == "room1"
        assert data[0]["user_id"] == "stefan"

    def test_write_deferred_appends_multiple(self, tmp_path):
        from istota.skills.email import _write_deferred_sent_email

        env = {
            "ISTOTA_TASK_ID": "42",
            "ISTOTA_DEFERRED_DIR": str(tmp_path),
            "ISTOTA_USER_ID": "stefan",
        }
        with patch.dict("os.environ", env, clear=False):
            _write_deferred_sent_email("<msg1@test.com>", "a@x.com", "First")
            _write_deferred_sent_email("<msg2@test.com>", "b@x.com", "Second")

        data = json.loads((tmp_path / "task_42_sent_emails.json").read_text())
        assert len(data) == 2

    def test_write_deferred_skips_without_env(self, tmp_path):
        from istota.skills.email import _write_deferred_sent_email

        env = {"ISTOTA_TASK_ID": "", "ISTOTA_DEFERRED_DIR": ""}
        with patch.dict("os.environ", env, clear=False):
            _write_deferred_sent_email("<msg@test.com>", "bob@x.com", "Hello")

        # No file should be written
        assert not list(tmp_path.glob("*.json"))

    def test_cmd_send_writes_deferred(self, tmp_path):
        from istota.skills.email import cmd_send

        env = {
            "SMTP_HOST": "smtp.test",
            "SMTP_PORT": "587",
            "SMTP_FROM": "bot@test.com",
            "ISTOTA_TASK_ID": "99",
            "ISTOTA_DEFERRED_DIR": str(tmp_path),
            "ISTOTA_USER_ID": "stefan",
        }
        args = MagicMock()
        args.to = "bob@example.com"
        args.subject = "Meeting"
        args.body = "Let's meet"
        args.body_file = None
        args.html = False

        with (
            patch.dict("os.environ", env, clear=False),
            patch("istota.skills.email._send_smtp"),
        ):
            result = cmd_send(args)

        assert result["status"] == "ok"
        path = tmp_path / "task_99_sent_emails.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["to_addr"] == "bob@example.com"
        assert data[0]["subject"] == "Meeting"


# =============================================================================
# TestMatchThread
# =============================================================================


class TestMatchThread:
    def test_match_by_references(self, db_path):
        from istota.email_poller import _match_thread

        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<sent1@bot.com>",
                to_addr="bob@ext.com",
                subject="Meeting",
                conversation_token="room1",
            )

            # Simulate inbound email with References containing our sent message ID
            email = _email(
                sender="bob@ext.com",
                subject="Re: Meeting",
            )
            email.references = "<sent1@bot.com>"

            match = _match_thread(conn, email)
            assert match is not None
            assert match.user_id == "stefan"
            assert match.conversation_token == "room1"

    def test_no_match_without_references(self, db_path):
        from istota.email_poller import _match_thread

        with db.get_db(db_path) as conn:
            email = _email(sender="unknown@ext.com", subject="Random")
            email.references = None

            assert _match_thread(conn, email) is None

    def test_no_match_with_unknown_references(self, db_path):
        from istota.email_poller import _match_thread

        with db.get_db(db_path) as conn:
            email = _email(sender="bob@ext.com", subject="Re: Something")
            email.references = "<unknown@other.com>"

            assert _match_thread(conn, email) is None

    def test_match_multiple_references(self, db_path):
        """References header with multiple IDs — should match our sent one."""
        from istota.email_poller import _match_thread

        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<sent2@bot.com>",
                to_addr="alice@ext.com",
                subject="Hello",
            )

            email = _email(sender="alice@ext.com", subject="Re: Hello")
            email.references = "<original@alice.com> <sent2@bot.com>"

            match = _match_thread(conn, email)
            assert match is not None
            assert match.message_id == "<sent2@bot.com>"


# =============================================================================
# TestPollEmailsThreadMatching
# =============================================================================


class TestPollEmailsThreadMatching:
    """Tests for email poller routing emissary replies via thread matching."""

    def test_unknown_sender_reply_routes_to_originating_user(self, make_config):
        """Reply from unknown sender matching a sent thread routes to originating user."""
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(email_addresses=["stefan@test.com"])}

        # Pre-record an outbound email from stefan
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<outbound@bot.com>",
                to_addr="external@proton.me",
                subject="Set up a meeting",
                conversation_token="talk_room_42",
            )

        envelope = _envelope(id="2", sender="external@proton.me", subject="Re: Set up a meeting")
        email = Email(
            id="2", subject="Re: Set up a meeting", sender="external@proton.me",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="How about Tuesday?", attachments=[],
            message_id="<reply@proton.me>",
            references="<outbound@bot.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
            patch("istota.email_poller.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "stefan"
            assert task.output_target == "talk"
            assert task.conversation_token == "talk_room_42"
            assert "Emissary email reply" in task.prompt
            assert "external@proton.me" in task.prompt
            assert "How about Tuesday?" in task.prompt

    def test_unknown_sender_no_thread_match_discarded(self, make_config):
        """Unknown sender with no thread match is discarded as before."""
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(email_addresses=["stefan@test.com"])}

        envelope = _envelope(id="3", sender="stranger@random.com", subject="Buy stuff")
        email = Email(
            id="3", subject="Buy stuff", sender="stranger@random.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Spam", attachments=[],
            message_id="<spam@random.com>",
            references=None,
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
            patch("istota.email_poller.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert task_ids == []

        # Should still be marked as processed
        with db.get_db(config.db_path) as conn:
            assert db.is_email_processed(conn, "3")

    def test_known_sender_still_works_normally(self, make_config):
        """Known sender emails are routed normally (no output_target override)."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(sender="alice@test.com")
        email = _email(sender="alice@test.com")

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
            patch("istota.email_poller.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "alice"
            assert task.output_target is None  # Normal email routing
            assert "Emissary" not in task.prompt

    def test_emissary_reply_without_conversation_token_uses_thread_id(self, make_config):
        """If original sent email had no conversation_token, fall back to thread_id."""
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(email_addresses=["stefan@test.com"])}

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Hello",
                conversation_token=None,  # No Talk context
            )

        envelope = _envelope(id="4", sender="ext@x.com", subject="Re: Hello")
        email = Email(
            id="4", subject="Re: Hello", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hi back", attachments=[],
            message_id="<r@x.com>",
            references="<out@bot.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
            patch("istota.email_poller.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "stefan"
            assert task.output_target == "talk"
            # Should use thread_id since no conversation_token on sent email
            assert task.conversation_token is not None


# =============================================================================
# TestExtractUserFromRecipient
# =============================================================================


class TestExtractUserFromRecipient:
    """Tests for plus-address routing via recipient headers."""

    def _config_with_users(self):
        config = Config()
        config.email = _email_config()  # bot_email = "bot@test.com"
        config.users = {
            "stefan": UserConfig(email_addresses=["stefan@example.com"]),
            "alice": UserConfig(email_addresses=["alice@example.com"]),
        }
        return config

    def test_extracts_user_from_to_header(self):
        config = self._config_with_users()
        email = _email(to=("bot+stefan@test.com",))
        assert _extract_user_from_recipient(config, email) == "stefan"

    def test_extracts_user_from_cc_header(self):
        config = self._config_with_users()
        email = _email(to=("someone@other.com",), cc=("bot+alice@test.com",))
        assert _extract_user_from_recipient(config, email) == "alice"

    def test_returns_none_for_bare_bot_address(self):
        config = self._config_with_users()
        email = _email(to=("bot@test.com",))
        assert _extract_user_from_recipient(config, email) is None

    def test_returns_none_for_invalid_user(self):
        config = self._config_with_users()
        email = _email(to=("bot+nonexistent@test.com",))
        assert _extract_user_from_recipient(config, email) is None

    def test_case_insensitive_matching(self):
        config = self._config_with_users()
        email = _email(to=("BOT+Stefan@Test.Com",))
        assert _extract_user_from_recipient(config, email) == "stefan"

    def test_ignores_different_domain(self):
        config = self._config_with_users()
        email = _email(to=("bot+stefan@other-domain.com",))
        assert _extract_user_from_recipient(config, email) is None

    def test_returns_none_when_no_recipients(self):
        config = self._config_with_users()
        email = _email(to=(), cc=())
        assert _extract_user_from_recipient(config, email) is None

    def test_first_valid_match_wins(self):
        """If both To and Cc have plus-addresses, To wins."""
        config = self._config_with_users()
        email = _email(to=("bot+stefan@test.com",), cc=("bot+alice@test.com",))
        assert _extract_user_from_recipient(config, email) == "stefan"


# =============================================================================
# TestPollEmailsPlusAddressRouting
# =============================================================================


class TestPollEmailsPlusAddressRouting:
    """Tests for plus-address routing in the poll loop."""

    def test_plus_address_routes_unknown_sender(self, make_config):
        """Unknown sender emailing bot+stefan@ should route to stefan."""
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(email_addresses=["stefan@test.com"])}

        envelope = _envelope(id="10", sender="stranger@external.com", subject="Hello agent")
        email = Email(
            id="10", subject="Hello agent", sender="stranger@external.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Can you help me?", attachments=[],
            message_id="<ext1@external.com>", references=None,
            to=("bot+stefan@test.com",), cc=(),
        )

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
            patch("istota.email_poller.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "stefan"
            assert task.source_type == "email"
            assert "stranger@external.com" in task.prompt

    def test_plus_address_takes_precedence_over_sender_match(self, make_config):
        """If sender matches alice but To is bot+stefan@, route to stefan."""
        config = make_config()
        config.email = _email_config()
        config.users = {
            "stefan": UserConfig(email_addresses=["stefan@test.com"]),
            "alice": UserConfig(email_addresses=["alice@test.com"]),
        }

        envelope = _envelope(id="11", sender="alice@test.com", subject="For stefan")
        email = Email(
            id="11", subject="For stefan", sender="alice@test.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Route this to stefan", attachments=[],
            message_id="<a11@test.com>", references=None,
            to=("bot+stefan@test.com",), cc=(),
        )

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
            patch("istota.email_poller.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "stefan"  # plus-address wins over sender

    def test_invalid_plus_address_falls_through_to_sender(self, make_config):
        """Plus-address with invalid user falls through to sender-based routing."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="12", sender="alice@test.com", subject="Test")
        email = Email(
            id="12", subject="Test", sender="alice@test.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<a12@test.com>", references=None,
            to=("bot+nonexistent@test.com",), cc=(),
        )

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
            patch("istota.email_poller.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "alice"  # fell through to sender match

    def test_routing_method_stored_for_plus_address(self, make_config):
        """routing_method should be 'plus_address' when routed via plus-addressing."""
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(email_addresses=["stefan@test.com"])}

        envelope = _envelope(id="13", sender="stranger@ext.com", subject="Hi")
        email = Email(
            id="13", subject="Hi", sender="stranger@ext.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<s13@ext.com>", references=None,
            to=("bot+stefan@test.com",), cc=(),
        )

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
            patch("istota.email_poller.download_attachments", return_value=[]),
        ):
            poll_emails(config)

        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id = ?", ("13",)
            ).fetchone()
            assert row is not None
            assert row[0] == "plus_address"

    def test_routing_method_stored_for_sender_match(self, make_config):
        """routing_method should be 'sender_match' for known sender routing."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="14", sender="alice@test.com", subject="Hi")
        email = _email(id="14", sender="alice@test.com")

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
            patch("istota.email_poller.download_attachments", return_value=[]),
        ):
            poll_emails(config)

        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id = ?", ("14",)
            ).fetchone()
            assert row is not None
            assert row[0] == "sender_match"

    def test_routing_method_stored_for_thread_match(self, make_config):
        """routing_method should be 'thread_match' for emissary reply routing."""
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(email_addresses=["stefan@test.com"])}

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<out15@bot.com>",
                to_addr="ext@x.com",
                subject="Hello",
            )

        envelope = _envelope(id="15", sender="ext@x.com", subject="Re: Hello")
        email = Email(
            id="15", subject="Re: Hello", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Reply", attachments=[],
            message_id="<r15@x.com>",
            references="<out15@bot.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
            patch("istota.email_poller.download_attachments", return_value=[]),
        ):
            poll_emails(config)

        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id = ?", ("15",)
            ).fetchone()
            assert row is not None
            assert row[0] == "thread_match"

    def test_routing_method_stored_for_discard(self, make_config):
        """routing_method should be 'discarded' for unknown sender with no match."""
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(email_addresses=["stefan@test.com"])}

        envelope = _envelope(id="16", sender="spam@nowhere.com", subject="Spam")
        email = Email(
            id="16", subject="Spam", sender="spam@nowhere.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Buy stuff", attachments=[],
            message_id="<spam16@nowhere.com>", references=None,
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.email_poller.list_emails", return_value=[envelope]),
            patch("istota.email_poller.read_email", return_value=email),
            patch("istota.email_poller.download_attachments", return_value=[]),
        ):
            poll_emails(config)

        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id = ?", ("16",)
            ).fetchone()
            assert row is not None
            assert row[0] == "discarded"
