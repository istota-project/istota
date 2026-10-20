"""The HTTP client every servarr verb goes through, and its six guards.

This runs host-side, spawned by the skill proxy outside the sandbox, with the
daemon's network and nothing confining its egress. The URL and the key come
from the same operator-written table (`[servarr.apps.<app>]`), so there is no
vault binding to check, unlike `skills/wordpress/client.py`, of which this is a
reduced copy. Every request passes, in order:

1. **A fixed target.** The URL is the configured base, the app's API prefix and
   a path of fixed lowercase segments with at most one trailing id; anything
   else is refused before a byte is sent. Query values are encoded by httpx.
   Ids from argv are validated by `library_id` and `mbid` before use.
2. **Scheme and address.** HTTPS to a host whose addresses are all public
   (`sandbox.net_guard.ip_is_public`); plain HTTP, or a non-public address,
   only for a host the operator listed in ``[servarr] private_hosts``, matched
   exactly. The connection goes to the checked address with the Host header
   (and SNI) on the name, so DNS cannot change its answer in between.
3. **No redirects.** ``follow_redirects=False``; a 3xx is refused naming its
   target, so the key is never replayed elsewhere.
4. **The retry policy.** A local ``GET`` is retried once on a 5xx or a refused
   connection, never on a timeout. Lookups and release searches are never
   retried. ``POST`` and ``PUT`` are sent once, and an ending after the body
   went out is ``outcome_unknown`` with the caller's lookup hint. No other
   method is sent at all, which is the behavioural form of the no-deletes rule.
5. **One connection failure per app.** After an app's first failure to
   connect, every later call to it in the same run fails the same way without
   connecting: a dropped connection hangs rather than refusing.
6. **The run's deadline.** Each request's timeout is the smaller of its own and
   the time left; one that cannot start before the deadline is ``time_budget``.

The first call to an app reads ``system/status`` and refuses below the
supported major version. The key goes into the ``X-Api-Key`` header only, never
a query string, and appears in no message, ``repr`` or fenced excerpt.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx

from istota import __version__
from istota.lib.untrusted import frame_untrusted
from istota.sandbox import net_guard
from istota.sandbox.net_guard import ip_is_public, resolve_host
from istota.skills._cli import error_envelope

from .apps import APPS

log = logging.getLogger(__name__)

LABEL = "SERVARR CONTENT"
#: Local API calls answer from the app's own database in milliseconds.
LOCAL_TIMEOUT = 10.0
#: A request with less than this left before the run's deadline is not started.
MIN_START_SECONDS = 1.0
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
EXCERPT_CHARS = 500
USER_AGENT = f"istota-servarr/{__version__}"

_METHODS = frozenset({"GET", "POST", "PUT"})
_KINDS = ("local", "lookup", "release")
#: Match on the validator's code, never on its message (which is translated).
EXISTS_CODES = frozenset({"MovieExistsValidator", "SeriesExistsValidator", "ArtistExistsValidator",
                          "AlbumExistsValidator"})

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ID_RE = re.compile(r"\A[1-9][0-9]{0,17}\Z")
_UUID_RE = re.compile(rf"\A{_UUID}\Z")
_PATH_RE = re.compile(rf"\A[a-z]+(?:/[a-z]+)*(?:/(?:[1-9][0-9]{{0,17}}|{_UUID}))?\Z")
_VERSION_RE = re.compile(r"\A(\d+)\.")
#: A name the server returns that the model has to echo back exactly (a
#: profile, a root folder, a health check's class): bare when it has this
#: shape, fenced when it does not. No brackets, so it cannot forge a marker.
_SELECTOR_RE = re.compile(r"\A[A-Za-z0-9 _.:/()+,&'-]{1,100}\Z")


class ServarrError(Exception):
    """A refusal with its ``reason`` code and any extra envelope fields."""

    def __init__(self, message: str, reason: str, **extra) -> None:
        super().__init__(message)
        self.reason = reason
        self.extra = extra


def envelope_for(exc: ServarrError) -> dict:
    """The error envelope the CLI prints for a refusal."""
    return error_envelope(str(exc), reason=exc.reason, **exc.extra)


def fence(text: object) -> str:
    """Server-authored text, between the servarr markers."""
    return frame_untrusted(text, LABEL)


def selector(value):
    """A server-supplied name, bare if name-shaped, fenced otherwise."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text if _SELECTOR_RE.fullmatch(text) else fence(text)


