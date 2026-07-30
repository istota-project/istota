"""RFC 2047 encoding for ntfy HTTP header values.

httpx serializes header values as ASCII, so a title like ``📈 NOK BUY ↑``
raises ``UnicodeEncodeError`` and the notification never leaves — the body is
UTF-8 and fine, it's only the headers that are fragile (ISSUE-213). ntfy
decodes RFC 2047 encoded-words (``=?UTF-8?B?…?=``) in any header back to the
original glyphs, so encoding is lossless where stripping is not.

Shared by the ntfy transport (``istota.transport.ntfy``) and the sandboxed ntfy
skill CLI (``istota.skills.ntfy``). It lives at the top level, stdlib-only, so
the skill subprocess doesn't import the whole transport package (registry, Talk
client, email) just to sanitize a header.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator

__all__ = [
    "MAX_ENCODED_WORD_CHARS",
    "ascii_header_value",
    "encode_header_value",
    "scrub_header_value",
]

_PREFIX = "=?UTF-8?B?"
_SUFFIX = "?="

# RFC 2047 caps an encoded-word at 75 characters.
MAX_ENCODED_WORD_CHARS = 75

# Prefix + suffix cost 12 characters, and base64 output is a multiple of 4, so
# 60 base64 characters fit — which is 45 raw UTF-8 bytes.
_MAX_CHUNK_BYTES = 45


def scrub_header_value(value: str) -> str:
    """Drop CR and fold LF to a space — no header injection, whatever we do next."""
    return value.replace("\r", "").replace("\n", " ")


def _chunks(value: str) -> Iterator[bytes]:
    """Split on character boundaries into pieces that fit one encoded-word."""
    buffer = b""
    for char in value:
        encoded = char.encode("utf-8", errors="replace")
        if buffer and len(buffer) + len(encoded) > _MAX_CHUNK_BYTES:
            yield buffer
            buffer = b""
        buffer += encoded
    if buffer:
        yield buffer


def encode_header_value(value: str) -> str:
    """Return an ASCII-safe header value, RFC 2047-encoding it if it isn't already.

    Pure-ASCII values pass through untouched (only scrubbed), so the common case
    stays readable on the wire. Anything else becomes one or more encoded-words
    joined by a space — RFC 2047 says whitespace between adjacent encoded-words
    is dropped on decode, so the original spacing survives inside the payloads.

    Total by construction: encoding uses ``errors="replace"``, so even a lone
    surrogate yields a valid header rather than costing us the notification.
    """
    scrubbed = scrub_header_value(value)
    if scrubbed.isascii():
        return scrubbed
    return " ".join(
        _PREFIX + base64.b64encode(chunk).decode("ascii") + _SUFFIX
        for chunk in _chunks(scrubbed)
    )


def ascii_header_value(value: str) -> str:
    """Lossy last resort: drop every non-ASCII character and collapse whitespace.

    Only for the fallback path — if a header somehow still fails to serialize,
    a flattened title beats losing the message body. May return "", in which
    case the caller should omit the header entirely.
    """
    stripped = scrub_header_value(value).encode("ascii", errors="ignore").decode("ascii")
    return " ".join(stripped.split())
