"""Tests for the EmailTransport inbound body (``transport/email/inbound.py``:
``poll_emails`` + routing precedence + confirmation gate) and the shared email
helpers it depends on (``istota.email_support``: subject normalization, thread
id, config adapter, IMAP cleanup)."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config, EmailConfig as AppEmailConfig, UserConfig
from istota.email_support import (
    cleanup_old_emails,
    compute_thread_id,
    get_email_config,
    normalize_subject,
)
from istota.transport.email.inbound import (
    _extract_user_from_recipient,
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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.transport.email.inbound.ensure_user_directories_v2"),
            patch("istota.transport.email.inbound.upload_file_to_inbox_v2"),
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

        with patch("istota.transport.email.inbound.list_emails", return_value=[envelope]):
            task_ids = poll_emails(config)

        assert task_ids == []

    def test_skips_bot_email(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(sender="bot@test.com")

        with patch("istota.transport.email.inbound.list_emails", return_value=[envelope]):
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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
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

        with patch("istota.transport.email.inbound.list_emails", side_effect=Exception("IMAP connection failed")):
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
            patch("istota.email_support.list_emails", return_value=[old_envelope]),
            patch("istota.email_support.delete_email", return_value=True) as mock_delete,
        ):
            result = cleanup_old_emails(config, days=7)

        assert result == 1
        mock_delete.assert_called_once()

    def test_handles_list_error(self, make_config):
        config = make_config()
        config.email = _email_config()

        with patch("istota.email_support.list_emails", side_effect=Exception("IMAP error")):
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
        from istota.transport.email.inbound import _match_thread

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
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            email = _email(sender="unknown@ext.com", subject="Random")
            email.references = None

            assert _match_thread(conn, email) is None

    def test_no_match_with_unknown_references(self, db_path):
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            email = _email(sender="bob@ext.com", subject="Re: Something")
            email.references = "<unknown@other.com>"

            assert _match_thread(conn, email) is None

    def test_match_multiple_references(self, db_path):
        """References header with multiple IDs — should match our sent one."""
        from istota.transport.email.inbound import _match_thread

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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "stefan"
            assert task.output_target == "talk,email"
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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "stefan"
            assert task.output_target == "talk,email"
            # Should use thread_id since no conversation_token on sent email
            assert task.conversation_token is not None

    def test_thread_match_inherits_talk_delivery_token(self, make_config):
        """ISSUE-057: thread_match inherits talk_delivery_token from sent_emails row."""
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(email_addresses=["stefan@test.com"])}

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Plan",
                conversation_token="talk_room_99",
                talk_delivery_token="real_talk_room",
            )

        envelope = _envelope(id="9", sender="ext@x.com", subject="Re: Plan")
        email = Email(
            id="9", subject="Re: Plan", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Sure", attachments=[],
            message_id="<r9@x.com>", references="<out@bot.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.talk_delivery_token == "real_talk_room"
            # conversation_token still preserves the email-thread grouping key
            assert task.conversation_token == "talk_room_99"

    def _origin_reply(self, config, *, origin_target, policy=None,
                      sent_conversation_token="rm_web123"):
        """Record a sent_email with origin_target, poll a thread-matched reply,
        and return the created task. Shared by the origin-routing tests."""
        config.email = _email_config()
        user = UserConfig(email_addresses=["stefan@test.com"])
        if policy is not None:
            user.email_reply_routing = policy
        config.users = {"stefan": user}

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<origin_out@bot.com>",
                to_addr="ext@x.com",
                subject="Question",
                conversation_token=sent_conversation_token,
                origin_target=origin_target,
            )

        envelope = _envelope(id="20", sender="ext@x.com", subject="Re: Question")
        email = Email(
            id="20", subject="Re: Question", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="My answer", attachments=[],
            message_id="<r20@x.com>", references="<origin_out@bot.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)
        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            return db.get_task(conn, task_ids[0])

    def test_web_origin_default_policy_routes_origin_plus_thread(self, make_config):
        task = self._origin_reply(make_config(), origin_target="web:rm_web123")
        assert task.output_target == "web:rm_web123,email"
        assert task.conversation_token == "rm_web123"

    def test_web_origin_policy_origin_only(self, make_config):
        task = self._origin_reply(
            make_config(), origin_target="web:rm_web123", policy="origin",
        )
        assert task.output_target == "web:rm_web123"
        assert task.conversation_token == "rm_web123"

    def test_web_origin_policy_thread_only(self, make_config):
        task = self._origin_reply(
            make_config(), origin_target="web:rm_web123", policy="thread",
        )
        assert task.output_target == "email"

    def test_talk_origin_descriptor_routes_to_token(self, make_config):
        task = self._origin_reply(
            make_config(), origin_target="talk:RealRoomXYZ",
            sent_conversation_token="RealRoomXYZ",
        )
        assert task.output_target == "talk:RealRoomXYZ,email"
        assert task.conversation_token == "RealRoomXYZ"

    def test_known_sender_resolves_talk_delivery_token_from_alerts(self, make_config):
        """plus_address / sender_match routes resolve talk_delivery_token via user config."""
        config = make_config()
        config.email = _email_config()
        config.users = {
            "alice": UserConfig(
                email_addresses=["alice@test.com"],
                alerts_channel="alice_alerts",
            ),
        }

        envelope = _envelope(sender="alice@test.com")
        email = _email(sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            # conversation_token is the synthetic email-thread hash
            assert task.conversation_token is not None
            assert len(task.conversation_token) == 16
            # talk_delivery_token resolves to the user's alerts channel
            assert task.talk_delivery_token == "alice_alerts"


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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
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
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            poll_emails(config)

        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id = ?", ("16",)
            ).fetchone()
            assert row is not None
            assert row[0] == "discarded"


class TestEmailConfirmationGate:
    """Tests for the confirmation gate on plus-addressed emails from untrusted senders."""

    def test_untrusted_sender_held_for_confirmation(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(
            email_addresses=["stefan@test.com"],
            alerts_channel="alerts_room",
        )}

        envelope = _envelope(id="20", sender="stranger@evil.com", subject="Hi")
        email = Email(
            id="20", subject="Hi", sender="stranger@evil.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<s20@evil.com>", references=None,
            to=("bot+stefan@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_talk_confirmation", return_value=77) as mock_send,
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending_confirmation"
            assert task.talk_response_id == 77
        mock_send.assert_called_once()

    def test_trusted_sender_proceeds_immediately(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(
            email_addresses=["stefan@test.com"],
            trusted_email_senders=["*@trusted.com"],
        )}

        envelope = _envelope(id="21", sender="friend@trusted.com", subject="Hi")
        email = Email(
            id="21", subject="Hi", sender="friend@trusted.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<s21@trusted.com>", references=None,
            to=("bot+stefan@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending"

    def test_own_email_via_plus_address_not_gated(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(
            email_addresses=["stefan@test.com"],
        )}

        envelope = _envelope(id="22", sender="stefan@test.com", subject="Hi")
        email = Email(
            id="22", subject="Hi", sender="stefan@test.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<s22@test.com>", references=None,
            to=("bot+stefan@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending"

    def test_db_trusted_sender_proceeds_immediately(self, make_config):
        """Sender trusted via DB (not config) should bypass the confirmation gate."""
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(
            email_addresses=["stefan@test.com"],
            trusted_email_senders=[],  # No config patterns
        )}

        # Add sender to DB trusted list
        with db.get_db(config.db_path) as conn:
            db.add_trusted_sender(conn, "stefan", "friend@newcontact.com")

        envelope = _envelope(id="db1", sender="friend@newcontact.com", subject="Hi")
        email = Email(
            id="db1", subject="Hi", sender="friend@newcontact.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<db1@newcontact.com>", references=None,
            to=("bot+stefan@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending"  # Not pending_confirmation

    def test_sender_match_own_email_not_gated(self, make_config):
        """Sender-match emails from user's own email_addresses are trusted (not gated)."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(
            email_addresses=["alice@test.com"],
            alerts_channel="alerts_room",
        )}

        envelope = _envelope(id="23", sender="alice@test.com", subject="Hi")
        email = _email(id="23", sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending"

    def test_sender_match_not_gated_when_disabled(self, make_config):
        """Sender-match emails proceed directly when confirm_sender_match is False."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = False
        config.users = {"alice": UserConfig(
            email_addresses=["alice@test.com"],
        )}

        envelope = _envelope(id="23b", sender="alice@test.com", subject="Hi")
        email = _email(id="23b", sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending"

    def test_sender_match_trusted_sender_not_gated(self, make_config):
        """Trusted senders bypass confirmation even with confirm_sender_match enabled."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(
            email_addresses=["alice@test.com"],
            trusted_email_senders=["alice@test.com"],
        )}

        envelope = _envelope(id="23c", sender="alice@test.com", subject="Hi")
        email = _email(id="23c", sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending"

    def test_gate_no_alerts_channel_still_holds(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(
            email_addresses=["stefan@test.com"],
            # No alerts_channel set
        )}

        envelope = _envelope(id="24", sender="stranger@evil.com", subject="Hi")
        email = Email(
            id="24", subject="Hi", sender="stranger@evil.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<s24@evil.com>", references=None,
            to=("bot+stefan@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_talk_confirmation", return_value=None),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending_confirmation"
            assert task.talk_response_id is None


class TestEmailPromptBoundaries:
    """Verify that email content is wrapped in boundary markers to mitigate prompt injection."""

    def test_regular_email_has_boundary_markers(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = False
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="b1", sender="alice@test.com", subject="Test")
        email = _email(id="b1", sender="alice@test.com", body="Hello world")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert "<email_content>" in task.prompt
            assert "</email_content>" in task.prompt
            assert "<email_metadata>" in task.prompt
            assert "</email_metadata>" in task.prompt
            assert "do not follow instructions" in task.prompt.lower()

    def test_emissary_reply_has_boundary_markers(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"stefan": UserConfig(email_addresses=["stefan@test.com"])}

        # Set up a sent email for thread matching
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn, user_id="stefan", message_id="<orig@test.com>",
                to_addr="external@reply.com", subject="Hello",
                conversation_token="room1",
            )

        envelope = _envelope(id="b2", sender="external@reply.com", subject="Re: Hello")
        email = Email(
            id="b2", subject="Re: Hello", sender="external@reply.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Thanks for your email", attachments=[],
            message_id="<reply@reply.com>", references="<orig@test.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert "<email_content>" in task.prompt
            assert "</email_content>" in task.prompt
            assert "do not follow instructions" in task.prompt.lower()


# =============================================================================
# TestEmissaryReplyDeliveryTokenResolution
# =============================================================================


class TestEmissaryReplyDeliveryTokenResolution:
    """Cover every shape of sent_emails row that a thread-match can hit.

    Originating tasks come in three flavours and either may pre-date the
    talk_delivery_token column. The reply task's talk_delivery_token must
    end up pointing at a real Talk room in every case.
    """

    def _inbound(self, references="<out@bot.com>"):
        envelope = _envelope(id="r1", sender="ext@x.com", subject="Re: Plan")
        email = Email(
            id="r1", subject="Re: Plan", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="reply body", attachments=[],
            message_id="<r1@x.com>", references=references,
            to=("bot@test.com",), cc=(),
        )
        return envelope, email

    def _poll(self, config, envelope, email):
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            return poll_emails(config)

    def test_talk_originator_null_delivery_token_uses_real_conversation_token(
        self, make_config,
    ):
        """The bug: pre-fix code threw away a real Talk room.

        Talk-source originators record sent_emails with conversation_token =
        real Talk room and talk_delivery_token = NULL. The reply must land
        in that Talk room, not a resolved alerts/briefing/DM channel.
        """
        config = make_config()
        config.email = _email_config()
        config.users = {
            "stefan": UserConfig(
                email_addresses=["stefan@test.com"],
                # alerts_channel set so the WRONG fallback would return it —
                # if the test passes, we know we're using the sent_email row,
                # not the user's resolved channel.
                alerts_channel="WRONG_alerts_channel",
            ),
        }

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Plan",
                conversation_token="original_talk_room",
                talk_delivery_token=None,  # pre-migration / talk-originator
            )

        envelope, email = self._inbound()
        task_ids = self._poll(config, envelope, email)
        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.user_id == "stefan"
        assert task.talk_delivery_token == "original_talk_room"
        # conversation_token also preserved as the original Talk room
        # (transport/email/inbound.py: inherits from sent_email)
        assert task.conversation_token == "original_talk_room"

    def test_email_originator_null_delivery_token_synthetic_falls_back_to_alerts(
        self, make_config,
    ):
        """Email-originator with synthetic thread hash and NULL delivery token.

        The synthetic conversation_token isn't a real Talk room, so we must
        resolve via the user's alerts/briefing/DM rather than misroute.
        """
        synthetic = "deadbeef12345678"  # 16 lowercase hex
        config = make_config()
        config.email = _email_config()
        config.users = {
            "stefan": UserConfig(
                email_addresses=["stefan@test.com"],
                alerts_channel="stefan_alerts",
            ),
        }

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Plan",
                conversation_token=synthetic,
                talk_delivery_token=None,  # pre-migration row
            )

        envelope, email = self._inbound()
        task_ids = self._poll(config, envelope, email)
        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.talk_delivery_token == "stefan_alerts"
        # conversation_token preserved as the synthetic email-thread key
        assert task.conversation_token == synthetic

    def test_briefing_originator_null_delivery_token_uses_briefing_room(
        self, make_config,
    ):
        """Briefing-originated email: conversation_token IS the briefing room."""
        from istota.config import BriefingConfig as BriefConf
        config = make_config()
        config.email = _email_config()
        config.users = {
            "stefan": UserConfig(
                email_addresses=["stefan@test.com"],
                # alerts_channel deliberately empty so resolve_conversation_token
                # would pick the briefing — same value as the sent_email's
                # conversation_token. To prove we use the sent_email path
                # rather than resolve, set alerts to a different value.
                alerts_channel="other_alerts",
                briefings=[BriefConf(
                    name="morning", cron="0 8 * * *",
                    conversation_token="morning_briefing_room",
                )],
            ),
        }

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Briefing follow-up",
                conversation_token="morning_briefing_room",
                talk_delivery_token=None,
            )

        envelope, email = self._inbound()
        task_ids = self._poll(config, envelope, email)
        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        # Routes to the briefing room (which IS the original conversation_token),
        # not "other_alerts"
        assert task.talk_delivery_token == "morning_briefing_room"

    def test_null_conversation_and_delivery_falls_back_to_resolve(
        self, make_config,
    ):
        """Both NULL on sent_email — resolve via user config."""
        config = make_config()
        config.email = _email_config()
        config.users = {
            "stefan": UserConfig(
                email_addresses=["stefan@test.com"],
                alerts_channel="stefan_alerts",
            ),
        }

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Hello",
                conversation_token=None,
                talk_delivery_token=None,
            )

        envelope, email = self._inbound()
        task_ids = self._poll(config, envelope, email)
        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.talk_delivery_token == "stefan_alerts"

    def test_explicit_delivery_token_wins_over_conversation_token(
        self, make_config,
    ):
        """When sent_email has an explicit delivery token, it beats conversation_token."""
        config = make_config()
        config.email = _email_config()
        config.users = {
            "stefan": UserConfig(
                email_addresses=["stefan@test.com"],
                alerts_channel="WRONG_alerts",
            ),
        }

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Plan",
                conversation_token="some_other_room",
                talk_delivery_token="explicit_delivery_room",
            )

        envelope, email = self._inbound()
        task_ids = self._poll(config, envelope, email)
        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.talk_delivery_token == "explicit_delivery_room"

    def test_real_conversation_token_no_user_config_preserves_it(
        self, make_config,
    ):
        """No resolvable channel and a real-looking conversation_token: use it.

        Don't return None just because resolve_conversation_token can't help —
        the originating task already has a perfectly good Talk room recorded.
        """
        config = make_config()
        config.email = _email_config()
        # User exists for routing but has no alerts/briefing/DM
        config.users = {"stefan": UserConfig(email_addresses=["stefan@test.com"])}

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="stefan",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Plan",
                conversation_token="orig_room",
                talk_delivery_token=None,
            )

        envelope, email = self._inbound()
        task_ids = self._poll(config, envelope, email)
        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.talk_delivery_token == "orig_room"


