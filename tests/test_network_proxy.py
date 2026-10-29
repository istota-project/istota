"""Tests for network proxy (CONNECT proxy on Unix socket)."""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from istota.sandbox.network_proxy import (
    BRIDGE_PORT,
    NetworkProxy,
    write_bridge_script,
)


@pytest.fixture
def proxy_sock():
    """Use /tmp for short paths (AF_UNIX limit is ~104 chars on macOS)."""
    import tempfile
    sock = Path(tempfile.gettempdir()) / f"istota-test-net-{os.getpid()}.sock"
    yield sock
    sock.unlink(missing_ok=True)


class TestNetworkProxyLifecycle:
    def test_start_stop(self, proxy_sock):
        proxy = NetworkProxy(proxy_sock, {"api.anthropic.com:443"}, trusted_roots={os.getpid()})
        proxy.start()
        assert proxy_sock.exists()
        proxy.stop()
        assert not proxy_sock.exists()

    def test_context_manager(self, proxy_sock):
        with NetworkProxy(proxy_sock, {"api.anthropic.com:443"}, trusted_roots={os.getpid()}):
            assert proxy_sock.exists()
        assert not proxy_sock.exists()

    def test_cleans_stale_socket(self, proxy_sock):
        proxy_sock.touch()
        with NetworkProxy(proxy_sock, set(), trusted_roots={os.getpid()}):
            assert proxy_sock.exists()

    def test_socket_is_owner_only(self, proxy_sock):
        """Socket must be 0o600 so other local users cannot connect."""
        with NetworkProxy(proxy_sock, set(), trusted_roots={os.getpid()}):
            mode = proxy_sock.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"

    def test_stop_returns_promptly(self, proxy_sock):
        """The accept loop must be woken, not polled out.

        It used to sit in ``accept()`` on a 1s socket timeout, so every
        teardown — one per task, and one per executor test — paid a full
        second waiting for the loop to notice the stop event.
        """
        proxy = NetworkProxy(proxy_sock, set(), trusted_roots={os.getpid()})
        proxy.start()
        started = time.monotonic()
        proxy.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 0.25, f"stop() took {elapsed:.2f}s"

    def test_double_stop_is_safe(self, proxy_sock):
        proxy = NetworkProxy(proxy_sock, set(), trusted_roots={os.getpid()})
        proxy.start()
        proxy.stop()
        proxy.stop()  # Should not raise

    def test_restart_after_stop_serves_again(self, proxy_sock):
        """start() must clear the stop state, or the second run is silently deaf."""
        proxy = NetworkProxy(proxy_sock, set(), trusted_roots={os.getpid()})
        proxy.start()
        proxy.stop()
        proxy.start()
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect(str(proxy_sock))
            sock.sendall(b"CONNECT blocked.example.com:443 HTTP/1.1\r\n\r\n")
            assert b"403" in sock.recv(1024)
            sock.close()
        finally:
            proxy.stop()


class TestNetworkProxyBlocking:
    """Test that non-allowlisted hosts are blocked with 403."""

    def test_blocked_host_returns_403(self, proxy_sock):
        with NetworkProxy(proxy_sock, {"api.anthropic.com:443"}, trusted_roots={os.getpid()}):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy_sock))
            client.sendall(b"CONNECT evil.example.com:443 HTTP/1.1\r\nHost: evil.example.com\r\n\r\n")
            response = client.recv(4096)
            client.close()
        assert b"403 Forbidden" in response

    def test_blocked_host_different_port(self, proxy_sock):
        with NetworkProxy(proxy_sock, {"api.anthropic.com:443"}, trusted_roots={os.getpid()}):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy_sock))
            # Same host, wrong port
            client.sendall(b"CONNECT api.anthropic.com:8080 HTTP/1.1\r\n\r\n")
            response = client.recv(4096)
            client.close()
        assert b"403 Forbidden" in response

    def test_non_connect_method_returns_405(self, proxy_sock):
        with NetworkProxy(proxy_sock, {"example.com:443"}, trusted_roots={os.getpid()}):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy_sock))
            client.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            response = client.recv(4096)
            client.close()
        assert b"405 Method Not Allowed" in response

    def test_malformed_request_returns_400(self, proxy_sock):
        with NetworkProxy(proxy_sock, set(), trusted_roots={os.getpid()}):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy_sock))
            client.sendall(b"BOGUS\r\n\r\n")
            response = client.recv(4096)
            client.close()
        assert b"400 Bad Request" in response


