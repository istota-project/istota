"""Auth message content and the shared SMTP boundary."""

import html
from unittest.mock import patch

import pytest

from istota.config import Config


@pytest.mark.parametrize("builder,ttl,unit", [("build_enrol_email", 168, "hours"),
                                               ("build_reset_email", 1, "hour")])
def test_bodies_carry_same_link_and_escape_html(builder, ttl, unit):
    from istota.webui import auth_mail as web_auth_mail
    link = "https://bot.example.com/istota/auth/set-password?token=example&other=value"
    subject, plain, rich = getattr(web_auth_mail, builder)("Example bot", "<Alice>", link, ttl)
    assert "Example bot" in subject
    assert link in plain and html.escape(link, quote=True) in rich
    assert "<Alice>" in plain and "&lt;Alice&gt;" in rich
    assert f"{ttl} {unit}" in plain


def test_sign_in_code_mail_carries_a_code_and_no_link():
    from istota.webui import auth_mail as web_auth_mail
    subject, plain, rich = web_auth_mail.build_sign_in_code_email("Example <bot>", "<Alice>", "042917", 10)
    # In the subject and alone on a line: what one-time-code autofill reads from Mail.
    assert subject == "042917 is your Example <bot> sign-in code"
    assert "\n042917\n" in plain and "042917" in rich
    assert "http" not in plain and "href" not in rich
    assert "10 minutes" in plain and "ignore" in plain.lower()
    assert "<Alice>" in plain and "&lt;Alice&gt;" in rich and "Example &lt;bot&gt;" in rich


def test_disabled_mail_returns_false_and_logs(caplog):
    from istota.webui.auth_mail import send_auth_email
    config = Config()
    config.email.enabled = False
    with patch("istota.skills.email.send_email") as send:
        assert not send_auth_email(config, "alice@example.com", "Subject", "Plain", "HTML")
    send.assert_not_called()
    assert "not configured" in caplog.text and "alice@example.com" not in caplog.text


def test_send_uses_multipart_and_sanitizes_failures(caplog):
    from istota.webui.auth_mail import send_auth_email
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
