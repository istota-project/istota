"""The HTTP client every WordPress verb goes through, and its four guards.

This runs host-side, in the daemon's network namespace, with nothing confining
its egress (skill CLIs are spawned by the proxy outside the sandbox). So every
request passes, in order:

1. **The bound-host check** (`sites.check_bound`). The authority must be one the
   vault entry is bound to, or nothing is resolved, connected or sent. This is
   what stops a model-edited site record, or a ``--blog`` host, from carrying the
   password somewhere else.
2. **The SSRF guard.** The host is resolved, and the request is refused if *any*
   address is non-public (`istota.net_guard.ip_is_public`, the rule the native
   WebFetch tool uses), unless the operator listed the exact host in
   ``[wordpress] private_hosts``. The connection is then made to the address
   that was checked, with the Host header and TLS SNI on the name, so DNS cannot
   change its answer between the check and the connect.
3. **No redirects.** ``follow_redirects=False``, and a 3xx is reported naming its
   target, so Basic auth is never replayed to another host.
4. **The retry policy.** A request that cannot apply twice (``GET``,
   ``OPTIONS``, an update to an existing id) is retried once on a connection
   error or a 5xx. Anything else is sent once, and an ambiguous ending is
   ``outcome_unknown`` rather than a guess (the one-send rule of `sms.md`).

Everything the server says in an error is fenced before it reaches a message.
The password is revealed once, into the Authorization header, and appears in no
message, log line or ``repr``. ``trust_env=False`` so a proxy variable cannot
redirect authenticated traffic; the TLS trust store still honours
``SSL_CERT_FILE``, which the skill environment carries for a local CA.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
import socket
from urllib.parse import urlsplit

import httpx

from istota.net_guard import ip_is_public
from istota.untrusted import frame_untrusted

from .sites import SiteError, check_bound

log = logging.getLogger(__name__)

LABEL = "WORDPRESS CONTENT"
REQUEST_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 120.0
#: A response past this is refused rather than held in memory. A long
#: flexible-content post is tens of kilobytes; this bounds a misbehaving site.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
USER_AGENT = "istota-wordpress"

_IDEMPOTENT_METHODS = frozenset({"GET", "OPTIONS", "HEAD"})
_WP_CODE_RE = re.compile(r"\A[a-z0-9_]{1,64}\Z")
_FIELD_NAME_RE = re.compile(r"\A[A-Za-z0-9_\-\[\]]{1,64}\Z")
#: A selector the model echoes back (a role, a capability, a namespace, a
#: rest_base): bare when it has this shape, fenced when it does not.
_SELECTOR_RE = re.compile(r"\A[A-Za-z0-9_.:/\-]{1,128}\Z")

AUTH_FAILED_HELP = (
    "WordPress rejected the application password. The three ordinary causes "
    "all look like a wrong password: the password belongs to a different user "
    "than the vault entry's username; the web server strips the Authorization "
    "header (Apache with CGI or FPM needs it passed through); or a "
    "TLS-terminating proxy in front of WordPress makes it think the request is "
    "not over HTTPS, which disables application passwords."
)


class WordPressError(Exception):
    """A refusal with its `reason` code (spec §9.2) and any extra envelope fields."""

    def __init__(self, message: str, reason: str, **extra) -> None:
        super().__init__(message)
        self.reason = reason
        self.extra = extra
        #: The site's error body (``code``, ``message``, ``data``), never put in
        #: the envelope: a verb that knows an ability's error params reads them
        #: here and fences what it passes on.
        self.wp_data: dict = {}


def fence(text: object) -> str:
    """Site-authored text, between the WordPress markers."""
    return frame_untrusted(text, LABEL)


def resolve_host(host: str, port: int) -> list[str]:
    """Every address `host` resolves to, in resolver order; empty on failure."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError, UnicodeError):
        return []
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