class TestNetworkProxyAllowed:
    """Test that allowlisted hosts are tunneled correctly."""

    def test_allowed_host_gets_200_and_tunnels(self, proxy_sock):
        """Verify CONNECT to an allowed host returns 200 and forwards data."""
        # Start a local TCP server to act as the upstream
        upstream_received = []
        upstream_ready = threading.Event()

        def upstream_server(srv_sock):
            upstream_ready.set()
            conn, _ = srv_sock.accept()
            data = conn.recv(4096)
            upstream_received.append(data)
            conn.sendall(b"UPSTREAM-REPLY")
            conn.close()

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        _, port = srv.getsockname()
        t = threading.Thread(target=upstream_server, args=(srv,), daemon=True)
        t.start()
        upstream_ready.wait()

        allowed = {f"127.0.0.1:{port}"}
        hello = _client_hello(None)
        with NetworkProxy(
            proxy_sock, allowed, trusted_roots={os.getpid()}, private_hosts=["127.0.0.1"],
        ):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy_sock))
            client.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())

            # Read the 200 response
            response = b""
            while b"\r\n\r\n" not in response:
                response += client.recv(4096)
            assert b"200 Connection Established" in response

            # An IP literal carries no SNI (RFC 6066), so none is required.
            client.sendall(hello)
            reply = client.recv(4096)
            client.close()

        srv.close()
        t.join(timeout=2)

        assert upstream_received[0] == hello
        assert reply == b"UPSTREAM-REPLY"

    def test_upstream_unreachable_returns_502(self, proxy_sock):
        """Verify that a failed upstream connect returns 502."""
        # Use a port that's definitely not listening
        allowed = {"127.0.0.1:1"}
        with NetworkProxy(
            proxy_sock, allowed, trusted_roots={os.getpid()}, private_hosts=["127.0.0.1"],
        ):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy_sock))
            client.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
            response = client.recv(4096)
            client.close()
        assert b"502 Bad Gateway" in response

    def test_connect_without_port_defaults_to_443(self, proxy_sock):
        """CONNECT host (no port) should default to 443."""
        # Use a non-routable host so upstream connect fails with 502
        host = "no-such-host.invalid"
        with NetworkProxy(proxy_sock, {f"{host}:443"}, trusted_roots={os.getpid()}):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy_sock))
            client.sendall(f"CONNECT {host} HTTP/1.1\r\n\r\n".encode())
            response = client.recv(4096)
            client.close()
        # Should be 502 (upstream unreachable), not 403 (blocked)
        assert b"502 Bad Gateway" in response


class TestBridgeScript:
    def test_write_bridge_script(self, tmp_path):
        path = tmp_path / "net-bridge"
        write_bridge_script(path)
        assert path.exists()
        content = path.read_text()
        assert "socket.AF_UNIX" in content
        assert "127.0.0.1" in content
        # Should be executable
        import stat
        assert path.stat().st_mode & stat.S_IXUSR

    def test_bridge_script_content_is_valid_python(self, tmp_path):
        path = tmp_path / "net-bridge"
        write_bridge_script(path)
        import py_compile
        py_compile.compile(str(path), doraise=True)

    def test_bridge_port_is_defined(self):
        assert BRIDGE_PORT == 18080


