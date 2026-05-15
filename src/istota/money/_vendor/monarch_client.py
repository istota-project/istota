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
- The ``monarch-client*`` headers some downstream forks send are tolerated
  but ignored.

This module exposes only the operations we actually consume from
``monarch_api.py``. If we end up needing more (account list, balance edits),
add them here rather than reaching back into the third-party package.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp


logger = logging.getLogger(__name__)


GRAPHQL_URL = "https://api.monarch.com/graphql"
LOGIN_URL = "https://api.monarch.com/auth/login/"
APP_ORIGIN = "https://app.monarch.com"
APP_REFERER = "https://app.monarch.com/"

# /auth/login/ validates the monarch-client* headers (the API rejects with
# "Please update to the latest version of the app to continue login." when
# they're missing or stale). /graphql ignores the same headers — but we send
# them everywhere for consistency. Bump CLIENT_VERSION when Monarch starts
# rejecting it again; values are observable in the browser DevTools network
# tab on any app.monarch.com request.
CLIENT_NAME = "web"
CLIENT_VERSION = "2025.10.0"

DEFAULT_TIMEOUT_SECS = 60
_MAX_BODY_LOG_CHARS = 600


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
    """Raised when /auth/login/ tells us our monarch-client-version is stale.

    Bump ``CLIENT_VERSION`` at the top of this module to whatever the live
    web app sends (visible in browser DevTools under Network → any
    api.monarch.com request → Request Headers).
    """


class MonarchCaptchaRequired(Exception):
    """Raised when /auth/login/ demands a CAPTCHA challenge.

    Monarch trips this on accounts / IPs it has previously rate-limited or
    flagged as automated. Once tripped, the gate stays sticky for that
    (account, IP) pair and there is no programmatic path through it. The
    user must use the browser cookie-paste workflow.
    """


def _safe_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}


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
            "User-Agent": "Istota/MonarchClient (+https://github.com/istota)",
            "Origin": APP_ORIGIN,
            "Referer": APP_REFERER,
            "X-Csrftoken": self._auth.csrftoken,
            "monarch-client": CLIENT_NAME,
            "monarch-client-version": CLIENT_VERSION,
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
        """
        if not (email and password):
            raise MonarchAuthError("email and password are required")

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
            "User-Agent": "Istota/MonarchClient (+https://github.com/istota)",
            "Origin": APP_ORIGIN,
            "Referer": APP_REFERER,
            # Required at /auth/login/ — the API rejects missing/stale clients
            # with "Please update to the latest version of the app".
            "monarch-client": CLIENT_NAME,
            "monarch-client-version": CLIENT_VERSION,
        }
        body = json.dumps(payload)

        async with aiohttp.ClientSession(
            cookie_jar=jar, timeout=timeout,
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
                    #  - Client outdated (CLIENT_VERSION constant is stale)
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
                    if "update to the latest version" in detail.lower():
                        logger.warning(
                            "monarch_login_client_outdated current=%s body=%s",
                            CLIENT_VERSION, snippet,
                        )
                        raise MonarchClientOutdated(
                            f"Monarch reports our client is outdated. "
                            f"Bump CLIENT_VERSION (currently {CLIENT_VERSION}) "
                            f"in monarch_client.py to whatever the live web app "
                            f"sends. Server response: {detail}"
                        )
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
