"""Tests for skills/email.py module."""

import json
import smtplib
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.email import (
    Email,
    EmailConfig,
    _config_from_env,
    _parse_email_date,
    _sanitize_header,
    _write_deferred_sent_email,
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

    @patch("istota.skills.email.AND", create=True)
    @patch("istota.skills.email._get_mailbox")
    def test_read_email(self, mock_get_mb, mock_and, email_config):
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
        mock_send.return_value = "<msg-id@test.com>"
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
        args.cc = None
        args.bcc = None
        args.attach = None
        args.reply_to = None

        result = cmd_send(args)

        mock_send.assert_called_once_with(
            to="alice@example.com",
            subject="Hello",
            body="Test body",
            config=mock_config.return_value,
            content_type="plain",
            cc=None,
            bcc=None,
            attachments=None,
            reply_to=None,
        )
        assert result == {
            "status": "ok", "message_id": "<msg-id@test.com>",
            "to": "alice@example.com", "cc": [],
            "subject": "Hello", "attachments": [],
        }

    @patch("istota.skills.email._config_from_env")
    @patch("istota.skills.email.send_email")
    def test_send_html(self, mock_send, mock_config):
        mock_send.return_value = "<msg-id@test.com>"
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
        mock_send.return_value = "<msg-id@test.com>"
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
        mock_send.return_value = "<msg-id@test.com>"
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


# --- _write_deferred_sent_email tests ---


class TestWriteDeferredSentEmail:
    def test_skips_when_env_vars_missing(self, tmp_path):
        with patch.dict("os.environ", {}, clear=True):
            _write_deferred_sent_email("<id@x>", "alice@example.com", "Hi")
        # Nothing should have been written anywhere
        assert list(tmp_path.iterdir()) == []

    def test_writes_entry(self, tmp_path):
        env = {
            "ISTOTA_TASK_ID": "42",
            "ISTOTA_DEFERRED_DIR": str(tmp_path),
            "ISTOTA_USER_ID": "alice",
            "ISTOTA_CONVERSATION_TOKEN": "tok123",
        }
        with patch.dict("os.environ", env, clear=True):
            _write_deferred_sent_email("<msg-1@test.com>", "bob@example.com", "Hello")

        out_file = tmp_path / "task_42_sent_emails.json"
        assert out_file.exists()
        data = json.loads(out_file.read_text())
        assert data == [
            {
                "message_id": "<msg-1@test.com>",
                "to_addr": "bob@example.com",
                "subject": "Hello",
                "conversation_token": "tok123",
                "user_id": "alice",
            }
        ]

    def test_appends_multiple_entries(self, tmp_path):
        env = {
            "ISTOTA_TASK_ID": "42",
            "ISTOTA_DEFERRED_DIR": str(tmp_path),
        }
        with patch.dict("os.environ", env, clear=True):
            _write_deferred_sent_email("<msg-1@x>", "a@example.com", "First")
            _write_deferred_sent_email("<msg-2@x>", "b@example.com", "Second")

        data = json.loads((tmp_path / "task_42_sent_emails.json").read_text())
        assert [e["message_id"] for e in data] == ["<msg-1@x>", "<msg-2@x>"]
        assert [e["to_addr"] for e in data] == ["a@example.com", "b@example.com"]

    def test_recovers_from_corrupt_existing_file(self, tmp_path):
        env = {
            "ISTOTA_TASK_ID": "42",
            "ISTOTA_DEFERRED_DIR": str(tmp_path),
        }
        (tmp_path / "task_42_sent_emails.json").write_text("not json")

        with patch.dict("os.environ", env, clear=True):
            _write_deferred_sent_email("<msg@x>", "a@example.com", "Subj")

        data = json.loads((tmp_path / "task_42_sent_emails.json").read_text())
        assert len(data) == 1
        assert data[0]["message_id"] == "<msg@x>"


class TestWriteSentEmailDirectFallback:
    """ISSUE-233: every other deferred-op writer falls back to a direct DB
    write when no deferred dir is configured. This one returned silently, so a
    send from an unsandboxed context recorded nothing and the correspondent's
    reply had no ``sent_emails`` row to thread back against."""

    @pytest.fixture
    def task_db(self, tmp_path):
        from istota import db

        path = tmp_path / "istota.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            task_id = db.create_task(
                conn,
                prompt="send the invoice",
                user_id="alice",
                source_type="scheduled",
                conversation_token="room7",
            )
        return path, task_id

    def _rows(self, path):
        from istota import db

        with db.get_db(path) as conn:
            return conn.execute(
                'SELECT user_id, message_id, to_addr, subject, task_id, '
                'conversation_token, origin_target FROM sent_emails'
            ).fetchall()

    def test_direct_write_when_no_deferred_dir(self, task_db):
        path, task_id = task_db
        env = {
            "ISTOTA_TASK_ID": str(task_id),
            "ISTOTA_DB_PATH": str(path),
            "ISTOTA_USER_ID": "alice",
        }
        with patch.dict("os.environ", env, clear=True):
            _write_deferred_sent_email("<msg@x>", "vendor@example.com", "Invoice")

        rows = self._rows(path)
        assert len(rows) == 1
        assert rows[0]["message_id"] == "<msg@x>"
        assert rows[0]["to_addr"] == "vendor@example.com"
        assert rows[0]["subject"] == "Invoice"

    def test_direct_write_carries_task_identity(self, task_db):
        """The row must be attributed the same way the deferred replay would —
        every identity field read off the task row, not off the env."""
        path, task_id = task_db
        env = {
            "ISTOTA_TASK_ID": str(task_id),
            "ISTOTA_DB_PATH": str(path),
            "ISTOTA_USER_ID": "alice",
        }
        with patch.dict("os.environ", env, clear=True):
            _write_deferred_sent_email("<msg@x>", "vendor@example.com", "Invoice")

        row = self._rows(path)[0]
        assert row["user_id"] == "alice"
        assert row["task_id"] == task_id
        assert row["conversation_token"] == "room7"
        assert row["origin_target"]

    def test_refuses_when_env_user_is_not_the_tasks_user(self, task_db):
        """The task row is the identity, but the env chose which row. A
        disagreement means the env is not describing this task, so the send
        must not be attributed to another user's conversation."""
        path, task_id = task_db
        env = {
            "ISTOTA_TASK_ID": str(task_id),
            "ISTOTA_DB_PATH": str(path),
            "ISTOTA_USER_ID": "mallory",
        }
        with patch.dict("os.environ", env, clear=True):
            _write_deferred_sent_email("<msg@x>", "vendor@example.com", "Invoice")
        assert self._rows(path) == []

    def test_reply_threads_back_against_the_direct_row(self, task_db):
        """The point of the row: an inbound reply quoting the Message-ID must
        resolve to the originating task."""
        from istota import db

        path, task_id = task_db
        env = {
            "ISTOTA_TASK_ID": str(task_id),
            "ISTOTA_DB_PATH": str(path),
            "ISTOTA_USER_ID": "alice",
        }
        with patch.dict("os.environ", env, clear=True):
            _write_deferred_sent_email("<msg@x>", "vendor@example.com", "Invoice")

        with db.get_db(path) as conn:
            found = db.find_sent_email_by_references(conn, ["<msg@x>"])
        assert found is not None
        assert found.task_id == task_id

    def test_deferred_dir_still_wins(self, task_db, tmp_path):
        """When a deferred dir is configured the file is the only writer — a
        direct write as well would double-record on replay."""
        path, task_id = task_db
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {
            "ISTOTA_TASK_ID": str(task_id),
            "ISTOTA_DEFERRED_DIR": str(deferred),
            "ISTOTA_DB_PATH": str(path),
            "ISTOTA_USER_ID": "alice",
        }
        with patch.dict("os.environ", env, clear=True):
            _write_deferred_sent_email("<msg@x>", "vendor@example.com", "Invoice")

        assert (deferred / f"task_{task_id}_sent_emails.json").exists()
        assert self._rows(path) == []

    def test_no_task_id_is_still_a_noop(self, task_db):
        """An ad-hoc CLI send outside any task has nothing to attribute the row
        to, so it stays untracked — unchanged behaviour."""
        path, _ = task_db
        with patch.dict("os.environ", {"ISTOTA_DB_PATH": str(path)}, clear=True):
            _write_deferred_sent_email("<msg@x>", "vendor@example.com", "Invoice")
        assert self._rows(path) == []

    def test_no_db_path_is_a_noop(self):
        with patch.dict("os.environ", {"ISTOTA_TASK_ID": "1"}, clear=True):
            _write_deferred_sent_email("<msg@x>", "vendor@example.com", "Invoice")

    def test_db_failure_does_not_raise(self, tmp_path):
        """The mail is already gone by the time this runs — a DB problem must
        not turn a delivered send into a failed task."""
        broken = tmp_path / "istota.db"
        broken.write_text("this is not a sqlite file")
        env = {
            "ISTOTA_TASK_ID": "1",
            "ISTOTA_DB_PATH": str(broken),
            "ISTOTA_USER_ID": "alice",
        }
        with patch.dict("os.environ", env, clear=True):
            _write_deferred_sent_email("<msg@x>", "vendor@example.com", "Invoice")

    def test_missing_db_file_is_not_created(self, tmp_path):
        """A path pointing at no file is a misconfiguration. Connecting would
        create an empty DB as a side effect of a function that only records."""
        missing = tmp_path / "istota.db"
        env = {
            "ISTOTA_TASK_ID": "1",
            "ISTOTA_DB_PATH": str(missing),
            "ISTOTA_USER_ID": "alice",
        }
        with patch.dict("os.environ", env, clear=True):
            _write_deferred_sent_email("<msg@x>", "vendor@example.com", "Invoice")
        assert not missing.exists()

    def test_non_numeric_task_id_does_not_raise(self, task_db):
        path, _ = task_db
        env = {
            "ISTOTA_TASK_ID": "not-a-number",
            "ISTOTA_DB_PATH": str(path),
            "ISTOTA_USER_ID": "alice",
        }
        with patch.dict("os.environ", env, clear=True):
            _write_deferred_sent_email("<msg@x>", "vendor@example.com", "Invoice")
        assert self._rows(path) == []

    def test_unknown_task_id_is_a_noop(self, task_db):
        """A task id with no row cannot be attributed; recording it under the
        env's user id would let an unsandboxed caller forge attribution."""
        path, _ = task_db
        env = {
            "ISTOTA_TASK_ID": "99999",
            "ISTOTA_DB_PATH": str(path),
            "ISTOTA_USER_ID": "alice",
        }
        with patch.dict("os.environ", env, clear=True):
            _write_deferred_sent_email("<msg@x>", "vendor@example.com", "Invoice")
        assert self._rows(path) == []


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


