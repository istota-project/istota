"""Slim Monarch Money client — vendored after upstream auth broke (2026-05-15).

Background: ``monarchmoneycommunity`` 1.3.x talks to ``api.monarch.com`` with
``Authorization: Token <...>`` only. The web API now enforces Django CSRF on
``/graphql``, so every request needs:

- session cookies (``session_id`` and ``csrftoken``)
- ``X-Csrftoken`` header matching the ``csrftoken`` cookie
- ``Origin: https://app.monarch.com`` and ``Referer: https://app.monarch.com/``

Local probing (see PR description) confirmed:
- The two cookies above are the entire durable credential set;
  ``cf_clearance`` and ``__cf_bm`` are only needed at login time.
- ``/graphql`` tolerates but ignores the ``monarch-client*`` headers.
  ``/auth/login/`` does **not** — see below.

Client-version gate (2026-07-30). ``/auth/login/`` validates
``monarch-client-version`` and rejects a stale value with 403 "Please update to
the latest version of the app to continue login." Three properties make this
nastier than it looks:

- It is checked **after** the credentials are validated. A probe with a bogus
  password gets 404 "Invalid email and password combination" and never reaches
  the gate, so no credential-free test can tell you the version is stale.
- The web route turns it into a 503, so the only symptom is a *correct* login
  failing in a way that reads like a Monarch outage.
- The accepted value moves fast. ``2025.10.0`` was live-verified as accepted in
  2026-05 (Monarch validated the field loosely then) and had silently stopped
  working by 2026-07. Observing ``VERSION_URL`` over a single afternoon caught
  it going ``v1.0.3697`` → ``v1.0.3696`` — it moves within hours, and not
  always forwards.

A compile-time constant therefore cannot be right for long, so it is only a
cold-start fallback: the version is read from ``VERSION_URL`` *before*
attempting a login, and re-read once if the attempt is rejected anyway. The
pre-fetch matters because the alternative — discover only after a rejection —
spends a failed credential submission on every cold process, against an
endpoint with a rate limiter and a sticky CAPTCHA gate.

This module exposes only the operations we actually consume from
``monarch_api.py``. If we end up needing more (account list, balance edits),
add them here rather than reaching back into the third-party package.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp


logger = logging.getLogger(__name__)


GRAPHQL_URL = "https://api.monarch.com/graphql"
LOGIN_URL = "https://api.monarch.com/auth/login/"
APP_ORIGIN = "https://app.monarch.com"
APP_REFERER = "https://app.monarch.com/"

# The web app's version manifest — a ~23-byte `{"version": "..."}` document
# carrying exactly the `clientVersion` its bundle sends on every request. It
# is what lets a stale CLIENT_VERSION heal itself (see below).
VERSION_URL = "https://app.monarch.com/version.json"

# /auth/login/ validates the monarch-client* headers, rejecting a stale value
# with "Please update to the latest version of the app to continue login."
# The live app sends a different client name per transport; /auth/login/ is the
# REST one. /graphql ignores both headers, but we send the matching pair there
# too so the request looks like what the app actually issues.
REST_CLIENT_NAME = "monarch-core-web-app-rest"
GRAPHQL_CLIENT_NAME = "monarch-core-web-app-graphql"

# Cold-start fallback only — used when VERSION_URL can't be reached. Expect it
# to be stale (the value moved twice on the day it was set); it exists so an
# unreachable manifest degrades to "probably wrong" rather than "certainly
# missing". Kept current on a best-effort basis, not relied upon.
CLIENT_VERSION = "v1.0.3696"

# Process-wide cache of a version learned from VERSION_URL. `None` means
# "not discovered yet — fetch before the next login". Process-local by design:
# the web and scheduler units are separate processes and each pays one cheap
# GET, which is far less than coordinating shared state for a 23-byte document.
_discovered_version: str | None = None

# The discovered value is interpolated into an HTTP request header, so it is
# validated against a conservative charset rather than trusted — a payload
# carrying CR/LF would otherwise smuggle extra headers into the login request.
# Both the charset *and* the pre-match strip are load-bearing; `\Z` rather than
# `$` so a trailing newline can't slip through even if the strip is ever lost.
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}\Z")

# Sent on every request. Not cosmetic: Cloudflare fronts app.monarch.com and
# 403s aiohttp's default "Python/3.x aiohttp/3.y" User-Agent, so leaving it
# unset makes discovery fail from a host where curl succeeds (verified
# 2026-07-30). A stock browser UA is what the endpoints expect to see.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# app.monarch.com sends a Content-Security-Policy header (~9.5 KB) well over
# aiohttp's 8190-byte default field cap, which surfaces as a bogus 400 "Got
# more than 8190 bytes when reading" before the body is ever parsed. Applied to
# all three sessions, not just discovery: api.monarch.com is under the same
# fronting and a response header's size must not decide whether a call works.
_MAX_HEADER_FIELD_BYTES = 65536

DEFAULT_TIMEOUT_SECS = 60
VERSION_FETCH_TIMEOUT_SECS = 15
_MAX_BODY_LOG_CHARS = 600
_OUTDATED_MARKER = "update to the latest version"


class MonarchAuthError(Exception):
    """Raised when the API rejects our credentials (401/403)."""


class MonarchAPIError(Exception):
    """Raised when the API returns a non-2xx response that isn't an auth issue,
    or when the GraphQL response contains an ``errors`` array."""


class MonarchMFARequired(Exception):
    """Raised when /auth/login/ demands a TOTP code we weren't given."""


