"""Tests for the Docker-API allowlist proxy (Stage 1).

The pure ``classify_request`` is the unit-testable core; the asyncio
splice/mediate machinery is exercised against a fake upstream docker
socket. Live end-to-end (real docker daemon, real bwrap bind, an actually-
refused ``docker run --privileged``) is deferred to the prod host, like the
existing bwrap / network-isolation integration gaps.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile

import pytest

from istota import docker_proxy as dp
from istota.docker_proxy import DockerApiProxy, classify_request, is_exec_create

OWNED = "devbox-alice"


@pytest.fixture
def sockdir():
    """A short unix-socket dir under /tmp.

    macOS caps AF_UNIX paths at ~104 bytes; pytest's tmp_path is too long.
    """
    base = os.path.join(tempfile.gettempdir(), f"dpx{os.getpid()}{id(object())%100000}")
    os.makedirs(base, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _allow(method, path, *, body=None, tracked=frozenset()):
    return classify_request(
        method, path, body, container_name=OWNED, tracked_exec_ids=set(tracked),
    )


# ---- classify_request table -------------------------------------------------


class TestClassifyAllowed:
    def test_ping(self):
        assert _allow("GET", "/_ping") == (True, "ping")
        assert _allow("HEAD", "/_ping") == (True, "ping")

    def test_version(self):
        assert _allow("GET", "/version")[0] is True

    def test_version_prefixed_path(self):
        assert _allow("GET", "/v1.43/version")[0] is True
        assert _allow("GET", f"/v1.43/containers/{OWNED}/json") == (True, "inspect")

    def test_containers_list(self):
        assert _allow("GET", "/containers/json") == (True, "containers_list")

    def test_inspect_owned(self):
        assert _allow("GET", f"/containers/{OWNED}/json") == (True, "inspect")

    def test_archive_owned(self):
        assert _allow("GET", f"/containers/{OWNED}/archive") == (True, "archive")
        assert _allow("HEAD", f"/containers/{OWNED}/archive") == (True, "archive")
        assert _allow("PUT", f"/containers/{OWNED}/archive") == (True, "archive")

    def test_restart_owned(self):
        assert _allow("POST", f"/containers/{OWNED}/restart") == (True, "restart")

    def test_query_string_ignored(self):
        assert _allow("PUT", f"/containers/{OWNED}/archive?path=%2Ftmp") == (True, "archive")


class TestClassifyForbidden:
    @pytest.mark.parametrize("method,path", [
        ("POST", "/containers/create"),
        ("POST", "/build"),
        ("POST", "/images/create"),
        ("POST", "/networks/create"),
        ("POST", "/volumes/create"),
        ("DELETE", f"/containers/{OWNED}"),
        ("POST", f"/containers/{OWNED}/update"),
        ("GET", "/info"),
        ("POST", "/swarm/init"),
    ])
    def test_forbidden_endpoints(self, method, path):
        allowed, reason = _allow(method, path)
        assert allowed is False
        assert reason in ("forbidden", "not_owned")

    def test_inspect_foreign_container(self):
        assert _allow("GET", "/containers/devbox-bob/json") == (False, "not_owned")

    def test_archive_foreign_container(self):
        assert _allow("PUT", "/containers/devbox-bob/archive") == (False, "not_owned")

    def test_restart_foreign_container(self):
        assert _allow("POST", "/containers/devbox-bob/restart") == (False, "not_owned")

    def test_exec_create_foreign(self):
        body = json.dumps({"Cmd": ["ls"]}).encode()
        assert _allow("POST", "/containers/devbox-bob/exec", body=body) == (False, "not_owned")


class TestExecCreateBody:
    def test_exec_create_ok(self):
        body = json.dumps({"Cmd": ["sh", "-c", "ls"]}).encode()
        assert _allow("POST", f"/containers/{OWNED}/exec", body=body) == (True, "exec_create")

    def test_exec_create_privileged_rejected(self):
        body = json.dumps({"Cmd": ["sh"], "Privileged": True}).encode()
        assert _allow("POST", f"/containers/{OWNED}/exec", body=body) == (False, "privileged")

    def test_exec_create_hostconfig_rejected(self):
        body = json.dumps({"Cmd": ["sh"], "HostConfig": {"Binds": ["/:/host"]}}).encode()
        assert _allow("POST", f"/containers/{OWNED}/exec", body=body) == (False, "hostconfig")

    def test_exec_create_no_body_rejected(self):
        assert _allow("POST", f"/containers/{OWNED}/exec", body=None) == (False, "no_content_length")

    def test_exec_create_bad_json_rejected(self):
        assert _allow("POST", f"/containers/{OWNED}/exec", body=b"not json")[0] is False

    def test_is_exec_create_helper(self):
        assert is_exec_create("POST", f"/containers/{OWNED}/exec") is True
        assert is_exec_create("POST", f"/v1.43/containers/{OWNED}/exec") is True
        assert is_exec_create("GET", f"/containers/{OWNED}/json") is False


class TestExecIdTracking:
    def test_exec_start_untracked_denied(self):
        assert _allow("POST", "/exec/deadbeef/start") == (False, "untracked_exec")

    def test_exec_start_tracked_allowed(self):
        assert _allow("POST", "/exec/abc123/start", tracked={"abc123"}) == (True, "exec_start")

    def test_exec_inspect_tracked_allowed(self):
        assert _allow("GET", "/exec/abc123/json", tracked={"abc123"}) == (True, "exec_inspect")

    def test_exec_inspect_untracked_denied(self):
        assert _allow("GET", "/exec/abc123/json") == (False, "untracked_exec")


class TestSweep:
    def test_ttl_sweep_evicts_old(self):
        proxy = DockerApiProxy(
            user_id="alice", container_name=OWNED,
            upstream_socket="/nonexistent", listen_socket="/tmp/none.sock",
            exec_ttl_seconds=300,
        )
        # created at t=0
        proxy._exec_ids["old"] = 0.0
        proxy._exec_ids["fresh"] = 1000.0
        # now=400 -> cutoff 100 -> "old" (0.0) swept, "fresh" (1000) kept
        proxy._sweep_exec_ids(now=400.0)
        assert "old" not in proxy._exec_ids
        assert "fresh" in proxy._exec_ids

    def test_track_then_classify(self):
        proxy = DockerApiProxy(
            user_id="alice", container_name=OWNED,
            upstream_socket="/nonexistent", listen_socket="/tmp/none.sock",
        )
        proxy._track_exec("xyz")
        assert classify_request(
            "POST", "/exec/xyz/start", None,
            container_name=OWNED, tracked_exec_ids=set(proxy._exec_ids),
        ) == (True, "exec_start")


# ---- fake-upstream integration ---------------------------------------------


class _FakeUpstream:
    """A minimal asyncio unix server that pretends to be the docker socket.

    Records whether it was connected to. For exec-create it returns a canned
    201 with a JSON ``{"Id": ...}`` body; for everything else it echoes a
    canned 200 and then mirrors any further bytes (so the splice test can
    verify full-duplex copying).
    """

    def __init__(self, path: str, *, exec_id: str = "execid123"):
        self.path = path
        self.exec_id = exec_id
        self.connected = False
        self.received = bytearray()
        self._server = None

    async def start(self):
        self._server = await asyncio.start_unix_server(self._handle, path=self.path)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader, writer):
        self.connected = True
        head = await reader.readuntil(b"\r\n\r\n")
        self.received.extend(head)
        request_line = head.split(b"\r\n", 1)[0].decode()
        is_exec = b"/exec\r\n" in head or b"/exec " in request_line.encode() or "/exec " in request_line
        # crude: exec-create path ends with /exec
        path = request_line.split(" ")[1] if len(request_line.split(" ")) > 1 else ""
        if path.endswith("/exec"):
            # read content-length body
            cl = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    cl = int(line.split(b":", 1)[1].strip())
            if cl:
                self.received.extend(await reader.readexactly(cl))
            body = json.dumps({"Id": self.exec_id}).encode()
            resp = (
                b"HTTP/1.1 201 Created\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            writer.write(resp)
            await writer.drain()
        else:
            body = b'{"ok":true}'
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            writer.write(resp)
            await writer.drain()
            # Mirror anything further the client sends (full-duplex proof).
            try:
                while True:
                    data = await reader.read(1024)
                    if not data:
                        break
                    self.received.extend(data)
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _start_proxy(proxy: DockerApiProxy):
    task = asyncio.create_task(proxy.serve_forever())
    # wait for the listen socket to appear
    from pathlib import Path
    for _ in range(100):
        if Path(proxy.listen_socket).exists():
            break
        await asyncio.sleep(0.01)
    return task


async def _send(listen_socket: str, raw: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(listen_socket)
    writer.write(raw)
    await writer.drain()
    out = await reader.read(65536)
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return out


@pytest.mark.asyncio
class TestProxyIntegration:
    async def test_allowed_request_spliced(self, sockdir):
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = DockerApiProxy(
            user_id="alice", container_name=OWNED,
            upstream_socket=os.path.join(sockdir, "docker.sock"),
            listen_socket=os.path.join(sockdir, "proxy.sock"),
        )
        task = await _start_proxy(proxy)
        try:
            out = await _send(proxy.listen_socket, b"GET /version HTTP/1.1\r\nHost: x\r\n\r\n")
            assert b"200 OK" in out
            assert b'{"ok":true}' in out
            assert up.connected is True
        finally:
            task.cancel()
            await up.stop()

    async def test_denied_request_never_opens_upstream(self, sockdir):
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = DockerApiProxy(
            user_id="alice", container_name=OWNED,
            upstream_socket=os.path.join(sockdir, "docker.sock"),
            listen_socket=os.path.join(sockdir, "proxy.sock"),
        )
        task = await _start_proxy(proxy)
        try:
            out = await _send(
                proxy.listen_socket,
                b"POST /containers/create HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}",
            )
            assert b"403 Forbidden" in out
            assert b"istota-docker-proxy" in out
            await asyncio.sleep(0.05)
            assert up.connected is False
        finally:
            task.cancel()
            await up.stop()

    async def test_exec_create_captures_id_and_authorizes_start(self, sockdir):
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"), exec_id="trackedexec")
        await up.start()
        proxy = DockerApiProxy(
            user_id="alice", container_name=OWNED,
            upstream_socket=os.path.join(sockdir, "docker.sock"),
            listen_socket=os.path.join(sockdir, "proxy.sock"),
        )
        task = await _start_proxy(proxy)
        try:
            body = json.dumps({"Cmd": ["ls"]}).encode()
            req = (
                f"POST /containers/{OWNED}/exec HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode() + body
            out = await _send(proxy.listen_socket, req)
            assert b"201 Created" in out
            assert b"trackedexec" in out
            # the id is now tracked
            assert "trackedexec" in proxy._exec_ids
            # exec-start on the tracked id is authorized
            allowed, reason = classify_request(
                "POST", "/exec/trackedexec/start", None,
                container_name=OWNED, tracked_exec_ids=set(proxy._exec_ids),
            )
            assert (allowed, reason) == (True, "exec_start")
        finally:
            task.cancel()
            await up.stop()

    async def test_exec_create_privileged_denied_no_upstream(self, sockdir):
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = DockerApiProxy(
            user_id="alice", container_name=OWNED,
            upstream_socket=os.path.join(sockdir, "docker.sock"),
            listen_socket=os.path.join(sockdir, "proxy.sock"),
        )
        task = await _start_proxy(proxy)
        try:
            body = json.dumps({"Cmd": ["sh"], "Privileged": True}).encode()
            req = (
                f"POST /containers/{OWNED}/exec HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode() + body
            out = await _send(proxy.listen_socket, req)
            assert b"403 Forbidden" in out
            await asyncio.sleep(0.05)
            assert up.connected is False
        finally:
            task.cancel()
            await up.stop()


# ---- audit format ----------------------------------------------------------


class TestAudit:
    def test_audit_line_format(self, caplog):
        with caplog.at_level(logging.INFO, logger="istota.docker_proxy.audit"):
            dp._audit(
                user_id="alice", method="POST",
                path=f"/containers/{OWNED}/exec?foo=bar",
                result="deny", reason="privileged", dur_ms=3,
            )
        line = caplog.records[-1].getMessage()
        assert "user=alice" in line
        assert "method=POST" in line
        assert "result=deny" in line
        assert "reason=privileged" in line
        assert "dur_ms=3" in line
        # query string stripped
        assert "foo=bar" not in line