from istota.skills.email import AND as _AND


@pytest.mark.skipif(
    _AND is None,
    reason="imap_tools not installed (install with: uv sync --extra email)",
)
class TestDownloadAttachmentsSecurity:
    """Verify that attachment filenames are sanitized against path traversal."""

    def test_path_traversal_stripped(self, tmp_path, email_config):
        """Filenames with ../ components should have directory parts stripped."""
        from istota.skills.email import download_attachments

        mock_att = MagicMock()
        mock_att.filename = "../../etc/passwd"
        mock_att.payload = b"evil content"

        mock_msg = MagicMock()
        mock_msg.attachments = [mock_att]

        mock_mailbox = MagicMock()
        mock_mailbox.__enter__ = MagicMock(return_value=mock_mailbox)
        mock_mailbox.__exit__ = MagicMock(return_value=False)
        mock_mailbox.fetch.return_value = [mock_msg]

        with patch("istota.skills.email._get_mailbox", return_value=mock_mailbox):
            result = download_attachments("1", target_dir=tmp_path, config=email_config)

        # Should write as "passwd" in target_dir, not traverse
        assert len(result) == 1
        assert result[0].parent == tmp_path
        assert result[0].name == "passwd"
        assert not (tmp_path / ".." / ".." / "etc" / "passwd").exists()

    def test_absolute_path_stripped(self, tmp_path, email_config):
        from istota.skills.email import download_attachments

        mock_att = MagicMock()
        mock_att.filename = "/etc/shadow"
        mock_att.payload = b"evil"

        mock_msg = MagicMock()
        mock_msg.attachments = [mock_att]

        mock_mailbox = MagicMock()
        mock_mailbox.__enter__ = MagicMock(return_value=mock_mailbox)
        mock_mailbox.__exit__ = MagicMock(return_value=False)
        mock_mailbox.fetch.return_value = [mock_msg]

        with patch("istota.skills.email._get_mailbox", return_value=mock_mailbox):
            result = download_attachments("1", target_dir=tmp_path, config=email_config)

        assert len(result) == 1
        assert result[0].name == "shadow"
        assert result[0].parent == tmp_path

    def test_empty_filename_after_strip_skipped(self, tmp_path, email_config):
        from istota.skills.email import download_attachments

        mock_att = MagicMock()
        mock_att.filename = "../../"
        mock_att.payload = b"evil"

        mock_msg = MagicMock()
        mock_msg.attachments = [mock_att]

        mock_mailbox = MagicMock()
        mock_mailbox.__enter__ = MagicMock(return_value=mock_mailbox)
        mock_mailbox.__exit__ = MagicMock(return_value=False)
        mock_mailbox.fetch.return_value = [mock_msg]

        with patch("istota.skills.email._get_mailbox", return_value=mock_mailbox):
            result = download_attachments("1", target_dir=tmp_path, config=email_config)

        assert len(result) == 0
