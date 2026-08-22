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
import time

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

    def __init__(self, path: str, *, exec_id: str = "execid123",
                 chunked: bool = False, interim: bool = False):
        self.path = path
        self.exec_id = exec_id
        # The real docker daemon returns Transfer-Encoding: chunked for the
        # mediated endpoints, so a Content-Length-only fake never exercises
        # the branch production actually takes.
        self.chunked = chunked
        # Emit an interim 1xx head before the response, as a daemon does when
        # a client sends Expect: 100-continue.
        self.interim = interim
        self.connected = False
        self.received = bytearray()
        self._server = None

    def _encode(self, status_line: bytes, body: bytes) -> bytes:
        prefix = b"HTTP/1.1 100 Continue\r\n\r\n" if self.interim else b""
        if self.chunked:
            return (
                prefix + status_line + b"\r\n"
                b"Content-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                + f"{len(body):x}".encode() + b"\r\n" + body + b"\r\n"
                b"0\r\n\r\n"
            )
        return (
            prefix + status_line + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )

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
        b"/exec\r\n" in head or b"/exec " in request_line.encode() or "/exec " in request_line
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
            resp = self._encode(b"HTTP/1.1 201 Created", body)
            writer.write(resp)
            await writer.drain()
        else:
            body = b'{"ok":true}'
            resp = self._encode(b"HTTP/1.1 200 OK", body)
            writer.write(resp)
            await writer.drain()
            # A real daemon honours Connection: close and does not wait for a
            # second request; the archive path depends on that to terminate.
            # Drain briefly first, and record what arrives, so that a proxy
            # which wrongly forwards a pipelined follow-up is caught by the
            # test rather than hidden behind an early close.
            if b"connection: close" in bytes(head).lower():
                try:
                    extra = await asyncio.wait_for(reader.read(65536), 0.3)
                    if extra:
                        self.received.extend(extra)
                except Exception:
                    pass
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                return
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
    async def test_allowed_request_is_mediated(self, sockdir):
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


# ---- ISSUE-294: keep-alive must not tunnel past the allowlist ---------------


