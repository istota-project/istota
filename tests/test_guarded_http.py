"""The guarded HTTP client in `sandbox.guarded_http`.

Every daemon-side fetch to a host a user or a remote server chose goes through
`guarded_send`: the address rule from `net_guard`, a connection pinned to the
checked address, redirects re-checked hop by hop, a byte cap, and one closed
set of refusal codes. The remote ends here are stdlib HTTP servers on loopback,
named in `private_hosts`, so a refusal is one a real connection could have
contradicted. The redirect rules that need a public name use httpx's mock
transport, which stands in for the network and nothing of istota's.
"""

from __future__ import annotations

import base64
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from istota.sandbox import guarded_http
from istota.sandbox.guarded_http import GuardRefused

PUBLIC = "93.184.216.34"


class _Server:
    """A loopback server answering each path from `routes` and recording requests."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.routes: dict[str, tuple[int, dict, bytes]] = {}
        self.received: list[dict] = []
        self.delay = 0.0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                server.received.append({
                    "method": self.command, "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                })
                if server.delay:
                    time.sleep(server.delay)
                if self.path == "/drip":
                    # One byte at a time, each inside the per-read timeout.
                    self.send_response(200)
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    try:
                        for _ in range(1000):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            time.sleep(0.1)
                    except OSError:
                        pass
                    return
                status, headers, payload = server.routes.get(
                    self.path.split("?", 1)[0], (404, {}, b"missing"),
                )
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PROPFIND = do_HEAD = _answer

        self.httpd = ThreadingHTTPServer((host, 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str = "/", host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.close()


@pytest.fixture
def other():
    s = _Server()
    yield s
    s.close()


@pytest.fixture
def client():
    c = guarded_http.new_client(timeout=5.0)
    yield c
    c.close()


def _send(client, url, **kwargs):
    kwargs.setdefault("private_hosts", ["127.0.0.1"])
    kwargs.setdefault("max_bytes", 1024 * 1024)
    kwargs.setdefault("timeout", 5.0)
    kwargs.setdefault("deadline_seconds", 10.0)
    return guarded_http.guarded_send(client, kwargs.pop("method", "GET"), url, **kwargs)


class TestPinnedRequest:
    def test_the_connection_goes_to_the_address_with_host_and_sni_on_the_name(self):
        request = guarded_http.pinned_request(
            "GET", "https://cal.example.org:8443/feed.ics?k=v", PUBLIC,
        )
        assert request.url.host == PUBLIC
        assert request.url.port == 8443
        assert request.url.query == b"k=v"
        assert request.headers["Host"] == "cal.example.org:8443"
        assert request.extensions["sni_hostname"] == "cal.example.org"

    def test_the_default_port_is_left_out_of_the_host_header(self):
        request = guarded_http.pinned_request("GET", "https://cal.example.org/x", PUBLIC)
        assert request.headers["Host"] == "cal.example.org"

    def test_plain_http_carries_no_sni(self):
        request = guarded_http.pinned_request("GET", "http://nas.local/x", "10.0.0.5")
        assert request.headers["Host"] == "nas.local"
        assert "sni_hostname" not in request.extensions

    def test_an_ipv6_address_is_bracketed_in_the_url(self):
        request = guarded_http.pinned_request("GET", "http://nas.local:8080/x", "fd00::5")
        assert request.url.host == "fd00::5"
        assert request.headers["Host"] == "nas.local:8080"

    def test_auth_becomes_a_basic_header_the_caller_cannot_replace(self):
        request = guarded_http.pinned_request(
            "GET", "https://dav.example.org/x", PUBLIC,
            headers={"Authorization": "Bearer planted", "Host": "elsewhere"},
            auth=("share-secret", ""),
        )
        assert request.headers["Authorization"] == "Basic " + base64.b64encode(
            b"share-secret:").decode()
        assert request.headers["Host"] == "dav.example.org"

    def test_the_timeout_rides_the_request(self):
        request = guarded_http.pinned_request("GET", "https://x.example/", PUBLIC, timeout=3)
        assert request.extensions["timeout"]["read"] == 3


class TestTheAddressRule:
    def test_a_listed_loopback_host_is_fetched(self, server, client):
        server.routes["/feed"] = (200, {"ETag": '"a"'}, b"BEGIN:VCALENDAR")
        response = _send(client, server.url("/feed"))
        assert response.status_code == 200
        assert response.content == b"BEGIN:VCALENDAR"
        assert response.headers["etag"] == '"a"'

    def test_plain_http_to_an_unlisted_host_sends_nothing(self, server, client):
        with pytest.raises(GuardRefused) as err:
            _send(client, server.url("/feed"), private_hosts=[])
        assert err.value.code == "host_refused"
        assert server.received == []

    def test_https_to_an_unlisted_private_address_sends_nothing(self, client):
        with pytest.raises(GuardRefused) as err:
            _send(client, "https://127.0.0.1:1/feed", private_hosts=[])
        assert err.value.code == "host_refused"

    def test_a_scheme_other_than_http_is_refused(self, client):
        with pytest.raises(GuardRefused) as err:
            _send(client, "file:///etc/passwd")
        assert err.value.code == "host_refused"

    def test_the_next_checked_address_is_tried_on_connection_refusal(self, server, client):
        server.routes["/feed"] = (200, {}, b"ok")
        response = _send(
            client, f"http://multi.test:{server.port}/feed", private_hosts=["multi.test"],
            resolve=lambda host, port: ["::1", "127.0.0.1"],
        )
        assert response.content == b"ok"
        assert server.received[0]["headers"]["host"] == f"multi.test:{server.port}"

    def test_every_address_refusing_is_connection_failed(self, client):
        closed = _Server()
        port = closed.port
        closed.close()
        with pytest.raises(GuardRefused) as err:
            _send(client, f"http://127.0.0.1:{port}/feed")
        assert err.value.code == "connection_failed"


class TestTheBody:
    def test_the_byte_cap_holds(self, server, client):
        server.routes["/big"] = (200, {}, b"x" * 5000)
        with pytest.raises(GuardRefused) as err:
            _send(client, server.url("/big"), max_bytes=1000)
        assert err.value.code == "too_large"

    def test_a_body_at_the_cap_is_read(self, server, client):
        server.routes["/exact"] = (200, {}, b"x" * 1000)
        assert len(_send(client, server.url("/exact"), max_bytes=1000).content) == 1000

    def test_a_slow_answer_is_a_timeout(self, server, client):
        server.routes["/slow"] = (200, {}, b"late")
        server.delay = 1.0
        with pytest.raises(GuardRefused) as err:
            _send(client, server.url("/slow"), timeout=0.2)
        assert err.value.code == "timeout"

    def test_a_dripped_body_stops_at_the_deadline(self, server, client):
        # Every read arrives inside the 1 s per-read timeout; only the whole-call
        # deadline can end it.
        started = time.monotonic()
        with pytest.raises(GuardRefused) as err:
            _send(client, server.url("/drip"), timeout=1.0, deadline_seconds=0.5)
        assert err.value.code == "timeout"
        assert time.monotonic() - started < 3.0

    def test_the_refusal_carries_no_path_or_query(self, server, client):
        server.routes["/big"] = (200, {}, b"x" * 5000)
        with pytest.raises(GuardRefused) as err:
            _send(client, server.url("/big?token=feed-secret"), max_bytes=10)
        assert "feed-secret" not in str(err.value)
        assert "/big" not in str(err.value)
        assert str(err.value) == "too_large"


class TestRedirects:
    def test_no_redirect_is_followed_by_default(self, server, other, client):
        server.routes["/a"] = (302, {"Location": other.url("/b")}, b"")
        with pytest.raises(GuardRefused) as err:
            _send(client, server.url("/a"))
        assert err.value.code == "redirect_refused"
        assert other.received == []

    def test_a_redirect_within_the_limit_is_followed(self, server, other, client):
        server.routes["/a"] = (302, {"Location": other.url("/b")}, b"")
        other.routes["/b"] = (200, {}, b"there")
        assert _send(client, server.url("/a"), max_redirects=3).content == b"there"

    def test_hops_past_the_limit_are_refused(self, server, client):
        server.routes["/a"] = (302, {"Location": "/b"}, b"")
        server.routes["/b"] = (302, {"Location": "/c"}, b"")
        server.routes["/c"] = (200, {}, b"end")
        with pytest.raises(GuardRefused) as err:
            _send(client, server.url("/a"), max_redirects=1)
        assert err.value.code == "redirect_refused"

    def test_authorization_is_dropped_on_a_cross_origin_hop(self, server, other, client):
        server.routes["/a"] = (302, {"Location": other.url("/b")}, b"")
        other.routes["/b"] = (200, {}, b"there")
        _send(client, server.url("/a"), max_redirects=3, auth=("secret", ""),
              headers={"Authorization": "Bearer also-secret"})
        assert "authorization" in server.received[0]["headers"]
        assert "authorization" not in other.received[0]["headers"]

    def test_authorization_is_kept_on_a_same_origin_hop(self, server, client):
        server.routes["/a"] = (302, {"Location": "/b"}, b"")
        server.routes["/b"] = (200, {}, b"there")
        _send(client, server.url("/a"), max_redirects=3, auth=("secret", ""))
        assert "authorization" in server.received[1]["headers"]

    def test_a_hop_to_another_listed_private_host_is_refused(self, server, other, client):
        server.routes["/a"] = (302, {"Location": other.url("/b", host="127.0.0.1")}, b"")
        other.routes["/b"] = (200, {}, b"internal")
        with pytest.raises(GuardRefused) as err:
            _send(client, server.url("/a", host="localhost"), max_redirects=3,
                  private_hosts=["localhost", "127.0.0.1"])
        assert err.value.code == "host_refused"
        assert other.received == []

    def test_a_303_turns_a_post_into_a_get_without_a_body(self, server, client):
        server.routes["/a"] = (303, {"Location": "/b"}, b"")
        server.routes["/b"] = (200, {}, b"done")
        _send(client, server.url("/a"), method="POST", content=b"payload", max_redirects=1)
        assert server.received[1]["method"] == "GET"
        assert server.received[1]["body"] == b""


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False,
                        follow_redirects=False)


class TestHopRulesOnPublicNames:
    """Names that resolve to public addresses, which loopback cannot stand in for."""

    @staticmethod
    def _resolve(table):
        return lambda host, port: table.get(host, [])

    def test_https_to_http_is_refused_for_an_unlisted_host(self):
        seen = []

        def handler(request):
            seen.append(request.headers["host"])
            return httpx.Response(302, headers={"Location": "http://feeds.example/x"})

        with _mock_client(handler) as c, pytest.raises(GuardRefused) as err:
            guarded_http.guarded_send(
                c, "GET", "https://cal.example/feed", private_hosts=[], max_bytes=100,
                max_redirects=3, timeout=5, deadline_seconds=10,
                resolve=self._resolve({"cal.example": [PUBLIC], "feeds.example": [PUBLIC]}),
            )
        assert err.value.code == "host_refused"
        assert seen == ["cal.example"]

    def test_plain_http_to_a_public_host_is_sent_when_the_caller_allows_it(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, content=b"ok")

        with _mock_client(handler) as c:
            response = guarded_http.guarded_send(
                c, "GET", "http://blog.example/rss", private_hosts=[], max_bytes=100,
                timeout=5, deadline_seconds=10, allow_plain_http=True,
                resolve=self._resolve({"blog.example": [PUBLIC]}),
            )
        assert response.content == b"ok"
        assert seen == [f"http://{PUBLIC}/rss"]

    def test_allowing_plain_http_still_refuses_a_private_address(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200)

        with _mock_client(handler) as c, pytest.raises(GuardRefused) as err:
            guarded_http.guarded_send(
                c, "GET", "http://nas.internal/rss", private_hosts=[], max_bytes=100,
                timeout=5, deadline_seconds=10, allow_plain_http=True,
                resolve=self._resolve({"nas.internal": ["10.0.0.5"]}),
            )
        assert err.value.code == "host_refused"
        assert seen == []

    def test_a_public_feed_cannot_bounce_onto_a_listed_internal_host(self):
        seen = []

        def handler(request):
            seen.append(request.headers["host"])
            return httpx.Response(302, headers={"Location": "https://nas.internal/x"})

        with _mock_client(handler) as c, pytest.raises(GuardRefused) as err:
            guarded_http.guarded_send(
                c, "GET", "https://cal.example/feed", private_hosts=["nas.internal"],
                max_bytes=100, max_redirects=3, timeout=5, deadline_seconds=10,
                resolve=self._resolve({"cal.example": [PUBLIC], "nas.internal": ["10.0.0.5"]}),
            )
        assert err.value.code == "host_refused"
        assert seen == ["cal.example"]

    def test_the_listed_host_may_redirect_within_itself(self):
        seen = []

        def handler(request):
            seen.append((request.url.host, request.url.path))
            if request.url.path == "/feed":
                return httpx.Response(302, headers={"Location": "https://nas.internal/moved"})
            return httpx.Response(200, content=b"ok")

        with _mock_client(handler) as c:
            response = guarded_http.guarded_send(
                c, "GET", "https://nas.internal/feed", private_hosts=["nas.internal"],
                max_bytes=100, max_redirects=3, timeout=5, deadline_seconds=10,
                resolve=self._resolve({"nas.internal": ["10.0.0.5"]}),
            )
        assert response.content == b"ok"
        assert seen == [("10.0.0.5", "/feed"), ("10.0.0.5", "/moved")]

    def test_a_hop_whose_dns_turned_private_is_refused(self):
        def handler(request):
            return httpx.Response(302, headers={"Location": "https://cdn.example/x"})

        with _mock_client(handler) as c, pytest.raises(GuardRefused) as err:
            guarded_http.guarded_send(
                c, "GET", "https://cal.example/feed", private_hosts=[], max_bytes=100,
                max_redirects=3, timeout=5, deadline_seconds=10,
                resolve=self._resolve({"cal.example": [PUBLIC], "cdn.example": [PUBLIC, "10.1.1.1"]}),
            )
        assert err.value.code == "host_refused"
