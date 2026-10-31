"""`email.sender_flags`: the doctor says when a sender flag cannot be earned."""

from __future__ import annotations

import pytest

from istota import doctor
from istota.config import Config, EmailConfig, UserConfig


def _config(*, authserv_id="", threshold=3, users=None, enabled=True) -> Config:
    config = Config()
    config.email = EmailConfig(enabled=enabled, authserv_id=authserv_id)
    config.scheduler.email_sender_flag_hold_threshold = threshold
    config.users = users or {}
    return config


def _trusting(*entries: str) -> UserConfig:
    return UserConfig(trusted_email_senders=list(entries))


def _check(config: Config) -> doctor.CheckResult:
    return doctor.check_email_sender_flags(config, probe=False)


@pytest.mark.parametrize("kwargs", [
    {"enabled": False},
    {"threshold": 0},
    {"threshold": -1},
])
def test_it_says_inactive_with_email_or_holding_off(kwargs):
    result = _check(_config(users={"front": _trusting("*")}, **kwargs))

    assert result.status == doctor.OK
    assert "inactive" in result.detail


def test_it_is_ok_when_authserv_id_is_set():
    result = _check(_config(
        authserv_id="mx.example.com", users={"front": _trusting("*")},
    ))

    assert result.status == doctor.OK
    assert "inactive" not in result.detail


def test_it_warns_with_a_count_when_a_pattern_trusted_user_runs_unheld():
    result = _check(_config(users={
        "front": _trusting("*"),
        "desk": _trusting("*@corp.example", "partner@corp.example"),
        "named": _trusting("partner@corp.example"),
        "none": _trusting(),
    }))

    assert result.status == doctor.WARN
    assert "2 user(s)" in result.detail
    assert "authserv_id" in result.remedy
    for text in (result.detail, result.remedy):
        assert "corp.example" not in text
        assert "partner" not in text
        assert "front" not in text


@pytest.mark.parametrize("entry", ["p?rtner@corp.example", "[p]artner@corp.example"])
def test_any_glob_character_makes_an_entry_a_pattern(entry):
    result = _check(_config(users={"front": _trusting(entry)}))

    assert result.status == doctor.WARN


def test_without_authserv_id_named_addresses_alone_are_already_gated():
    result = _check(_config(users={"named": _trusting("partner@corp.example")}))

    assert result.status == doctor.OK
    assert "ignored" in result.detail
    assert "authserv_id" in result.detail
    assert "partner" not in result.detail


def test_a_blank_authserv_id_counts_as_unset():
    result = _check(_config(authserv_id="   ", users={"front": _trusting("*")}))

    assert result.status == doctor.WARN


def test_it_is_registered_for_the_deployment():
    assert ("email.sender_flags", doctor.check_email_sender_flags) in doctor.CHECKS
    assert _check(_config()).scope == doctor.DEPLOYMENT