class MonarchCloudflareBlocked(Exception):
    """Raised when Cloudflare challenges the request before it reaches Monarch.

    Server-side IPs (especially cloud providers) get this regularly. The user
    has to fall back to the browser cookie-paste workflow.
    """


class MonarchClientOutdated(Exception):
    """Raised when /auth/login/ refuses our monarch-client-version *and*
    discovery couldn't produce one it accepts.

    ``login_with_credentials`` already resolved the version from
    ``VERSION_URL`` before attempting, re-read it after the refusal, and
    retried once if the re-read differed — so reaching this means the manifest
    is unreachable, or agrees with the value that was just refused. Either way
    a version bump alone may not be the fix; check whether the header contract
    changed (``curl https://app.monarch.com/version.json``, and DevTools →
    Network → any api.monarch.com request → Request Headers).
    ``scripts/probe_monarch_login.py`` reports both values.
    """


class MonarchCaptchaRequired(Exception):
    """Raised when /auth/login/ demands a CAPTCHA challenge.

    Monarch trips this on accounts / IPs it has previously rate-limited or
    flagged as automated. Once tripped, the gate stays sticky for that
    (account, IP) pair and there is no programmatic path through it. The
    user must use the browser cookie-paste workflow.
    """


def _safe_json(text: str) -> dict[str, Any]:
    """Parse a response body, yielding ``{}`` for anything that isn't a JSON
    object. The isinstance guard is not redundant: ``json.loads`` happily
    returns a list/str/int/None for a valid non-object body, and every caller
    immediately does ``.get(...)`` — which would raise ``AttributeError`` from
    inside error-handling code and mask the real failure.
    """
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _looks_like_cloudflare(status: int, body: str) -> bool:
    """Cloudflare's challenge / block pages are HTML, never JSON; paired with
    a 403 / 429 / 503 they're a clear signal we never reached Monarch.
    """
    if status not in (403, 429, 503):
        return False
    if not body:
        return False
    head = body.lstrip()[:200].lower()
    return (
        "<html" in head
        and ("cloudflare" in body.lower()[:2000]
             or "cf-ray" in body.lower()[:2000]
             or "attention required" in body.lower()[:2000])
    )


def _current_client_version() -> str:
    """The version to send: whatever we last discovered, else the constant."""
    return _discovered_version or CLIENT_VERSION


async def _resolve_client_version(*, force_refresh: bool = False) -> str:
    """The version to send on a login, discovering it if we don't have one.

    ``force_refresh`` re-reads even when a value is cached — used after a
    rejection, where the cached value is precisely what was refused.
    """
    global _discovered_version

    if _discovered_version is not None and not force_refresh:
        return _discovered_version

    live = await fetch_live_client_version()
    if live is not None:
        if live != _discovered_version:
            logger.info(
                "monarch_client_version_resolved old=%s new=%s",
                _discovered_version or CLIENT_VERSION, live,
            )
        _discovered_version = live
    return _current_client_version()


