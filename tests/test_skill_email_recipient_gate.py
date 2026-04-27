"""Tests for the email skill's outbound recipient gate (Layer A).

Covers _is_recipient_allowed env-var matching, _defer_send file format, and
the cmd_send integration that uses both.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.email import (
    EmailConfig,
    _defer_send,
    _is_recipient_allowed,
    cmd_send,
)


class TestIsRecipientAllowed:
    def test_no_env_vars_fails_open(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_KNOWN_RECIPIENTS", raising=False)
        monkeypatch.delenv("ISTOTA_TRUSTED_RECIPIENT_PATTERNS", raising=False)
        assert _is_recipient_allowed("anyone@example.com") is True

    def test_known_address_match(self, monkeypatch):
        monkeypatch.setenv(
            "ISTOTA_KNOWN_RECIPIENTS",
            "alice@example.com\nbob@example.com\n",
        )
        monkeypatch.delenv("ISTOTA_TRUSTED_RECIPIENT_PATTERNS", raising=False)
        assert _is_recipient_allowed("alice@example.com") is True
        assert _is_recipient_allowed("BOB@example.com") is True  # case insensitive
        assert _is_recipient_allowed("eve@example.com") is False

    def test_pattern_match(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_KNOWN_RECIPIENTS", "")
        monkeypatch.setenv(
            "ISTOTA_TRUSTED_RECIPIENT_PATTERNS",
            "*@company.com\nsupport+*@vendor.io",
        )
        assert _is_recipient_allowed("anyone@company.com") is True
        assert _is_recipient_allowed("support+billing@vendor.io") is True
        assert _is_recipient_allowed("random@example.com") is False

    def test_empty_address_blocked(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_KNOWN_RECIPIENTS", "alice@example.com")
        assert _is_recipient_allowed("") is False
        assert _is_recipient_allowed("   ") is False

    def test_known_with_blank_lines(self, monkeypatch):
        monkeypatch.setenv(
            "ISTOTA_KNOWN_RECIPIENTS",
            "\n\nalice@example.com\n\nbob@example.com\n\n",
        )
        assert _is_recipient_allowed("alice@example.com") is True
        assert _is_recipient_allowed("eve@example.com") is False

    def test_only_patterns_set(self, monkeypatch):
        # Known list empty but patterns present → gate is active
        monkeypatch.setenv("ISTOTA_KNOWN_RECIPIENTS", "")
        monkeypatch.setenv("ISTOTA_TRUSTED_RECIPIENT_PATTERNS", "*@allowed.io")
        assert _is_recipient_allowed("a@allowed.io") is True
        assert _is_recipient_allowed("a@blocked.io") is False


class TestDeferSend:
    def test_writes_pending_send_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ISTOTA_TASK_ID", "42")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))

        result = _defer_send(
            "stranger@example.com", "Hi", "body text", "plain",
        )

        assert result["status"] == "pending_confirmation"
        assert result["to"] == "stranger@example.com"
        assert result["reason"] == "unknown_recipient"
        assert result["queued"] == 1

        path = tmp_path / "task_42_pending_send.json"
        data = json.loads(path.read_text())
        assert data == [{
            "to": "stranger@example.com",
            "subject": "Hi",
            "body": "body text",
            "content_type": "plain",
        }]

    def test_appends_multiple_sends(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ISTOTA_TASK_ID", "7")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))

        _defer_send("a@x.com", "S1", "B1", "plain")
        result = _defer_send("b@y.com", "S2", "B2", "html")

        assert result["queued"] == 2
        data = json.loads((tmp_path / "task_7_pending_send.json").read_text())
        assert len(data) == 2
        assert data[0]["to"] == "a@x.com"
        assert data[1]["to"] == "b@y.com"
        assert data[1]["content_type"] == "html"

    def test_raises_without_task_context(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        with pytest.raises(ValueError, match="ISTOTA_TASK_ID"):
            _defer_send("x@y.com", "s", "b", "plain")


class TestCmdSendGate:
    @patch("istota.skills.email._config_from_env")
    @patch("istota.skills.email.send_email")
    def test_known_recipient_sends(self, mock_send, mock_config, monkeypatch):
        mock_config.return_value = EmailConfig(
            imap_host="", imap_port=993, imap_user="", imap_password="",
            smtp_host="smtp.test.com", smtp_port=587,
        )
        monkeypatch.setenv("ISTOTA_KNOWN_RECIPIENTS", "alice@example.com")
        monkeypatch.setenv("ISTOTA_TRUSTED_RECIPIENT_PATTERNS", "")

        args = MagicMock()
        args.to = "alice@example.com"
        args.subject = "Hi"
        args.body = "Body"
        args.body_file = None
        args.html = False

        result = cmd_send(args)

        mock_send.assert_called_once()
        assert result["status"] == "ok"

    @patch("istota.skills.email._config_from_env")
    @patch("istota.skills.email.send_email")
    def test_unknown_recipient_defers(
        self, mock_send, mock_config, monkeypatch, tmp_path,
    ):
        mock_config.return_value = EmailConfig(
            imap_host="", imap_port=993, imap_user="", imap_password="",
            smtp_host="smtp.test.com", smtp_port=587,
        )
        monkeypatch.setenv("ISTOTA_KNOWN_RECIPIENTS", "alice@example.com")
        monkeypatch.setenv("ISTOTA_TRUSTED_RECIPIENT_PATTERNS", "")
        monkeypatch.setenv("ISTOTA_TASK_ID", "99")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))

        args = MagicMock()
        args.to = "stranger@elsewhere.com"
        args.subject = "Hi"
        args.body = "Body"
        args.body_file = None
        args.html = False

        result = cmd_send(args)

        mock_send.assert_not_called()
        assert result["status"] == "pending_confirmation"
        assert result["reason"] == "unknown_recipient"
        assert (tmp_path / "task_99_pending_send.json").exists()

    @patch("istota.skills.email._config_from_env")
    @patch("istota.skills.email.send_email")
    def test_pattern_match_sends(self, mock_send, mock_config, monkeypatch):
        mock_config.return_value = EmailConfig(
            imap_host="", imap_port=993, imap_user="", imap_password="",
            smtp_host="smtp.test.com", smtp_port=587,
        )
        monkeypatch.setenv("ISTOTA_KNOWN_RECIPIENTS", "")
        monkeypatch.setenv("ISTOTA_TRUSTED_RECIPIENT_PATTERNS", "*@trusted.org")

        args = MagicMock()
        args.to = "newperson@trusted.org"
        args.subject = "Hi"
        args.body = "Body"
        args.body_file = None
        args.html = False

        result = cmd_send(args)

        mock_send.assert_called_once()
        assert result["status"] == "ok"
