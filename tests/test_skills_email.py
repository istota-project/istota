"""Tests for skills/email.py module."""

import json
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from unittest.mock import MagicMock, call, patch

import pytest

from istota.skills.email import (
    Email,
    EmailConfig,
    EmailEnvelope,
    _config_from_env,
    _parse_email_date,
    _sanitize_header,
    cmd_output,
    cmd_send,
    list_emails,
    main,
    read_email,
    reply_to_email,
    send_email,
)


@pytest.fixture
def email_config():
    return EmailConfig(
        imap_host="imap.test.com",
        imap_port=993,
        imap_user="user@test.com",
        imap_password="secret",
        smtp_host="smtp.test.com",
        smtp_port=587,
        bot_email="bot@test.com",
    )


def _make_mock_mailbox(messages=None):
    """Create a mock MailBox context manager with optional messages."""
    mock_mb = MagicMock()
    mock_mb.__enter__ = MagicMock(return_value=mock_mb)
    mock_mb.__exit__ = MagicMock(return_value=False)
    if messages is not None:
        mock_mb.fetch.return_value = messages
    return mock_mb


def _make_mock_message(uid="123", subject="Test Subject", from_="alice@example.com",
                       date_str="Mon, 27 Jan 2025 12:00:00 +0000", flags=None,
                       text="Hello body", html="", attachments=None, headers=None):
    """Create a mock email message."""
    msg = MagicMock()
    msg.uid = uid
    msg.subject = subject
    msg.from_ = from_
    msg.date_str = date_str
    msg.flags = flags or []
    msg.text = text
    msg.html = html
    msg.attachments = attachments or []
    msg.headers = headers or {}
    return msg


# --- list_emails tests ---


class TestEmailOperations:
    @patch("istota.skills.email._get_mailbox")
    def test_list_emails(self, mock_get_mb, email_config):
        mock_msg = _make_mock_message()
        mock_mb = _make_mock_mailbox([mock_msg])
        mock_get_mb.return_value = mock_mb

        result = list_emails(config=email_config)

        assert len(result) == 1
        assert result[0].id == "123"
        assert result[0].subject == "Test Subject"
        assert result[0].sender == "alice@example.com"
        assert result[0].is_read is False
        mock_mb.login.assert_called_once_with("user@test.com", "secret")
        mock_mb.folder.set.assert_called_once_with("INBOX")

    @patch("istota.skills.email._get_mailbox")
    def test_list_emails_seen_flag(self, mock_get_mb, email_config):
        mock_msg = _make_mock_message(flags=["\\Seen"])
        mock_mb = _make_mock_mailbox([mock_msg])
        mock_get_mb.return_value = mock_mb

        result = list_emails(config=email_config)
        assert result[0].is_read is True

    @patch("istota.skills.email._get_mailbox")
    def test_read_email(self, mock_get_mb, email_config):
        mock_msg = _make_mock_message(
            uid="456",
            subject="Important",
            text="Email body content",
            headers={
                "message-id": ("<abc@example.com>",),
                "references": ("<ref1@example.com>",),
            },
        )
        mock_mb = _make_mock_mailbox([mock_msg])
        mock_get_mb.return_value = mock_mb

        result = read_email("456", config=email_config)

        assert isinstance(result, Email)
        assert result.id == "456"
        assert result.subject == "Important"
        assert result.body == "Email body content"
        assert result.message_id == "<abc@example.com>"
        assert result.references == "<ref1@example.com>"

    @patch("istota.skills.email._save_to_sent")
    @patch("istota.skills.email.smtplib.SMTP")
    def test_send_email(self, mock_smtp_class, mock_save, email_config):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        send_email(
            to="bob@example.com",
            subject="Hello",
            body="Test body",
            config=email_config,
        )

        mock_smtp_class.assert_called_once_with("smtp.test.com", 587)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("user@test.com", "secret")
        mock_server.send_message.assert_called_once()

        # Verify the message content
        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["To"] == "bob@example.com"
        assert sent_msg["Subject"] == "Hello"
        assert sent_msg["From"] == "bot@test.com"
        assert sent_msg["Message-ID"] is not None

    @patch("istota.skills.email._save_to_sent")
    @patch("istota.skills.email.smtplib.SMTP")
    def test_reply_with_threading(self, mock_smtp_class, mock_save, email_config):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        reply_to_email(
            to_addr="alice@example.com",
            subject="Meeting",
            body="Sure, I'll be there",
            config=email_config,
            in_reply_to="<orig123@example.com>",
            references="<ref1@example.com> <orig123@example.com>",
        )

        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["In-Reply-To"] == "<orig123@example.com>"
        assert sent_msg["References"] == "<ref1@example.com> <orig123@example.com>"
        assert sent_msg["Subject"] == "Re: Meeting"

    @patch("istota.skills.email._save_to_sent")
    @patch("istota.skills.email.smtplib.SMTP")
    def test_reply_already_has_re_prefix(self, mock_smtp_class, mock_save, email_config):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        reply_to_email(
            to_addr="alice@example.com",
            subject="Re: Meeting",
            body="Confirmed",
            config=email_config,
        )

        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["Subject"] == "Re: Meeting"
        # Should NOT double the Re: prefix
        assert not sent_msg["Subject"].startswith("Re: Re:")

    @patch("istota.skills.email._save_to_sent")
    @patch("istota.skills.email.smtplib.SMTP")
    def test_reply_uses_in_reply_to_as_references_fallback(
        self, mock_smtp_class, mock_save, email_config
    ):
        """When references is None but in_reply_to is set, use it as References."""
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        reply_to_email(
            to_addr="alice@example.com",
            subject="Topic",
            body="Reply",
            config=email_config,
            in_reply_to="<orig@example.com>",
            references=None,
        )

        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["References"] == "<orig@example.com>"

    def test_send_email_requires_config(self):
        with pytest.raises(ValueError, match="config is required"):
            send_email(to="x@y.com", subject="Hi", body="Test", config=None)

    def test_list_emails_requires_config(self):
        with pytest.raises(ValueError, match="config is required"):
            list_emails(config=None)