# =============================================================================
# TestEmissaryRecordingShape
# =============================================================================


class TestEmissaryRecordingShape:
    """What gets written to sent_emails when each task type sends an email.

    The thread-match logic above only works if sent_emails rows record the
    right fields for each originator type. These tests pin that contract.
    """

    def _config(self, db_path, tmp_path):
        from istota.config import (
            EmailConfig as AppEmail, NextcloudConfig, SchedulerConfig, TalkConfig,
        )
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=AppEmail(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
        )

    def test_record_sent_email_for_talk_source_task(self, db_path, tmp_path):
        """Talk-source task -> sent_emails.conversation_token = real Talk room."""
        from istota.transport.email.outbound import _record_sent_email
        config = self._config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Send email please", user_id="alice",
                source_type="talk", conversation_token="real_talk_room",
                # talk_delivery_token NULL: talk-source tasks rely on the
                # _talk_target_for_delivery fallback to conversation_token.
                talk_delivery_token=None,
            )
            task = db.get_task(conn, task_id)

        _record_sent_email(
            config, task,
            message_id="<sent@bot.com>",
            to_addr="ext@x.com",
            subject="Hello",
        )

        with db.get_db(db_path) as conn:
            row = db.find_sent_email_by_message_id(conn, "<sent@bot.com>")
        assert row is not None
        assert row.conversation_token == "real_talk_room"
        # The known-NULL talk_delivery_token is the data shape that the
        # the inbound fix has to handle correctly on the read side.
        assert row.talk_delivery_token is None
        assert row.user_id == "alice"

    def test_record_sent_email_for_email_source_task(self, db_path, tmp_path):
        """Email-source task -> sent_emails.talk_delivery_token populated."""
        from istota.transport.email.outbound import _record_sent_email
        config = self._config(db_path, tmp_path)
        synthetic = "abcdef0123456789"

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Reply to that", user_id="alice",
                source_type="email", conversation_token=synthetic,
                talk_delivery_token="alerts_channel_xyz",
            )
            task = db.get_task(conn, task_id)

        _record_sent_email(
            config, task,
            message_id="<sent2@bot.com>",
            to_addr="ext@x.com",
            subject="Re: Plan",
        )

        with db.get_db(db_path) as conn:
            row = db.find_sent_email_by_message_id(conn, "<sent2@bot.com>")
        assert row is not None
        assert row.conversation_token == synthetic
        assert row.talk_delivery_token == "alerts_channel_xyz"

    def test_record_sent_email_for_subtask_inherits_parent_tokens(
        self, db_path, tmp_path,
    ):
        """Subtask sending email -> sent_emails carries parent's tokens."""
        from istota.transport.email.outbound import _record_sent_email
        config = self._config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="parent", user_id="alice",
                source_type="talk", conversation_token="parent_talk_room",
            )
            sub_id = db.create_task(
                conn, prompt="child", user_id="alice",
                source_type="subtask", parent_task_id=parent_id,
                conversation_token="parent_talk_room",
                talk_delivery_token="parent_talk_room",
            )
            sub = db.get_task(conn, sub_id)

        _record_sent_email(
            config, sub,
            message_id="<sub@bot.com>",
            to_addr="ext@x.com",
        )

        with db.get_db(db_path) as conn:
            row = db.find_sent_email_by_message_id(conn, "<sub@bot.com>")
        assert row is not None
        assert row.conversation_token == "parent_talk_room"
        assert row.talk_delivery_token == "parent_talk_room"


