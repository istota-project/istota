"""`nextcloud.connection`: does the bot's account sign in, and as whom.

One test per row of the spec's status table, with `sign_in` and
`fetch_capabilities` stubbed at the `capabilities` module seam, which is where
the check reaches them. The severity rule is what most rows pin: a fault in
the configuration or the account FAILs, anything that may be an outage WARNs,
because only a FAIL alerts and the alert route runs through this Nextcloud.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from istota import doctor
from istota.config import Config, NextcloudConfig, TalkConfig
from istota.doctor import (
    CHECK_SCOPES,
    CHECKS,
    DEEP_CHECKS,
    DEPLOYMENT,
    FAIL,
    LIVE_CHECKS,
    OK,
    SKIP,
    WARN,
    check_nextcloud_connection,
)
from istota.nextcloud import capabilities
from istota.nextcloud._http import OcsError
from istota.nextcloud.capabilities import SignIn

NAME = "nextcloud.connection"
PASSWORD = "app-password-7f3c91d2e8"
SERVER_TEXT = "server-said-zq81-do-not-echo"


def _config(*, url="https://cloud.example.com", username="istota", talk=True,
            bot_username="istota") -> Config:
    return Config(
        nextcloud=NextcloudConfig(url=url, username=username, app_password=PASSWORD),
        talk=TalkConfig(enabled=talk, bot_username=bot_username),
    )


def _failed_exc(http_status=None, ocs_status=None, cause=None) -> OcsError:
    exc = OcsError(f"Could not reach Nextcloud: {SERVER_TEXT}", http_status, ocs_status,
                   "/cloud/user")
    if cause is not None:
        exc.__cause__ = cause
    return exc


def _failed(outcome, http_status=None, ocs_status=None, cause=None) -> SignIn:
    exc = _failed_exc(http_status, ocs_status, cause)
    return SignIn(outcome, {}, "", http_status, ocs_status, exc)


def _signed_in(account_id="istota", display_name="Istota", outcome="ok") -> SignIn:
    account = {"id": account_id, "display-name": display_name}
    return SignIn(outcome, account, account_id, None, None, None)


def _caps(version="31.0.5", talk=True) -> dict:
    caps = {"core": {}}
    if talk:
        caps["spreed"] = {"features": []}
    return {"version": {"string": version}, "capabilities": caps}


class _Seam:
    def __init__(self, monkeypatch, sign=None, caps=None, caps_exc=None):
        self.sign_calls = 0
        self.caps_calls = 0

        def fake_sign_in(config, *args, **kwargs):
            self.sign_calls += 1
            return sign

        def fake_caps(config, *args, **kwargs):
            self.caps_calls += 1
            if caps_exc is not None:
                raise caps_exc
            return caps

        monkeypatch.setattr(capabilities, "sign_in", fake_sign_in)
        monkeypatch.setattr(capabilities, "fetch_capabilities", fake_caps)


class TestRegistration:
    def test_it_is_registered_after_the_signaling_checks(self):
        names = [name for name, _ in CHECKS]
        assert NAME in names
        assert names.index(NAME) > names.index("talk.signaling_watchers")

    def test_it_is_deployment_scoped_and_neither_deep_nor_live(self):
        assert CHECK_SCOPES[NAME] == DEPLOYMENT
        assert NAME not in DEEP_CHECKS
        assert NAME not in LIVE_CHECKS


class TestPreconditions:
    def test_no_url_skips(self, monkeypatch):
        seam = _Seam(monkeypatch)
        result = check_nextcloud_connection(_config(url=""), probe=True)
        assert (result.status, result.detail) == (SKIP, "no [nextcloud] url configured")
        assert result.scope == DEPLOYMENT
        assert seam.sign_calls == 0

    def test_an_empty_username_fails(self, monkeypatch):
        seam = _Seam(monkeypatch)
        result = check_nextcloud_connection(_config(username=""), probe=True)
        assert result.status == FAIL
        assert result.detail == "[nextcloud] url is set but username is empty"
        assert result.remedy
        assert seam.sign_calls == 0

    @pytest.mark.parametrize("url", ["placeholder", "ftp://cloud.example.com",
                                     "https://", "https://cloud.example.com:99999"])
    def test_a_url_that_is_not_http_fails(self, monkeypatch, url):
        seam = _Seam(monkeypatch)
        result = check_nextcloud_connection(_config(url=url), probe=True)
        assert result.status == FAIL
        assert result.detail == "[nextcloud] url is not an http(s) URL"
        assert result.remedy
        assert seam.sign_calls == 0

    def test_a_url_with_userinfo_fails_without_showing_it(self, monkeypatch):
        seam = _Seam(monkeypatch)
        url = "https://istota:hunter2-secret@cloud.example.com"
        result = check_nextcloud_connection(_config(url=url), probe=True)
        assert result.status == FAIL
        assert result.detail == "[nextcloud] url contains credentials"
        assert "hunter2" not in result.detail + result.remedy
        assert seam.sign_calls == 0

    def test_probe_false_skips_and_calls_nothing(self, monkeypatch):
        seam = _Seam(monkeypatch, sign=_signed_in(), caps=_caps())
        result = check_nextcloud_connection(_config(), probe=False)
        assert result.status == SKIP
        assert result.detail == (
            "sign-in cannot be observed without a network request (probe disabled)"
        )
        assert (seam.sign_calls, seam.caps_calls) == (0, 0)


class TestFailedSignIn:
    """A failed sign-in never reaches the capabilities call: one request per run."""

    def _run(self, monkeypatch, sign):
        seam = _Seam(monkeypatch, sign=sign, caps=_caps())
        result = check_nextcloud_connection(_config(), probe=True)
        assert seam.caps_calls == 0
        assert result.remedy
        assert result.scope == DEPLOYMENT
        return result

    def test_unreachable_warns_with_the_reason(self, monkeypatch):
        cause = httpx.ConnectError("[Errno 61] Connection refused")
        result = self._run(monkeypatch, _failed("unreachable", cause=cause))
        assert result.status == WARN
        assert "cloud.example.com" in result.detail
        assert "refused" in result.detail

    def test_slow_warns(self, monkeypatch):
        result = self._run(monkeypatch, _failed("slow", cause=httpx.ReadTimeout("x")))
        assert result.status == WARN
        assert result.detail == (
            "cloud.example.com accepted the connection and did not answer within 10 seconds"
        )
        assert "brute-force" in result.remedy

    def test_rate_limited_warns(self, monkeypatch):
        result = self._run(monkeypatch, _failed("rate_limited", http_status=429))
        assert (result.status, result.detail) == (
            WARN, "cloud.example.com rate-limited the check (HTTP 429)",
        )

    def test_server_error_warns(self, monkeypatch):
        result = self._run(monkeypatch, _failed("server_error", http_status=503))
        assert (result.status, result.detail) == (WARN, "cloud.example.com answered HTTP 503")
        assert "maintenance" in result.remedy

    def test_auth_failed_fails(self, monkeypatch):
        result = self._run(monkeypatch, _failed("auth_failed", http_status=401))
        assert result.status == FAIL
        assert result.detail == "cloud.example.com did not accept the credentials for istota"
        assert "app password" in result.remedy

    def test_forbidden_fails(self, monkeypatch):
        result = self._run(monkeypatch, _failed("forbidden", http_status=403))
        assert (result.status, result.detail) == (
            FAIL, "cloud.example.com refused to show the account's details (HTTP 403)",
        )

    def test_not_found_fails(self, monkeypatch):
        result = self._run(monkeypatch, _failed("not_found", http_status=404))
        assert (result.status, result.detail) == (
            FAIL, "cloud.example.com has no OCS API at this address (HTTP 404)",
        )
        assert "/index.php" in result.remedy

    def test_an_unexpected_answer_fails_with_its_codes(self, monkeypatch):
        result = self._run(monkeypatch, _failed("error", http_status=418, ocs_status=102))
        assert result.status == FAIL
        assert result.detail == "cloud.example.com: unexpected answer (HTTP 418, OCS 102)"
        assert "nextcloud_connection_check" in result.remedy

    def test_no_account_id_fails(self, monkeypatch):
        result = self._run(monkeypatch, _signed_in(account_id="", outcome="error"))
        assert result.status == FAIL
        assert result.detail == "cloud.example.com returned no account id"

    def test_the_wrong_account_fails_naming_both(self, monkeypatch):
        result = self._run(
            monkeypatch, _signed_in(account_id="bot-id", outcome="wrong_account"),
        )
        assert result.status == FAIL
        assert result.detail == "the credentials sign in as bot-id, not istota"
        assert "[nextcloud] username" in result.remedy


class TestSignedIn:
    def test_ok(self, monkeypatch):
        seam = _Seam(monkeypatch, sign=_signed_in(), caps=_caps())
        result = check_nextcloud_connection(_config(), probe=True)
        assert (result.status, result.detail) == (
            OK, "signed in as istota (Istota) on Nextcloud 31.0.5",
        )
        assert (seam.sign_calls, seam.caps_calls) == (1, 1)

    def test_a_bot_username_mismatch_fails(self, monkeypatch):
        _Seam(monkeypatch, sign=_signed_in(), caps=_caps())
        result = check_nextcloud_connection(_config(bot_username="Istota"), probe=True)
        assert result.status == FAIL
        assert result.detail == "signed in as istota; [talk] bot_username is Istota"
        assert "[talk] bot_username" in result.remedy

    def test_an_empty_bot_username_fails_while_talk_is_on(self, monkeypatch):
        _Seam(monkeypatch, sign=_signed_in(), caps=_caps())
        result = check_nextcloud_connection(_config(bot_username=""), probe=True)
        assert result.status == FAIL

    def test_talk_not_installed_warns(self, monkeypatch):
        _Seam(monkeypatch, sign=_signed_in(), caps=_caps(talk=False))
        result = check_nextcloud_connection(_config(), probe=True)
        assert result.status == WARN
        assert result.detail == "signed in as istota; Talk is not installed on cloud.example.com"
        assert "[talk] enabled = false" in result.remedy

    def test_talk_not_installed_is_reported_before_a_bot_username_mismatch(self, monkeypatch):
        _Seam(monkeypatch, sign=_signed_in(), caps=_caps(talk=False))
        result = check_nextcloud_connection(_config(bot_username="Istota"), probe=True)
        assert result.status == WARN
        assert "Talk is not installed" in result.detail

    def test_a_mismatch_still_fails_when_capabilities_cannot_be_read(self, monkeypatch):
        _Seam(monkeypatch, sign=_signed_in(), caps_exc=_failed_exc(http_status=500))
        result = check_nextcloud_connection(_config(bot_username="Istota"), probe=True)
        assert result.status == FAIL
        assert "[talk] bot_username is Istota" in result.detail

    def test_talk_off_asks_neither_talk_question(self, monkeypatch):
        _Seam(monkeypatch, sign=_signed_in(), caps=_caps(talk=False))
        result = check_nextcloud_connection(
            _config(talk=False, bot_username="someone-else"), probe=True,
        )
        assert result.status == OK

    def test_capabilities_failing_warns(self, monkeypatch):
        _Seam(monkeypatch, sign=_signed_in(), caps_exc=_failed_exc(http_status=500))
        result = check_nextcloud_connection(_config(), probe=True)
        assert result.status == WARN
        assert result.detail == "signed in as istota; could not read the server's capabilities"
        assert "nextcloud_connection_check" in result.remedy


_EVERY_OUTCOME = [
    _failed("unreachable", cause=httpx.ConnectError(f"refused {SERVER_TEXT}")),
    _failed("slow", cause=httpx.ReadTimeout(SERVER_TEXT)),
    _failed("rate_limited", http_status=429),
    _failed("server_error", http_status=500),
    _failed("auth_failed", http_status=401, ocs_status=997),
    _failed("forbidden", http_status=403),
    _failed("not_found", http_status=404),
    _failed("error", http_status=418),
    _signed_in(account_id="", outcome="error"),
    _signed_in(account_id="other", outcome="wrong_account"),
]


class TestNothingLeaks:
    """No credential and no server text in any field, or in the log."""

    @pytest.mark.parametrize("sign", _EVERY_OUTCOME, ids=lambda s: s.outcome)
    def test_every_failed_outcome(self, monkeypatch, caplog, sign):
        _Seam(monkeypatch, sign=sign, caps=_caps())
        with caplog.at_level(logging.DEBUG, logger="istota.doctor"):
            result = check_nextcloud_connection(_config(), probe=True)
        rendered = " ".join([result.name, result.status, result.detail, result.remedy])
        for secret in (PASSWORD, SERVER_TEXT):
            assert secret not in rendered
            assert secret not in caplog.text

    def test_the_capabilities_failure(self, monkeypatch, caplog):
        _Seam(monkeypatch, sign=_signed_in(), caps_exc=_failed_exc(http_status=500))
        with caplog.at_level(logging.DEBUG, logger="istota.doctor"):
            result = check_nextcloud_connection(_config(), probe=True)
        assert SERVER_TEXT not in result.detail + result.remedy + caplog.text


class TestLogging:
    def test_a_failed_outcome_logs_one_warning_with_codes_and_class_names(
        self, monkeypatch, caplog,
    ):
        sign = _failed("unreachable", cause=httpx.ConnectError("refused"))
        _Seam(monkeypatch, sign=sign, caps=_caps())
        with caplog.at_level(logging.DEBUG, logger="istota.doctor"):
            check_nextcloud_connection(_config(), probe=True)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert message.startswith("nextcloud_connection_check outcome=unreachable")
        assert "http_status=None" in message
        assert "ocs_status=None" in message
        assert "OcsError" in message and "ConnectError" in message

    def test_ok_logs_no_warning(self, monkeypatch, caplog):
        _Seam(monkeypatch, sign=_signed_in(), caps=_caps())
        with caplog.at_level(logging.DEBUG, logger="istota.doctor"):
            check_nextcloud_connection(_config(), probe=True)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestRedactUserinfo:
    @pytest.mark.parametrize("remove, expected", [(False, "(unparseable)"), (True, "")])
    def test_a_password_that_ends_the_netloc_early_is_dropped_whole(self, remove, expected):
        url = f"https://bot:se#cret-{PASSWORD}@cloud.example.com"
        assert doctor._redact_userinfo(url, remove=remove) == expected

    def test_remove_keeps_the_address(self):
        url = f"https://bot:{PASSWORD}@cloud.example.com:8443/nc"
        assert doctor._redact_userinfo(url, remove=True) == "https://cloud.example.com:8443/nc"