def _wp_code(body: bytes) -> str | None:
    """The ``code`` of a WordPress error body, when it is one and well formed."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    code = data.get("code") if isinstance(data, dict) else None
    return code if isinstance(code, str) and _WP_CODE_RE.fullmatch(code) else None


def _host_header(host: str, port: int) -> str:
    """The Host header for a name or an address literal, brackets on IPv6."""
    name = f"[{host}]" if ":" in host else host
    return name if port == 443 else f"{name}:{port}"


class WordPressClient:
    def __init__(
        self,
        *,
        site_url: str,
        username,
        password,
        bound_hosts,
        private_hosts=(),
        transport: httpx.BaseTransport | None = None,
        resolve=None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self.site_url = site_url.rstrip("/")
        self._username = username
        self._password = password
        self._bound = tuple(bound_hosts or ())
        self._private = frozenset(
            str(h).strip().lower().rstrip(".") for h in (private_hosts or ()) if str(h).strip()
        )
        self._resolve = resolve or resolve_host
        self._timeout = timeout
        # `verify` is built with trust_env=True on its own: that reads only
        # SSL_CERT_FILE / SSL_CERT_DIR, while the client's trust_env=False keeps
        # proxy variables out. httpx couples the two otherwise.
        verify = True if transport is not None else httpx.create_ssl_context(trust_env=True)
        self._http = httpx.Client(
            transport=transport, trust_env=False, follow_redirects=False,
            verify=verify, timeout=timeout,
        )

    def __repr__(self) -> str:
        return f"WordPressClient({self.site_url})"

    def close(self) -> None:
        self._http.close()

    # -- the four guards ------------------------------------------------------

    def _url(self, route: str, base: str | None) -> str:
        root = (base or self.site_url).rstrip("/")
        return f"{root}/wp-json/{route.lstrip('/')}"

    def _checked_addresses(self, host: str, port: int) -> list[str]:
        name = host.lower().rstrip(".")
        try:
            addresses = [ipaddress.ip_address(name)]
        except ValueError:
            addresses = []
            for raw in self._resolve(host, port):
                try:
                    addresses.append(ipaddress.ip_address(raw.split("%", 1)[0]))
                except ValueError:
                    continue
        if not addresses:
            raise WordPressError(f"Could not resolve {name}; nothing was sent.",
                                 "connection_failed")
        if name not in self._private:
            for address in addresses:
                if not ip_is_public(address):
                    raise WordPressError(
                        f"{name} resolves to a private or reserved address ({address}). "
                        f"Only a host the operator lists in [wordpress] private_hosts "
                        f"may; nothing was sent.",
                        "host_refused",
                    )
        return [str(a) for a in addresses]

    def _build(self, method, url, host, port, address, params, body, *,
               content=None, headers=None, timeout=None) -> httpx.Request:
        target = httpx.URL(url, params=params).copy_with(host=address, port=port)
        credentials = f"{self._username.reveal()}:{self._password.reveal()}"
        # The caller's headers first, so they can never replace these four.
        merged = dict(headers or {})
        merged.update({
            "Host": _host_header(host, port),
            "Authorization": "Basic " + base64.b64encode(credentials.encode()).decode(),
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        if content is not None:
            request = httpx.Request(method, target, headers=merged, content=content)
        else:
            request = httpx.Request(method, target, headers=merged, json=body)
        extensions = dict(request.extensions or {})
        extensions["sni_hostname"] = host
        if timeout is not None:
            extensions["timeout"] = httpx.Timeout(timeout).as_dict()
        request.extensions = extensions
        return request

    def _send(self, request: httpx.Request) -> tuple[httpx.Response, bytes]:
        response = self._http.send(request, stream=True)
        try:
            buffer = bytearray()
            for chunk in response.iter_bytes():
                buffer.extend(chunk)
                if len(buffer) > MAX_RESPONSE_BYTES:
                    raise WordPressError(
                        f"The response was larger than {MAX_RESPONSE_BYTES // (1024 * 1024)} MiB "
                        f"and was not read.",
                        "response_too_large",
                    )
            return response, bytes(buffer)
        finally:
            response.close()

    # -- the one request path -------------------------------------------------

    def request(
        self,
        method: str,
        route: str,
        *,
        params: dict | None = None,
        json: object = None,
        idempotent: bool | None = None,
        base: str | None = None,
        content: bytes | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
        definite_codes: frozenset[str] = frozenset(),
    ) -> tuple[object, httpx.Headers]:
        """One call to ``{base}/wp-json/{route}``: ``(parsed JSON, headers)``.

        `content` sends raw bytes (a media upload) in place of `json`, with the
        caller's `headers` beside the client's own, which they cannot replace.
        `definite_codes` are WordPress error codes that, on a 5xx to a request
        that is not idempotent, mean nothing was stored, because the server
        answers them before it writes (``rest_upload_sideload_error``). Such an
        answer is a refusal rather than ``outcome_unknown``.
        """
        method = method.upper()
        if idempotent is None:
            idempotent = method in _IDEMPOTENT_METHODS
        url = self._url(route, base)
        try:
            check_bound(url, self._bound)
        except SiteError as exc:
            raise WordPressError(str(exc), exc.reason) from None
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or 443
        addresses = self._checked_addresses(host, port)

        attempts = 2 if idempotent else 1
        attempt = 0
        index = 0
        while True:
            last = attempt + 1 >= attempts
            request = self._build(method, url, host, port, addresses[index], params, json,
                                  content=content, headers=headers, timeout=timeout)
            try:
                response, body = self._send(request)
            except WordPressError as exc:
                # Too large to read: the server answered, so a write applied.
                if not idempotent:
                    raise self._outcome_unknown(method, route, exc.reason) from None
                raise
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Nothing reached the server, so trying the next checked
                # address is safe whatever the method, and costs no retry.
                if index + 1 < len(addresses):
                    index += 1
                    continue
                if not last:
                    attempt += 1
                    index = 0
                    continue
                raise WordPressError(
                    f"Could not connect to {host}: {type(exc).__name__}.",
                    "connection_failed",
                ) from None
            except httpx.TransportError as exc:
                if not idempotent:
                    raise self._outcome_unknown(method, route, type(exc).__name__) from None
                if not last:
                    attempt += 1
                    continue
                raise WordPressError(
                    f"The connection to {host} failed: {type(exc).__name__}.",
                    "connection_failed",
                ) from None
            # 501 is a refusal, not a failure: WordPress answers it for a trash
            # the type does not have, with nothing done.
            if response.status_code >= 500 and response.status_code != 501:
                if not idempotent and _wp_code(body) not in definite_codes:
                    raise self._outcome_unknown(method, route, f"HTTP {response.status_code}")
                if not last:
                    attempt += 1
                    continue
            try:
                return self._interpret(response, body), response.headers
            except WordPressError as exc:
                # A 2xx is a write that applied. If its answer cannot be read
                # (a PHP notice printed before the JSON), saying "bad_response"
                # would invite a second send; the lookup is the way forward.
                if not idempotent and 200 <= response.status_code < 300:
                    raise self._outcome_unknown(
                        method, route, f"HTTP {response.status_code}, {exc.reason}",
                    ) from None
                raise

    def get(self, route: str, *, params: dict | None = None, base: str | None = None):
        return self.request("GET", route, params=params, base=base)

    def _outcome_unknown(self, method: str, route: str, cause: str) -> WordPressError:
        return WordPressError(
            f"{method} {route} ended ambiguously ({cause}) after the request was "
            f"sent, so it may or may not have applied. It was not retried; look "
            f"the item up before trying again, and do not repeat the write blind.",
            "outcome_unknown",
        )

    # -- reading the answer ---------------------------------------------------

    def _interpret(self, response: httpx.Response, body: bytes) -> object:
        status = response.status_code
        if 300 <= status < 400:
            location = response.headers.get("location", "")
            raise WordPressError(
                f"The site answered HTTP {status}, redirecting to "
                f"{fence(location) or '(no location)'}. Redirects are not followed, "
                f"so nothing went further. If the site has moved, correct the URL in "
                f"its vault entry.",
                "host_refused", redirect=True, http_status=status,
            )
        data = None
        parsed = False
        if body.strip():
            try:
                data = json.loads(body)
                parsed = True
            except ValueError:
                parsed = False
        if 200 <= status < 300:
            if body.strip() and not parsed:
                raise WordPressError(
                    f"HTTP {status} with a body that is not JSON; is this a "
                    f"WordPress REST endpoint?",
                    "bad_response",
                )
            return data
        error = self._error_for(status, data if isinstance(data, dict) else {})
        error.wp_data = data if isinstance(data, dict) else {}
        raise error

    def _error_for(self, status: int, data: dict) -> WordPressError:
        code = data.get("code")
        code = code if isinstance(code, str) and _WP_CODE_RE.fullmatch(code) else None
        said = fence(data.get("message"))
        tail = f" WordPress said: {said}" if said else ""
        extra = {"http_status": status}
        if code:
            extra["wp_code"] = code

        if status == 401:
            return WordPressError(AUTH_FAILED_HELP + tail, "auth_failed", **extra)
        if status == 403:
            return WordPressError(
                f"The account may not do this (HTTP 403).{tail}", "permission_denied", **extra,
            )
        if status == 404:
            if code == "rest_no_route":
                return WordPressError(
                    f"The site has no such REST route.{tail}", "unknown_route", **extra,
                )
            return WordPressError(f"Not found (HTTP 404).{tail}", "not_found", **extra)
        if status == 400 and code in ("rest_invalid_param", "rest_missing_callback_param"):
            params = (data.get("data") or {}).get("params") if isinstance(data.get("data"), dict) else None
            names = params if isinstance(params, (dict, list)) else []
            # Field names come off the server; keep only identifier-shaped ones
            # so they can sit outside the fence.
            fields = sorted(str(k) for k in names if _FIELD_NAME_RE.fullmatch(str(k)))
            # The per-field reason ("acf[x] must be at most 3 characters long") is
            # what lets the model correct the value; the top message only says
            # "Invalid parameter(s)".
            field_errors = {}
            if isinstance(params, dict):
                for key in fields:
                    said_here = fence(params.get(key)) if isinstance(params.get(key), str) else ""
                    if said_here:
                        field_errors[key] = said_here
            if field_errors:
                detail = "".join(f" {key}: {msg}" for key, msg in field_errors.items())
                extra["field_errors"] = field_errors
            else:
                detail = tail
            return WordPressError(
                f"WordPress refused these fields: {', '.join(fields) or '(unnamed)'}.{detail}",
                "validation_error", fields=fields, **extra,
            )
        if status == 501:
            return WordPressError(f"The site does not support this (HTTP 501).{tail}",
                                  "request_refused", **extra)
        if status >= 500:
            return WordPressError(f"The site failed (HTTP {status}).{tail}", "server_error", **extra)
        return WordPressError(f"The site refused the request (HTTP {status}).{tail}",
                              "request_refused", **extra)


def selector(value):
    """A site-supplied selector, bare if it is identifier-shaped, fenced if not.

    Role slugs, capability names, REST namespaces and logins are chosen by
    plugins or by whoever registered, so one that could carry a sentence is
    fenced like any other site text.
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    text = str(value)
    return text if _SELECTOR_RE.fullmatch(text) else fence(text)


