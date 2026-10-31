"""The origin and host checks and the bounded body reader, for the two web apps.

``web_app`` and the first-run setup server both need them, and the setup
server must run with no config, so it cannot import ``web_app`` (which loads
config at import). Each app keeps its own policy around these: ``web_app``
compares the origin with the external origin ``[site] hostname`` gives, or in
no-auth mode with the loopback names; the setup server compares it with the
request's own ``Host``.
The ``Host`` check is ``web_app``'s alone, on an install with local login
codes or no-auth.

Stdlib only at import; Starlette's ``ClientDisconnect`` is imported where it
is caught.
"""

from __future__ import annotations

from urllib.parse import urlsplit


class BodyError(Exception):
    """A request body refused before it was used, with the status to answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


_DEFAULT_PORTS = {"http": 80, "https": 443}


def origin_key(url: str) -> tuple[str, str, int] | None:
    """A web URL's scheme, lowercased hostname and effective port, or None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    default = _DEFAULT_PORTS.get(parts.scheme)
    if default is None or not parts.hostname:
        return None
    return parts.scheme, parts.hostname, default if port is None else port


def published_authority(hostname: str, request_host: str) -> str:
    """The authority a plain-http stack answers at: ``hostname``, with a port.

    A portless hostname does not say where `local` ingress published (8080 by
    default), and a page cannot forge ``Host``, so a ``Host`` with the same name
    supplies the port. A page on a sibling port still sends its own in Origin.
    """
    try:
        configured = urlsplit(f"//{hostname}")
        if configured.port is not None or not configured.hostname:
            return hostname
    except ValueError:
        return hostname
    seen = origin_key(f"http://{request_host}")
    return request_host if seen is not None and seen[1] == configured.hostname else hostname


def origin_refusal(headers, authority: str, scheme: str | None = None) -> str | None:
    """Why the request's ``Origin`` (or ``Referer``) is not ``authority``.

    None when it is. Scheme, name and effective port are all compared, because
    a browser's origin is all three. With no ``scheme`` the request's own is
    taken, which still fixes the default port when ``authority`` names none.
    """
    origin = headers.get("origin") or headers.get("referer")
    if not origin:
        return "missing origin"
    seen = origin_key(origin)
    if seen is None or seen != origin_key(f"{scheme or seen[0]}://{authority}"):
        return "origin mismatch"
    return None


def origin_authority_refusal(headers, allowed: frozenset[str]) -> str | None:
    """Why the request's ``Origin`` (or ``Referer``) authority is not one of
    ``allowed``, compared like ``host_refusal``: exact, port included."""
    origin = headers.get("origin") or headers.get("referer")
    if not origin:
        return "missing origin"
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or parts.netloc.lower() not in allowed:
        return "origin mismatch"
    return None


def cross_site_refusal(headers) -> str | None:
    """Why a browser request from another site is refused, or None.

    Reads Fetch Metadata. ``same-site`` is refused too, because a site ignores
    the port: a page on another loopback port sends it. A top-level navigation
    is allowed, and so is a request with no ``Sec-Fetch-Site``, which is what
    curl and older browsers send.
    """
    site = (headers.get("sec-fetch-site") or "").lower()
    if site not in ("cross-site", "same-site"):
        return None
    if (headers.get("sec-fetch-mode") or "").lower() == "navigate" and (
        headers.get("sec-fetch-dest") or ""
    ).lower() == "document":
        return None
    return "cross-site request"


def host_refusal(headers, allowed: frozenset[str]) -> str | None:
    """Why the request's ``Host`` is not one of ``allowed``, or None when it is.

    Against DNS rebinding: a page on another name that resolves to loopback
    reaches the listener with its own name in ``Host``. The comparison is
    exact, port included, and case-insensitive.
    """
    host = (headers.get("host") or "").strip().lower()
    if not host:
        return "missing host"
    if host not in allowed:
        return "unknown host"
    return None


async def read_bounded_body(request, limit: int, *, require_length: bool = True) -> bytes:
    """Read the request body, refusing before it exists in memory.

    Native handoff forms opt into a missing length with ``require_length=False``;
    the running cap still applies. Existing upload callers require the header.

    Two checks, and neither substitutes for the other. The declared length is
    what lets the refusal happen before a byte is read; the running total is
    the enforcement, because the declared length is a claim. The only upstream
    bound is nginx's `client_max_body_size`, which the deployment renders from
    the chat attachment limit and sets to 100 MB — so a cap checked on
    `len(await file.read())` materializes up to that per concurrent request
    before refusing.
    """
    from starlette.requests import ClientDisconnect

    declared = request.headers.get("content-length")
    try:
        length = 0 if declared is None and not require_length else int(declared)
    except (TypeError, ValueError):
        # A multipart upload from a browser always sets it.
        raise BodyError(413, "the upload must declare its length") from None
    if length < 0 or length > limit:
        raise BodyError(413, "that upload is too large")

    buf = bytearray()
    try:
        async for chunk in request.stream():
            if len(buf) + len(chunk) > limit:
                # The rest of the stream is not read.
                raise BodyError(413, "that upload is too large")
            buf += chunk
    except ClientDisconnect:
        # An upload the user cancels in the picker, or one that dies on a
        # mobile connection, is the ordinary case rather than the exotic one.
        # Nothing reaches the caller either way; converting it keeps a
        # traceback per aborted upload out of the log.
        raise BodyError(400, "the upload was interrupted") from None
    return bytes(buf)
