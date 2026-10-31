"""Unix socket CONNECT proxy for network isolation.

Runs in the host network namespace, listens on a Unix socket.
Inside the bwrap sandbox (--unshare-net), a TCP-to-Unix bridge
forwards connections from 127.0.0.1:PORT to this socket.
HTTPS CONNECT requests to allowlisted host:port pairs are tunneled. With the
credential broker enabled, bound HTTP destinations use its grant-checked parser.

The allowlist names a host, so the proxy checks what the name reaches
(ISSUE-883): every upstream connect, raw or brokered, goes to an address
`net_guard` passed, resolved once and pinned, and a non-public address only
for a name on ``private_hosts``. A raw tunnel then carries only TLS whose
ClientHello names the CONNECT host. The HTTP ``Host`` inside that TLS is not
visible here, so a shared front that routes on it (domain fronting) is still
reachable through an allowed name; only the broker's interception sees it.
"""

import ipaddress
import logging
import socket
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from istota.lib.egress_log import append_refusal
from istota.sandbox import net_guard, peer_process
from istota.sandbox.unix_server import UnixSocketServer, arm_until, drain_queued

logger = logging.getLogger("istota.sandbox.network_proxy")

# Owner-only, so no other local user can reach the CONNECT allowlist. This
# proxy's own decision, stated here rather than inherited from the server
# lifecycle it is handed to.
SOCKET_MODE = 0o600

# Listen backlog. Every connection is a sandboxed process reaching for the
# network, and the bridge opens one per outbound TCP connection.
LISTEN_BACKLOG = 32

# Connections one task may hold open; a raw tunnel costs two host threads.
DEFAULT_MAX_CONNECTIONS = 128
BUSY_REPLY = b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n"

# One request head, the size h11 holds the broker's to, and the whole time it
# may take to arrive (#890).
MAX_REQUEST_HEAD_BYTES = 65536
REQUEST_DEADLINE_SECONDS = 30

# The child may connect before its spawn callback registers the root.
PEER_REGISTRATION_GRACE_SECONDS = 2.0

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
            try: dst.shutdown(socket.SHUT_WR)
            except OSError: pass
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