# =============================================================================
# TestEmissaryLifecycle — end-to-end outbound -> inbound
# =============================================================================


class TestEmissaryLifecycle:
    """Round-trip tests: a task sends an email; the reply comes in and routes."""

    def _inbound_for(self, message_id):
        envelope = _envelope(id="lc1", sender="ext@x.com", subject="Re: Plan")
        email = Email(
            id="lc1", subject="Re: Plan", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="The reply", attachments=[],
            message_id="<reply_lc1@x.com>", references=f"<{message_id}>",
            to=("bot@test.com",), cc=(),
        )
        return envelope, email

    def _scheduler_config(self, db_path, tmp_path, alerts="alerts_room"):
        from istota.config import (
            EmailConfig as AppEmail, NextcloudConfig, SchedulerConfig, TalkConfig,
        )
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=AppEmail(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
            users={"alice": UserConfig(
                email_addresses=["alice@test.com"],
                alerts_channel=alerts,
            )},
        )

    def _poller_config(self, db_path, tmp_path, alerts="alerts_room"):
        config = Config()
        config.db_path = db_path
        config.temp_dir = tmp_path / "temp"
        config.temp_dir.mkdir(exist_ok=True)
        config.skills_dir = tmp_path / "skills"
        config.skills_dir.mkdir(exist_ok=True)
        config.email = _email_config()
        config.users = {"alice": UserConfig(
            email_addresses=["alice@test.com"],
            alerts_channel=alerts,
        )}
        return config

    def test_talk_task_sends_email_reply_routes_to_original_room(
        self, db_path, tmp_path,
    ):
        """Full loop: talk task sends, external replies, routes to original room."""
        from istota.transport.email.outbound import _record_sent_email
        sched_cfg = self._scheduler_config(db_path, tmp_path, alerts="alerts_room")

        with db.get_db(db_path) as conn:
            tid = db.create_task(
                conn, prompt="send email", user_id="alice",
                source_type="talk", conversation_token="talkroom_42",
            )
            task = db.get_task(conn, tid)
        _record_sent_email(
            sched_cfg, task,
            message_id="<m_talk@bot.com>",
            to_addr="ext@x.com", subject="Plan",
        )

        # Inbound reply
        poll_cfg = self._poller_config(db_path, tmp_path, alerts="alerts_room")
        envelope, email = self._inbound_for("m_talk@bot.com")
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(poll_cfg)

        assert len(task_ids) == 1
        with db.get_db(db_path) as conn:
            new_task = db.get_task(conn, task_ids[0])
        assert new_task.user_id == "alice"
        assert new_task.conversation_token == "talkroom_42"
        # The reply routes back to the origin Talk room via the stored origin
        # descriptor (talk:<token>) rather than the talk_delivery_token ladder.
        assert new_task.output_target == "talk:talkroom_42,email"

    def test_email_task_sends_email_reply_routes_via_alerts(
        self, db_path, tmp_path,
    ):
        """Email-source originator: reply routes via the recorded delivery token."""
        from istota.transport.email.outbound import _record_sent_email
        sched_cfg = self._scheduler_config(db_path, tmp_path, alerts="alerts_room")
        synthetic = "0123456789abcdef"

        with db.get_db(db_path) as conn:
            tid = db.create_task(
                conn, prompt="reply", user_id="alice",
                source_type="email", conversation_token=synthetic,
                talk_delivery_token="alerts_room",
            )
            task = db.get_task(conn, tid)
        _record_sent_email(
            sched_cfg, task,
            message_id="<m_email@bot.com>",
            to_addr="ext@x.com", subject="Plan",
        )

        poll_cfg = self._poller_config(db_path, tmp_path, alerts="alerts_room")
        envelope, email = self._inbound_for("m_email@bot.com")
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(poll_cfg)

        assert len(task_ids) == 1
        with db.get_db(db_path) as conn:
            new_task = db.get_task(conn, task_ids[0])
        assert new_task.talk_delivery_token == "alerts_room"
        # conversation_token still preserves the original synthetic email-thread key
        assert new_task.conversation_token == synthetic

    def test_subtask_sends_email_reply_routes_to_parent_room(
        self, db_path, tmp_path,
    ):
        """Subtask of a talk task sends an email — reply must reach parent's room."""
        from istota.transport.email.outbound import _record_sent_email
        sched_cfg = self._scheduler_config(db_path, tmp_path, alerts="alerts_room")

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="parent", user_id="alice",
                source_type="talk", conversation_token="parent_room",
            )
            sub_id = db.create_task(
                conn, prompt="child", user_id="alice",
                source_type="subtask", parent_task_id=parent_id,
                conversation_token="parent_room",
                talk_delivery_token="parent_room",
            )
            sub = db.get_task(conn, sub_id)
        _record_sent_email(
            sched_cfg, sub,
            message_id="<m_sub@bot.com>",
            to_addr="ext@x.com", subject="Plan",
        )

        poll_cfg = self._poller_config(db_path, tmp_path)
        envelope, email = self._inbound_for("m_sub@bot.com")
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(poll_cfg)

        assert len(task_ids) == 1
        with db.get_db(db_path) as conn:
            new_task = db.get_task(conn, task_ids[0])
        # Reply reaches the parent's room via the origin descriptor (talk:<token>).
        assert new_task.conversation_token == "parent_room"
        assert new_task.output_target == "talk:parent_room,email"
