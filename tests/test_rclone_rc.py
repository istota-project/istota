"""The daemon's rc client, against an HTTP server on a real Unix socket.

The server stands in for rclone's rc: it checks Basic auth the way
`--rc-user`/`--rc-pass` makes rclone check it, and records what it was sent.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from istota.nextcloud import rclone_rc


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def rc(monkeypatch):
    # AF_UNIX paths are short; pytest's tmp_path is not, on macOS.
    base = Path(tempfile.mkdtemp(prefix="rc", dir="/tmp"))
    sock = base / "rc.sock"
    creds = base / "rc.env"
    creds.write_text("RCLONE_RC_USER=istota\nRCLONE_RC_PASS=s3cret\n")
    seen: list[dict] = []
    expected = "Basic " + base64.b64encode(b"istota:s3cret").decode()

    class Handler(BaseHTTPRequestHandler):
        def address_string(self):
            return "unix"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                         "body": body})
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.end_headers()
                return
            answer = {"forgotten": list(body.values())}
            if self.path == "/vfs/refresh":
                answer = {"result": {v: "OK" for v in body.values()} or {"": "OK"}}
            data = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = _Server(str(sock), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(rclone_rc.RC_ENV_VAR, "1")
    try:
        yield {"socket": sock, "creds": creds, "seen": seen}
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(base, ignore_errors=True)


def _call(rc, command, params=None, **kwargs):
    return rclone_rc.call(command, params, socket_path=rc["socket"],
                          credentials_path=rc["creds"], **kwargs)


class TestCall:
    def test_an_authenticated_call_answers(self, rc):
        result = _call(rc, "rc/noop")
        assert result.ok, result
        assert rc["seen"][0]["auth"].startswith("Basic ")

    def test_without_the_password_the_rc_refuses(self, rc):
        assert _call(rc, "rc/noop", authenticate=False).reason == "unauthorized"

    def test_a_call_outside_the_three_is_never_sent(self, rc):
        for command in ("operations/deletefile", "core/command", "vfs/forgetx"):
            assert _call(rc, command).reason == "refused_call"
        assert rc["seen"] == []

    def test_no_credentials_file_sends_nothing(self, rc):
        rc["creds"].unlink()
        assert _call(rc, "rc/noop").reason == "no_credentials"
        assert rc["seen"] == []

    def test_a_link_at_the_credentials_file_is_not_followed(self, rc, tmp_path):
        real = tmp_path / "elsewhere.env"
        real.write_text("RCLONE_RC_USER=istota\nRCLONE_RC_PASS=s3cret\n")
        rc["creds"].unlink()
        os.symlink(real, rc["creds"])
        assert _call(rc, "rc/noop").reason == "no_credentials"

    def test_a_missing_socket_is_unreachable(self, rc):
        rc["socket"].unlink()
        assert _call(rc, "rc/noop").reason == "unreachable"


class TestForgetAndRefresh:
    def _patch_defaults(self, rc, monkeypatch):
        real = rclone_rc.call

        def call(command, params=None, **kwargs):
            kwargs.setdefault("socket_path", rc["socket"])
            kwargs.setdefault("credentials_path", rc["creds"])
            return real(command, params, **kwargs)

        monkeypatch.setattr(rclone_rc, "call", call)

    def test_several_directories_go_in_one_forget(self, rc, monkeypatch):
        self._patch_defaults(rc, monkeypatch)
        assert rclone_rc.forget_dirs(["Users/a/istota/config", "Users/b/istota/scripts"]).ok
        assert rc["seen"] == [{
            "path": "/vfs/forget", "auth": rc["seen"][0]["auth"],
            "body": {"dir": "Users/a/istota/config", "dir2": "Users/b/istota/scripts"},
        }]

    def test_the_root_is_refreshed_with_no_directory_named(self, rc, monkeypatch):
        self._patch_defaults(rc, monkeypatch)
        assert rclone_rc.refresh_dirs(["", "Users"]).ok
        assert [(s["path"], s["body"]) for s in rc["seen"]] == [
            ("/vfs/refresh", {}), ("/vfs/refresh", {"dir": "Users"}),
        ]

    def test_forget_all_names_no_directory(self, rc, monkeypatch):
        self._patch_defaults(rc, monkeypatch)
        assert rclone_rc.forget_all().ok
        assert [(s["path"], s["body"]) for s in rc["seen"]] == [("/vfs/forget", {})]

    def test_nothing_is_sent_when_the_rc_is_not_bound_in(self, rc, monkeypatch):
        self._patch_defaults(rc, monkeypatch)
        monkeypatch.delenv(rclone_rc.RC_ENV_VAR)
        assert rclone_rc.forget_dirs(["Users"]).reason == "not_configured"
        assert rc["seen"] == []