async def _read_one_response(reader) -> bytes:
    """Read exactly one HTTP response: head plus its framed body."""
    head = await reader.readuntil(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    body = await reader.readexactly(length) if length else b""
    return head + body


async def _send_sequence(listen_socket: str, requests: list[bytes]) -> list[bytes]:
    """Send several requests down ONE connection, reading a response to each.

    The one-shot ``_send`` helper cannot express the shape ISSUE-294 was
    about — it opens a connection, writes once, and closes — so every test of
    per-request classification needs this sibling.
    """
    reader, writer = await asyncio.open_unix_connection(listen_socket)
    out: list[bytes] = []
    try:
        for raw in requests:
            writer.write(raw)
            await writer.drain()
            try:
                out.append(await asyncio.wait_for(_read_one_response(reader), 5))
            except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
                out.append(b"<connection closed>")
                break
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return out


def _proxy_for(sockdir):
    return DockerApiProxy(
        user_id="alice", container_name=OWNED,
        upstream_socket=os.path.join(sockdir, "docker.sock"),
        listen_socket=os.path.join(sockdir, "proxy.sock"),
    )


@pytest.mark.asyncio
class TestKeepAliveClassification:
    async def test_forbidden_second_request_is_refused_not_tunneled(self, sockdir):
        """An allowed first request must not buy an unfiltered tunnel.

        ``GET /_ping`` is allowed unconditionally, so before the fix any
        client could follow it with a forbidden endpoint on the same
        connection and have the bytes copied straight to the daemon.
        """
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        task = await _start_proxy(proxy)
        try:
            responses = await _send_sequence(proxy.listen_socket, [
                b"GET /_ping HTTP/1.1\r\nHost: x\r\n\r\n",
                b"GET /info HTTP/1.1\r\nHost: x\r\n\r\n",
            ])
            assert b"200 OK" in responses[0]
            assert b"403 Forbidden" in responses[1]
            await asyncio.sleep(0.05)
            assert b"/info" not in bytes(up.received)
        finally:
            task.cancel()
            await up.stop()

    async def test_forbidden_post_second_request_is_refused(self, sockdir):
        """The escape that surfaced the bug: create riding behind a handshake."""
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        task = await _start_proxy(proxy)
        try:
            body = json.dumps({"Image": "x", "HostConfig": {"Privileged": True}}).encode()
            responses = await _send_sequence(proxy.listen_socket, [
                b"GET /v1.43/version HTTP/1.1\r\nHost: x\r\n\r\n",
                (
                    f"POST /v1.43/containers/create HTTP/1.1\r\nHost: x\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode() + body,
            ])
            assert b"200 OK" in responses[0]
            assert b"403 Forbidden" in responses[1]
            await asyncio.sleep(0.05)
            assert b"/containers/create" not in bytes(up.received)
        finally:
            task.cancel()
            await up.stop()

    async def test_two_allowed_requests_both_served(self, sockdir):
        """Mediation has to keep the connection usable, not just safe."""
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        task = await _start_proxy(proxy)
        try:
            responses = await _send_sequence(proxy.listen_socket, [
                b"GET /v1.43/version HTTP/1.1\r\nHost: x\r\n\r\n",
                f"GET /v1.43/containers/{OWNED}/json HTTP/1.1\r\nHost: x\r\n\r\n".encode(),
            ])
            assert len(responses) == 2
            for resp in responses:
                assert b"200 OK" in resp
                assert b'{"ok":true}' in resp
        finally:
            task.cancel()
            await up.stop()

    async def test_every_request_is_audited(self, sockdir, caplog):
        """The audit trail went blind past the first request on a connection."""
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        task = await _start_proxy(proxy)
        try:
            with caplog.at_level(logging.INFO, logger="istota.docker_proxy.audit"):
                await _send_sequence(proxy.listen_socket, [
                    b"GET /_ping HTTP/1.1\r\nHost: x\r\n\r\n",
                    b"GET /info HTTP/1.1\r\nHost: x\r\n\r\n",
                ])
                await asyncio.sleep(0.05)
            lines = [
                r.getMessage() for r in caplog.records
                if "docker_proxy user=" in r.getMessage()
            ]
            assert len(lines) == 2, lines
            assert "path=/_ping result=allow" in lines[0]
            assert "path=/info result=deny" in lines[1]
        finally:
            task.cancel()
            await up.stop()

    async def test_archive_stream_carries_no_pipelined_followup(self, sockdir):
        """A tar stream is relayed opaquely, so its connection must be terminal.

        The proxy never parses the archive body, so the only way it can
        promise nothing rides behind it is to read no more from the client
        than the request declared.
        """
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        task = await _start_proxy(proxy)
        try:
            reader, writer = await asyncio.open_unix_connection(proxy.listen_socket)
            writer.write(
                f"GET /v1.43/containers/{OWNED}/archive?path=/tmp HTTP/1.1\r\n"
                f"Host: x\r\n\r\n".encode()
                + b"GET /info HTTP/1.1\r\nHost: x\r\n\r\n"
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(65536), 5)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(0.05)
            assert b"/info" not in bytes(up.received)
            assert b"connection: close" in bytes(up.received).lower()
        finally:
            task.cancel()
            await up.stop()

    async def test_content_length_with_transfer_encoding_is_rejected(self, sockdir):
        """Ambiguous body framing is request smuggling; refuse rather than guess."""
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        task = await _start_proxy(proxy)
        try:
            out = await _send(
                proxy.listen_socket,
                b"GET /_ping HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n",
            )
            assert b"400 Bad Request" in out
            await asyncio.sleep(0.05)
            assert up.connected is False
        finally:
            task.cancel()
            await up.stop()


class TestRequestFraming:
    def test_clean_head_has_no_error(self):
        assert dp.request_framing_error(
            b"GET /_ping HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n\r\n"
        ) is None

    def test_content_length_and_transfer_encoding(self):
        assert dp.request_framing_error(
            b"POST /x HTTP/1.1\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n"
        ) == "smuggling_cl_and_te"

    def test_disagreeing_duplicate_content_length(self):
        assert dp.request_framing_error(
            b"POST /x HTTP/1.1\r\nContent-Length: 3\r\nContent-Length: 9\r\n\r\n"
        ) == "smuggling_duplicate_cl"

    def test_non_numeric_content_length(self):
        assert dp.request_framing_error(
            b"POST /x HTTP/1.1\r\nContent-Length: +3\r\n\r\n"
        ) == "bad_content_length"

    def test_chunked_alone_is_allowed_here(self):
        # The mediated path rejects it separately; archive streams it.
        assert dp.request_framing_error(
            b"PUT /x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
        ) is None


# ---- Head structure: the proxy and the daemon must read a head alike -------


class TestHeadStructure:
    def test_clean_head_passes(self):
        assert dp.head_structure_error(
            b"GET /_ping HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
        ) is None

    def test_bare_lf_blank_line_is_rejected(self):
        # One CRLFCRLF, so readuntil() sees a single head — but Go's textproto
        # ends a line at a bare \n, so the daemon sees three requests.
        assert dp.head_structure_error(
            b"GET /_ping HTTP/1.1\r\nHost: x\n\nPOST /containers/create HTTP/1.1\r\n\r\n"
        ) == "bare_lf_in_head"

    def test_bare_lf_inside_a_header_is_rejected(self):
        assert dp.head_structure_error(
            b"POST /x HTTP/1.1\r\nHost: x\nContent-Length: 44\r\n\r\n"
        ) == "bare_lf_in_head"

    def test_obs_fold_continuation_is_rejected(self):
        assert dp.head_structure_error(
            b"POST /x HTTP/1.1\r\nX-Pad: pad\r\n Content-Length: 44\r\n\r\n"
        ) == "obs_fold_header"

    def test_tab_continuation_is_rejected(self):
        assert dp.head_structure_error(
            b"POST /x HTTP/1.1\r\nX-Pad: pad\r\n\tContent-Length: 44\r\n\r\n"
        ) == "obs_fold_header"

    def test_space_before_colon_is_rejected(self):
        assert dp.head_structure_error(
            b"POST /x HTTP/1.1\r\nContent-Length : 44\r\n\r\n"
        ) == "bad_header_name"

    def test_header_line_without_a_colon_is_rejected(self):
        assert dp.head_structure_error(
            b"POST /x HTTP/1.1\r\nnonsense\r\n\r\n"
        ) == "malformed_header_line"

    def test_stray_cr_is_rejected(self):
        assert dp.head_structure_error(
            b"POST /x HTTP/1.1\r\nHost: a\rb\r\n\r\n"
        ) == "bare_cr_in_head"


@pytest.mark.asyncio
class TestSmugglingIsRefused:
    async def test_bare_lf_head_never_reaches_the_daemon(self, sockdir):
        """The head is forwarded verbatim, so the two parsers must agree.

        A blob with one CRLFCRLF is one request to this proxy and three to
        the Go daemon behind it — and the smuggled one can be a privileged,
        host-mounting container create.
        """
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        task = await _start_proxy(proxy)
        try:
            out = await _send(
                proxy.listen_socket,
                b"GET /_ping HTTP/1.1\r\nHost: x\n\n"
                b"POST /v1.43/containers/create HTTP/1.1\r\nContent-Length: 0\r\n\r\n",
            )
            assert b"400 Bad Request" in out
            await asyncio.sleep(0.05)
            assert up.connected is False
            assert b"/containers/create" not in bytes(up.received)
        finally:
            task.cancel()
            await up.stop()

    async def test_obs_fold_content_length_never_reaches_the_daemon(self, sockdir):
        """A folded Content-Length is a header here and a value to the daemon.

        The proxy would read N bytes as body and forward them; the daemon,
        seeing no Content-Length, would read them as the next request.
        """
        smuggled = b"POST /v1.43/containers/create HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        task = await _start_proxy(proxy)
        try:
            out = await _send(
                proxy.listen_socket,
                f"POST /v1.43/containers/{OWNED}/restart HTTP/1.1\r\nHost: x\r\n"
                f"X-Pad: pad\r\n Content-Length: {len(smuggled)}\r\n\r\n".encode()
                + smuggled,
            )
            assert b"400 Bad Request" in out
            await asyncio.sleep(0.05)
            assert up.connected is False
            assert b"/containers/create" not in bytes(up.received)
        finally:
            task.cancel()
            await up.stop()

    async def test_expect_continue_is_refused_rather_than_deadlocked(self, sockdir):
        """The mediated path reads the body first, so it cannot hold a 100 handshake."""
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        task = await _start_proxy(proxy)
        try:
            out = await asyncio.wait_for(
                _send(
                    proxy.listen_socket,
                    f"POST /v1.43/containers/{OWNED}/exec HTTP/1.1\r\nHost: x\r\n"
                    f"Expect: 100-continue\r\nContent-Length: 9999\r\n\r\n".encode(),
                ),
                5,
            )
            assert b"417 Expectation Failed" in out
            await asyncio.sleep(0.05)
            assert up.connected is False
        finally:
            task.cancel()
            await up.stop()


# ---- Chunked framing, which is what the real daemon returns ----------------


@pytest.mark.asyncio
class TestChunkedFraming:
    async def test_chunked_response_is_relayed_and_connection_survives(self, sockdir):
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"), chunked=True)
        await up.start()
        proxy = _proxy_for(sockdir)
        task = await _start_proxy(proxy)
        try:
            reader, writer = await asyncio.open_unix_connection(proxy.listen_socket)
            out = []
            for _ in range(2):
                writer.write(b"GET /v1.43/version HTTP/1.1\r\nHost: x\r\n\r\n")
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                body = await asyncio.wait_for(reader.readuntil(b"0\r\n\r\n"), 5)
                out.append(head + body)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            assert len(out) == 2
            for resp in out:
                assert b"200 OK" in resp
                assert b"Transfer-Encoding: chunked" in resp
                assert b'{"ok":true}' in resp
        finally:
            task.cancel()
            await up.stop()

    async def test_chunked_archive_body_stops_at_the_terminal_chunk(self, sockdir):
        """`docker cp` in sends a chunked tar; nothing after it may be forwarded."""
        up = _FakeUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        task = await _start_proxy(proxy)
        try:
            reader, writer = await asyncio.open_unix_connection(proxy.listen_socket)
            writer.write(
                f"PUT /v1.43/containers/{OWNED}/archive?path=/tmp HTTP/1.1\r\n"
                f"Host: x\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
                + b"5\r\nhello\r\n0\r\n\r\n"
                + b"GET /info HTTP/1.1\r\nHost: x\r\n\r\n"
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(65536), 5)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(0.1)
            received = bytes(up.received)
            assert b"hello" in received
            assert b"/info" not in received
        finally:
            task.cancel()
            await up.stop()


class _CollectingWriter:
    """Enough of a StreamWriter for the copy helpers."""

    def __init__(self):
        self.data = bytearray()

    def write(self, chunk):
        self.data.extend(chunk)

    async def drain(self):
        return None


def _reader_with(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


class TestChunkSizeBounds:
    def test_hex_size_parses(self):
        assert dp._chunk_size(b"1f\r\n") == 31

    def test_extension_is_ignored(self):
        assert dp._chunk_size(b"1f;name=value\r\n") == 31

    def test_non_hex_is_rejected(self):
        with pytest.raises(ValueError):
            dp._chunk_size(b"0x10\r\n")

    def test_empty_is_rejected(self):
        with pytest.raises(ValueError):
            dp._chunk_size(b"\r\n")


@pytest.mark.asyncio
class TestChunkedCopyBounds:
    async def test_oversized_chunk_is_refused_before_anything_is_relayed(self):
        """A chunk size is client-chosen, so an uncapped one is a host OOM."""
        reader = _reader_with(b"ffffffffffffffff\r\n")
        writer = _CollectingWriter()
        with pytest.raises(ValueError, match="chunk too large"):
            await dp._copy_chunked(reader, writer)
        assert bytes(writer.data) == b""

    async def test_malformed_chunk_terminator_is_refused(self):
        reader = _reader_with(b"5\r\nhelloXX0\r\n\r\n")
        writer = _CollectingWriter()
        with pytest.raises(ValueError, match="terminator"):
            await dp._copy_chunked(reader, writer)

    async def test_well_formed_body_is_copied_verbatim(self):
        raw = b"5\r\nhello\r\n3\r\nabc\r\n0\r\n\r\n"
        writer = _CollectingWriter()
        await dp._copy_chunked(_reader_with(raw), writer)
        assert bytes(writer.data) == raw

    async def test_oversized_response_chunk_is_refused(self):
        with pytest.raises(ValueError):
            await dp._read_chunked_body(
                _reader_with(b"ffffffffffffffff\r\n"), limit=dp._MAX_MEDIATED_RESPONSE,
            )


@pytest.mark.asyncio
class TestResponseFraming:
    async def test_interim_continue_is_kept_apart_from_the_response(self, sockdir):
        """A 1xx is not an answer; folding it in loses the exec id behind it."""
        proxy = _proxy_for(sockdir)
        body = b'{"Id":"abcdef"}'
        reader = _reader_with(
            b"HTTP/1.1 100 Continue\r\n\r\n"
            b"HTTP/1.1 201 Created\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        interim, response, reusable = await proxy._read_full_response(
            reader, request_method="POST",
        )
        assert interim == b"HTTP/1.1 100 Continue\r\n\r\n"
        assert response.endswith(body)
        assert reusable is True
        assert dp._parse_response_body_id(response) == "abcdef"

    async def test_no_content_response_has_no_body(self, sockdir):
        proxy = _proxy_for(sockdir)
        reader = _reader_with(b"HTTP/1.1 204 No Content\r\n\r\n")
        interim, response, reusable = await proxy._read_full_response(
            reader, request_method="POST",
        )
        assert interim == b""
        assert response == b"HTTP/1.1 204 No Content\r\n\r\n"
        assert reusable is True

    async def test_head_request_response_has_no_body(self, sockdir):
        proxy = _proxy_for(sockdir)
        reader = _reader_with(b"HTTP/1.1 200 OK\r\nContent-Length: 44\r\n\r\n")
        _, response, _ = await proxy._read_full_response(reader, request_method="HEAD")
        assert response == b"HTTP/1.1 200 OK\r\nContent-Length: 44\r\n\r\n"

    async def test_unframed_persistent_response_is_refused(self, sockdir):
        """Reading to EOF on a connection the daemon means to reuse would hang."""
        proxy = _proxy_for(sockdir)
        reader = _reader_with(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nhi")
        with pytest.raises(ValueError, match="unframed"):
            await proxy._read_full_response(reader, request_method="GET")

    async def test_upstream_close_ends_the_client_connection(self, sockdir):
        proxy = _proxy_for(sockdir)
        reader = _reader_with(
            b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\nhi"
        )
        _, _, reusable = await proxy._read_full_response(reader, request_method="GET")
        assert reusable is False


class TestKeepAliveDecision:
    def test_http11_persists_by_default(self):
        assert dp._client_wants_keep_alive(b"GET / HTTP/1.1\r\n\r\n", {}) is True

    def test_http11_honours_close(self):
        assert dp._client_wants_keep_alive(
            b"GET / HTTP/1.1\r\n\r\n", {"connection": "close"},
        ) is False

    def test_http10_does_not_persist_by_default(self):
        assert dp._client_wants_keep_alive(b"GET / HTTP/1.0\r\n\r\n", {}) is False

    def test_http10_honours_keep_alive(self):
        assert dp._client_wants_keep_alive(
            b"GET / HTTP/1.0\r\n\r\n", {"connection": "keep-alive"},
        ) is True


class TestTerminalOps:
    def test_only_the_two_opaque_ops_end_the_connection(self):
        assert dp.is_terminal_op("archive") is True
        assert dp.is_terminal_op("exec_start") is True
        for reason in ("ping", "version", "containers_list", "inspect",
                       "restart", "exec_create", "exec_inspect"):
            assert dp.is_terminal_op(reason) is False


# ---- exec start: the daemon does not always hijack -------------------------


class _ExecStartUpstream:
    """A fake that mimics moby's exec-start behaviour.

    The point is the branch: ``postContainerExecStart`` calls
    ``HijackConnection`` only when the start body does not say
    ``Detach: true``. A detached start — and any error before the hijack —
    leaves the daemon parsing HTTP on that connection, which is what makes a
    pipelined follow-up dangerous.
    """

    def __init__(self, path: str, *, status: int = 200):
        self.path = path
        self.status = status
        self.received = bytearray()
        self.hijacked = False
        self._server = None

    async def start(self):
        self._server = await asyncio.start_unix_server(self._handle, path=self.path)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        self.received.extend(head)
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        body = await reader.readexactly(length) if length else b""
        self.received.extend(body)

        detach = False
        try:
            detach = json.loads(body or b"{}").get("Detach") is True
        except ValueError:
            pass

        if detach or self.status != 200:
            # No hijack: an ordinary framed response, connection still HTTP.
            payload = b'{"message":"started"}'
            writer.write(
                f"HTTP/1.1 {self.status} OK\r\n".encode()
                + b"Content-Type: application/json\r\n"
                + b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n"
                + payload
            )
            await writer.drain()
            # Keep reading, and record it: anything that arrives here is a
            # request the proxy forwarded without classifying.
            try:
                extra = await asyncio.wait_for(reader.read(65536), 0.3)
                if extra:
                    self.received.extend(extra)
            except Exception:
                pass
        else:
            self.hijacked = True
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/vnd.docker.raw-stream\r\n\r\n"
            )
            await writer.drain()
            try:
                while True:
                    data = await asyncio.wait_for(reader.read(4096), 0.3)
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


def _exec_start_request(exec_id: str, body: bytes) -> bytes:
    return (
        f"POST /v1.43/exec/{exec_id}/start HTTP/1.1\r\nHost: x\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body


@pytest.mark.asyncio
class TestExecStart:
    async def test_detached_start_carries_no_pipelined_request(self, sockdir):
        """A detached exec start is not a hijack, so nothing may ride behind it.

        This is ISSUE-294's escape on the one op the first fix assumed was
        safe by construction: with an unconditional full-duplex splice, the
        pipelined create below reaches the daemon and a privileged,
        host-mounting container is created.
        """
        up = _ExecStartUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        proxy._exec_ids["tracked"] = time.monotonic()
        task = await _start_proxy(proxy)
        try:
            reader, writer = await asyncio.open_unix_connection(proxy.listen_socket)
            writer.write(
                _exec_start_request("tracked", b'{"Detach": true, "Tty": false}')
                + b"POST /v1.43/containers/create HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: 2\r\n\r\n{}"
            )
            await writer.drain()
            out = await asyncio.wait_for(reader.read(65536), 5)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(0.1)

            assert b"200 OK" in out
            assert up.hijacked is False
            assert b"/containers/create" not in bytes(up.received)
        finally:
            task.cancel()
            await up.stop()

    async def test_error_before_hijack_carries_no_pipelined_request(self, sockdir):
        """A stopped container answers 409 and never hijacks either."""
        up = _ExecStartUpstream(os.path.join(sockdir, "docker.sock"), status=409)
        await up.start()
        proxy = _proxy_for(sockdir)
        proxy._exec_ids["tracked"] = time.monotonic()
        task = await _start_proxy(proxy)
        try:
            reader, writer = await asyncio.open_unix_connection(proxy.listen_socket)
            writer.write(
                _exec_start_request("tracked", b'{"Detach": false, "Tty": false}')
                + b"POST /v1.43/containers/create HTTP/1.1\r\nHost: x\r\n\r\n"
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(65536), 5)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(0.1)
            assert up.hijacked is False
            assert b"/containers/create" not in bytes(up.received)
        finally:
            task.cancel()
            await up.stop()

    async def test_hijacked_start_still_streams_both_ways(self, sockdir):
        """The ordinary interactive case must keep working."""
        up = _ExecStartUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        proxy._exec_ids["tracked"] = time.monotonic()
        task = await _start_proxy(proxy)
        try:
            reader, writer = await asyncio.open_unix_connection(proxy.listen_socket)
            writer.write(_exec_start_request("tracked", b'{"Detach": false, "Tty": false}'))
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert b"vnd.docker.raw-stream" in head
            # Post-hijack bytes are stdio, and the fake mirrors them back.
            writer.write(b"hello stdin")
            await writer.drain()
            echoed = await asyncio.wait_for(reader.read(64), 5)
            assert echoed == b"hello stdin"
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(0.05)
            assert up.hijacked is True
        finally:
            task.cancel()
            await up.stop()

    async def test_start_evicts_its_exec_id(self, sockdir):
        up = _ExecStartUpstream(os.path.join(sockdir, "docker.sock"))
        await up.start()
        proxy = _proxy_for(sockdir)
        proxy._exec_ids["tracked"] = time.monotonic()
        task = await _start_proxy(proxy)
        try:
            await _send(
                proxy.listen_socket,
                _exec_start_request("tracked", b'{"Detach": true}'),
            )
            await asyncio.sleep(0.1)
            assert "tracked" not in proxy._exec_ids
        finally:
            task.cancel()
            await up.stop()


class TestHijackDetection:
    def test_upgrade_status_is_a_hijack(self):
        assert dp.is_hijack_response(101, {}) is True

    def test_raw_stream_content_type_is_a_hijack(self):
        assert dp.is_hijack_response(
            200, {"content-type": "application/vnd.docker.raw-stream"},
        ) is True

    def test_multiplexed_stream_is_a_hijack(self):
        assert dp.is_hijack_response(
            200, {"content-type": "application/vnd.docker.multiplexed-stream"},
        ) is True

    def test_detached_json_response_is_not_a_hijack(self):
        assert dp.is_hijack_response(
            200, {"content-type": "application/json"},
        ) is False

    def test_error_response_is_not_a_hijack(self):
        assert dp.is_hijack_response(409, {"content-type": "application/json"}) is False


class TestExecIdFromChunkedResponse:
    def test_chunked_exec_create_response_still_yields_the_id(self):
        """A chunked body parsed as JSON yields nothing, and breaks docker exec."""
        payload = b'{"Id":"abcdef123"}'
        raw = (
            b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            + f"{len(payload):x}".encode() + b"\r\n" + payload + b"\r\n0\r\n\r\n"
        )
        assert dp._parse_response_body_id(raw) == "abcdef123"

    def test_content_length_response_still_yields_the_id(self):
        payload = b'{"Id":"abcdef123"}'
        raw = (
            b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
        )
        assert dp._parse_response_body_id(raw) == "abcdef123"

    def test_malformed_body_yields_nothing(self):
        raw = b"HTTP/1.1 201 Created\r\nContent-Length: 3\r\n\r\nnot json"
        assert dp._parse_response_body_id(raw) is None


@pytest.mark.asyncio
class TestExecCreateChunked:
    async def test_chunked_exec_create_tracks_the_id(self, sockdir):
        """The real daemon frames this chunked; the id must still be captured."""
        up = _FakeUpstream(
            os.path.join(sockdir, "docker.sock"), exec_id="chunkedexec", chunked=True,
        )
        await up.start()
        proxy = _proxy_for(sockdir)
        task = await _start_proxy(proxy)
        try:
            body = json.dumps({"Cmd": ["ls"]}).encode()
            await _send(
                proxy.listen_socket,
                (
                    f"POST /v1.43/containers/{OWNED}/exec HTTP/1.1\r\nHost: x\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode() + body,
            )
            await asyncio.sleep(0.1)
            assert "chunkedexec" in proxy._exec_ids
        finally:
            task.cancel()
            await up.stop()


class TestTransferEncodingStrictness:
    def test_plain_chunked_is_accepted(self):
        assert dp.request_framing_error(
            b"PUT /x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
        ) is None

    def test_xchunked_is_rejected(self):
        assert dp.request_framing_error(
            b"PUT /x HTTP/1.1\r\nTransfer-Encoding: xchunked\r\n\r\n"
        ) == "unsupported_transfer_encoding"

    def test_chunked_not_last_is_rejected(self):
        assert dp.request_framing_error(
            b"PUT /x HTTP/1.1\r\nTransfer-Encoding: chunked, gzip\r\n\r\n"
        ) == "unsupported_transfer_encoding"

    def test_gzip_then_chunked_is_accepted(self):
        assert dp.request_framing_error(
            b"PUT /x HTTP/1.1\r\nTransfer-Encoding: gzip, chunked\r\n\r\n"
        ) is None

    def test_two_transfer_encoding_headers_are_rejected(self):
        assert dp.request_framing_error(
            b"PUT /x HTTP/1.1\r\nTransfer-Encoding: gzip\r\nTransfer-Encoding: chunked\r\n\r\n"
        ) == "unsupported_transfer_encoding"

    def test_identical_duplicate_content_length_is_accepted(self):
        # Go's fixLength dedups equal values, so the two parsers agree.
        assert dp.request_framing_error(
            b"POST /x HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 5\r\n\r\n"
        ) is None
