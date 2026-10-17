"""A download served from a file descriptor the route already opened.

Starlette's `FileResponse` takes a path and opens it again when the response
is sent, so a route that confined the file with a no-follow walk would hand
the check's result to a second, unchecked resolution of the same name. This
streams the open descriptor instead, with the headers `FileResponse` sets
for a plain GET: length, last-modified and etag from `fstat`, and the same
`Content-Disposition` spelling. Range requests are not served.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Mapping
from email.utils import formatdate
from urllib.parse import quote

from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

_CHUNK = 64 * 1024


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


def fd_file_response(
    fd: int,
    *,
    filename: str,
    media_type: str,
    disposition_type: str = "attachment",
    headers: Mapping[str, str] | None = None,
) -> StreamingResponse:
    """Stream the regular file open at `fd`; the response takes ownership of it.

    At most the `fstat` size is sent, so the body matches `Content-Length`
    even if the file grows meanwhile. The descriptor is closed when the body
    ends, again by the background task (a second close is a no-op), and by
    the file object's finalizer if a disconnect abandons the body.
    """
    try:
        info = os.fstat(fd)
        file = os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise
    size = info.st_size

    def body() -> Iterator[bytes]:
        with file:
            remaining = size
            while remaining > 0:
                chunk = file.read(min(_CHUNK, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk

    response_headers = {
        **stat_headers(info),
        "content-disposition": content_disposition(disposition_type, filename),
        **(headers or {}),
    }
    return StreamingResponse(
        body(),
        media_type=media_type,
        headers=response_headers,
        background=BackgroundTask(file.close),
    )
