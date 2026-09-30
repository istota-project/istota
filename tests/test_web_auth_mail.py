"""Auth message content and the shared SMTP boundary."""

import html
from unittest.mock import patch

import pytest

from istota.config import Config


@pytest.mark.parametrize("builder,ttl,unit", [("build_enrol_email", 168, "hours"),
                                               ("build_reset_email", 1, "hour"),
                                               ("build_login_link_email", 15, "minutes")])
def test_bodies_carry_same_link_and_escape_html(builder, ttl, unit):
    from istota import web_auth_mail
    link = "https://bot.example.com/istota/auth/login-link?token=example&other=value"
    subject, plain, rich = getattr(web_auth_mail, builder)("Example bot", "<Alice>", link, ttl)
    assert "Example bot" in subject
    assert link in plain and html.escape(link, quote=True) in rich
    assert "<Alice>" in plain and "&lt;Alice&gt;" in rich
    assert f"{ttl} {unit}" in plain
    if builder == "build_login_link_email":
        assert "anyone" in plain.lower() and "ignore" in plain.lower()


def test_disabled_mail_returns_false_and_logs(caplog):
    from istota.web_auth_mail import send_auth_email
    config = Config()
    config.email.enabled = False
    with patch("istota.skills.email.send_email") as send:
        assert not send_auth_email(config, "alice@example.com", "Subject", "Plain", "HTML")
    send.assert_not_called()
    assert "not configured" in caplog.text and "alice@example.com" not in caplog.text


def test_send_uses_multipart_and_sanitizes_failures(caplog):
    from istota.web_auth_mail import send_auth_email
    config = Config()
    config.email.enabled = True
    config.email.bot_email = "bot@example.com"
    with patch("istota.skills.email.send_email") as send:
        assert send_auth_email(config, "alice@example.com", "Subject", "Plain", "HTML")
        assert send.call_args.kwargs["body"] == "Plain"
        assert send.call_args.kwargs["html_body"] == "HTML"
        assert send.call_args.kwargs["from_addr"] == config.email.bot_email
        send.side_effect = RuntimeError("private token or address")
        assert not send_auth_email(config, "alice@example.com", "Subject", "Plain", "HTML")
    assert "private token" not in caplog.text and "alice@example.com" not in caplog.text
