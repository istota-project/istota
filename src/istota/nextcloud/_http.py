"""Nextcloud HTTP foundation: OCS + WebDAV requests with a structured error model.

Everything above this module talks to Nextcloud through ``ocs_request`` /
``dav_request``. Both raise :class:`OcsError` on any failure, carrying the HTTP
status, the OCS status code, and the server's own message — so a permissions
denial, a missing path, an expired app password and a network timeout are
distinguishable at the call site instead of collapsing to ``None``.

The ``None``-returning legacy variants used by best-effort daemon paths live in
``istota.nextcloud_client`` (the back-compat shim), not here.
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from ..config import Config

logger = logging.getLogger("istota.nextcloud.http")

DEFAULT_TIMEOUT = 10.0
DAV_TIMEOUT = 30.0

_OCS_SUCCESS = (100, 200)

# OCS status codes. Nextcloud reuses HTTP-looking codes plus a 99x range of its
# own; both are flattened here so one table describes every failure.
_OCS_STATUS_MESSAGES: dict[int, str] = {
    400: "Bad request (invalid parameters)",
    401: "Unauthorised — check the app password",
    403: "Forbidden — insufficient permissions",
    404: "Not found",
    405: "Method not allowed on this endpoint",
    996: "Server error",
    997: (
        "Unauthorised — this endpoint usually requires admin rights, "
        "or the app password is no longer valid"
    ),
    998: "Not found",
    999: "Unknown error — the required Nextcloud app is not enabled, or the endpoint is unavailable",
}


def describe_ocs_status(code: int) -> str:
    """Human text for an OCS status code."""
    known = _OCS_STATUS_MESSAGES.get(code)
    if known:
        return known
    return f"Nextcloud returned OCS status {code}"


def is_ocs_success(code: int | None) -> bool:
    return code in _OCS_SUCCESS


@dataclass(frozen=True)
class OcsError(Exception):
    """A Nextcloud request that did not succeed.

    ``message`` is the server's own text where it supplied one, otherwise a
    description synthesized from the status-code table.
    """

    message: str
    http_status: int | None
    ocs_status: int | None
    endpoint: str

    def __str__(self) -> str:
        return self.message

    def to_envelope(self) -> dict[str, Any]:
        """The CLI error envelope for this failure."""
        return {
            "status": "error",
            "error": self.message,
            "http_status": self.http_status,
            "ocs_status": self.ocs_status,
            "endpoint": self.endpoint,
        }


@dataclass(frozen=True)
class OcsResult:
    """A successful OCS response, with the envelope metadata kept."""

    data: Any
    ocs_status: int | None
    message: str
    http_status: int


class PathScopeError(Exception):
    """A caller-supplied path resolved outside the user's workspace."""


# --- connection helpers ---


def nc_auth(config: Config) -> tuple[str, str]:
    return (config.nextcloud.username, config.nextcloud.app_password)


def nc_base_url(config: Config) -> str:
    return config.nextcloud.url.rstrip("/")


def ocs_headers() -> dict[str, str]:
    return {"OCS-APIRequest": "true", "Accept": "application/json"}


def nc_configured(config: Config) -> bool:
    return bool(config.nextcloud.url and config.nextcloud.username)


def _not_configured(endpoint: str) -> OcsError:
    return OcsError(
        "Nextcloud is not configured (nextcloud.url / nextcloud.username are unset)",
        None,
        None,
        endpoint,
    )


# --- OCS ---


