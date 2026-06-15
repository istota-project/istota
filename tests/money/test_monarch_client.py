"""Tests for the vendored Monarch client and monarch_api wrapper.

The point of this file is to pin the request shape — cookies, headers,
URL — that we now know is what the live API actually requires (verified
2026-05-15 against api.monarch.com). If Monarch changes auth again, the
probe + this file are how we'll catch the regression fast.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from istota.money._vendor.monarch_client import (
    APP_ORIGIN,
    APP_REFERER,
    GRAPHQL_URL,
    LOGIN_URL,
    MonarchAPIError,
    MonarchAuthError,
    MonarchCaptchaRequired,
    MonarchClient,
    MonarchClientOutdated,
    MonarchCloudflareBlocked,
    MonarchCookieAuth,
    MonarchMFARequired,
)
from istota.money.core.importers import monarch_api
from istota.money.core.models import (
    MonarchConfig,
    MonarchCredentials,
    MonarchSyncSettings,
    MonarchTagFilters,
)


# -----------------------------------------------------------------------------
# Test doubles for aiohttp.ClientSession
# -----------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_exc) -> None:
        return None


class _FakeSession:
    """Captures every call so tests can assert on cookies + headers."""

    last_call: dict | None = None
    _next_response: _FakeResponse | None = None
    _set_cookies_on_post: dict | None = None
    _captured_jar = None

    def __init__(self, *, cookies=None, cookie_jar=None, timeout=None) -> None:  # noqa: D401, ARG002
        type(self).last_call = {"cookies": dict(cookies) if cookies else {}}
        if cookie_jar is not None:
            type(self)._captured_jar = cookie_jar

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    def post(self, url, *, data, headers):
        type(self).last_call.update({
            "url": url, "data": data, "headers": dict(headers),
        })
        # Simulate Monarch setting cookies on the response. We do this by
        # injecting cookies into the captured jar via update_cookies(), which
        # is what aiohttp would call internally.
        if type(self)._set_cookies_on_post and type(self)._captured_jar is not None:
            type(self)._captured_jar.update_cookies(
                type(self)._set_cookies_on_post,
            )
        return type(self)._next_response


def _install_fake_session(
    monkeypatch, *, status=200, body=None, set_cookies=None,
):
    body = body if body is not None else json.dumps({"data": {}})
    _FakeSession.last_call = None
    _FakeSession._next_response = _FakeResponse(status, body)
    _FakeSession._set_cookies_on_post = set_cookies
    _FakeSession._captured_jar = None
    monkeypatch.setattr(
        "istota.money._vendor.monarch_client.aiohttp.ClientSession",
        _FakeSession,
    )


# -----------------------------------------------------------------------------
# MonarchClient — request shape
# -----------------------------------------------------------------------------


class TestRequestShape:
    """Pin the exact cookie + header set that survives Django CSRF."""

    @pytest.mark.asyncio
    async def test_get_transactions_sends_required_cookies_and_headers(
        self, monkeypatch,
    ):
        _install_fake_session(monkeypatch, body=json.dumps({
            "data": {"allTransactions": {"results": []}}
        }))
        client = MonarchClient(MonarchCookieAuth(
            session_id="SID-x", csrftoken="CSRF-y",
        ))

        await client.get_transactions(
            start_date="2026-04-01", end_date="2026-05-01",
        )

        call = _FakeSession.last_call
        assert call["url"] == GRAPHQL_URL
        # Cookies we discovered are required (and the only ones needed).
        assert call["cookies"] == {
            "session_id": "SID-x", "csrftoken": "CSRF-y",
        }
        # Headers Django CSRF middleware checks.
        assert call["headers"]["X-Csrftoken"] == "CSRF-y"
        assert call["headers"]["Origin"] == APP_ORIGIN
        assert call["headers"]["Referer"] == APP_REFERER
        assert call["headers"]["Content-Type"] == "application/json"
        # We deliberately do NOT send Authorization (cookies replace it).
        assert "Authorization" not in call["headers"]

    @pytest.mark.asyncio
    async def test_get_transactions_passes_date_range_and_paging(
        self, monkeypatch,
    ):
        _install_fake_session(monkeypatch, body=json.dumps({"data": {}}))
        client = MonarchClient(MonarchCookieAuth(
            session_id="s", csrftoken="c",
        ))

        await client.get_transactions(
            start_date="2026-01-01", end_date="2026-02-01",
            limit=250, offset=10,
        )

        body = json.loads(_FakeSession.last_call["data"])
        assert body["operationName"] == "GetTransactionsList"
        assert body["variables"]["limit"] == 250
        assert body["variables"]["offset"] == 10
        assert body["variables"]["filters"]["startDate"] == "2026-01-01"
        assert body["variables"]["filters"]["endDate"] == "2026-02-01"


# -----------------------------------------------------------------------------
# MonarchClient — error handling
# -----------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_403_csrf_failure_raises_auth_error(self, monkeypatch):
        _install_fake_session(
            monkeypatch, status=403,
            body='{"detail":"CSRF Failed: Referer checking failed - no Referer."}',
        )
        client = MonarchClient(MonarchCookieAuth(session_id="s", csrftoken="c"))

        with pytest.raises(MonarchAuthError) as exc:
            await client.whoami()
        # Operator should see the original Django message in the exception.
        assert "CSRF" in str(exc.value)

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self, monkeypatch):
        _install_fake_session(monkeypatch, status=401, body='{"detail":"unauth"}')
        client = MonarchClient(MonarchCookieAuth(session_id="s", csrftoken="c"))

        with pytest.raises(MonarchAuthError):
            await client.whoami()

    @pytest.mark.asyncio
    async def test_500_raises_api_error(self, monkeypatch):
        _install_fake_session(monkeypatch, status=500, body="server died")
        client = MonarchClient(MonarchCookieAuth(session_id="s", csrftoken="c"))

        with pytest.raises(MonarchAPIError):
            await client.whoami()

    @pytest.mark.asyncio
    async def test_graphql_errors_array_raises_api_error(self, monkeypatch):
        _install_fake_session(monkeypatch, body=json.dumps({
            "errors": [{"message": "field not found"}],
        }))
        client = MonarchClient(MonarchCookieAuth(session_id="s", csrftoken="c"))

        with pytest.raises(MonarchAPIError) as exc:
            await client.whoami()
        assert "field not found" in str(exc.value)

    def test_missing_cookie_creds_rejected_at_construction(self):
        with pytest.raises(MonarchAuthError):
            MonarchClient(MonarchCookieAuth(session_id="", csrftoken="c"))
        with pytest.raises(MonarchAuthError):
            MonarchClient(MonarchCookieAuth(session_id="s", csrftoken=""))


# -----------------------------------------------------------------------------
# monarch_api wrapper — credential surfacing + result shape
# -----------------------------------------------------------------------------


def _config_with(**cred_kwargs) -> MonarchConfig:
    return MonarchConfig(
        credentials=MonarchCredentials(**cred_kwargs),
        sync=MonarchSyncSettings(),
        accounts={},
        categories={},
        tags=MonarchTagFilters(),
    )


class TestLoginWithCredentials:
    """Login flow tests. We mock the HTTP layer; live verification lives in
    scripts/probe_monarch_login.py."""

    @pytest.mark.asyncio
    async def test_login_posts_expected_payload(self, monkeypatch):
        _install_fake_session(
            monkeypatch, body=json.dumps({"token": "x"}),
            set_cookies={"session_id": "SID-new", "csrftoken": "CSRF-new"},
        )

        out = await MonarchClient.login_with_credentials(
            email="alice@example.com", password="hunter2",
        )

        call = _FakeSession.last_call
        assert call["url"] == LOGIN_URL
        # Headers Django CSRF + the API expect on /auth/login/.
        assert call["headers"]["Origin"] == APP_ORIGIN
        assert call["headers"]["Referer"] == APP_REFERER
        # /auth/login/ rejects with "Please update to the latest version of
        # the app" if these are missing — verified live 2026-05-15.
        assert call["headers"]["monarch-client"]
        assert call["headers"]["monarch-client-version"]
        body = json.loads(call["data"])
        assert body == {
            "username": "alice@example.com",
            "password": "hunter2",
            "supports_mfa": True,
            "trusted_device": True,
        }
        # Returned cookie pair is what /graphql needs.
        assert isinstance(out, MonarchCookieAuth)
        assert out.session_id == "SID-new"
        assert out.csrftoken == "CSRF-new"

    @pytest.mark.asyncio
    async def test_login_with_mfa_includes_totp(self, monkeypatch):
        _install_fake_session(
            monkeypatch, body=json.dumps({"token": "x"}),
            set_cookies={"session_id": "s", "csrftoken": "c"},
        )

        await MonarchClient.login_with_credentials(
            email="a@b.com", password="pw", mfa_totp="123456",
        )
        body = json.loads(_FakeSession.last_call["data"])
        assert body["totp"] == "123456"

    @pytest.mark.asyncio
    async def test_mfa_required_distinguished_from_generic_403(self, monkeypatch):
        _install_fake_session(
            monkeypatch, status=403,
            body=json.dumps({"detail": "MFA token required",
                             "error_code": "REQUIRES_MFA"}),
        )

        with pytest.raises(MonarchMFARequired):
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )

    @pytest.mark.asyncio
    async def test_generic_403_is_auth_error(self, monkeypatch):
        _install_fake_session(
            monkeypatch, status=403, body=json.dumps({"detail": "wrong password"}),
        )

        with pytest.raises(MonarchAuthError):
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )

    @pytest.mark.asyncio
    async def test_outdated_client_distinguished(self, monkeypatch):
        """The 'Please update to the latest version' 403 is the live response
        when monarch-client* headers are missing or malformed (verified
        2026-05-15). It surfaces as MonarchClientOutdated so operators know
        to bump CLIENT_VERSION rather than checking the password."""
        _install_fake_session(
            monkeypatch, status=403,
            body=json.dumps({
                "detail": "Please update to the latest version of the app to continue login.",
            }),
        )

        with pytest.raises(MonarchClientOutdated) as exc:
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )
        assert "CLIENT_VERSION" in str(exc.value)

    @pytest.mark.asyncio
    async def test_captcha_required_distinguished(self, monkeypatch):
        """Monarch's bot-protection gate: 429 with error_code CAPTCHA_REQUIRED.
        Verified live 2026-05-15 against stefan@cynium.com after the account
        was flagged. Once tripped, programmatic login is permanently dead for
        that (account, IP) pair — UI must route the user to cookie-paste.
        """
        _install_fake_session(
            monkeypatch, status=429,
            body=json.dumps({
                "detail": "CAPTCHA is required to proceed.",
                "error_code": "CAPTCHA_REQUIRED",
            }),
        )

        with pytest.raises(MonarchCaptchaRequired) as exc:
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )
        assert "cookie-paste" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_404_treated_as_auth_error(self, monkeypatch):
        """Monarch returns 404 (not 401) for 'Invalid email and password
        combination'. Verified live 2026-05-15."""
        _install_fake_session(
            monkeypatch, status=404,
            body=json.dumps({"detail": "Invalid email and password combination"}),
        )

        with pytest.raises(MonarchAuthError) as exc:
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )
        assert "Invalid email and password" in str(exc.value)

    @pytest.mark.asyncio
    async def test_cloudflare_block_distinguished(self, monkeypatch):
        # Cloudflare's classic challenge HTML.
        cf_body = (
            "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
            "<body>Cloudflare attention required (Ray ID: abc123)</body></html>"
        )
        _install_fake_session(monkeypatch, status=403, body=cf_body)

        with pytest.raises(MonarchCloudflareBlocked) as exc:
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )
        # Error message must point operators at the browser fallback.
        msg = str(exc.value).lower()
        assert "cloudflare" in msg
        assert "paste browser cookies" in msg

    @pytest.mark.asyncio
    async def test_login_2xx_but_no_cookies_raises(self, monkeypatch):
        _install_fake_session(
            monkeypatch, body=json.dumps({"token": "x"}),
            set_cookies=None,  # no cookies set
        )

        with pytest.raises(MonarchAuthError) as exc:
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )
        assert "no session cookies" in str(exc.value)

    @pytest.mark.asyncio
    async def test_missing_email_or_password_rejected_early(self):
        with pytest.raises(MonarchAuthError):
            await MonarchClient.login_with_credentials(email="", password="x")
        with pytest.raises(MonarchAuthError):
            await MonarchClient.login_with_credentials(email="a@b", password="")