def _enable_tcp_keepalive(sock: socket.socket) -> None:
    """Enable TCP keepalive on a socket to detect dead connections."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    # Linux-specific tuning: start probes after 60s idle, every 10s, fail after 6
    for opt, val in [
        ("TCP_KEEPIDLE", 60),
        ("TCP_KEEPINTVL", 10),
        ("TCP_KEEPCNT", 6),
    ]:
        if hasattr(socket, opt):
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)


class UpstreamRefused(Exception):
    """The destination resolved to a non-public address no grant covers."""


class _RequestTooLarge(Exception):
    """The request head passed `MAX_REQUEST_HEAD_BYTES`."""


class _RequestTimedOut(Exception):
    """The request head did not arrive by its deadline."""


def _read_until(client: socket.socket, data: bytes, marker: bytes, deadline: float) -> bytes | None:
    """``data`` grown until it holds ``marker``; None if the client closed first."""
    while marker not in data:
        if len(data) > MAX_REQUEST_HEAD_BYTES:
            raise _RequestTooLarge()
        try:
            arm_until(client, deadline)
            chunk = client.recv(4096)
        except TimeoutError:
            raise _RequestTimedOut() from None
        if not chunk:
            return None
        data += chunk
    if len(data.split(marker, 1)[0]) > MAX_REQUEST_HEAD_BYTES:
        raise _RequestTooLarge()
    return data


# A ClientHello is a few KiB; one larger than this is refused, not buffered.
MAX_CLIENT_HELLO_BYTES = 65536
# RFC 8446 5.1: a plaintext record carries at most 2^14 bytes.
_MAX_RECORD_BYTES = 16384


def _unbracket(host: str) -> str:
    """CONNECT brackets an IPv6 literal; sockets and the address rule do not."""
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def checked_addresses(host: str, port: int, *, private_hosts) -> list[str]:
    """The addresses an upstream connect to ``host`` may use, resolved once.

    Raises `UpstreamRefused` when one is non-public and the name is not on
    ``private_hosts``; empty when nothing resolved.
    """
    addresses, refused = net_guard.checked_addresses(
        _unbracket(host), port, private_hosts=private_hosts,
    )
    if refused is not None:
        raise UpstreamRefused()
    return addresses


def connect_addresses(addresses: list[str], port: int, *, timeout: float = 10) -> socket.socket:
    """A connection to the first of ``addresses`` that accepts; OSError if none."""
    error: OSError = OSError("no address resolved")
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout=timeout)
        except OSError as exc:
            error = exc
    raise error


def checked_connection(host: str, port: int, *, private_hosts, timeout: float = 10) -> socket.socket:
    """`checked_addresses` then `connect_addresses`: the one upstream connect."""
    return connect_addresses(
        checked_addresses(host, port, private_hosts=private_hosts), port, timeout=timeout,
    )


def _recv_exact(sock: socket.socket, size: int, deadline: float | None = None) -> bytes | None:
    data = b""
    while len(data) < size:
        if deadline is not None:
            arm_until(sock, deadline)
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _u16(data: bytes, pos: int) -> int:
    if pos + 2 > len(data):
        raise ValueError("truncated")
    return int.from_bytes(data[pos:pos + 2], "big")


def _server_name(hello: bytes) -> str | None:
    """The one host_name in a ClientHello body's server_name extension, or None.

    More than one server_name extension, or more than one host name, is
    malformed: a server and this check could otherwise read different ones.
    """
    pos = 2 + 32  # legacy_version, random
    if pos >= len(hello):
        raise ValueError("truncated")
    pos += 1 + hello[pos]  # session id
    pos += 2 + _u16(hello, pos)  # cipher suites
    if pos >= len(hello):
        raise ValueError("truncated")
    pos += 1 + hello[pos]  # compression methods
    if pos == len(hello):
        return None
    end = pos + 2 + _u16(hello, pos)
    pos += 2
    if end != len(hello):
        raise ValueError("extensions length")
    names = []
    while pos < end:
        kind, length = _u16(hello, pos), _u16(hello, pos + 2)
        pos += 4
        if pos + length > end:
            raise ValueError("extension length")
        if kind == 0:
            data = hello[pos:pos + length]
            if 2 + _u16(data, 0) != len(data):
                raise ValueError("server_name length")
            inner = 2
            while inner < len(data):
                if inner + 3 > len(data):
                    raise ValueError("truncated")
                name_type, name_length = data[inner], _u16(data, inner + 1)
                inner += 3
                if inner + name_length > len(data):
                    raise ValueError("truncated")
                if name_type == 0:
                    names.append(data[inner:inner + name_length].decode("ascii"))
                inner += name_length
        pos += length
    if len(names) > 1:
        raise ValueError("several server names")
    return names[0] if names else None


def read_client_hello(
    sock: socket.socket, deadline: float | None = None,
) -> tuple[bytes, str | None] | None:
    """The records carrying a client's first ClientHello, and the name it asks for.

    None when the first bytes are not a TLS handshake, the hello is malformed
    or over `MAX_CLIENT_HELLO_BYTES`, the client closed first, or ``deadline``
    passed. The records are returned whole, to be forwarded upstream unchanged.
    """
    raw = b""
    handshake = b""
    try:
        while True:
            header = _recv_exact(sock, 5, deadline)
            if header is None or header[0] != 0x16 or header[1] != 0x03:
                return None
            length = _u16(header, 3)
            if not 0 < length <= _MAX_RECORD_BYTES:
                return None
            body = _recv_exact(sock, length, deadline)
            if body is None:
                return None
            raw += header + body
            handshake += body
            if len(raw) > MAX_CLIENT_HELLO_BYTES or handshake[0] != 0x01:
                return None
            if len(handshake) < 4:
                continue
            needed = 4 + int.from_bytes(handshake[1:4], "big")
            if needed > MAX_CLIENT_HELLO_BYTES:
                return None
            if len(handshake) >= needed:
                return raw, _server_name(handshake[4:needed])
    except (ValueError, OSError):
        return None


def _sni_matches(server_name: str | None, host: str) -> bool:
    """Whether a ClientHello's name is the CONNECT host's.

    RFC 6066 leaves IP literals out of SNI, so a tunnel opened to an address
    may carry none; the broker's `check_sni` makes the same exception.
    """
    host = _unbracket(host)
    if server_name is None:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return False
        return True
    return net_guard.normal_host(server_name) == net_guard.normal_host(host)


def write_bridge_script(path: Path) -> None:
    """Write the TCP-to-Unix bridge script to the given path."""
    path.write_text(BRIDGE_SCRIPT)
    path.chmod(0o700)


class NetworkProxy:
    """CONNECT proxy on a Unix socket with domain allowlist.

    Usage::

        with NetworkProxy(sock_path, allowed_hosts) as proxy:
            # Register the brain child through its spawn callback.
            proxy.authorize_pid(child.pid)
            # Claude subprocess runs in sandbox with --unshare-net
            ...

    Raw TLS unless an optional broker covers the host in the task snapshot.
    TLS is end-to-end between the client and upstream.
    """

    def __init__(
        self,
        socket_path: Path,
        allowed_hosts: set[str],  # {"api.anthropic.com:443", ...}
        *,
        trusted_roots: Iterable[int] = (),
        broker=None,
        private_hosts: Iterable[str] = (),
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        refusal_log: Path | None = None,
    ):
        self.allowed_hosts = allowed_hosts
        # A limited task's `egress-refused.jsonl`; None records nothing.
        self.refusal_log = refusal_log
        self.broker = broker
        # Exact names whose addresses may be non-public; nothing is implied.
        self.private_hosts = net_guard.host_set(private_hosts)
        # Raw tunnels' upstream sockets, which have no timeout: `stop` wakes them.
        self._upstreams: set[socket.socket] = set()
        self._upstreams_lock = threading.Lock()
        self._peer_roots = peer_process.PeerRoots()
        for pid in trusted_roots:
            self.authorize_pid(pid)
        self._server = UnixSocketServer(
            socket_path,
            # Resolved per connection, not captured here: the accept loop
            # used to call `self._handle_connection` at accept time, and a
            # test substitutes it on the instance.
            lambda client: self._handle_connection(client),
            name="network-proxy",
            label="Network proxy",
            socket_mode=SOCKET_MODE,
            backlog=LISTEN_BACKLOG,
            logger=logger,
            max_connections=max_connections,
            busy_reply=BUSY_REPLY,
        )

    def authorize_pid(self, pid: int) -> None:
        """Serve only this process and its descendants, pinned to its lifetime."""
        if not self._peer_roots.authorize(pid):
            logger.warning("network_proxy_root_unregistered pid=%s", pid)

    @property
    def socket_path(self) -> Path:
        """The path the server is bound to. One copy, held by the server."""
        return self._server.socket_path

    def start(self) -> None:
        self._server.start()
        logger.debug(
            "Network proxy started on %s (allowed: %s)",
            self.socket_path, self.allowed_hosts,
        )

    def stop(self) -> None:
        self._server.stop()
        with self._upstreams_lock:
            upstreams = list(self._upstreams)
        for upstream in upstreams:
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        logger.debug("Network proxy stopped")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def _handle_connection(self, client: socket.socket) -> None:
        try:
            pid = peer_process.peer_pid(client)
            if not self._peer_roots.contains(
                pid, grace_seconds=PEER_REGISTRATION_GRACE_SECONDS,
            ):
                logger.warning("network_proxy_rejected pid=%s reason=peer", pid)
                # Drain only bytes already queued, without parsing a request.
                # Unread bytes at close can discard the refusal on Linux.
                client.setblocking(False)
                try:
                    for _ in range(16):
                        if not client.recv(4096):
                            break
                except OSError:
                    pass
                client.settimeout(1)
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            deadline = time.monotonic() + REQUEST_DEADLINE_SECONDS
            data = _read_until(client, b"", b"\r\n", deadline)
            if data is None:
                return

            first_line = data.split(b"\r\n")[0].decode("utf-8", errors="replace")
            parts = first_line.split()
            if len(parts) < 2:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return

            method = parts[0].upper()

            if method == "CONNECT":
                # Consume remaining headers up to blank line
                data = _read_until(client, data, b"\r\n\r\n", deadline)
                if data is None:
                    return

                client.settimeout(30)
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
            elif self.broker is not None:
                client.settimeout(30)
                self._handle_http(client, parts[1], data)
            else:
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")

        except _RequestTooLarge:
            self._answer(client, b"HTTP/1.1 431 Request Header Fields Too Large\r\nConnection: close\r\n\r\n")
        except _RequestTimedOut:
            self._answer(client, b"HTTP/1.1 408 Request Timeout\r\nConnection: close\r\n\r\n")
        except Exception:
            logger.debug("Error handling network proxy connection", exc_info=True)
        finally:
            try:
                client.close()
            except OSError:
                pass

    @staticmethod
    def _answer(client: socket.socket, reply: bytes) -> None:
        """Best-effort, because a peer past a limit may no longer be reading."""
        try:
            drain_queued(client)
            client.settimeout(1)
            client.sendall(reply)
        except OSError:
            pass

    def _handle_http(self, client, url, data):
        from urllib.parse import urlsplit
        from istota.credentials.broker.bindings import credential_host
        from istota.credentials.broker.intercept import _requests
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "http" or parsed.fragment:
                raise ValueError("expected HTTP URL")
            target = credential_host(url, allow_http=True)
            host = parsed.hostname
            port = parsed.port or 80
            authority = f"[{host}]" if ":" in host else host
            if f"{authority}:{port}" not in self.allowed_hosts:
                append_refusal(self.refusal_log, "connect", host.lower())
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return
            if not self.broker.covers(target):
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return
        except ValueError:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            return
        _requests(self.broker, client, host, port, target, plain_http=True, initial_data=data,
                  private_hosts=self.private_hosts)

    def _handle_connect(
        self, client: socket.socket, host: str, port: int,
    ) -> None:
        target = f"{host}:{port}"
        if target not in self.allowed_hosts:
            # A refused name can carry data, so a limited task's stays in its refusal log.
            logger.debug("Network proxy blocked: %s", "(not logged)" if self.refusal_log else target)
            append_refusal(self.refusal_log, "connect", host.lower())
            client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return

        try:
            addresses = checked_addresses(host, port, private_hosts=self.private_hosts)
        except UpstreamRefused:
            logger.warning("network_proxy_rejected target=%s reason=address", target)
            client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return

        if self.broker is not None:
            from istota.credentials.broker.bindings import https_host
            from istota.credentials.broker.intercept import intercept
            if self.broker.covers(https_host(f"https://{host}:{port}")):
                # Each upstream connect it makes is checked again, by name.
                intercept(self.broker, client, host, port, private_hosts=self.private_hosts)
                return

        try:
            upstream = connect_addresses(addresses, port)
        except OSError as e:
            logger.debug(
                "Network proxy upstream connect failed: %s: %s", target, e,
            )
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        with self._upstreams_lock:
            self._upstreams.add(upstream)
        try:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            hello = read_client_hello(client, time.monotonic() + REQUEST_DEADLINE_SECONDS)
            if hello is None or not _sni_matches(hello[1], host):
                logger.warning("network_proxy_rejected target=%s reason=sni", target)
                return
            upstream.sendall(hello[0])

            # Clear the 30s request-parsing timeout — the tunnel may be
            # idle for long stretches during streaming API responses.
            client.settimeout(None)
            upstream.settimeout(None)

            # Enable TCP keepalive on upstream to detect dead connections
            # (NAT timeouts, load balancer drops, etc.)
            _enable_tcp_keepalive(upstream)

            logger.debug("Network proxy tunnel established: %s", target)

            self._bridge(client, upstream, target)
        finally:
            with self._upstreams_lock:
                self._upstreams.discard(upstream)
            try:
                upstream.close()
            except OSError:
                pass

    @staticmethod
    def _bridge(
        a: socket.socket, b: socket.socket, target: str = "",
    ) -> None:
        """Bidirectional forwarding between two sockets."""

        def forward(
            src: socket.socket, dst: socket.socket, label: str,
        ) -> None:
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError as e:
                logger.debug("Network proxy bridge %s error: %s", label, e)
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        label = f"[{target}]" if target else ""
        t = threading.Thread(
            target=forward, args=(a, b, f"{label} client→upstream"),
            daemon=True,
        )
        t.start()
        forward(b, a, f"{label} upstream→client")
        t.join(timeout=5)
        # The handler's connection slot is what bounds this thread, so it may
        # not outlive the handler. On Linux closing a socket does not wake a
        # blocked `recv`; a shutdown does.
        if t.is_alive():
            for sock in (a, b):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            t.join()
        logger.debug("Network proxy tunnel closed: %s", target)
