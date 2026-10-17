"""`webui.fd_response.fd_file_response`: a file streamed from an fd the route
already opened, with `FileResponse`'s headers and, when the route passes the
request's headers, one byte range (ISSUE-776).

`/chat/files` serves audio and video to native players that seek with Range
requests, and it streams the descriptor it read the sniffed head from, so the
body must come from explicit offsets rather than the fd's position.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from istota.webui.fd_response import fd_file_response, stat_headers

BODY = bytes(range(256)) * 4  # 1024 bytes, every offset distinguishable


@pytest.fixture
def served(tmp_path: Path):
    path = tmp_path / "clip.mp3"
    path.write_bytes(BODY)
    app = FastAPI()

    @app.get("/ranged")
    def ranged(request: Request):
        fd = os.open(path, os.O_RDONLY)
        # Leave the fd's position where a head read would have left it.
        os.read(fd, 16)
        return fd_file_response(
            fd, filename="clip.mp3", media_type="audio/mpeg",
            request_headers=request.headers,
        )

    @app.get("/plain")
    def plain():
        fd = os.open(path, os.O_RDONLY)
        return fd_file_response(fd, filename="clip.mp3", media_type="audio/mpeg")

    return TestClient(app), path


def test_a_plain_get_is_the_whole_file_from_offset_zero(served):
    client, path = served
    resp = client.get("/ranged")
    assert resp.status_code == 200
    assert resp.content == BODY
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-length"] == str(len(BODY))
    assert resp.headers["etag"] == stat_headers(os.stat(path))["etag"]


@pytest.mark.parametrize("header,start,end", [
    ("bytes=100-199", 100, 199),
    ("bytes=1000-", 1000, 1023),
    ("bytes=-24", 1000, 1023),
    ("bytes=1000-5000", 1000, 1023),
    ("bytes=0-0", 0, 0),
    (" bytes = 10 - 19 ", 10, 19),
])
def test_a_single_range_is_a_206_with_those_bytes(served, header, start, end):
    client, _ = served
    resp = client.get("/ranged", headers={"range": header})
    assert resp.status_code == 206
    assert resp.content == BODY[start:end + 1]
    assert resp.headers["content-range"] == f"bytes {start}-{end}/{len(BODY)}"
    assert resp.headers["content-length"] == str(end - start + 1)
    assert resp.headers["content-disposition"] == 'attachment; filename="clip.mp3"'


@pytest.mark.parametrize("header", ["bytes=1024-", "bytes=5000-6000", "bytes=-0"])
def test_an_unsatisfiable_range_is_a_416(served, header):
    client, _ = served
    resp = client.get("/ranged", headers={"range": header})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(BODY)}"
    assert resp.content == b""


@pytest.mark.parametrize("header", [
    "bytes=20-10", "bytes=abc", "items=0-10", "bytes=", "bytes=0-10,20-30", "bytes=-",
])
def test_a_range_it_does_not_serve_is_ignored(served, header):
    """Malformed and multi-range requests get the whole file, which RFC 9110
    permits; a 416 would stop a player that only wanted to start."""
    client, _ = served
    resp = client.get("/ranged", headers={"range": header})
    assert resp.status_code == 200
    assert resp.content == BODY


def test_if_range_applies_the_range_only_while_the_file_is_unchanged(served):
    client, path = served
    etag = stat_headers(os.stat(path))["etag"]
    match = client.get("/ranged", headers={"range": "bytes=0-9", "if-range": etag})
    assert match.status_code == 206
    assert match.content == BODY[:10]
    stale = client.get("/ranged", headers={"range": "bytes=0-9", "if-range": '"other"'})
    assert stale.status_code == 200
    assert stale.content == BODY


def test_without_the_request_headers_a_range_is_not_served(served):
    """The invoice route passes none, so its response is what it was."""
    client, _ = served
    resp = client.get("/plain", headers={"range": "bytes=0-9"})
    assert resp.status_code == 200
    assert resp.content == BODY
    assert "accept-ranges" not in resp.headers
