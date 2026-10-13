"""`email.sender_match`: the doctor names what the default sender check trusts."""

from __future__ import annotations

from istota import doctor
from istota.config import Config, EmailConfig


def _config(**email) -> Config:
    config = Config()
    config.email = EmailConfig(**email)
    return config


def test_it_skips_without_email():
    result = doctor.check_email_sender_match(_config(enabled=False), probe=False)
    assert result.status == doctor.SKIP


def test_it_warns_while_the_from_line_is_trusted():
    result = doctor.check_email_sender_match(_config(enabled=True), probe=False)

    assert result.status == doctor.WARN
    assert "confirm_sender_match" in result.detail
    assert "From:" in result.detail
    assert "authserv_id" in result.remedy
    assert "Authentication-Results" in result.remedy


def test_it_is_ok_when_the_mail_server_verdict_is_read():
    result = doctor.check_email_sender_match(
        _config(enabled=True, authserv_id="mx.example.com", confirm_sender_match="verify"),
        probe=False,
    )
    assert result.status == doctor.OK


def test_it_is_ok_when_every_self_claim_is_held():
    result = doctor.check_email_sender_match(
        _config(enabled=True, confirm_sender_match="gate"), probe=False,
    )
    assert result.status == doctor.OK


def test_it_is_registered():
    names = [name for name, _check in doctor.CHECKS]
    assert "email.sender_match" in names