class TestNetworkProxyPeers:
    def test_unregistered_peer_never_opens_upstream(self, proxy_sock, monkeypatch):
        monkeypatch.setattr(
            "istota.sandbox.network_proxy.PEER_REGISTRATION_GRACE_SECONDS", 0.05,
        )
        with patch("istota.sandbox.network_proxy.socket.create_connection") as upstream:
            with NetworkProxy(proxy_sock, {"example.com:443"}):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(5)
                    client.connect(str(proxy_sock))
                    # No request bytes: authentication must precede parsing.
                    response = client.recv(4096)
        assert b"403 Forbidden" in response
        upstream.assert_not_called()

    def test_sibling_is_refused_but_registered_child_reaches_upstream(
        self, proxy_sock, monkeypatch,
    ):
        monkeypatch.setattr(
            "istota.sandbox.network_proxy.PEER_REGISTRATION_GRACE_SECONDS", 0.05,
        )
        script = r"""
import socket, sys
sys.stdin.readline()
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(5)
    client.connect(sys.argv[1])
    client.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
    print(client.recv(4096).decode())
"""
        with NetworkProxy(
            proxy_sock, {"127.0.0.1:1"}, trusted_roots=(), private_hosts=["127.0.0.1"],
        ) as proxy:
            with subprocess.Popen(
                [sys.executable, "-c", script, str(proxy_sock)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            ) as child:
                proxy.authorize_pid(child.pid)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sibling:
                    sibling.settimeout(5)
                    sibling.connect(str(proxy_sock))
                    sibling.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
                    assert b"403 Forbidden" in sibling.recv(4096)
                output, _ = child.communicate("go\n", timeout=10)
                assert child.returncode == 0
                assert "502 Bad Gateway" in output

    def test_missing_peer_identity_is_refused(self, proxy_sock, monkeypatch):
        monkeypatch.setattr("istota.sandbox.peer_process.peer_pid", lambda conn: None)
        with NetworkProxy(proxy_sock, {"example.com:443"}, trusted_roots={os.getpid()}):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(str(proxy_sock))
                assert b"403 Forbidden" in client.recv(4096)

    def test_registration_after_connect_is_waited_for(self, proxy_sock, monkeypatch):
        from istota.sandbox import peer_process

        connected = threading.Event()
        real_peer_pid = peer_process.peer_pid

        def observe_peer(client):
            pid = real_peer_pid(client)
            connected.set()
            return pid

        monkeypatch.setattr(peer_process, "peer_pid", observe_peer)
        with NetworkProxy(proxy_sock, {"127.0.0.1:1"}, private_hosts=["127.0.0.1"]) as proxy:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(str(proxy_sock))
                client.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
                assert connected.wait(2)
                proxy.authorize_pid(os.getpid())
                assert b"502 Bad Gateway" in client.recv(4096)


def _client_hello(server_name=None):
    """A real TLS ClientHello, as the first bytes a client sends down a tunnel."""
    import ssl

    context = ssl.create_default_context()
    if server_name is None:
        context.check_hostname = False
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=server_name)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


class _Upstream:
    """One loopback listener that records what the tunnel forwarded to it."""

    def __init__(self):
        self.received = []
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.srv.settimeout(5)
        self.port = self.srv.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(2)
            data = b""
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    conn.sendall(b"UPSTREAM-REPLY")
            except OSError:
                pass
            self.received.append(data)

    def close(self):
        self.srv.close()
        self.thread.join(timeout=5)