def as_rows(data, app: str, what: str) -> list[dict]:
    """A list answer's objects, or ``bad_response`` when it is not a list."""
    if not isinstance(data, list):
        raise ServarrError(f"{app} answered {what} with something other than a list.",
                           "bad_response", app=app)
    return [row for row in data if isinstance(row, dict)]


def number(value):
    """An id, count or size off the wire: kept when numeric, else None."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def library_id(value) -> int:
    """An arr library id from argv: a positive integer, or a refusal."""
    text = str(value).strip()
    if not _ID_RE.fullmatch(text):
        raise ServarrError(f"{value!r} is not a library id (a positive whole number).",
                           "invalid_id")
    return int(text)


def external_id(value, flag: str) -> int:
    """A TMDB or TVDB id from argv (`flag` names it): a positive integer."""
    text = str(value).strip()
    if not _ID_RE.fullmatch(text):
        raise ServarrError(f"{flag} {value!r} is not an id (a positive whole number); "
                           f"nothing was sent.", "invalid_id", flag=flag)
    return int(text)


def mbid(value) -> str:
    """A MusicBrainz id from argv: a UUID, lowercased, or a refusal."""
    text = str(value).strip().lower()
    if not _UUID_RE.fullmatch(text):
        raise ServarrError(f"{value!r} is not a MusicBrainz id (a UUID).", "invalid_id")
    return text


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def private_set(hosts) -> frozenset:
    """``[servarr] private_hosts`` as the exact, lowercased names guard 2 matches."""
    return net_guard.host_set(hosts)


def url_problem(base_url: str) -> str:
    """Why a configured URL can take no request at all, as a phrase, or ""."""
    try:
        parts = urlsplit(base_url)
        parts.port  # raises on an out-of-range or non-numeric port
    except ValueError:
        return "is not a valid URL"
    if (parts.scheme or "").lower() not in ("http", "https") or not parts.hostname:
        return "must be http(s) with a host"
    if parts.username is not None or parts.password is not None:
        return "carries a user name or password; remove it"
    if parts.query or parts.fragment:
        return "carries a query or fragment"
    return ""


def plain_http_refused(scheme: str, host: str, private: frozenset) -> bool:
    """Plain HTTP goes only to a host the operator listed in `private_hosts`."""
    return scheme == "http" and net_guard.normal_host(host) not in private


def config_problem(app: str, base_url: str, private_hosts) -> str:
    """What stops `app`'s configured URL before any request, or "".

    The checks guard 2 makes before resolving anything, an IP literal's
    address included, for doctor and the admin card. The text names the config key, never a credential.
    """
    problem = url_problem(base_url)
    if problem:
        return f"[servarr.apps.{app}] url {problem}"
    parts = urlsplit(base_url)
    private = private_set(private_hosts)
    if plain_http_refused(parts.scheme.lower(), parts.hostname, private):
        return (f"[servarr.apps.{app}] url is plain HTTP to {parts.hostname}, which is not "
                f"in [servarr] private_hosts")
    host = net_guard.normal_host(parts.hostname)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and host not in private and not ip_is_public(literal):
        return (f"[servarr.apps.{app}] url is {host}, a private address, which is not in "
                f"[servarr] private_hosts")
    return ""


def _host_header(host: str, port: int, scheme: str) -> str:
    """The Host header, brackets on IPv6, the port only when not the default."""
    name = f"[{host}]" if ":" in host else host
    return name if port == _default_port(scheme) else f"{name}:{port}"


class ArrClient:
    """One app of the connection, for one CLI run."""

    def __init__(
        self,
        *,
        app: str,
        base_url: str,
        api_key: str,
        private_hosts=(),
        transport: httpx.BaseTransport | None = None,
        resolve: Callable[[str, int], list[str]] | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        lookup_timeout: float = 30.0,
        release_timeout: float = 120.0,
    ) -> None:
        self.app = app
        self._kind = APPS[app]
        self.base_url = base_url
        self._key = api_key or ""
        self._private = private_set(private_hosts)
        self._resolve = resolve or resolve_host
        self._deadline = deadline
        self._clock = clock
        self._timeouts = {"local": LOCAL_TIMEOUT, "lookup": float(lookup_timeout),
                          "release": float(release_timeout)}
        self._failure: ServarrError | None = None
        self._version_error: ServarrError | None = None
        self.version: str | None = None
        # `verify` is built with trust_env=True on its own: that reads only
        # SSL_CERT_FILE / SSL_CERT_DIR, while the client's trust_env=False keeps
        # proxy variables out. httpx couples the two otherwise.
        verify = True if transport is not None else httpx.create_ssl_context(trust_env=True)
        self._http = httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                                  verify=verify, timeout=LOCAL_TIMEOUT)

    def __repr__(self) -> str:
        return f"ArrClient({self.app})"

    def close(self) -> None:
        self._http.close()

    def _error(self, message: str, reason: str, **extra) -> ServarrError:
        return ServarrError(message, reason, app=self.app, **extra)

    # -- guards 1 and 2: the target ------------------------------------------

    def _target(self) -> tuple[str, str, str, int, str]:
        """``(scheme, netloc, host, port, prefix)`` of the configured URL, checked."""
        problem = url_problem(self.base_url)
        if problem:
            raise self._error(f"[servarr.apps.{self.app}] url {problem}; nothing was sent.",
                              "config_invalid")
        parts = urlsplit(self.base_url)
        scheme = parts.scheme.lower()
        return (scheme, parts.netloc, parts.hostname, parts.port or _default_port(scheme),
                parts.path.rstrip("/"))

    def _checked_addresses(self, scheme: str, host: str, port: int) -> list[str]:
        name = net_guard.normal_host(host)
        listed = name in self._private
        if plain_http_refused(scheme, name, self._private):
            raise self._error(
                f"{self.app} is configured over plain HTTP to {name}, which is not in "
                f"[servarr] private_hosts. Plain HTTP is allowed only to a listed host; "
                f"nothing was sent.",
                "host_refused",
            )
        addresses = net_guard.host_addresses(host, port, resolve=self._resolve)
        if not addresses:
            failure = self._error(f"Could not resolve {name} for {self.app}; nothing was sent.",
                                  "connection_failed")
            self._failure = failure
            raise failure
        if not listed:
            address = net_guard.first_non_public(addresses)
            if address is not None:
                raise self._error(
                    f"{name} resolves to a private or reserved address ({address}). Only "
                    f"a host the operator lists in [servarr] private_hosts may; nothing "
                    f"was sent.",
                    "host_refused",
                )
        return [str(a) for a in addresses]

    # -- guard 6: the deadline -----------------------------------------------

    def _timeout(self, kind: str) -> float:
        timeout = self._timeouts[kind]
        if self._deadline is None:
            return timeout
        left = self._deadline - self._clock()
        if left < MIN_START_SECONDS:
            raise self._error(
                f"Out of time for this call before reaching {self.app}; nothing more was sent.",
                "time_budget",
            )
        return min(timeout, left)

    # -- the one request path -------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: object = None,
        kind: str = "local",
        lookup: str | None = None,
        answer_on: tuple[int, ...] = (),
        retry: bool = True,
    ):
        """One call to ``{base}{api}/{path}``; the parsed JSON body, or None.

        `kind` is ``local``, ``lookup`` (a ``*/lookup`` that reaches a metadata
        service) or ``release`` (an interactive search across indexers); the
        last two get their own timeouts and are never retried. `lookup` is the
        command a caller hands to ``outcome_unknown`` so the model can find out
        whether an ambiguous write applied. `answer_on` names error statuses
        whose JSON body is the answer itself, as prowlarr's ``indexer/testall``
        answers 400 with its per-indexer results when any indexer fails.
        ``retry=False`` drops guard 4's one retry of a local ``GET``.
        """
        method = method.upper()
        if method not in _METHODS:
            raise self._error(f"{method} is not a method this skill sends; nothing was sent.",
                              "method_refused")
        if kind not in _KINDS:
            raise ValueError(f"unknown request kind {kind!r}")
        if not isinstance(path, str) or not _PATH_RE.fullmatch(path):
            raise self._error("Refused a request path outside the fixed API shape; nothing "
                              "was sent.", "invalid_path")
        if self._failure is not None:
            raise ServarrError(str(self._failure), self._failure.reason, **self._failure.extra)
        if path != "system/status":
            self._ensure_version()
        return self._send_with_policy(method, path, params, json, kind, lookup,
                                      frozenset(answer_on), retry)

    def system_status(self, *, retry: bool = True) -> dict:
        """``GET system/status``, with the version check applied to the answer.

        ``retry=False`` sends it once whatever the answer, for doctor's probe.
        """
        data = self.request("GET", "system/status", retry=retry)
        self._check_version(data)
        return data

    def _ensure_version(self) -> None:
        if self._version_error is not None:
            raise ServarrError(str(self._version_error), self._version_error.reason,
                               **self._version_error.extra)
        if self.version is None:
            self.system_status()

    def _check_version(self, data) -> None:
        version = data.get("version") if isinstance(data, dict) else None
        match = _VERSION_RE.match(version) if isinstance(version, str) else None
        if match is None:
            raise self._error(f"{self.app} did not report a version this skill can read.",
                              "bad_response")
        minimum = self._kind.min_major
        if int(match.group(1)) < minimum:
            self._version_error = self._error(
                f"{self.app} {version} is older than this skill supports ({self.app} "
                f"{minimum} or newer); nothing else was sent to it.",
                "unsupported_version", version=version, minimum=minimum,
            )
            raise self._version_error
        self.version = version

    def _send_with_policy(self, method, path, params, body, kind, lookup, answer_on,
                          retry=True):
        scheme, netloc, host, port, prefix = self._target()
        addresses = self._checked_addresses(scheme, host, port)
        url = f"{scheme}://{netloc}{prefix}{self._kind.api}/{path}"
        writes = method != "GET"
        attempts = 2 if (retry and not writes and kind == "local") else 1
        attempt = 0
        index = 0
        while True:
            last = attempt + 1 >= attempts
            timeout = self._timeout(kind)
            request = self._build(method, url, scheme, host, port, addresses[index],
                                  params, body, timeout)
            try:
                response, raw = self._send(request)
            except ServarrError as exc:
                if writes:
                    raise self._outcome_unknown(method, path, exc.reason, lookup) from None
                raise
            except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                # A dropped connection: no retry, and the app is not tried again.
                raise self._connection_failed(host, port, type(exc).__name__) from None
            except httpx.ConnectError as exc:
                # Nothing reached the server, so the next checked address is safe
                # whatever the method, and costs no retry.
                if index + 1 < len(addresses):
                    index += 1
                    continue
                if not last:
                    attempt += 1
                    index = 0
                    continue
                raise self._connection_failed(host, port, type(exc).__name__) from None
            except httpx.TransportError as exc:
                if writes:
                    raise self._outcome_unknown(method, path, type(exc).__name__,
                                                lookup) from None
                raise self._error(
                    f"The request to {self.app} failed after connecting: "
                    f"{type(exc).__name__}. It was not retried.",
                    "connection_failed",
                ) from None
            if response.status_code >= 500 and not last:
                attempt += 1
                continue
            try:
                return self._interpret(response, raw, answer_on)
            except ServarrError as exc:
                # A 2xx is a write that applied; an answer that cannot be read
                # must not invite a second send.
                if writes and 200 <= response.status_code < 300:
                    raise self._outcome_unknown(method, path, exc.reason, lookup) from None
                raise

    def _build(self, method, url, scheme, host, port, address, params, body,
               timeout) -> httpx.Request:
        target = httpx.URL(url, params=params).copy_with(host=address, port=port)
        headers = {
            "Host": _host_header(host, port, scheme),
            "X-Api-Key": self._key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if body is None:
            request = httpx.Request(method, target, headers=headers)
        else:
            request = httpx.Request(method, target, headers=headers, json=body)
        extensions = dict(request.extensions or {})
        if scheme == "https":
            extensions["sni_hostname"] = host
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
                    raise self._error(
                        f"{self.app}'s answer was larger than "
                        f"{MAX_RESPONSE_BYTES // (1024 * 1024)} MiB and was not read.",
                        "response_too_large",
                    )
            return response, bytes(buffer)
        finally:
            response.close()

    def _connection_failed(self, host: str, port: int, cause: str) -> ServarrError:
        name = f"[{host}]" if ":" in host else host
        failure = self._error(
            f"Could not connect to {self.app} at {name}:{port} ({cause}). The app may be "
            f"down, or a firewall (such as a Docker host's DOCKER-USER allowlist) may be "
            f"dropping this host. Nothing more will be sent to {self.app} in this call.",
            "connection_failed",
        )
        self._failure = failure
        return failure

    def _outcome_unknown(self, method: str, path: str, cause: str,
                         lookup: str | None) -> ServarrError:
        extra = {"lookup": lookup} if lookup else {}
        return self._error(
            f"{method} {path} to {self.app} ended ambiguously ({cause}) after the request "
            f"was sent, so it may or may not have applied. It was not retried; look the "
            f"item up before trying again, and do not repeat the write blind.",
            "outcome_unknown", **extra,
        )

    # -- reading the answer ---------------------------------------------------

    def _said(self, text: str, limit: int | None = None) -> str:
        """Server text, with the key removed in case it was echoed, cut to
        `limit`, then fenced. Redacting before the cut keeps an echoed key that
        crosses it from leaving its head behind."""
        if self._key:
            text = text.replace(self._key, "<redacted>")
        if limit is not None:
            text = text[:limit]
        return fence(text)

    def _interpret(self, response: httpx.Response, raw: bytes,
                   answer_on: frozenset = frozenset()):
        status = response.status_code
        if 300 <= status < 400:
            location = response.headers.get("location", "")
            raise self._error(
                f"{self.app} answered HTTP {status}, redirecting to "
                f"{self._said(location) or '(no location)'}. Redirects are not followed, so "
                f"the key went nowhere else. If the app has moved, the operator corrects "
                f"[servarr.apps.{self.app}] url.",
                "redirect_refused", http_status=status,
            )
        data = None
        parsed = False
        if raw.strip():
            try:
                data = json.loads(raw)
                parsed = True
            except ValueError:
                parsed = False
        if 200 <= status < 300:
            if raw.strip() and not parsed:
                raise self._error(f"{self.app} answered HTTP {status} with a body that is "
                                  f"not JSON.", "bad_response", http_status=status)
            return data
        if status in answer_on:
            if not parsed:
                raise self._error(f"{self.app} answered HTTP {status} with a body that is "
                                  f"not JSON.", "bad_response", http_status=status)
            return data
        if status == 401:
            raise self._error(
                f"{self.app} refused the API key (HTTP 401). The key in "
                f"[servarr.apps.{self.app}] is wrong or was rotated; tell the operator.",
                "unauthorized", http_status=status,
            )
        if status == 404:
            raise self._error(f"{self.app} has no such item (HTTP 404).", "not_found",
                              http_status=status)
        if status in (400, 409) and parsed and _is_validation_list(data):
            codes = [item.get("errorCode") for item in data]
            exists = next((c for c in codes if c in EXISTS_CODES), None)
            if exists:
                raise self._error(f"{self.app} already has this item.", "already_present",
                                  http_status=status, error_code=exists)
            problems = [
                {"property": selector(item.get("propertyName")),
                 "message": self._said(str(item.get("errorMessage") or ""))}
                for item in data
            ]
            raise self._error(f"{self.app} refused the request: see problems.", "rejected",
                              http_status=status, problems=problems)
        excerpt = self._said(raw.decode("utf-8", errors="replace"), EXCERPT_CHARS)
        raise self._error(f"{self.app} answered HTTP {status}.", "api_error",
                          http_status=status, body=excerpt)


def _is_validation_list(data) -> bool:
    """Whether a body is the arrs' validation failure shape: a list of objects
    each naming a property and an error message."""
    return (
        isinstance(data, list) and bool(data)
        and all(isinstance(item, dict) and "errorMessage" in item for item in data)
    )
