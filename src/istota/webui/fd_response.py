"""A download served from a file descriptor the route already opened.

Starlette's `FileResponse` takes a path and opens it again when the response
is sent, so a route that confined the file with a no-follow walk would hand
the check's result to a second, unchecked resolution of the same name. This
streams the open descriptor instead, with the headers `FileResponse` sets
for a plain GET: length, last-modified and etag from `fstat`, and the same
`Content-Disposition` spelling.

A route that passes the request's headers also gets one byte range, which a
native media player needs to seek (`/chat/files`, ISSUE-776). Only a single
range is served: a malformed or multi-range request gets the whole file,
which RFC 9110 permits, and one starting past the end is a 416.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Mapping
from email.utils import formatdate
from urllib.parse import quote

from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

_CHUNK = 64 * 1024

UNSATISFIABLE = "unsatisfiable"

_MAX_OFFSET_DIGITS = 20


def content_disposition(disposition_type: str, filename: str) -> str:
    """`FileResponse`'s spelling: a quoted name, or RFC 5987 when it needs escaping."""
    quoted = quote(filename)
    if quoted != filename:
        return f"{disposition_type}; filename*=utf-8''{quoted}"
    return f'{disposition_type}; filename="{filename}"'


def stat_headers(info: os.stat_result) -> dict[str, str]:
    etag_base = f"{info.st_mtime}-{info.st_size}"
    etag = hashlib.md5(etag_base.encode(), usedforsecurity=False).hexdigest()
    return {
        "content-length": str(info.st_size),
        "last-modified": formatdate(info.st_mtime, usegmt=True),
        "etag": f'"{etag}"',
    }


def byte_range(header: str, size: int) -> tuple[int, int] | str | None:
    """The inclusive ``(first, last)`` one ``Range`` header asks for.

    None for anything not served (malformed, another unit, several ranges),
    which means the whole file; `UNSATISFIABLE` for a range starting at or
    past the end, or a zero-length suffix.
    """
    unit, sep, spec = header.partition("=")
    if not sep or unit.strip().lower() != "bytes" or "," in spec:
        return None
    first_text, sep, last_text = spec.partition("-")
    first_text, last_text = first_text.strip(), last_text.strip()
    if not sep or not (first_text or last_text):
        return None
    if (first_text and not first_text.isdecimal()) or (last_text and not last_text.isdecimal()):
        return None
    # `int()` refuses more than 4300 digits; no file offset needs 20.
    if len(first_text) > _MAX_OFFSET_DIGITS or len(last_text) > _MAX_OFFSET_DIGITS:
        return None
    if not first_text:
        suffix = int(last_text)
        if suffix == 0 or size == 0:
            return UNSATISFIABLE
        return max(0, size - suffix), size - 1
    first = int(first_text)
    if last_text and int(last_text) < first:
        return None
    if first >= size:
        return UNSATISFIABLE
    last = int(last_text) if last_text else size - 1
    return first, min(last, size - 1)


def fd_file_response(
    fd: int,
    *,
    filename: str,
    media_type: str,
    disposition_type: str = "attachment",
    headers: Mapping[str, str] | None = None,
    request_headers: Mapping[str, str] | None = None,
) -> Response:
    """Stream the regular file open at `fd`; the response takes ownership of it.

    The body is read at explicit offsets, so the fd's position does not
    matter (a route may have read a head through it first). At most the
    `fstat` size is sent, so the body matches `Content-Length` even if the
    file grows meanwhile. The descriptor is closed when the body ends, again
    by the background task (a second close is a no-op), and by the file
    object's finalizer if a disconnect abandons the body.

    `request_headers` (lowercase keys, or Starlette's `Headers`) turns Range
    on: ``Accept-Ranges`` is sent, and a ``Range`` is served as a 206 while
    ``If-Range``, when present, still equals the etag or last-modified.
    """
    try:
        info = os.fstat(fd)
        file = os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise
    size = info.st_size
    response_headers = {
        **stat_headers(info),
        "content-disposition": content_disposition(disposition_type, filename),
    }
    status_code = 200
    first, last = 0, size - 1
    if request_headers is not None:
        response_headers["accept-ranges"] = "bytes"
        asked = request_headers.get("range")
        if_range = request_headers.get("if-range")
        if if_range is not None and if_range not in (
            response_headers["etag"], response_headers["last-modified"],
        ):
            asked = None
        wanted = byte_range(asked, size) if asked is not None else None
        if wanted == UNSATISFIABLE:
            file.close()
            return Response(
                status_code=416,
                headers={**(headers or {}), "content-range": f"bytes */{size}"},
            )
        if wanted is not None:
            first, last = wanted
            status_code = 206
            response_headers["content-range"] = f"bytes {first}-{last}/{size}"
            response_headers["content-length"] = str(last - first + 1)
    response_headers.update(headers or {})

    def body() -> Iterator[bytes]:
        with file:
            offset = first
            remaining = last - first + 1
            while remaining > 0:
                # `fileno()` each time: a closed file raises rather than hand
                # pread a number another open may have reused.
                chunk = os.pread(file.fileno(), min(_CHUNK, remaining), offset)
                if not chunk:
                    return
                offset += len(chunk)
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        body(),
        status_code=status_code,
        media_type=media_type,
        headers=response_headers,
        background=BackgroundTask(file.close),
    )
