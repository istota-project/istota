"""Tests for the vendored Monarch client and monarch_api wrapper.

The point of this file is to pin the request shape — cookies, headers,
URL — that we now know is what the live API actually requires (verified
2026-05-15 against api.monarch.com). If Monarch changes auth again, the
probe + this file are how we'll catch the regression fast.
"""

from __future__ import annotations

import json

import pytest

from istota.money._vendor import monarch_client
from istota.money._vendor.monarch_client import (
    APP_ORIGIN,
    APP_REFERER,
    CLIENT_PLATFORM,
    CLIENT_VERSION,
    CSRF_URL,
    GRAPHQL_URL,
    LEGACY_LOGIN_URL,
    WEB_LOGIN_URL,
    GRAPHQL_CLIENT_NAME,
    REST_CLIENT_NAME,
    USER_AGENT,
    VERSION_URL,
    fetch_live_client_version,
    MonarchAPIError,
    MonarchAuthError,
    MonarchCaptchaRequired,
    MonarchClient,
    MonarchClientOutdated,
    MonarchCloudflareBlocked,
    MonarchCookieAuth,
    MonarchEmailOTPRequired,
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
    """Captures every call so tests can assert on cookies + headers.

    ``post_calls`` / ``get_calls`` accumulate across a test so the retry path
    (login → version discovery → login again) can be asserted turn by turn;
    ``last_call`` keeps the single-call shape the older tests read.
    """

    last_call: dict | None = None
    post_calls: list[dict] = []
    get_calls: list[dict] = []
    _next_response: _FakeResponse | None = None
    _post_responses: list[_FakeResponse] | None = None
    _get_responses: list[_FakeResponse] | None = None
    _default_get: _FakeResponse | None = None
    _set_cookies_on_post: dict | None = None
    _set_cookies_on_get: dict | None = None
    _csrf_response: _FakeResponse | None = None
    csrf_get_calls: list[dict] = []
    _cookies_after_post: int = 0
    _captured_jar = None

    session_kwargs: list[dict] = []

    def __init__(self, *, cookies=None, cookie_jar=None, timeout=None, **kwargs) -> None:  # noqa: D401, ARG002
        type(self).last_call = {"cookies": dict(cookies) if cookies else {}}
        type(self).session_kwargs.append(dict(kwargs))
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
        type(self).post_calls.append(dict(type(self).last_call))
        # Simulate Monarch setting cookies on the response. We do this by
        # injecting cookies into the captured jar via update_cookies(), which
        # is what aiohttp would call internally. `_cookies_after_post` lets a
        # retry test set them only on the attempt that is meant to succeed.
        should_set = (
            type(self)._set_cookies_on_post
            and len(type(self).post_calls) > type(self)._cookies_after_post
        )
        if should_set and type(self)._captured_jar is not None:
            type(self)._captured_jar.update_cookies(
                type(self)._set_cookies_on_post,
            )
        return type(self)._pop(
            type(self)._post_responses, type(self)._next_response,
        )

    def get(self, url, *, headers=None):
        call = {"url": url, "headers": dict(headers or {})}
        # The CSRF bootstrap is routed by URL rather than sharing the version
        # queue: a login now issues *two* unrelated GETs, and letting the
        # bootstrap consume a response queued for version discovery would make
        # the discovery tests assert on the wrong request.
        if url == CSRF_URL:
            type(self).csrf_get_calls.append(call)
            # The bootstrap learns its token from a Set-Cookie, so the fake
            # seeds the jar the same way the POST does.
            if (
                type(self)._set_cookies_on_get
                and type(self)._captured_jar is not None
            ):
                type(self)._captured_jar.update_cookies(
                    type(self)._set_cookies_on_get,
                )
            return type(self)._csrf_response or _FakeResponse(404, "")

        type(self).get_calls.append(call)
        return type(self)._pop(
            type(self)._get_responses, type(self)._default_get,
        )

    @classmethod
    def _pop(cls, queue, default):
        """Pop one queued response, or fall back to this method's default.

        A queue is consumed strictly: over-consumption raises rather than
        replaying the last element forever. Without that, a regression which
        retried twice (or fetched the version twice) would quietly receive a
        fresh response and pass — the "exactly once" tests would be asserting
        nothing.
        """
        if queue is None:
            return default
        if not queue:
            raise AssertionError(
                "fake session over-consumed: more requests than queued responses"
            )
        return queue.pop(0)


def _reset_fake_session():
    _FakeSession.last_call = None
    _FakeSession.post_calls = []
    _FakeSession.get_calls = []
    _FakeSession.session_kwargs = []
    _FakeSession._next_response = None
    _FakeSession._post_responses = None
    _FakeSession._get_responses = None
    _FakeSession._default_get = None
    _FakeSession._set_cookies_on_post = None
    _FakeSession._set_cookies_on_get = None
    _FakeSession._csrf_response = None
    _FakeSession.csrf_get_calls = []
    _FakeSession._cookies_after_post = 0
    _FakeSession._captured_jar = None


def _install_fake_session(
    monkeypatch, *, status=200, body=None, set_cookies=None,
    post_responses=None, get_responses=None, cookies_after_post=0,
    set_cookies_on_get=None,
):
    body = body if body is not None else json.dumps({"data": {}})
    _reset_fake_session()
    _FakeSession._next_response = _FakeResponse(status, body)
    _FakeSession._post_responses = [
        _FakeResponse(s, b) for s, b in (post_responses or [])
    ] or None
    _FakeSession._get_responses = [
        _FakeResponse(s, b) for s, b in (get_responses or [])
    ] or None
    # Login now resolves the client version before it posts, so every login
    # test issues a discovery GET. Unless a test says otherwise, that GET fails
    # cleanly — the login then proceeds on the compiled-in CLIENT_VERSION,
    # which is the shape the pre-existing login tests were written against.
    _FakeSession._default_get = _FakeResponse(404, "")
    _FakeSession._set_cookies_on_post = set_cookies
    _FakeSession._set_cookies_on_get = set_cookies_on_get
    # A bootstrap only yields a token when the test seeds one; otherwise it
    # 404s, which is the "proceed without the header" path.
    _FakeSession._csrf_response = (
        _FakeResponse(200, "{}") if set_cookies_on_get else _FakeResponse(404, "")
    )
    _FakeSession._cookies_after_post = cookies_after_post
    monkeypatch.setattr(
        "istota.money._vendor.monarch_client.aiohttp.ClientSession",
        _FakeSession,
    )


@pytest.fixture(autouse=True)
def _clear_discovered_version():
    """The discovered client version is a process-global cache; clear it around
    every test so ordering can't leak one test's discovery into another."""
    monarch_client._discovered_version = None
    yield
    monarch_client._discovered_version = None


_OUTDATED_BODY = json.dumps({
    "detail": "Please update to the latest version of the app to continue login.",
})


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
        # Pin the client pair on the GraphQL path too. It is believed to be
        # ignored here, but that belief was established by probing with a value
        # this diff changed — and "a header we thought was ignored" is exactly
        # what broke login. A silent revert should fail a test.
        assert call["headers"]["monarch-client"] == GRAPHQL_CLIENT_NAME
        assert call["headers"]["monarch-client-version"] == CLIENT_VERSION
        assert call["headers"]["User-Agent"] == USER_AGENT
        # Monarch's oversized CSP header would otherwise abort the read.
        assert _FakeSession.session_kwargs[0]["max_field_size"] > 8190
        assert _FakeSession.session_kwargs[0]["max_line_size"] > 8190

    @pytest.mark.asyncio
    async def test_graphql_uses_a_discovered_version_when_one_is_known(
        self, monkeypatch,
    ):
        """`_current_client_version()` feeds both transports, so a version
        learned during login must show up on the sync path too."""
        monarch_client._discovered_version = "v1.0.4242"
        _install_fake_session(monkeypatch, body=json.dumps({"data": {}}))
        client = MonarchClient(MonarchCookieAuth(session_id="s", csrftoken="c"))

        await client.get_transactions(
            start_date="2026-04-01", end_date="2026-05-01",
        )

        assert _FakeSession.last_call["headers"][
            "monarch-client-version"] == "v1.0.4242"

    @pytest.mark.asyncio
    async def test_login_session_raises_header_size_ceiling(self, monkeypatch):
        _install_fake_session(
            monkeypatch, body=json.dumps({"token": "x"}),
            set_cookies={"session_id": "s", "csrftoken": "c"},
        )

        await MonarchClient.login_with_credentials(email="a@b.com", password="pw")

        # Every session the module opens, not just the discovery GET.
        assert all(
            kw["max_field_size"] > 8190 and kw["max_line_size"] > 8190
            for kw in _FakeSession.session_kwargs
        )

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
        assert call["url"] == WEB_LOGIN_URL
        # Headers Django CSRF + the API expect on the login route.
        assert call["headers"]["Origin"] == APP_ORIGIN
        assert call["headers"]["Referer"] == APP_REFERER
        # The login route rejects with "Please update to the latest version of
        # the app" if these are missing — verified live 2026-05-15.
        assert call["headers"]["monarch-client"]
        assert call["headers"]["monarch-client-version"]
        body = json.loads(call["data"])
        assert body == {
            "username": "alice@example.com",
            "password": "hunter2",
            "supports_mfa": True,
            "supports_email_otp": True,
            "supports_recaptcha": True,
            "web_stay_signed_in": True,
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
    async def test_login_sends_live_web_app_client_headers(self, monkeypatch):
        """The header values must be the ones the live web app sends. Monarch
        checks them *after* validating credentials, so a wrong value can only
        be caught by a real login — which is how 2025.10.0 went stale
        unnoticed (ISSUE: 503 on correct credentials, 2026-07-30)."""
        _install_fake_session(
            monkeypatch, body=json.dumps({"token": "x"}),
            set_cookies={"session_id": "s", "csrftoken": "c"},
        )

        await MonarchClient.login_with_credentials(
            email="a@b.com", password="pw",
        )

        headers = _FakeSession.last_call["headers"]
        assert headers["monarch-client"] == REST_CLIENT_NAME
        assert headers["monarch-client-version"] == CLIENT_VERSION


class TestWebLoginEndpoint:
    """The endpoint + header set the live web app actually uses.

    Monarch moved browser login from the token endpoint ``/auth/login/`` to the
    cookie endpoint ``/auth/web/login/``. The old route still exists and still
    validates credentials, but now refuses a *web* client afterwards with
    "Please update to the latest version of the app" — regardless of the
    version sent. That message is what made this look like a stale-version bug
    for a second time: on 2026-07-30 prod sent ``v1.0.3698``, byte-identical to
    the value the live bundle hardcodes, and was still refused (ISSUE: Monarch
    503 on correct credentials).

    Everything pinned here was read off a real browser request capture.
    """

    @pytest.mark.asyncio
    async def test_login_posts_to_the_web_cookie_endpoint(self, monkeypatch):
        """The token endpoint cannot serve this flow: it returns a bearer
        token, while every downstream call needs the session cookie pair."""
        _install_fake_session(
            monkeypatch, body=json.dumps({"id": "u1"}),
            set_cookies={"session_id": "s", "csrftoken": "c"},
        )

        await MonarchClient.login_with_credentials(
            email="a@b.com", password="pw",
        )

        assert _FakeSession.last_call["url"] == WEB_LOGIN_URL
        assert _FakeSession.last_call["url"] != LEGACY_LOGIN_URL

    @pytest.mark.asyncio
    async def test_login_sends_platform_and_device_headers(self, monkeypatch):
        _install_fake_session(
            monkeypatch, body=json.dumps({"id": "u1"}),
            set_cookies={"session_id": "s", "csrftoken": "c"},
        )

        await MonarchClient.login_with_credentials(
            email="a@b.com", password="pw",
        )

        headers = _FakeSession.last_call["headers"]
        assert headers["Client-Platform"] == CLIENT_PLATFORM
        assert headers["Device-UUID"]

    @pytest.mark.asyncio
    async def test_login_bootstraps_csrf_and_echoes_it(self, monkeypatch):
        """Django's double-submit check compares the header against the
        cookie, so the token has to be fetched first and echoed back."""
        _install_fake_session(
            monkeypatch, body=json.dumps({"id": "u1"}),
            set_cookies={"session_id": "s", "csrftoken": "c"},
            set_cookies_on_get={"csrftoken": "CSRF-BOOT"},
        )

        await MonarchClient.login_with_credentials(
            email="a@b.com", password="pw",
        )

        assert len(_FakeSession.csrf_get_calls) == 1
        assert _FakeSession.last_call["headers"]["X-CSRFToken"] == "CSRF-BOOT"

    @pytest.mark.asyncio
    async def test_login_proceeds_when_csrf_bootstrap_fails(self, monkeypatch):
        """Verified live: the endpoint reaches credential validation without a
        CSRF token, so a failed bootstrap must not block the attempt —
        degrading to no header beats refusing to try."""
        _install_fake_session(
            monkeypatch, body=json.dumps({"id": "u1"}),
            set_cookies={"session_id": "s", "csrftoken": "c"},
        )

        out = await MonarchClient.login_with_credentials(
            email="a@b.com", password="pw",
        )

        assert "X-CSRFToken" not in _FakeSession.last_call["headers"]
        assert out.session_id == "s"

    @pytest.mark.asyncio
    async def test_login_payload_matches_the_web_app(self, monkeypatch):
        """The app advertises the challenge types it can handle; omitting them
        lets Monarch pick one we cannot answer."""
        _install_fake_session(
            monkeypatch, body=json.dumps({"id": "u1"}),
            set_cookies={"session_id": "s", "csrftoken": "c"},
        )

        await MonarchClient.login_with_credentials(
            email="alice@example.com", password="hunter2",
        )

        assert json.loads(_FakeSession.last_call["data"]) == {
            "username": "alice@example.com",
            "password": "hunter2",
            "supports_mfa": True,
            "supports_email_otp": True,
            "supports_recaptcha": True,
            "web_stay_signed_in": True,
        }

    @pytest.mark.asyncio
    async def test_email_otp_challenge_is_distinct_from_mfa(self, monkeypatch):
        """Live-confirmed 2026-07-30: with a device Monarch doesn't know, a
        *correct* password comes back as this. Reporting it as an auth error
        tells the user their password is wrong when it isn't."""
        _install_fake_session(
            monkeypatch, status=403,
            body=json.dumps({
                "detail": "Retrieve the code from your email to continue login.",
                "error_code": "EMAIL_OTP_REQUIRED",
            }),
        )

        with pytest.raises(MonarchEmailOTPRequired):
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )

    @pytest.mark.asyncio
    async def test_email_otp_is_sent_in_its_own_field(self, monkeypatch):
        """`email_otp`, not `totp` — Monarch validates them separately, so
        putting an emailed code in the TOTP field fails as a bad code."""
        _install_fake_session(
            monkeypatch, body=json.dumps({"id": "u1"}),
            set_cookies={"session_id": "s", "csrftoken": "c"},
        )

        await MonarchClient.login_with_credentials(
            email="a@b.com", password="pw", email_otp="820512",
        )

        body = json.loads(_FakeSession.last_call["data"])
        assert body["email_otp"] == "820512"
        assert "totp" not in body

    @pytest.mark.asyncio
    async def test_email_otp_challenge_does_not_burn_a_version_retry(
        self, monkeypatch,
    ):
        """The challenge must not be mistaken for a client rejection: retrying
        would spend a second attempt against a rate-limited endpoint and, worse,
        invalidate the code the user is in the middle of typing."""
        _install_fake_session(
            monkeypatch, status=403,
            body=json.dumps({
                "detail": "Retrieve the code from your email to continue login.",
                "error_code": "EMAIL_OTP_REQUIRED",
            }),
        )

        with pytest.raises(MonarchEmailOTPRequired):
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )

        assert len(_FakeSession.post_calls) == 1

    def test_device_uuid_is_stable_per_account(self):
        """Monarch treats the device id as an identity. A value that changed
        every call would present each login as a brand-new device, which is
        what escalates a login into an MFA challenge."""
        first = monarch_client.device_uuid_for("alice@example.com")
        assert first == monarch_client.device_uuid_for("  Alice@Example.com  ")
        assert first != monarch_client.device_uuid_for("bob@example.com")
        assert len(first) == 36