async def fetch_live_client_version(
    *, timeout_seconds: int = VERSION_FETCH_TIMEOUT_SECS,
) -> str | None:
    """Read the web app's current client version from ``VERSION_URL``.

    Returns ``None`` — never raises — on any failure (network, non-200,
    non-JSON, missing/oddly-shaped ``version``). A failure here just means the
    caller keeps using ``CLIENT_VERSION``, which is strictly no worse than not
    having tried.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(
            timeout=timeout,
            max_line_size=_MAX_HEADER_FIELD_BYTES,
            max_field_size=_MAX_HEADER_FIELD_BYTES,
        ) as session:
            async with session.get(
                VERSION_URL,
                headers={
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "monarch_version_fetch_status status=%s", resp.status,
                    )
                    return None
                text = await resp.text()
    except Exception as exc:  # noqa: BLE001 — discovery is strictly best-effort
        logger.warning("monarch_version_fetch_failed error=%s", exc)
        return None

    version = _safe_json(text).get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version.strip()):
        logger.warning(
            "monarch_version_fetch_unusable body=%s",
            text[:_MAX_BODY_LOG_CHARS],
        )
        return None
    return version.strip()


@dataclass
class MonarchCookieAuth:
    """Cookie-based credentials.

    Both fields are mandatory. The csrftoken value MUST match the value
    inside the cookie jar (Django compares them byte-for-byte).
    """
    session_id: str
    csrftoken: str


_GET_TRANSACTIONS_QUERY = """\
query GetTransactionsList(
  $offset: Int, $limit: Int,
  $filters: TransactionFilterInput, $orderBy: TransactionOrdering
) {
  allTransactions(filters: $filters) {
    totalCount
    results(offset: $offset, limit: $limit, orderBy: $orderBy) {
      id
      ...TransactionOverviewFields
      __typename
    }
    __typename
  }
}