def ocs_request_full(
    config: Config,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> OcsResult:
    """Issue an OCS request, returning the full envelope. Raises OcsError."""
    if not nc_configured(config):
        raise _not_configured(path)

    url = f"{nc_base_url(config)}/ocs/v2.php{path}"
    verb = method.upper()

    kwargs: dict[str, Any] = {
        "auth": nc_auth(config),
        "headers": ocs_headers(),
        "timeout": timeout,
    }
    if params is not None:
        kwargs["params"] = params
    if data is not None:
        kwargs["data"] = data

    # Dispatched per-verb rather than through httpx.request so the shape of each
    # call stays the one the existing tests (and mocks) assert on.
    try:
        if verb == "GET":
            resp = httpx.get(url, **kwargs)
        elif verb == "POST":
            resp = httpx.post(url, **kwargs)
        elif verb == "PUT":
            resp = httpx.put(url, **kwargs)
        elif verb == "DELETE":
            resp = httpx.delete(url, **kwargs)
        else:
            resp = httpx.request(verb, url, **kwargs)
    except OcsError:
        raise
    except Exception as e:
        raise OcsError(f"Could not reach Nextcloud: {e}", None, None, path) from e

    raw_status = getattr(resp, "status_code", None)
    http_status = raw_status if isinstance(raw_status, int) else None

    try:
        body = resp.json()
    except Exception:
        body = None

    if not isinstance(body, dict) or "ocs" not in body:
        detail = (getattr(resp, "text", "") or "").strip()[:200]
        if http_status is not None and http_status >= 400:
            message = f"HTTP {http_status} from Nextcloud"
        else:
            message = "Nextcloud returned a non-OCS response"
        if detail:
            message = f"{message}: {detail}"
        raise OcsError(message, http_status, None, path)

    envelope = body.get("ocs") or {}
    meta = envelope.get("meta") or {}
    ocs_status = meta.get("statuscode")
    server_message = (meta.get("message") or "").strip()

    if ocs_status is None:
        # Some endpoints (and reverse proxies) answer without the meta block.
        # Fall back to the HTTP status; an unknown one is treated as success,
        # since the envelope itself parsed.
        ok = http_status is None or 200 <= http_status < 300
    else:
        ok = is_ocs_success(ocs_status)

    if not ok:
        message = server_message or describe_ocs_status(
            ocs_status if isinstance(ocs_status, int) else (http_status or -1)
        )
        raise OcsError(message, http_status, ocs_status, path)

    return OcsResult(
        data=envelope.get("data"),
        ocs_status=ocs_status,
        message=server_message,
        http_status=http_status if http_status is not None else 200,
    )


def ocs_request(
    config: Config,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """Issue an OCS request and return ``ocs.data``. Raises OcsError."""
    return ocs_request_full(
        config, method, path, params=params, data=data, timeout=timeout
    ).data


def ocs_get(
    config: Config,
    path: str,
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    return ocs_request(config, "GET", path, params=params, timeout=timeout)


def ocs_post(
    config: Config,
    path: str,
    data: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    return ocs_request(config, "POST", path, data=data or {}, timeout=timeout)


def ocs_put(
    config: Config,
    path: str,
    data: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    return ocs_request(config, "PUT", path, data=data or {}, timeout=timeout)


def ocs_delete(
    config: Config,
    path: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    return ocs_request(config, "DELETE", path, timeout=timeout)


# --- WebDAV ---


def dav_files_url(config: Config, path: str = "", *, username: str | None = None) -> str:
    """Absolute WebDAV URL for a path in the bot account's file tree."""
    user = username or config.nextcloud.username
    clean = (path or "").strip().lstrip("/")
    encoded = quote(clean, safe="/")
    base = f"{nc_base_url(config)}/remote.php/dav/files/{quote(user, safe='')}"
    return f"{base}/{encoded}" if encoded else base


def dav_root_url(config: Config) -> str:
    """Absolute WebDAV root (the target of server-side SEARCH)."""
    return f"{nc_base_url(config)}/remote.php/dav/"


def dav_request(
    config: Config,
    method: str,
    url: str,
    *,
    content: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = DAV_TIMEOUT,
    ok_statuses: tuple[int, ...] | None = None,
) -> httpx.Response:
    """Issue a WebDAV request. Raises OcsError on a non-success status.

    ``ok_statuses`` widens what counts as success — MKCOL against an existing
    collection answers 405, which is a no-op rather than a failure.
    """
    if not nc_configured(config):
        raise _not_configured(url)

    try:
        resp = httpx.request(
            method.upper(),
            url,
            content=content,
            headers=headers or {},
            auth=nc_auth(config),
            timeout=timeout,
        )
    except Exception as e:
        raise OcsError(f"Could not reach Nextcloud: {e}", None, None, url) from e

    raw_status = getattr(resp, "status_code", None)
    if not isinstance(raw_status, int):
        return resp
    status = raw_status
    if ok_statuses is not None:
        if status in ok_statuses or 200 <= status < 300:
            return resp
    elif 200 <= status < 300:
        return resp

    detail = (getattr(resp, "text", "") or "").strip()[:200]
    message = _dav_message(status, detail)
    raise OcsError(message, status, None, url)


def _dav_message(status: int, detail: str) -> str:
    base = {
        401: "Unauthorised — check the app password",
        403: "Forbidden — insufficient permissions on this path",
        404: "Not found",
        405: "Method not allowed on this path",
        409: "Conflict — a parent folder is missing",
        412: "Precondition failed",
        423: "Locked",
        507: "Insufficient storage (quota exceeded)",
    }.get(status, f"WebDAV request failed with HTTP {status}")
    return f"{base}: {detail}" if detail else base


# --- path scoping ---


def workspace_root(user_id: str) -> str:
    return f"/Users/{user_id}"


def resolve_scoped_path(
    path: str,
    user_id: str | None,
    *,
    is_admin: bool = False,
) -> str:
    """Normalize a caller-supplied Nextcloud path and confine it to the caller.

    The bot's credentials reach every user's workspace; the skill must not. A
    relative path is anchored at the caller's workspace root; an absolute path
    must resolve inside it. Admins may address anything (they already can via
    the framework DB and the sandbox).
    """
    raw = (path or "").strip()
    uid = (user_id or "").strip()

    if uid:
        root = workspace_root(uid)
        candidate = raw if raw.startswith("/") else posixpath.join(root, raw)
    else:
        if not is_admin:
            raise PathScopeError(
                "No calling user is set (ISTOTA_USER_ID), so no workspace can be "
                "resolved. Paths are refused."
            )
        candidate = raw if raw.startswith("/") else f"/{raw}"

    normalized = posixpath.normpath(candidate)
    if normalized == ".":
        normalized = "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")

    if is_admin or not uid:
        return normalized

    root = workspace_root(uid)
    if normalized != root and not normalized.startswith(root + "/"):
        raise PathScopeError(
            f"Path {normalized!r} is outside your workspace ({root}). "
            "Only paths under your own workspace can be addressed."
        )
    return normalized