class TestClientVersionDiscovery:
    """`app.monarch.com/version.json` is a 23-byte manifest carrying exactly
    the `clientVersion` the app bundle sends. It is what lets a stale constant
    self-heal instead of returning 503 until an operator ships a bump."""

    @pytest.mark.asyncio
    async def test_fetch_reads_version_manifest(self, monkeypatch):
        _install_fake_session(
            monkeypatch,
            get_responses=[(200, json.dumps({"version": "v1.0.9999"}))],
        )

        assert await fetch_live_client_version() == "v1.0.9999"
        assert _FakeSession.get_calls[0]["url"] == VERSION_URL

    @pytest.mark.asyncio
    async def test_fetch_sends_explicit_user_agent(self, monkeypatch):
        """Cloudflare fronts app.monarch.com and 403s aiohttp's default
        `Python/3.x aiohttp/3.y` UA, so an unset User-Agent makes discovery
        fail from a host where curl succeeds (verified live 2026-07-30).
        Without this the whole self-heal is silently dead."""
        _install_fake_session(
            monkeypatch,
            get_responses=[(200, json.dumps({"version": "v1.0.9999"}))],
        )

        await fetch_live_client_version()

        assert _FakeSession.get_calls[0]["headers"]["User-Agent"] == USER_AGENT
        assert "aiohttp" not in USER_AGENT.lower()
        assert "python" not in USER_AGENT.lower()

    @pytest.mark.asyncio
    async def test_fetch_raises_aiohttp_header_size_ceiling(self, monkeypatch):
        """app.monarch.com's CSP header exceeds aiohttp's 8190-byte default,
        which aborts the read as a bogus 400 before the body is parsed
        (verified live 2026-07-30). A response header's size must not decide
        whether discovery works."""
        _install_fake_session(
            monkeypatch,
            get_responses=[(200, json.dumps({"version": "v1.0.9999"}))],
        )

        await fetch_live_client_version()

        kwargs = _FakeSession.session_kwargs[0]
        assert kwargs["max_field_size"] > 8190
        assert kwargs["max_line_size"] > 8190

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status,body", [
        (500, json.dumps({"version": "v1.0.1"})),
        (200, "not json at all"),
        (200, json.dumps({})),
        (200, json.dumps({"version": ""})),
        (200, json.dumps({"version": "   "})),
        (200, json.dumps({"version": 17})),
        (200, json.dumps({"version": "v1.0.1" + "x" * 200})),
        # Valid JSON that isn't an object: json.loads returns a list/str/int,
        # and a bare .get() on it would raise AttributeError from inside the
        # error path, surfacing as a 500 instead of the actionable 503.
        (200, "[]"),
        (200, "null"),
        (200, "123"),
        (200, '"just a string"'),
    ])
    async def test_fetch_returns_none_on_unusable_payload(
        self, monkeypatch, status, body,
    ):
        """Discovery must never raise — a failure just means we keep the
        compiled-in fallback."""
        _install_fake_session(monkeypatch, get_responses=[(status, body)])

        assert await fetch_live_client_version() is None

    @pytest.mark.asyncio
    async def test_fetch_rejects_header_unsafe_version(self, monkeypatch):
        """The discovered value goes straight into an HTTP request header, so
        a remote payload carrying CR/LF must be refused rather than smuggled
        into the request as extra headers."""
        _install_fake_session(
            monkeypatch,
            get_responses=[(200, json.dumps({
                "version": "v1.0.1\r\nX-Injected: yes",
            }))],
        )

        assert await fetch_live_client_version() is None

    @pytest.mark.asyncio
    async def test_fetch_returns_the_value_it_validated(self, monkeypatch):
        """The regex runs against `version.strip()`, so the stripped value is
        what must be returned. Returning the raw one would hand aiohttp a
        header value with a trailing newline that was never checked."""
        _install_fake_session(
            monkeypatch,
            get_responses=[(200, json.dumps({"version": " v1.0.1\r\n "}))],
        )

        assert await fetch_live_client_version() == "v1.0.1"

    @pytest.mark.asyncio
    async def test_fetch_returns_none_on_transport_error(self, monkeypatch):
        _reset_fake_session()

        class _Boom:
            def __init__(self, **_kw):
                raise OSError("network down")

        monkeypatch.setattr(
            "istota.money._vendor.monarch_client.aiohttp.ClientSession", _Boom,
        )

        assert await fetch_live_client_version() is None