def _resolve_to_loopback(monkeypatch, name):
    original = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        return original("127.0.0.1" if host == name else host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def _connect(proxy_sock, target):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    client.connect(str(proxy_sock))
    client.sendall(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = client.recv(4096)
        if not chunk:
            break
        response += chunk
    return client, response


def _closed(client):
    try:
        return client.recv(4096) == b""
    except OSError:
        return True


class TestNetworkProxyAddressPolicy:
    """ISSUE-883: the allowlist names a host; the proxy must check what it reaches."""

    def test_allowed_name_resolving_to_loopback_is_refused(self, proxy_sock, monkeypatch):
        upstream = _Upstream()
        _resolve_to_loopback(monkeypatch, "allowed.example")
        target = f"allowed.example:{upstream.port}"
        try:
            with NetworkProxy(proxy_sock, {target}, trusted_roots={os.getpid()}):
                client, response = _connect(proxy_sock, target)
                client.close()
        finally:
            upstream.close()
        assert b"403 Forbidden" in response
        assert upstream.received == []

    def test_literal_private_address_is_refused(self, proxy_sock):
        with patch("istota.sandbox.network_proxy.socket.create_connection") as connect:
            with NetworkProxy(proxy_sock, {"169.254.169.254:80"}, trusted_roots={os.getpid()}):
                client, response = _connect(proxy_sock, "169.254.169.254:80")
                client.close()
        assert b"403 Forbidden" in response
        connect.assert_not_called()

    def test_private_hosts_grant_reaches_private_address(self, proxy_sock, monkeypatch):
        upstream = _Upstream()
        _resolve_to_loopback(monkeypatch, "git.lan")
        target = f"git.lan:{upstream.port}"
        hello = _client_hello("git.lan")
        try:
            with NetworkProxy(
                proxy_sock, {target}, trusted_roots={os.getpid()}, private_hosts=["Git.LAN."],
            ):
                client, response = _connect(proxy_sock, target)
                assert b"200 Connection Established" in response
                client.sendall(hello)
                assert client.recv(4096) == b"UPSTREAM-REPLY"
                client.close()
        finally:
            upstream.close()
        assert upstream.received == [hello]

    def test_connects_to_the_checked_address_not_the_name(self, proxy_sock, monkeypatch):
        """Pinned: one resolution decides, and the connect uses its answer."""
        calls = []

        def resolve(host, port, *args, **kwargs):
            calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        with patch(
            "istota.sandbox.network_proxy.socket.create_connection", side_effect=OSError("down"),
        ) as connect:
            with NetworkProxy(proxy_sock, {"public.example:443"}, trusted_roots={os.getpid()}):
                client, response = _connect(proxy_sock, "public.example:443")
                client.close()
        assert b"502 Bad Gateway" in response
        assert calls == ["public.example"]
        assert connect.call_args.args[0] == ("8.8.8.8", 443)


class TestReadClientHello:
    def _read(self, data):
        from istota.sandbox.network_proxy import read_client_hello

        a, b = socket.socketpair()
        with a, b:
            b.settimeout(2)
            a.sendall(data)
            a.shutdown(socket.SHUT_WR)
            return read_client_hello(b)

    def test_a_hello_split_across_records_is_reassembled(self):
        record = _client_hello("split.example")
        body = record[5:]
        head, tail = body[:40], body[40:]
        split = (
            record[:3] + len(head).to_bytes(2, "big") + head
            + record[:3] + len(tail).to_bytes(2, "big") + tail
        )
        assert self._read(split) == (split, "split.example")

    def test_a_truncated_hello_is_refused(self):
        assert self._read(_client_hello("cut.example")[:60]) is None


class TestNetworkProxyInnerAuthority:
    """ISSUE-883: a raw tunnel carries TLS for the name it was opened for."""

    @pytest.fixture
    def tunnel(self, proxy_sock, monkeypatch):
        upstream = _Upstream()
        _resolve_to_loopback(monkeypatch, "allowed.example")
        target = f"allowed.example:{upstream.port}"
        with NetworkProxy(
            proxy_sock, {target}, trusted_roots={os.getpid()}, private_hosts=["allowed.example"],
        ):
            client, response = _connect(proxy_sock, target)
            assert b"200 Connection Established" in response
            yield client, upstream
            client.close()
        upstream.close()

    def test_matching_sni_is_forwarded(self, tunnel):
        client, upstream = tunnel
        hello = _client_hello("allowed.example")
        client.sendall(hello)
        assert client.recv(4096) == b"UPSTREAM-REPLY"
        client.close()
        upstream.close()
        assert upstream.received == [hello]

    def test_mismatched_sni_closes_the_tunnel(self, tunnel):
        client, upstream = tunnel
        client.sendall(_client_hello("other.example"))
        assert _closed(client)
        upstream.close()
        assert upstream.received in ([], [b""])

    def test_missing_sni_closes_the_tunnel(self, tunnel):
        client, upstream = tunnel
        client.sendall(_client_hello(None))
        assert _closed(client)
        upstream.close()
        assert upstream.received in ([], [b""])

    def test_plain_http_naming_another_host_closes_the_tunnel(self, tunnel):
        """The audit's witness: a raw request with a different Host."""
        client, upstream = tunnel
        client.sendall(b"GET /collect?data=canary HTTP/1.1\r\nHost: other.example\r\n\r\n")
        assert _closed(client)
        upstream.close()
        assert upstream.received in ([], [b""])
