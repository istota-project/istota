"""`mail.connect_verified` and `mail.account_isolation`."""

from __future__ import annotations

import ssl
from unittest.mock import patch

from istota import doctor
from istota.config import Config, UserConfig


def _config(*, users: int = 1, enabled: bool = True) -> Config:
    config = Config()
    config.mail_accounts.enabled = enabled
    config.users = {f"u{i}": UserConfig() for i in range(users)}
    return config


class TestConnectVerified:
    def test_it_is_ok_on_the_shipped_context(self):
        result = doctor.check_mail_connect_verified(Config(), probe=False)
        assert result.status == doctor.OK
        assert result.scope == doctor.IMAGE

    def test_it_fails_on_a_context_that_does_not_verify(self):
        unverified = ssl.create_default_context()
        unverified.check_hostname = False
        unverified.verify_mode = ssl.CERT_NONE
        with patch("istota.mail.connect.tls_context", return_value=unverified):
            result = doctor.check_mail_connect_verified(Config(), probe=False)
        assert result.status == doctor.FAIL
        assert result.remedy

    def test_it_is_registered_as_an_image_check(self):
        assert ("mail.connect_verified", doctor.check_mail_connect_verified) in doctor.CHECKS
        assert doctor.CHECK_SCOPES["mail.connect_verified"] == doctor.IMAGE


class TestAccountIsolation:
    def test_it_skips_when_accounts_are_off(self):
        result = doctor.check_mail_account_isolation(_config(users=3, enabled=False), probe=False)
        assert result.status == doctor.SKIP

    def test_one_user_is_ok_without_asking_about_the_sandbox(self):
        with patch("istota.doctor._deployment_sandboxing") as verdict:
            result = doctor.check_mail_account_isolation(_config(users=1), probe=False)
        assert result.status == doctor.OK
        verdict.assert_not_called()

    def test_it_is_ok_where_the_sandbox_holds(self):
        with patch("istota.doctor._deployment_sandboxing", return_value=(True, "")):
            result = doctor.check_mail_account_isolation(_config(users=2), probe=False)
        assert result.status == doctor.OK

    def test_it_warns_without_a_sandbox(self):
        with patch("istota.doctor._deployment_sandboxing", return_value=(False, "")):
            result = doctor.check_mail_account_isolation(_config(users=2), probe=False)
        assert result.status == doctor.WARN
        assert "mail_accounts" in result.remedy

    def test_it_warns_when_the_verdict_is_unknown(self):
        with patch("istota.doctor._deployment_sandboxing", return_value=(None, "no probe")):
            result = doctor.check_mail_account_isolation(_config(users=2), probe=False)
        assert result.status == doctor.WARN
        assert "no probe" in result.detail

    def test_it_is_registered_as_a_deployment_check(self):
        assert doctor.CHECK_SCOPES["mail.account_isolation"] == doctor.DEPLOYMENT
