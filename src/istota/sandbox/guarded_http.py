"""HTTP to a host somebody else chose, from the daemon's own network.

The address rule is `net_guard`'s, and this is the HTTP half beside it: a
request built against the address that was checked, so DNS cannot change its
answer between the check and the connect, and one send path that applies the
rule to every hop.

`pinned_request` builds that request: the URL's host replaced by the checked
address, the ``Host`` header and TLS SNI on the name. The `wordpress` and
`servarr` skill clients build theirs with it and keep their own send loops,
because both refuse redirects and have retry and ``outcome_unknown`` rules of
their own.

`guarded_send` is the whole path for the callers that have no such rules (the
shared-source fetchers: ICS feeds, OCM discovery and WebDAV):

- plain ``http`` only to a host on ``private_hosts`` unless the caller passes
  ``allow_plain_http`` (the RSS poller, whose user chose the scheme and whose
  request carries nothing the daemon adds), and a
  non-public address only for a listed host either way;
- each checked address in turn on a refused connection;
- no redirect unless ``max_redirects`` allows it, and then every hop checked
  from scratch: a hop may reach a non-public address only when its host is
  listed and is the original request's host, so a public feed cannot bounce
  the daemon onto an internal one, and a hop to another origin carries no
  ``auth`` and no ``Authorization`` header;
- the body read to at most ``max_bytes``.

Every refusal is a `GuardRefused` with one code from `CODES`, and its text is
the code alone: no URL, path, query or response text, since a feed URL and a
share secret are bearer credentials.

``trust_env=False`` on the client so a proxy variable cannot redirect the
traffic; the TLS trust store still honours ``SSL_CERT_FILE``.
"""

from __future__ import annotations

import base64
import time
from urllib.parse import urljoin, urlsplit

import httpx

from istota.sandbox import net_guard

CODES = ("host_refused", "too_large", "redirect_refused", "connection_failed", "timeout")

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Headers that described the bytes on the wire; the answer handed back carries
# the decoded body, so they no longer describe it.
_WIRE_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


class GuardRefused(Exception):
    """A refused or failed fetch, with its ``code`` and nothing else."""

    def __init__(self, code: str) -> None:
        if code not in CODES:
            raise ValueError(f"unknown guard code {code!r}")
        super().__init__(code)
        self.code = code


