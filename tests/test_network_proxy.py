"""Tests for network proxy (CONNECT proxy on Unix socket)."""

import os
import socket
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from istota.network_proxy import (
    BRIDGE_PORT,
    BRIDGE_SCRIPT,
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
        proxy = NetworkProxy(proxy_sock, {"api.anthropic.com:443"})
        proxy.start()
        assert proxy_sock.exists()
        proxy.stop()
        assert not proxy_sock.exists()

    def test_context_manager(self, proxy_sock):
        with NetworkProxy(proxy_sock, {"api.anthropic.com:443"}):
            assert proxy_sock.exists()
        assert not proxy_sock.exists()

    def test_cleans_stale_socket(self, proxy_sock):
        proxy_sock.touch()
        with NetworkProxy(proxy_sock, set()):
            assert proxy_sock.exists()

    def test_socket_is_owner_only(self, proxy_sock):
        """Socket must be 0o600 so other local users cannot connect."""
        with NetworkProxy(proxy_sock, set()):
            mode = proxy_sock.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


class TestNetworkProxyBlocking:
    """Test that non-allowlisted hosts are blocked with 403."""

    def test_blocked_host_returns_403(self, proxy_sock):
        with NetworkProxy(proxy_sock, {"api.anthropic.com:443"}):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy_sock))
            client.sendall(b"CONNECT evil.example.com:443 HTTP/1.1\r\nHost: evil.example.com\r\n\r\n")
            response = client.recv(4096)
            client.close()
        assert b"403 Forbidden" in response

    def test_blocked_host_different_port(self, proxy_sock):
        with NetworkProxy(proxy_sock, {"api.anthropic.com:443"}):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy_sock))
            # Same host, wrong port
            client.sendall(b"CONNECT api.anthropic.com:8080 HTTP/1.1\r\n\r\n")
            response = client.recv(4096)
            client.close()
        assert b"403 Forbidden" in response

    def test_non_connect_method_returns_405(self, proxy_sock):
        with NetworkProxy(proxy_sock, {"example.com:443"}):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy_sock))
            client.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            response = client.recv(4096)
            client.close()
        assert b"405 Method Not Allowed" in response

    def test_malformed_request_returns_400(self, proxy_sock):
        with NetworkProxy(proxy_sock, set()):
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
        with NetworkProxy(proxy_sock, allowed):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy_sock))
            client.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())

            # Read the 200 response
            response = b""
            while b"\r\n\r\n" not in response:
                response += client.recv(4096)
            assert b"200 Connection Established" in response

            # Send data through the tunnel
            client.sendall(b"HELLO-FROM-CLIENT")
            reply = client.recv(4096)
            client.close()

        srv.close()
        t.join(timeout=2)

        assert upstream_received[0] == b"HELLO-FROM-CLIENT"
        assert reply == b"UPSTREAM-REPLY"

    def test_upstream_unreachable_returns_502(self, proxy_sock):
        """Verify that a failed upstream connect returns 502."""
        # Use a port that's definitely not listening
        allowed = {"127.0.0.1:1"}
        with NetworkProxy(proxy_sock, allowed):
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
        with NetworkProxy(proxy_sock, {f"{host}:443"}):
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
