"""The origin check and the bounded body reader, shared by the two web apps.

``web_app`` and the first-run setup server both need them, and the setup
server must run with no config, so it cannot import ``web_app`` (which loads
config at import). Each app keeps its own policy around these: ``web_app``
compares the origin with ``[site] hostname`` and skips the check in no-auth
mode; the setup server compares it with the request's own ``Host``.

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


def _hostname(authority: str) -> str | None:
    # urlsplit takes the brackets off an IPv6 literal and lowercases the name,
    # which is what the Origin side gets too.
    return urlsplit(f"//{authority}").hostname


def origin_refusal(headers, hostname: str) -> str | None:
    """Why the request's ``Origin`` (or ``Referer``) does not name ``hostname``.

    None when it does. The port is not compared: a published port and the
    port the app listens on differ on every deployment shape.
    """
    origin = headers.get("origin") or headers.get("referer")
    if not origin:
        return "missing origin"
    if urlsplit(origin).hostname != _hostname(hostname):
        return "origin mismatch"
    return None


async def read_bounded_body(request, limit: int) -> bytes:
    """Read the request body, refusing before it exists in memory.

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
        length = int(declared)
    except (TypeError, ValueError):
        # A multipart upload from a browser always sets it.
        raise BodyError(413, "the upload must declare its length") from None
    if length < 0 or length > limit:
        raise BodyError(413, "that upload is too large")

    buf = bytearray()
    try:
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > limit:
                # The rest of the stream is not read.
                raise BodyError(413, "that upload is too large")
    except ClientDisconnect:
        # An upload the user cancels in the picker, or one that dies on a
        # mobile connection, is the ordinary case rather than the exotic one.
        # Nothing reaches the caller either way; converting it keeps a
        # traceback per aborted upload out of the log.
        raise BodyError(400, "the upload was interrupted") from None
    return bytes(buf)
