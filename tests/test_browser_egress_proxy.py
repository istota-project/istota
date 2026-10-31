"""The forward proxy every Chrome connection goes through (#884).

The proxy runs for real on a loopback port, on its own thread's event loop, and
is driven over real sockets the way Chrome drives it. Only the resolver and the
outbound connect are replaced: a public name cannot be made to resolve to a
local test listener any other way.
"""

import asyncio
import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

BROWSER = Path(__file__).resolve().parents[1] / "docker" / "browser"
PUBLIC = "93.184.216.34"


@pytest.fixture
def proxy_module(monkeypatch):
    monkeypatch.syspath_prepend(str(BROWSER))
    for name in ("lib", "lib.istota_net_guard"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location("_egress_proxy", BROWSER / "egress_proxy.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "_egress_proxy", module)
    spec.loader.exec_module(module)
    return module


class Upstream:
    """A listener standing in for the far end: records bytes, answers once."""

    def __init__(self):
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.received = []
        self.accepted = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        self.accepted.set()
        with conn:
            conn.settimeout(5)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.received.append(data)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            except OSError:
                pass

    def close(self):
        self.sock.close()


@pytest.fixture
def upstream():
    server = Upstream()
    yield server
    server.close()


@pytest.fixture
def run_proxy(proxy_module, upstream):
    """Start the proxy; ``dns`` maps a name to the addresses it resolves to."""
    started = []

    def start(dns, private_hosts=(), far_port=None):
        connected = []

        def resolve(host, port):
            return dns.get(host, [])

        async def connect(address, port):
            connected.append((address, port))
            return await asyncio.open_connection("127.0.0.1", far_port or upstream.port)

        loop = asyncio.new_event_loop()
        ready = threading.Event()
        box = {}

        async def main():
            box["server"] = await proxy_module.serve(
                "127.0.0.1", 0, private_hosts=private_hosts, resolve=resolve, connect=connect,
            )
            ready.set()

        thread = threading.Thread(
            target=lambda: (loop.run_until_complete(main()), loop.run_forever()), daemon=True,
        )
        thread.start()
        assert ready.wait(5)
        started.append((loop, thread, box["server"]))
        return box["server"].sockets[0].getsockname()[1], connected

    yield start

    for loop, thread, server in started:
        async def stop(server=server):
            server.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(stop(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def _ask(port, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        out = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                out += chunk
        except TimeoutError:
            pass
        return out


def _connect_request(authority: str) -> bytes:
    return f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode()


class TestRefused:
    @pytest.mark.parametrize("authority", [
        "127.0.0.1:9223",  # the control API
        "localhost.:9301",  # a debugging port, by a name
        "169.254.169.254:80",  # cloud metadata
        "10.0.0.5:443",
        "[::1]:9223",
        "[::ffff:127.0.0.1]:9223",
        "0.0.0.0:9223",
    ])
    def test_connect_to_a_private_address(self, run_proxy, upstream, authority):
        port, connected = run_proxy({"localhost.": ["127.0.0.1"]})
        reply = _ask(port, _connect_request(authority))
        assert reply.startswith(b"HTTP/1.1 403")
        assert connected == []
        assert not upstream.accepted.is_set()

    def test_a_name_with_one_private_address_among_public_ones(self, run_proxy):
        port, connected = run_proxy({"mixed.example": [PUBLIC, "10.0.0.5"]})
        assert _ask(port, _connect_request("mixed.example:443")).startswith(b"HTTP/1.1 403")
        assert connected == []

    def test_plain_http_to_a_private_address_says_why(self, run_proxy):
        port, connected = run_proxy({})
        reply = _ask(port, b"GET http://127.0.0.1:9223/health HTTP/1.1\r\nHost: 127.0.0.1:9223\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 403")
        assert b"private address" in reply
        assert connected == []

    def test_each_request_resolves_again(self, run_proxy):
        """A rebinding answer: public on one lookup, private on the next."""
        dns = {"rebind.example": [PUBLIC]}
        port, connected = run_proxy(dns)
        assert _ask(port, _connect_request("rebind.example:443")).startswith(b"HTTP/1.1 200")
        dns["rebind.example"] = ["127.0.0.1"]
        assert _ask(port, _connect_request("rebind.example:443")).startswith(b"HTTP/1.1 403")
        assert connected == [(PUBLIC, 443)]

    @pytest.mark.parametrize("request_bytes", [
        b"GET /origin-form HTTP/1.1\r\nHost: example.com\r\n\r\n",
        b"GET ftp://example.com/ HTTP/1.1\r\n\r\n",
        b"CONNECT nonsense HTTP/1.1\r\n\r\n",
        b"not http at all\r\n\r\n",
    ])
    def test_a_request_it_cannot_place(self, run_proxy, request_bytes):
        port, connected = run_proxy({"example.com": [PUBLIC]})
        assert _ask(port, request_bytes).startswith(b"HTTP/1.1 400")
        assert connected == []

    def test_a_head_past_the_limit(self, run_proxy, proxy_module):
        port, connected = run_proxy({})
        filler = b"x" * (proxy_module.MAX_HEAD_BYTES + 1024)
        assert _ask(port, b"GET http://a.example/ HTTP/1.1\r\nX: " + filler + b"\r\n\r\n").startswith(b"HTTP/1.1 431")
        assert connected == []

    def test_a_name_that_does_not_resolve(self, run_proxy):
        port, connected = run_proxy({})
        assert _ask(port, _connect_request("gone.example:443")).startswith(b"HTTP/1.1 502")
        assert connected == []


class TestAllowed:
    def test_connect_tunnels_to_the_address_it_checked(self, run_proxy, upstream):
        port, connected = run_proxy({"public.example": [PUBLIC]})
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(_connect_request("public.example:443"))
            assert sock.recv(4096).startswith(b"HTTP/1.1 200")
            sock.sendall(b"inside the tunnel\r\n\r\n")
            assert sock.recv(4096).startswith(b"HTTP/1.1 200 OK")
        assert connected == [(PUBLIC, 443)]
        assert upstream.received == [b"inside the tunnel\r\n\r\n"]

    def test_plain_http_goes_out_in_origin_form_without_proxy_headers(self, run_proxy, upstream):
        port, connected = run_proxy({"public.example": [PUBLIC]})
        reply = _ask(port, (
            b"GET http://public.example/a/b?c=1 HTTP/1.1\r\n"
            b"Host: public.example\r\n"
            b"Proxy-Connection: keep-alive\r\n"
            b"Proxy-Authorization: Basic x\r\n"
            b"Connection: keep-alive\r\n"
            b"User-Agent: test\r\n\r\n"
        ))
        assert reply.endswith(b"ok")
        assert connected == [(PUBLIC, 80)]
        sent = upstream.received[0]
        assert sent.startswith(b"GET /a/b?c=1 HTTP/1.1\r\n")
        assert b"Host: public.example\r\n" in sent
        assert b"User-Agent: test\r\n" in sent
        assert b"Connection: close\r\n" in sent
        assert b"roxy-" not in sent
        assert b"keep-alive" not in sent

    def test_a_listed_name_reaches_its_private_address(self, run_proxy):
        port, connected = run_proxy({"wiki.corp": ["10.0.0.5"]}, private_hosts=["wiki.corp"])
        assert _ask(port, _connect_request("wiki.corp:443")).startswith(b"HTTP/1.1 200")
        assert connected == [("10.0.0.5", 443)]

    @pytest.mark.parametrize("address", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "0.0.0.0"])
    def test_listing_a_name_never_reaches_this_container(self, run_proxy, address):
        """The API and the debugging ports stay out of reach of a grant."""
        port, connected = run_proxy({"odd.corp": [address]}, private_hosts=["odd.corp"])
        assert _ask(port, _connect_request("odd.corp:9223")).startswith(b"HTTP/1.1 403")
        assert connected == []


class Stubborn:
    """A far end that answers once, ignores every close, and stays open."""

    def __init__(self, answer=b""):
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.answer = answer
        self.held = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.held.append(conn)
            if self.answer:
                conn.recv(4096)
                conn.sendall(self.answer)

    def close(self):
        for conn in self.held:
            conn.close()
        self.sock.close()


class TestAFarEndCannotHoldTheProxy:
    def test_a_far_end_ignoring_a_close_gives_its_slot_back(self, run_proxy, proxy_module, monkeypatch):
        monkeypatch.setattr(proxy_module, "MAX_CONNECTIONS", 1)
        monkeypatch.setattr(proxy_module, "RELAY_TICK_S", 0.05)
        monkeypatch.setattr(proxy_module, "CLOSE_GRACE_S", 0.2)
        far = Stubborn()
        try:
            port, _ = run_proxy({"public.example": [PUBLIC]}, far_port=far.port)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(_connect_request("public.example:443"))
                assert sock.recv(4096).startswith(b"HTTP/1.1 200")
            deadline = time.monotonic() + 5
            while True:
                with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                    sock.sendall(_connect_request("public.example:443"))
                    reply = sock.recv(4096)
                if reply.startswith(b"HTTP/1.1 200") or time.monotonic() > deadline:
                    break
                time.sleep(0.05)
            assert reply.startswith(b"HTTP/1.1 200")
        finally:
            far.close()

    def test_a_silent_tunnel_is_closed(self, run_proxy, proxy_module, monkeypatch):
        monkeypatch.setattr(proxy_module, "RELAY_TICK_S", 0.05)
        monkeypatch.setattr(proxy_module, "TUNNEL_IDLE_S", 0.2)
        far = Stubborn()
        try:
            port, _ = run_proxy({"public.example": [PUBLIC]}, far_port=far.port)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(_connect_request("public.example:443"))
                assert sock.recv(4096).startswith(b"HTTP/1.1 200")
                assert sock.recv(4096) == b""
        finally:
            far.close()

    def test_a_plain_response_says_the_connection_closes(self, run_proxy):
        far = Stubborn(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n"
                       b"Keep-Alive: timeout=5\r\n\r\nok")
        try:
            port, _ = run_proxy({"public.example": [PUBLIC]}, far_port=far.port)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(b"GET http://public.example/ HTTP/1.1\r\nHost: public.example\r\n\r\n")
                reply = b""
                while b"ok" not in reply:
                    reply += sock.recv(4096)
            assert b"Connection: close\r\n" in reply
            assert b"keep-alive" not in reply.lower()
            assert reply.endswith(b"\r\n\r\nok")
        finally:
            far.close()

    def test_a_lookup_past_its_deadline_is_a_bad_gateway(self, run_proxy, proxy_module, monkeypatch):
        monkeypatch.setattr(proxy_module, "DNS_TIMEOUT_S", 0.05)
        release = threading.Event()

        class SlowDns(dict):
            def get(self, host, default=None):
                release.wait(5)
                return default

        port, connected = run_proxy(SlowDns())
        try:
            assert _ask(port, _connect_request("slow.example:443")).startswith(b"HTTP/1.1 502")
            assert connected == []
        finally:
            release.set()


class TestThePdfReaderTakesTheSameGrant:
    def test_a_listed_name_on_loopback_is_refused(self, proxy_module, monkeypatch):
        import pdf_text

        with pytest.raises(pdf_text._Refused):
            pdf_text._target(
                "http://odd.corp/a.pdf", resolve=lambda host, port: ["127.0.0.1"],
                private_hosts=["odd.corp"], deny_address=proxy_module.is_loopback,
            )

    def test_a_listed_name_on_a_private_address_is_allowed(self, proxy_module):
        import pdf_text

        target = pdf_text._target(
            "http://wiki.corp/a.pdf", resolve=lambda host, port: ["10.0.0.5"],
            private_hosts=["wiki.corp"], deny_address=proxy_module.is_loopback,
        )
        assert target[3] == "10.0.0.5"


class TestConfiguration:
    def test_private_hosts_from_the_environment(self, proxy_module):
        assert proxy_module.private_hosts_from(" wiki.corp, Git.Corp ,,") == ["wiki.corp", "Git.Corp"]
        assert proxy_module.private_hosts_from("") == []

    def test_chrome_flags_leave_nothing_outside_the_proxy(self, proxy_module):
        flags = proxy_module.chrome_args()
        assert f"--proxy-server=http://127.0.0.1:{proxy_module.PROXY_PORT}" in flags
        # Chrome sends loopback direct unless told otherwise, and that is the API.
        assert "--proxy-bypass-list=<-loopback>" in flags