# --- _parse_email_date tests ---


class TestParseEmailDate:
    def test_rfc2822_format(self):
        result = _parse_email_date("Tue, 27 Jan 2026 11:19:17 +0000")
        assert result is not None
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 27

    def test_iso8601_format(self):
        result = _parse_email_date("2026-01-27 14:47+00:00")
        assert result is not None
        assert result.year == 2026
        assert result.hour == 14
        assert result.minute == 47

    def test_iso8601_with_timezone_offset(self):
        result = _parse_email_date("2026-01-26 08:17-08:00")
        assert result is not None
        assert result.year == 2026

    def test_invalid_date_returns_none(self):
        result = _parse_email_date("not a date at all")
        assert result is None

    def test_empty_string_returns_none(self):
        result = _parse_email_date("")
        assert result is None


# --- CLI tests ---


class TestConfigFromEnv:
    def test_builds_config_from_env(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.test.com")
        monkeypatch.setenv("SMTP_PORT", "465")
        monkeypatch.setenv("SMTP_USER", "sender@test.com")
        monkeypatch.setenv("SMTP_PASSWORD", "pass123")
        monkeypatch.setenv("SMTP_FROM", "bot@test.com")
        monkeypatch.setenv("IMAP_HOST", "imap.test.com")
        monkeypatch.setenv("IMAP_PORT", "993")
        monkeypatch.setenv("IMAP_USER", "imap@test.com")
        monkeypatch.setenv("IMAP_PASSWORD", "imappass")

        config = _config_from_env()

        assert config.smtp_host == "smtp.test.com"
        assert config.smtp_port == 465
        assert config.smtp_user == "sender@test.com"
        assert config.smtp_password == "pass123"
        assert config.bot_email == "bot@test.com"
        assert config.imap_host == "imap.test.com"
        assert config.imap_port == 993
        assert config.imap_user == "imap@test.com"
        assert config.imap_password == "imappass"

    def test_missing_smtp_host_raises(self, monkeypatch):
        monkeypatch.delenv("SMTP_HOST", raising=False)
        with pytest.raises(ValueError, match="SMTP_HOST"):
            _config_from_env()

    def test_defaults_for_optional_vars(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.test.com")
        # Clear everything else
        for var in ["SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM",
                     "IMAP_HOST", "IMAP_PORT", "IMAP_USER", "IMAP_PASSWORD"]:
            monkeypatch.delenv(var, raising=False)

        config = _config_from_env()
        assert config.smtp_port == 587
        assert config.imap_port == 993
        assert config.smtp_user == ""
        assert config.bot_email == ""


class TestCmdSend:
    @patch("istota.skills.email._config_from_env")
    @patch("istota.skills.email.send_email")
    def test_send_basic(self, mock_send, mock_config):
        mock_config.return_value = EmailConfig(
            imap_host="", imap_port=993, imap_user="", imap_password="",
            smtp_host="smtp.test.com", smtp_port=587, bot_email="bot@test.com",
        )
        args = MagicMock()
        args.to = "alice@example.com"
        args.subject = "Hello"
        args.body = "Test body"
        args.body_file = None
        args.html = False

        result = cmd_send(args)

        mock_send.assert_called_once_with(
            to="alice@example.com",
            subject="Hello",
            body="Test body",
            config=mock_config.return_value,
            content_type="plain",
        )
        assert result == {"status": "ok", "to": "alice@example.com", "subject": "Hello"}

    @patch("istota.skills.email._config_from_env")
    @patch("istota.skills.email.send_email")
    def test_send_html(self, mock_send, mock_config):
        mock_config.return_value = EmailConfig(
            imap_host="", imap_port=993, imap_user="", imap_password="",
            smtp_host="smtp.test.com", smtp_port=587,
        )
        args = MagicMock()
        args.to = "bob@example.com"
        args.subject = "Report"
        args.body = "<h1>Report</h1>"
        args.body_file = None
        args.html = True

        cmd_send(args)

        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["content_type"] == "html"

    @patch("istota.skills.email._config_from_env")
    @patch("istota.skills.email.send_email")
    def test_send_body_file(self, mock_send, mock_config, tmp_path):
        mock_config.return_value = EmailConfig(
            imap_host="", imap_port=993, imap_user="", imap_password="",
            smtp_host="smtp.test.com", smtp_port=587,
        )
        body_file = tmp_path / "body.html"
        body_file.write_text("<p>Hello from file</p>")

        args = MagicMock()
        args.to = "bob@example.com"
        args.subject = "File body"
        args.body = None
        args.body_file = str(body_file)
        args.html = True

        cmd_send(args)

        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["body"] == "<p>Hello from file</p>"

    @patch("istota.skills.email._config_from_env")
    def test_send_no_body_raises(self, mock_config):
        mock_config.return_value = EmailConfig(
            imap_host="", imap_port=993, imap_user="", imap_password="",
            smtp_host="smtp.test.com", smtp_port=587,
        )
        args = MagicMock()
        args.to = "bob@example.com"
        args.subject = "Empty"
        args.body = None
        args.body_file = None
        args.html = False

        with pytest.raises(ValueError, match="--body or --body-file"):
            cmd_send(args)


class TestEmailCLIMain:
    @patch("istota.skills.email._config_from_env")
    @patch("istota.skills.email.send_email")
    def test_main_send(self, mock_send, mock_config, capsys):
        mock_config.return_value = EmailConfig(
            imap_host="", imap_port=993, imap_user="", imap_password="",
            smtp_host="smtp.test.com", smtp_port=587,
        )

        main(["send", "--to", "alice@test.com", "--subject", "Hi", "--body", "Hello"])

        mock_send.assert_called_once()
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["to"] == "alice@test.com"

    @patch("istota.skills.email._config_from_env")
    @patch("istota.skills.email.send_email")
    def test_main_send_error(self, mock_send, mock_config, capsys):
        mock_config.return_value = EmailConfig(
            imap_host="", imap_port=993, imap_user="", imap_password="",
            smtp_host="smtp.test.com", smtp_port=587,
        )
        mock_send.side_effect = smtplib.SMTPAuthenticationError(535, b"Auth failed")

        with pytest.raises(SystemExit) as exc_info:
            main(["send", "--to", "alice@test.com", "--subject", "Hi", "--body", "Hello"])

        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "error"
        assert "Auth failed" in output["error"]

    def test_main_missing_command(self):
        with pytest.raises(SystemExit):
            main([])

    def test_main_output(self, tmp_path, capsys):
        deferred_dir = tmp_path / "deferred"
        deferred_dir.mkdir()
        env = {"ISTOTA_TASK_ID": "99", "ISTOTA_DEFERRED_DIR": str(deferred_dir)}
        with patch.dict("os.environ", env):
            main(["output", "--subject", "Test Subject", "--body", "Hello world"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"

        # Verify the deferred file was written correctly
        out_file = deferred_dir / "task_99_email_output.json"
        assert out_file.exists()
        data = json.loads(out_file.read_text())
        assert data["subject"] == "Test Subject"
        assert data["body"] == "Hello world"
        assert data["format"] == "plain"

    def test_main_output_html(self, tmp_path, capsys):
        deferred_dir = tmp_path / "deferred"
        deferred_dir.mkdir()
        env = {"ISTOTA_TASK_ID": "100", "ISTOTA_DEFERRED_DIR": str(deferred_dir)}
        with patch.dict("os.environ", env):
            main(["output", "--subject", "HTML", "--body", "<p>Hi</p>", "--html"])

        out_file = deferred_dir / "task_100_email_output.json"
        data = json.loads(out_file.read_text())
        assert data["format"] == "html"
        assert data["body"] == "<p>Hi</p>"

    def test_main_output_body_file(self, tmp_path, capsys):
        deferred_dir = tmp_path / "deferred"
        deferred_dir.mkdir()
        body_file = tmp_path / "body.txt"
        body_file.write_text("Body from file")
        env = {"ISTOTA_TASK_ID": "101", "ISTOTA_DEFERRED_DIR": str(deferred_dir)}
        with patch.dict("os.environ", env):
            main(["output", "--subject", "S", "--body-file", str(body_file)])

        out_file = deferred_dir / "task_101_email_output.json"
        data = json.loads(out_file.read_text())
        assert data["body"] == "Body from file"

    def test_main_output_no_subject(self, tmp_path, capsys):
        deferred_dir = tmp_path / "deferred"
        deferred_dir.mkdir()
        env = {"ISTOTA_TASK_ID": "102", "ISTOTA_DEFERRED_DIR": str(deferred_dir)}
        with patch.dict("os.environ", env):
            main(["output", "--body", "Reply body"])

        out_file = deferred_dir / "task_102_email_output.json"
        data = json.loads(out_file.read_text())
        assert data["subject"] is None
        assert data["body"] == "Reply body"

    def test_output_missing_env_vars(self, capsys):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SystemExit):
                main(["output", "--body", "test"])
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "error"


# --- _sanitize_header tests ---


class TestSanitizeHeader:
    def test_strips_newlines(self):
        assert _sanitize_header("Hello\nWorld") == "Hello World"

    def test_strips_carriage_returns(self):
        assert _sanitize_header("Hello\r\nWorld") == "Hello  World"

    def test_strips_leading_trailing_whitespace(self):
        assert _sanitize_header("  Hello  ") == "Hello"

    def test_passthrough_clean_string(self):
        assert _sanitize_header("Normal Subject") == "Normal Subject"

    def test_multiple_newlines(self):
        assert _sanitize_header("Line1\nLine2\nLine3") == "Line1 Line2 Line3"


class TestSendEmailSanitizesSubject:
    @patch("istota.skills.email._save_to_sent")
    @patch("istota.skills.email.smtplib.SMTP")
    def test_newlines_stripped_from_subject(self, mock_smtp_class, mock_save, email_config):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        send_email(
            to="bob@example.com",
            subject="Hello\nWorld",
            body="Test body",
            config=email_config,
        )

        sent_msg = mock_server.send_message.call_args[0][0]
        assert "\n" not in sent_msg["Subject"]
        assert sent_msg["Subject"] == "Hello World"

    @patch("istota.skills.email._save_to_sent")
    @patch("istota.skills.email.smtplib.SMTP")
    def test_reply_newlines_stripped_from_subject(self, mock_smtp_class, mock_save, email_config):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        reply_to_email(
            to_addr="alice@example.com",
            subject="Meeting\nNotes",
            body="Reply body",
            config=email_config,
        )

        sent_msg = mock_server.send_message.call_args[0][0]
        assert "\n" not in sent_msg["Subject"]
        assert sent_msg["Subject"] == "Re: Meeting Notes"

    @patch("istota.skills.email._save_to_sent")
    @patch("istota.skills.email.smtplib.SMTP")
    def test_reply_sanitizes_threading_headers(self, mock_smtp_class, mock_save, email_config):
        """Folded newlines in References/In-Reply-To from original email are stripped."""
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        # Simulate folded References header from original email
        folded_refs = "<msg1@example.com>\r\n <msg2@example.com>\r\n <msg3@example.com>"
        folded_reply_to = "<msg3@example.com>\r\n"

        reply_to_email(
            to_addr="alice@example.com",
            subject="Thread",
            body="Reply",
            config=email_config,
            in_reply_to=folded_reply_to,
            references=folded_refs,
        )

        sent_msg = mock_server.send_message.call_args[0][0]
        assert "\n" not in sent_msg["In-Reply-To"]
        assert "\r" not in sent_msg["In-Reply-To"]
        assert "\n" not in sent_msg["References"]
        assert "\r" not in sent_msg["References"]
        assert sent_msg["In-Reply-To"] == "<msg3@example.com>"
        # \r\n each become space, plus original space = 3 spaces between IDs
        assert sent_msg["References"] == "<msg1@example.com>   <msg2@example.com>   <msg3@example.com>"