class TestFetchMonarchTransactions:
    @pytest.mark.asyncio
    async def test_missing_cookies_raises_actionable_error(self):
        """The error message must point operators at the cookie-paste workflow."""
        config = _config_with()  # no creds at all

        with pytest.raises(ValueError) as exc:
            await monarch_api.fetch_monarch_transactions(config, lookback_days=30)
        msg = str(exc.value)
        assert "session_id" in msg
        assert "csrftoken" in msg
        assert "DevTools" in msg

    @pytest.mark.asyncio
    async def test_returns_results_array(self, monkeypatch):
        _install_fake_session(monkeypatch, body=json.dumps({
            "data": {"allTransactions": {
                "results": [{"id": "t-1", "amount": 1.0}],
            }},
        }))
        config = _config_with(session_id="s", csrftoken="c")

        result = await monarch_api.fetch_monarch_transactions(config, lookback_days=7)
        assert result == [{"id": "t-1", "amount": 1.0}]


class TestFetchTransactionsByIds:
    @pytest.mark.asyncio
    async def test_filters_to_requested_ids(self, monkeypatch):
        _install_fake_session(monkeypatch, body=json.dumps({
            "data": {"allTransactions": {"results": [
                {"id": "a"}, {"id": "b"}, {"id": "c"},
            ]}},
        }))
        config = _config_with(session_id="s", csrftoken="c")

        out = await monarch_api.fetch_transactions_by_ids(config, ["a", "c", "z"])
        assert set(out.keys()) == {"a", "c"}