class TestOutdatedClientRecovery:
    @pytest.mark.asyncio
    async def test_version_is_resolved_before_the_login_is_attempted(
        self, monkeypatch,
    ):
        """The headline behaviour: discovery happens *first*, so a cold process
        with a stale constant costs one 23-byte GET rather than a failed
        credential submission against a CAPTCHA-gated endpoint."""
        _install_fake_session(
            monkeypatch, body=json.dumps({"token": "x"}),
            get_responses=[(200, json.dumps({"version": "v1.0.4242"}))],
            set_cookies={"session_id": "s", "csrftoken": "c"},
        )

        await MonarchClient.login_with_credentials(email="a@b.com", password="pw")

        assert len(_FakeSession.post_calls) == 1  # no wasted attempt
        assert _FakeSession.post_calls[0]["headers"][
            "monarch-client-version"] == "v1.0.4242"

    @pytest.mark.asyncio
    async def test_login_falls_back_to_constant_when_discovery_fails(
        self, monkeypatch,
    ):
        """An unreachable manifest must not block login — the constant is a
        worse guess, not a blocker."""
        _install_fake_session(
            monkeypatch, body=json.dumps({"token": "x"}),
            get_responses=[(503, "<html>down</html>")],
            set_cookies={"session_id": "s", "csrftoken": "c"},
        )

        await MonarchClient.login_with_credentials(email="a@b.com", password="pw")

        assert _FakeSession.post_calls[0]["headers"][
            "monarch-client-version"] == CLIENT_VERSION

    @pytest.mark.asyncio
    async def test_outdated_client_retries_with_refreshed_version(
        self, monkeypatch,
    ):
        """If the pre-fetched version is refused anyway (it went stale between
        the fetch and the post, or the manifest was down), re-read and retry
        once."""
        _install_fake_session(
            monkeypatch,
            post_responses=[
                (403, _OUTDATED_BODY),
                (200, json.dumps({"token": "x"})),
            ],
            get_responses=[
                (200, json.dumps({"version": "v1.0.1000"})),
                (200, json.dumps({"version": "v1.0.4242"})),
            ],
            set_cookies={"session_id": "SID-new", "csrftoken": "CSRF-new"},
            cookies_after_post=1,  # only the retry sets cookies
        )

        out = await MonarchClient.login_with_credentials(
            email="a@b.com", password="pw",
        )

        assert len(_FakeSession.post_calls) == 2
        assert _FakeSession.post_calls[0]["headers"][
            "monarch-client-version"] == "v1.0.1000"
        assert _FakeSession.post_calls[1]["headers"][
            "monarch-client-version"] == "v1.0.4242"
        assert out.session_id == "SID-new"
        assert out.csrftoken == "CSRF-new"

    @pytest.mark.asyncio
    async def test_credentials_are_resent_verbatim_on_retry(self, monkeypatch):
        _install_fake_session(
            monkeypatch,
            post_responses=[
                (403, _OUTDATED_BODY),
                (200, json.dumps({"token": "x"})),
            ],
            get_responses=[
                (200, json.dumps({"version": "v1.0.1000"})),
                (200, json.dumps({"version": "v1.0.4242"})),
            ],
            set_cookies={"session_id": "s", "csrftoken": "c"},
            cookies_after_post=1,
        )

        await MonarchClient.login_with_credentials(
            email="a@b.com", password="pw", mfa_totp="123456",
        )

        first, second = (json.loads(c["data"]) for c in _FakeSession.post_calls)
        assert first == second
        assert second["totp"] == "123456"

    @pytest.mark.asyncio
    async def test_spent_totp_on_retry_asks_for_a_new_code(self, monkeypatch):
        """The first attempt was judged on its client version, so it had
        already validated — and consumed — the one-time code. Reporting the
        retry's rejection as bad credentials would send an MFA user off to
        re-check a password that is fine."""
        _install_fake_session(
            monkeypatch,
            post_responses=[
                (403, _OUTDATED_BODY),
                (403, json.dumps({"detail": "Invalid TOTP"})),
            ],
            get_responses=[
                (200, json.dumps({"version": "v1.0.1000"})),
                (200, json.dumps({"version": "v1.0.4242"})),
            ],
        )

        with pytest.raises(MonarchMFARequired) as exc:
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw", mfa_totp="123456",
            )
        assert "new code" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_retry_auth_failure_without_mfa_stays_an_auth_error(
        self, monkeypatch,
    ):
        """Only the MFA case is reinterpreted — a passwordless-account failure
        must still read as a credential rejection."""
        _install_fake_session(
            monkeypatch,
            post_responses=[
                (403, _OUTDATED_BODY),
                (404, json.dumps({"detail": "Invalid email and password"})),
            ],
            get_responses=[
                (200, json.dumps({"version": "v1.0.1000"})),
                (200, json.dumps({"version": "v1.0.4242"})),
            ],
        )

        with pytest.raises(MonarchAuthError):
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )

    @pytest.mark.asyncio
    async def test_resolved_version_is_reused_by_later_logins(
        self, monkeypatch,
    ):
        """Cached process-wide — a second login must not refetch."""
        _install_fake_session(
            monkeypatch, body=json.dumps({"token": "x"}),
            get_responses=[(200, json.dumps({"version": "v1.0.4242"}))],
            set_cookies={"session_id": "s", "csrftoken": "c"},
        )
        await MonarchClient.login_with_credentials(email="a@b.com", password="pw")

        _install_fake_session(
            monkeypatch, body=json.dumps({"token": "x"}),
            set_cookies={"session_id": "s2", "csrftoken": "c2"},
        )
        await MonarchClient.login_with_credentials(email="a@b.com", password="pw")

        assert len(_FakeSession.post_calls) == 1
        assert _FakeSession.get_calls == []
        assert _FakeSession.post_calls[0]["headers"][
            "monarch-client-version"] == "v1.0.4242"

    @pytest.mark.asyncio
    async def test_no_retry_when_refreshed_version_matches_sent(
        self, monkeypatch,
    ):
        """If the re-read agrees with what was just refused — including the
        case where the manifest is down and we keep the same value — resending
        identical bytes would only burn a second attempt."""
        _install_fake_session(
            monkeypatch, status=403, body=_OUTDATED_BODY,
            get_responses=[
                (200, json.dumps({"version": "v1.0.4242"})),
                (200, json.dumps({"version": "v1.0.4242"})),
            ],
        )

        with pytest.raises(MonarchClientOutdated) as exc:
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )
        assert len(_FakeSession.post_calls) == 1
        assert "v1.0.4242" in str(exc.value)

    @pytest.mark.asyncio
    async def test_outdated_with_unreachable_manifest_does_not_retry(
        self, monkeypatch,
    ):
        _install_fake_session(
            monkeypatch, status=403, body=_OUTDATED_BODY,
            get_responses=[(503, "<html>nope</html>"), (503, "<html>nope</html>")],
        )

        with pytest.raises(MonarchClientOutdated) as exc:
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )
        assert len(_FakeSession.post_calls) == 1
        assert VERSION_URL in str(exc.value)

    @pytest.mark.asyncio
    async def test_retry_that_is_still_outdated_gives_up_but_keeps_version(
        self, monkeypatch,
    ):
        """Exactly one retry, never a loop. The refreshed value is *kept*:
        reverting to the constant would make the next call repeat this whole
        two-attempt sequence, doubling the failed-login rate against a gate the
        module documents as sticky. Keeping it means the next call
        short-circuits after one attempt."""
        _install_fake_session(
            monkeypatch,
            post_responses=[(403, _OUTDATED_BODY), (403, _OUTDATED_BODY)],
            get_responses=[
                (200, json.dumps({"version": "v1.0.1000"})),
                (200, json.dumps({"version": "v1.0.4242"})),
            ],
        )

        with pytest.raises(MonarchClientOutdated) as exc:
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )
        assert len(_FakeSession.post_calls) == 2
        assert "v1.0.4242" in str(exc.value)
        assert monarch_client._discovered_version == "v1.0.4242"

    @pytest.mark.asyncio
    async def test_non_outdated_403_never_triggers_a_second_attempt(
        self, monkeypatch,
    ):
        """A wrong password costs the one pre-fetch and nothing more — no
        re-read, no second login attempt."""
        _install_fake_session(
            monkeypatch, status=403,
            body=json.dumps({"detail": "wrong password"}),
            get_responses=[(200, json.dumps({"version": "v1.0.4242"}))],
        )

        with pytest.raises(MonarchAuthError):
            await MonarchClient.login_with_credentials(
                email="a@b.com", password="pw",
            )
        assert len(_FakeSession.post_calls) == 1
        assert len(_FakeSession.get_calls) == 1

    @pytest.mark.asyncio
    async def test_captcha_required_distinguished(self, monkeypatch):
        """Monarch's bot-protection gate: 429 with error_code CAPTCHA_REQUIRED.
        Verified live 2026-05-15 against a real account after it was
        flagged. Once tripped, programmatic login is permanently dead for
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
        # Names the settings page's own disclosure label, not a description of
        # it: the message is read beside that control, and it used to say
        # "Option B", which named the ordering rather than the method and went
        # stale the moment the headings were reworded (ISSUE-222).
        assert "paste cookies from your browser" in str(exc.value).lower()

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
            "data": {"me": {"id": "u-1", "email": "bob@example.com"}},
        }))

        result = self._run(tmp_path)
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope == {
            "status": "ok", "auth_ok": True,
            "who": {"id": "u-1", "email": "bob@example.com"},
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