def new_client(*, timeout: float, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """A client for pinned requests: no proxy variables, no automatic redirects.

    `verify` is built with trust_env=True on its own: that reads only
    SSL_CERT_FILE / SSL_CERT_DIR, while the client's trust_env=False keeps
    proxy variables out. httpx couples the two otherwise.
    """
    verify = True if transport is not None else httpx.create_ssl_context(trust_env=True)
    return httpx.Client(
        transport=transport, trust_env=False, follow_redirects=False,
        verify=verify, timeout=timeout,
    )


def default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def host_header(host: str, port: int, scheme: str = "https") -> str:
    """The Host header, brackets on IPv6, the port only when not the scheme's default."""
    name = f"[{host}]" if ":" in host else host
    return name if port == default_port(scheme) else f"{name}:{port}"


def _without(headers: dict | None, names: set[str]) -> dict:
    return {key: value for key, value in (headers or {}).items() if key.lower() not in names}


def pinned_request(
    method: str,
    url: str,
    address: str,
    *,
    headers: dict | None = None,
    content: bytes | None = None,
    json: object = None,
    params: dict | None = None,
    auth: tuple[str, str] | None = None,
    timeout: float | None = None,
) -> httpx.Request:
    """``method url`` sent to ``address``, with the Host header and SNI on the name.

    The caller's headers go first, so they can never replace ``Host``, nor
    ``Authorization`` when ``auth`` (a username and password) is given.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = parts.hostname or ""
    port = parts.port or default_port(scheme)
    # `params` replaces the URL's own query, so only pass it when given.
    base = httpx.URL(url) if params is None else httpx.URL(url, params=params)
    target = base.copy_with(host=address, port=port)
    replaced = {"host"} | ({"authorization"} if auth is not None else set())
    merged = _without(headers, replaced)
    merged["Host"] = host_header(host, port, scheme)
    if auth is not None:
        credentials = f"{auth[0]}:{auth[1]}".encode()
        merged["Authorization"] = "Basic " + base64.b64encode(credentials).decode()
    if content is not None:
        request = httpx.Request(method, target, headers=merged, content=content)
    else:
        request = httpx.Request(method, target, headers=merged, json=json)
    extensions = dict(request.extensions or {})
    if scheme == "https":
        extensions["sni_hostname"] = host
    if timeout is not None:
        extensions["timeout"] = httpx.Timeout(timeout).as_dict()
    request.extensions = extensions
    return request


def _parse(url: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` of a URL the guard will send to, or a refusal."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise GuardRefused("host_refused") from None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise GuardRefused("host_refused")
    if parts.username is not None or parts.password is not None:
        raise GuardRefused("host_refused")
    return scheme, net_guard.normal_host(parts.hostname), port or default_port(scheme)


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise GuardRefused("timeout")
    return left


def _read_capped(response: httpx.Response, max_bytes: int, deadline: float) -> bytes:
    # A per-read timeout alone lets a server drip one byte inside it forever.
    buffer = bytearray()
    for chunk in response.iter_bytes():
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise GuardRefused("too_large")
        _remaining(deadline)
    return bytes(buffer)


def _send_pinned(client, method, url, addresses, *, headers, content, auth, timeout,
                 max_bytes, deadline) -> httpx.Response:
    last = "connection_failed"
    for address in addresses:
        request = pinned_request(method, url, address, headers=headers, content=content,
                                 auth=auth, timeout=min(timeout, _remaining(deadline)))
        try:
            response = client.send(request, stream=True, follow_redirects=False)
        except httpx.ConnectTimeout:
            last = "timeout"
            continue
        except httpx.ConnectError:
            last = "connection_failed"
            continue
        except httpx.TimeoutException:
            raise GuardRefused("timeout") from None
        except httpx.TransportError:
            raise GuardRefused("connection_failed") from None
        try:
            body = _read_capped(response, max_bytes, deadline)
        except httpx.TimeoutException:
            raise GuardRefused("timeout") from None
        except httpx.TransportError:
            raise GuardRefused("connection_failed") from None
        finally:
            response.close()
        kept = [(k, v) for k, v in response.headers.multi_items() if k.lower() not in _WIRE_HEADERS]
        return httpx.Response(response.status_code, headers=kept, content=body, request=request)
    raise GuardRefused(last)


def guarded_send(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    private_hosts,
    max_bytes: int,
    timeout: float,
    deadline_seconds: float,
    max_redirects: int = 0,
    auth: tuple[str, str] | None = None,
    headers: dict | None = None,
    content: bytes | None = None,
    resolve=None,
    allow_plain_http: bool = False,
) -> httpx.Response:
    """One request under the guard, redirects included; the answer, body read.

    A 3xx with a ``Location`` is followed while ``max_redirects`` allows,
    re-checked as a new request; past it, ``redirect_refused``. Any other
    answer, error statuses included, is returned for the caller to read.
    ``timeout`` bounds each connect and read; ``deadline_seconds`` bounds the
    whole call, every hop and the body included, and passing it is required.
    ``resolve`` replaces the system resolver, taking ``(host, port)``.
    """
    deadline = time.monotonic() + deadline_seconds
    listed = net_guard.host_set(private_hosts)
    method = method.upper()
    origin = _parse(url)
    carry_auth = True
    hops = 0
    while True:
        scheme, host, port = _parse(url)
        # Plain http, a first request or an https-to-http hop alike, goes only
        # to a listed host, unless the caller allows it for user-chosen URLs.
        if scheme == "http" and host not in listed and not allow_plain_http:
            raise GuardRefused("host_refused")
        allowed_private = listed if hops == 0 else listed & {origin[1]}
        addresses, refused = net_guard.checked_addresses(
            host, port, private_hosts=allowed_private, resolve=resolve,
        )
        if refused is not None:
            raise GuardRefused("host_refused")
        if not addresses:
            raise GuardRefused("connection_failed")
        carry_auth = carry_auth and (scheme, host, port) == origin
        hop_headers = headers if carry_auth else _without(headers, {"authorization"})
        response = _send_pinned(
            client, method, url, addresses, headers=hop_headers, content=content,
            auth=auth if carry_auth else None, timeout=timeout, max_bytes=max_bytes,
            deadline=deadline,
        )
        location = response.headers.get("location")
        if response.status_code not in _REDIRECT_STATUSES or not location:
            return response
        if hops >= max_redirects:
            raise GuardRefused("redirect_refused")
        hops += 1
        url = urljoin(url, location)
        if response.status_code == 303 or (
            response.status_code in (301, 302) and method not in ("GET", "HEAD")
        ):
            method = "GET"
            content = None