class TestDebugMonarchCommand:
    """Wiring test for the debug-monarch CLI subcommand. Exercises config
    resolution → vendored client → JSON envelope shape (which heartbeat
    checks will parse).

    Monarch config lives in the per-user money DB; we seed it and inject the
    resolved Context the istota way (the standalone config path is gone).
    """

    def _run(
        self, tmp_path, *, session_id="SID-x", csrftoken="CSRF-y",
        with_creds=True,
    ):
        import tomllib

        from click.testing import CliRunner
        from istota.money import config_store
        from istota.money.cli import Context, UserContext, cli

        db_path = tmp_path / "money.db"
        config_store.init_db(db_path)
        toml_text = (
            "[monarch]\n\n"
            "[monarch.sync]\nlookback_days = 7\n\n"
            '[monarch.profiles.default]\nledger = "default"\n'
        )
        config_store.save_monarch(
            db_path,
            config_store.monarch_config_from_toml_dict(tomllib.loads(toml_text)),
            replace_collections=True,
        )
        obj = Context()
        obj.users["u"] = UserContext(data_dir=tmp_path, ledgers=[], db_path=db_path)
        obj.activate_user("u")
        # Monarch cookies live in the encrypted secrets table (cookie-pair auth),
        # not in the DB config — supplied here via the resolved secrets overlay.
        if with_creds:
            obj.secrets = {"monarch": {"session_id": session_id, "csrftoken": csrftoken}}
        return CliRunner().invoke(cli, ["-u", "u", "debug-monarch"], obj=obj)

    def test_returns_ok_envelope_on_success(self, monkeypatch, tmp_path):
        _install_fake_session(monkeypatch, body=json.dumps({
            "data": {"me": {"id": "u-1", "email": "stefan@example.com"}},
        }))

        result = self._run(tmp_path)
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope == {
            "status": "ok", "auth_ok": True,
            "who": {"id": "u-1", "email": "stefan@example.com"},
        }

    def test_returns_error_envelope_on_403(self, monkeypatch, tmp_path):
        _install_fake_session(
            monkeypatch, status=403,
            body='{"detail":"CSRF Failed: Referer checking failed - no Referer."}',
        )

        result = self._run(tmp_path)
        envelope = json.loads(result.output)
        assert envelope["status"] == "error"
        assert envelope["auth_ok"] is False
        assert "CSRF" in envelope["error"]

    def test_returns_error_envelope_when_creds_missing(self, tmp_path):
        result = self._run(tmp_path, with_creds=False)
        envelope = json.loads(result.output)
        assert envelope["status"] == "error"
        assert envelope["auth_ok"] is False
        assert "session_id" in envelope["error"]