def selectors(values) -> list:
    return [selector(v) for v in values or [] if isinstance(v, str)]


def fence_tree(value, *, keep: frozenset[str] = frozenset()):
    """Every string in a JSON value fenced; keys never, and values under `keep` not.

    For content whose shape the skill does not know field by field: ACF values,
    registered meta, site settings, a generic route's body. Keys are field
    names the model has to echo back exactly, so they stay bare, and so does a
    value under a key in `keep` (``acf_fc_layout`` names a layout, a selector).
    """
    if isinstance(value, str):
        return fence(value)
    if isinstance(value, list):
        return [fence_tree(item, keep=keep) for item in value]
    if isinstance(value, dict):
        return {
            key: (item if key in keep and isinstance(item, str) else fence_tree(item, keep=keep))
            for key, item in value.items()
        }
    return value


def fence_keys(value, keys: frozenset[str]):
    """The values of `keys` fenced at any depth, everything else as it was.

    For a JSON Schema: ``title`` and ``description`` are field labels and
    instructions a site admin wrote, while types, enums and patterns are the
    contract the model must match exactly.
    """
    if isinstance(value, list):
        return [fence_keys(item, keys) for item in value]
    if isinstance(value, dict):
        return {
            key: (fence(item) if key in keys and isinstance(item, str) else fence_keys(item, keys))
            for key, item in value.items()
        }
    return value


def raw_text(value) -> str:
    """The editable text of a WordPress field: ``raw`` under ``context=edit``."""
    if isinstance(value, dict):
        for key in ("raw", "rendered"):
            text = value.get(key)
            if isinstance(text, str):
                return text
        return ""
    return value if isinstance(value, str) else ""
