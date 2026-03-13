"""Unix socket CONNECT proxy for network isolation.

Runs in the host network namespace, listens on a Unix socket.
Inside the bwrap sandbox (--unshare-net), a TCP-to-Unix bridge
forwards connections from 127.0.0.1:PORT to this socket.
Only HTTPS CONNECT requests to allowlisted host:port pairs are tunneled.
"""

import logging
import socket
import threading
from pathlib import Path

logger = logging.getLogger("istota.network_proxy")

# Bridge port inside the sandbox network namespace.  Deterministic since
# each task gets its own namespace — no port conflicts.
BRIDGE_PORT = 18080

# Bridge script written to .developer/net-bridge inside the sandbox.
# Listens on 127.0.0.1:PORT, forwards each TCP connection to the proxy
# Unix socket.  Runs as a background process started by the shell wrapper.
BRIDGE_SCRIPT = """\
#!/usr/bin/env python3
import socket, sys, threading

def bridge(tcp_conn, unix_path):
    unix_conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix_conn.connect(unix_path)
    def forward(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data: break
                dst.sendall(data)
        except OSError: pass
        finally:
            try: src.close()
            except: pass
            try: dst.close()
            except: pass
    threading.Thread(target=forward, args=(tcp_conn, unix_conn), daemon=True).start()
    forward(unix_conn, tcp_conn)

sock_path, port = sys.argv[1], int(sys.argv[2])
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port))
srv.listen(32)
while True:
    conn, _ = srv.accept()
    threading.Thread(target=bridge, args=(conn, sock_path), daemon=True).start()
"""


def write_bridge_script(path: Path) -> None:
    """Write the TCP-to-Unix bridge script to the given path."""
    path.write_text(BRIDGE_SCRIPT)
    path.chmod(0o700)


class NetworkProxy:
    """CONNECT proxy on a Unix socket with domain allowlist.

    Usage::

        with NetworkProxy(sock_path, allowed_hosts) as proxy:
            # Claude subprocess runs in sandbox with --unshare-net
            ...

    No MITM, no credential injection.  Pure connectivity gate.
    TLS is end-to-end between the client and upstream.
    """

    def __init__(
        self,
        socket_path: Path,
        allowed_hosts: set[str],  # {"api.anthropic.com:443", ...}
        timeout: int = 300,
    ):
        self.socket_path = socket_path
        self.allowed_hosts = allowed_hosts
        self.timeout = timeout
        self._server_sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(str(self.socket_path))
        self._server_sock.listen(32)
        self._server_sock.settimeout(1.0)

        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="network-proxy",
        )
        self._thread.start()
        logger.debug(
            "Network proxy started on %s (allowed: %s)",
            self.socket_path, self.allowed_hosts,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        logger.debug("Network proxy stopped")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                conn, _ = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            threading.Thread(
                target=self._handle_connection, args=(conn,),
                daemon=True, name="network-proxy-handler",
            ).start()

    def _handle_connection(self, client: socket.socket) -> None:
        try:
            client.settimeout(30)
            # Read until we have the full request line
            data = b""
            while b"\r\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data += chunk

            first_line = data.split(b"\r\n")[0].decode("utf-8", errors="replace")
            parts = first_line.split()
            if len(parts) < 2:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return

            method = parts[0].upper()

            if method == "CONNECT":
                # Consume remaining headers up to blank line
                while b"\r\n\r\n" not in data:
                    chunk = client.recv(4096)
                    if not chunk:
                        return
                    data += chunk

                target = parts[1]
                if ":" in target:
                    host, port_str = target.rsplit(":", 1)
                    try:
                        port = int(port_str)
                    except ValueError:
                        client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                        return
                else:
                    host = target
                    port = 443

                self._handle_connect(client, host, port)
            else:
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")

        except Exception:
            logger.debug("Error handling network proxy connection", exc_info=True)
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _handle_connect(
        self, client: socket.socket, host: str, port: int,
    ) -> None:
        target = f"{host}:{port}"
        if target not in self.allowed_hosts:
            logger.debug("Network proxy blocked: %s", target)
            client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return

        try:
            upstream = socket.create_connection((host, port), timeout=10)
        except OSError as e:
            logger.debug(
                "Network proxy upstream connect failed: %s: %s", target, e,
            )
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        try:
            self._bridge(client, upstream)
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    @staticmethod
    def _bridge(a: socket.socket, b: socket.socket) -> None:
        """Bidirectional forwarding between two sockets."""

        def forward(src: socket.socket, dst: socket.socket) -> None:
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t = threading.Thread(target=forward, args=(a, b), daemon=True)
        t.start()
        forward(b, a)
        t.join(timeout=5)