fragment TransactionOverviewFields on Transaction {
  id
  amount
  pending
  date
  hideFromReports
  plaidName
  notes
  isRecurring
  reviewStatus
  needsReview
  isSplitTransaction
  createdAt
  updatedAt
  category { id name __typename }
  merchant { name id transactionsCount __typename }
  account { id displayName __typename }
  tags { id name color order __typename }
  __typename
}
"""


_PROBE_QUERY = "query ProbeMe { me { id email name } }"


class MonarchClient:
    """Stateless GraphQL caller for ``api.monarch.com``.

    A single instance can issue many calls. Each call opens a short-lived
    aiohttp session with the configured cookies — there's no persistent
    connection pool because Monarch sync is a once-per-day batch and the
    overhead is negligible.
    """

    def __init__(
        self,
        auth: MonarchCookieAuth,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECS,
    ) -> None:
        if not auth.session_id or not auth.csrftoken:
            raise MonarchAuthError(
                "Monarch cookie auth requires both session_id and csrftoken"
            )
        self._auth = auth
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Origin": APP_ORIGIN,
            "Referer": APP_REFERER,
            "X-Csrftoken": self._auth.csrftoken,
            "monarch-client": GRAPHQL_CLIENT_NAME,
            "monarch-client-version": _current_client_version(),
        }

    def _cookies(self) -> dict[str, str]:
        return {
            "session_id": self._auth.session_id,
            "csrftoken": self._auth.csrftoken,
        }

    async def _post_graphql(
        self, operation: str, query: str, variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps({
            "operationName": operation,
            "variables": variables or {},
            "query": query,
        })
        async with aiohttp.ClientSession(
            cookies=self._cookies(), timeout=self._timeout,
            max_line_size=_MAX_HEADER_FIELD_BYTES,
            max_field_size=_MAX_HEADER_FIELD_BYTES,
        ) as session:
            async with session.post(
                GRAPHQL_URL, data=body, headers=self._headers(),
            ) as resp:
                text = await resp.text()
                if resp.status in (401, 403):
                    snippet = text[:_MAX_BODY_LOG_CHARS]
                    logger.warning(
                        "monarch_auth_rejected operation=%s status=%s body=%s",
                        operation, resp.status, snippet,
                    )
                    raise MonarchAuthError(
                        f"Monarch rejected credentials (HTTP {resp.status}): "
                        f"{snippet}"
                    )
                if resp.status >= 400:
                    snippet = text[:_MAX_BODY_LOG_CHARS]
                    logger.warning(
                        "monarch_api_error operation=%s status=%s body=%s",
                        operation, resp.status, snippet,
                    )
                    raise MonarchAPIError(
                        f"Monarch API error (HTTP {resp.status}): {snippet}"
                    )
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    snippet = text[:_MAX_BODY_LOG_CHARS]
                    logger.warning(
                        "monarch_bad_json operation=%s body=%s",
                        operation, snippet,
                    )
                    raise MonarchAPIError(
                        f"Monarch returned non-JSON response: {snippet}"
                    ) from exc
                if payload.get("errors"):
                    err = payload["errors"]
                    logger.warning(
                        "monarch_graphql_errors operation=%s errors=%s",
                        operation, json.dumps(err)[:_MAX_BODY_LOG_CHARS],
                    )
                    raise MonarchAPIError(
                        f"Monarch GraphQL errors: {json.dumps(err)[:_MAX_BODY_LOG_CHARS]}"
                    )
                return payload.get("data") or {}

    async def whoami(self) -> dict[str, Any]:
        """Tiny ``me`` query — useful for auth health checks."""
        data = await self._post_graphql("ProbeMe", _PROBE_QUERY)
        return data.get("me") or {}

    @staticmethod
    async def login_with_credentials(
        *,
        email: str,
        password: str,
        mfa_totp: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECS,
    ) -> MonarchCookieAuth:
        """Programmatic login via /auth/login/.

        Captures session cookies from the response. Returns the cookie pair
        the rest of this module needs.

        ``mfa_totp`` is the *current* 6-digit TOTP code, not the secret. The
        caller is responsible for generating it (we don't want to hold the
        TOTP secret in storage). If the account requires MFA and no code is
        supplied, ``MonarchMFARequired`` is raised.

        Server-side login from cloud IPs is often blocked by Cloudflare;
        ``MonarchCloudflareBlocked`` is raised so callers can route the user
        to the cookie-paste workflow with a useful message.

        The client version is read from ``VERSION_URL`` before the attempt
        (cached process-wide thereafter) and re-read once if Monarch rejects it
        anyway, retrying **exactly once**. One retry, never a loop: this is a
        live auth endpoint behind a rate limiter and a sticky CAPTCHA gate,
        where a failed attempt is a real cost.
        """
        if not (email and password):
            raise MonarchAuthError("email and password are required")

        attempted = await _resolve_client_version()
        try:
            return await MonarchClient._login_once(
                email=email, password=password, mfa_totp=mfa_totp,
                timeout_seconds=timeout_seconds, client_version=attempted,
            )
        except MonarchClientOutdated as first_exc:
            live = await _resolve_client_version(force_refresh=True)
            if live == attempted:
                # Either the manifest is unreachable (we kept the same value)
                # or it agrees with what was just refused. Retrying identical
                # bytes would only burn a second attempt.
                raise MonarchClientOutdated(
                    f"Monarch rejected client version {attempted}, which is "
                    f"the best value available from {VERSION_URL}. The header "
                    f"contract has likely changed — a version bump alone may "
                    f"not fix this. Server response: {first_exc}"
                ) from first_exc

            logger.warning(
                "monarch_client_version_refreshed old=%s new=%s",
                attempted, live,
            )
            try:
                return await MonarchClient._login_once(
                    email=email, password=password, mfa_totp=mfa_totp,
                    timeout_seconds=timeout_seconds, client_version=live,
                )
            except MonarchClientOutdated as retry_exc:
                # Keep the discovered value rather than reverting to the
                # constant: the next call then short-circuits on the branch
                # above after one attempt instead of repeating this two-attempt
                # sequence indefinitely.
                raise MonarchClientOutdated(
                    f"Monarch rejected both {attempted} and the live version "
                    f"{live} from {VERSION_URL}. The header contract has "
                    f"likely changed. Server response: {retry_exc}"
                ) from retry_exc
            except MonarchAuthError as retry_exc:
                if not mfa_totp:
                    raise
                # The first attempt got far enough to be judged on its client
                # version, so the credentials — TOTP included — were already
                # validated, and a one-time code is spent when it is validated.
                # Reporting that as "wrong credentials" would send an MFA user
                # to re-check a password that is fine; ask for a fresh code.
                raise MonarchMFARequired(
                    "Monarch's client version was out of date and has been "
                    "refreshed, but the one-time code was already used by the "
                    "first attempt. Enter a new code and try again."
                ) from retry_exc

    @staticmethod
    async def _login_once(
        *,
        email: str,
        password: str,
        mfa_totp: str | None,
        timeout_seconds: int,
        client_version: str,
    ) -> MonarchCookieAuth:
        """One /auth/login/ round trip with a given client version.

        Raises the same exception set as ``login_with_credentials``; the
        caller owns the retry policy for ``MonarchClientOutdated``.
        """
        payload: dict[str, Any] = {
            "username": email,
            "password": password,
            "supports_mfa": True,
            "trusted_device": True,
        }
        if mfa_totp:
            payload["totp"] = mfa_totp

        # CookieJar(unsafe=True) so cookies set by api.monarch.com (an IP-less
        # public host where aiohttp's default jar drops cookies) actually stick.
        jar = aiohttp.CookieJar(unsafe=True)
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Origin": APP_ORIGIN,
            "Referer": APP_REFERER,
            # Required at /auth/login/ — the API rejects missing/stale clients
            # with "Please update to the latest version of the app".
            "monarch-client": REST_CLIENT_NAME,
            "monarch-client-version": client_version,
        }
        body = json.dumps(payload)

        async with aiohttp.ClientSession(
            cookie_jar=jar, timeout=timeout,
            max_line_size=_MAX_HEADER_FIELD_BYTES,
            max_field_size=_MAX_HEADER_FIELD_BYTES,
        ) as session:
            async with session.post(
                LOGIN_URL, data=body, headers=headers,
            ) as resp:
                text = await resp.text()
                snippet = text[:_MAX_BODY_LOG_CHARS]

                if _looks_like_cloudflare(resp.status, text):
                    logger.warning(
                        "monarch_login_cloudflare_blocked status=%s body=%s",
                        resp.status, snippet,
                    )
                    raise MonarchCloudflareBlocked(
                        "Monarch login blocked by Cloudflare. Programmatic "
                        "login from this host is unavailable; paste browser "
                        "cookies (session_id + csrftoken) instead."
                    )

                if resp.status == 403:
                    # Distinguish three flavors of 403:
                    #  - MFA required (we need a TOTP code)
                    #  - Client outdated (the version we sent was refused)
                    #  - Everything else (bad creds, etc.)
                    parsed = _safe_json(text)
                    detail = parsed.get("detail", "") if text else ""
                    if "mfa" in detail.lower() or parsed.get(
                        "error_code", "",
                    ) == "REQUIRES_MFA":
                        logger.warning(
                            "monarch_login_mfa_required body=%s", snippet,
                        )
                        raise MonarchMFARequired(detail or "MFA required")
                    if _OUTDATED_MARKER in detail.lower():
                        logger.warning(
                            "monarch_login_client_outdated sent=%s body=%s",
                            client_version, snippet,
                        )
                        raise MonarchClientOutdated(detail)
                    logger.warning(
                        "monarch_login_403 body=%s", snippet,
                    )
                    raise MonarchAuthError(
                        f"Monarch login rejected (403): {snippet}"
                    )

                # 429 with CAPTCHA_REQUIRED is Monarch's bot-protection gate
                # — distinct from generic rate-limiting and from credential
                # failures. Once tripped, programmatic login is permanently
                # dead for that (account, IP) pair.
                parsed = _safe_json(text)
                if (
                    resp.status == 429
                    and parsed.get("error_code") == "CAPTCHA_REQUIRED"
                ):
                    logger.warning(
                        "monarch_login_captcha_required body=%s", snippet,
                    )
                    raise MonarchCaptchaRequired(
                        "Monarch requires a CAPTCHA we can't solve "
                        "programmatically. Use the browser cookie-paste "
                        "workflow instead (Option B in the settings page)."
                    )

                # Monarch returns 404 (not 401) for "Invalid email and
                # password combination". 401 / 404 / other 4xx all map to
                # MonarchAuthError so the UI can show "wrong credentials".
                if resp.status >= 400:
                    logger.warning(
                        "monarch_login_failed status=%s body=%s",
                        resp.status, snippet,
                    )
                    raise MonarchAuthError(
                        f"Monarch login failed (HTTP {resp.status}): {snippet}"
                    )

                # 2xx — extract cookies. The Set-Cookie headers from
                # api.monarch.com populate the jar; pull session_id + csrftoken
                # out by name.
                cookies = {c.key: c.value for c in jar}
                session_id = cookies.get("session_id")
                csrftoken = cookies.get("csrftoken")
                if not (session_id and csrftoken):
                    logger.warning(
                        "monarch_login_no_cookies cookies=%s body=%s",
                        sorted(cookies.keys()), snippet,
                    )
                    raise MonarchAuthError(
                        "Monarch login returned no session cookies. "
                        f"Cookies seen: {sorted(cookies.keys())}"
                    )
                return MonarchCookieAuth(
                    session_id=session_id, csrftoken=csrftoken,
                )

    async def get_transactions(
        self,
        *,
        start_date: str,
        end_date: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Fetch transactions in a date range.

        Returns the full ``data`` dict (mirrors the upstream surface the
        rest of istota.money already consumes — i.e. the caller pulls
        ``allTransactions.results`` out of it).
        """
        variables: dict[str, Any] = {
            "offset": offset,
            "limit": limit,
            "orderBy": "date",
            "filters": {
                "search": "",
                "categories": [],
                "accounts": [],
                "tags": [],
                "startDate": start_date,
                "endDate": end_date,
            },
        }
        return await self._post_graphql(
            "GetTransactionsList", _GET_TRANSACTIONS_QUERY, variables,
        )
