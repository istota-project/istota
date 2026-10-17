"""The browser service's `/render-document`: the envelope, the render origin, the guard, the endpoint.

The render runs against a context double at Patchright's boundary (route,
request, page and navigation callbacks); the real Chromium render of a built
bundle is the image tier's (`tests/image/test_browser_image.py`).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import ipaddress
import struct
import sys
import zlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_browser_user_scope import api, post  # noqa: F401 - the Flask fixture

BROWSER = Path(__file__).resolve().parents[1] / "docker" / "browser"
ORIGIN = "https://render.istota.invalid"
DOCUMENT = "<!doctype html><html><body><div data-render-frame></div><script type=module>window.istotaRender=1</script></body></html>"


@pytest.fixture
def dr(monkeypatch):
    monkeypatch.syspath_prepend(str(BROWSER))
    for name in ("lib", "lib.istota_net_guard"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location("_document_render", BROWSER / "document_render.py")
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up by name while the class is built.
    monkeypatch.setitem(sys.modules, "_document_render", module)
    spec.loader.exec_module(module)
    return module


def _png(width=640, height=360) -> bytes:
    row = b"\0" + b"\x80\x90\xa0" * width
    raw = zlib.compress(row * height)

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw)
            + chunk(b"IEND", b""))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_DELETE = object()


def _envelope(**changes):
    body = {
        "document": DOCUMENT,
        "document_sha256": _sha(DOCUMENT),
        "payload": {"observation": {"lat": 48.2}, "viewport": {"width": 640, "height": 360}},
        "assets": {
            "tiles/tile.png": {"content_type": "image/png",
                               "data_base64": base64.b64encode(_png(4, 4)).decode()},
        },
        "viewport": {"width": 640, "height": 360},
        "output": "png",
        "timeout_ms": 9000,
    }
    for path, value in changes.items():
        target = body
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[key]
        if value is _DELETE:
            target.pop(keys[-1])
        else:
            target[keys[-1]] = value
    return body


def _asset(content_type="image/png", data=b"x"):
    return {"content_type": content_type, "data_base64": base64.b64encode(data).decode()}


class TestTheEnvelope:
    def test_a_well_formed_envelope_passes(self, dr):
        envelope, refusal = dr.validate_envelope(_envelope())
        assert refusal is None
        assert envelope.document == DOCUMENT.encode()
        assert envelope.payload == _envelope()["payload"]
        assert envelope.assets == {"tiles/tile.png": ("image/png", _png(4, 4))}
        assert envelope.viewport == {"width": 640, "height": 360}
        assert envelope.timeout_ms == 9000

    def test_assets_are_optional(self, dr):
        envelope, refusal = dr.validate_envelope(_envelope(assets=_DELETE))
        assert refusal is None
        assert envelope.assets == {}

    def test_the_payload_is_not_interpreted(self, dr):
        """The service draws whatever the renderer makes of it; meaning is the caller's."""
        payload = {"anything": [1, "two", {"three": None}], "url": "http://169.254.169.254/"}
        envelope, refusal = dr.validate_envelope(_envelope(payload=payload))
        assert refusal is None
        assert envelope.payload == payload

    @pytest.mark.parametrize("changes", [
        {"url": "http://169.254.169.254/"},
        {"session_id": "abc12345"},
        {"html": "<p>x</p>"},
        {"document": _DELETE},
        {"document": 7},
        {"document_sha256": _sha("something else")},
        {"document_sha256": _DELETE},
        {"payload": []},
        {"payload": "x"},
        {"payload": {"big": "x" * (64 * 1024)}},
        {"viewport": {"width": 0, "height": 360}},
        {"viewport": {"width": 2001, "height": 360}},
        {"viewport": {"width": 640.5, "height": 360}},
        {"viewport": {"width": True, "height": 360}},
        {"viewport": {"width": 640, "height": 360, "scale": 2}},
        {"output": "pdf"},
        {"output": _DELETE},
        {"timeout_ms": 15001},
        {"timeout_ms": 0},
        {"timeout_ms": "9000"},
        {"assets": []},
        {"assets": {"../etc/passwd": _asset()}},
        {"assets": {"a/../../b.png": _asset()}},
        {"assets": {"/abs.png": _asset()}},
        {"assets": {"Upper.png": _asset()}},
        {"assets": {"a b.png": _asset()}},
        {"assets": {"a//b.png": _asset()}},
        {"assets": {"dir/": _asset()}},
        {"assets": {"page.html": _asset("text/html")}},
        {"assets": {"x.svg": _asset("image/svg+xml")}},
        {"assets": {"x.js": _asset("text/javascript")}},
        {"assets": {"x.png": {"content_type": "image/png", "data_base64": "not base64!"}}},
        {"assets": {"x.png": {"content_type": "image/png"}}},
        {"assets": {"x.png": {**_asset(), "url": "https://x.example/"}}},
        {"assets": {"x.png": _asset(data=b"\0" * (2 * 1024 * 1024 + 1))}},
    ], ids=lambda changes: ",".join(changes))
    def test_an_envelope_out_of_shape_is_refused(self, dr, changes):
        assert dr.validate_envelope(_envelope(**changes)) == (None, "bad_request")

    def test_the_document_cap(self, dr):
        big = "x" * (4 * 1024 * 1024 + 1)
        assert dr.validate_envelope(_envelope(document=big, document_sha256=_sha(big))) == (
            None, "bad_request")

    def test_the_asset_total_is_decoded_bytes(self, dr):
        half = b"\0" * (1024 * 1024)
        envelope, refusal = dr.validate_envelope(_envelope(assets={
            "a.png": _asset(data=half), "b.png": _asset(data=half)}))
        assert refusal is None
        _, refusal = dr.validate_envelope(_envelope(assets={
            "a.png": _asset(data=half), "b.png": _asset(data=half), "c.png": _asset(data=b"x")}))
        assert refusal == "bad_request"

    @pytest.mark.parametrize("body", [None, [], "x", {"document": None}])
    def test_a_body_that_is_not_an_envelope(self, dr, body):
        assert dr.validate_envelope(body) == (None, "bad_request")

    def test_the_context_takes_the_envelopes_viewport_and_nothing_of_the_users(self, dr):
        envelope, _ = dr.validate_envelope(_envelope(viewport={"width": 300, "height": 200}))
        options = dr.context_options(envelope)
        assert options["viewport"] == {"width": 300, "height": 200}
        assert options["accept_downloads"] is False
        assert options["service_workers"] == "block"


def _resolver(table):
    def resolve(host, port):
        return table.get(host, [])
    return resolve


class TestTheDestinationGuard:
    @pytest.mark.parametrize("url", ["data:image/png;base64,AAAA", "blob:https://render.istota.invalid/1"])
    def test_local_schemes_pass(self, dr, url):
        assert dr.destination_allowed(url, resolve=_resolver({}))

    def test_a_host_resolving_only_to_public_addresses_passes(self, dr):
        resolve = _resolver({"tiles.openfreemap.org": ["93.184.216.34", "2606:2800:220:1::1"]})
        assert dr.destination_allowed("https://tiles.openfreemap.org/styles/positron", resolve=resolve)

    @pytest.mark.parametrize("url,table", [
        ("http://web:8766/istota/", {"web": ["172.18.0.4"]}),
        ("http://daemon:8765/", {"daemon": ["172.18.0.3"]}),
        ("http://169.254.169.254/latest/meta-data/", {}),
        ("https://169.254.169.254/latest/meta-data/", {}),
        ("http://127.0.0.1:9223/sessions", {}),
        ("http://[::1]/", {}),
        ("http://[::ffff:10.0.0.1]/", {}),
        ("http://rebind.example/", {"rebind.example": ["93.184.216.34", "10.0.0.5"]}),
        ("http://nowhere.example/", {}),
        ("ftp://tiles.example/x", {"tiles.example": ["93.184.216.34"]}),
        ("ws://tiles.example/x", {"tiles.example": ["93.184.216.34"]}),
        ("file:///etc/passwd", {}),
        ("http://user@tiles.example/", {"tiles.example": ["93.184.216.34"]}),
        ("http://tiles.example:99999/", {"tiles.example": ["93.184.216.34"]}),
        # The render origin is served by the route, never fetched.
        ("https://render.istota.invalid/", {"render.istota.invalid": ["93.184.216.34"]}),
    ])
    def test_everything_else_needs_a_public_address(self, dr, url, table):
        assert not dr.destination_allowed(url, resolve=_resolver(table))

    def test_the_rule_is_the_vendored_net_guard(self, dr):
        assert dr.ip_is_public(ipaddress.ip_address("100.64.0.1")) is False
        assert dr.ip_is_public.__module__ == "lib.istota_net_guard"


class _Route:
    def __init__(self, url, navigation=False, context=None):
        self.request = SimpleNamespace(url=url, method="GET",
                                       is_navigation_request=lambda: navigation)
        self.context = context
        self.outcome = None

    def continue_(self):
        self.outcome = ("continue",)

    def abort(self, reason="failed"):
        self.outcome = ("abort", reason)

    def fetch(self, url=None, max_redirects=None, timeout=None, method=None):
        """The network, as far as the render can reach it: every call is recorded."""
        target = url or self.request.url
        self.context.network.append((target, max_redirects))
        status, headers = self.context.script.get("responses", {}).get(target, (200, {}))
        return SimpleNamespace(url=target, status=status, headers=headers)

    def fulfill(self, status=200, headers=None, body=b"", response=None):
        if response is not None:
            self.outcome = ("fulfill_response", response.url, response.status)
        else:
            self.outcome = ("fulfill", status, headers or {}, body)


class _Page:
    def __init__(self, context, script):
        self.context = context
        self.script = script
        self.handlers = {}
        self.visited = []
        self.closed = False
        self.document = None

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def goto(self, url, **kwargs):
        self.visited.append(url)
        outcome = self.context.fetch(url, navigation=True)
        if outcome[0] == "fulfill":
            self.document = outcome
        for handler in self.handlers.get("framenavigated", []):
            handler(SimpleNamespace(url=url, parent_frame=None))
        self.script.get("after_goto", lambda page: None)(self)

    def wait_for_function(self, expression, **kwargs):
        self.waited_for = expression
        self.script.get("wait", lambda page: None)(self)

    def evaluate(self, expression, arg=None):
        self.evaluated = arg
        self.script.get("during_render", lambda page: None)(self)
        return self.script.get("result", {"ok": True})

    def locator(self, selector):
        page = self

        class _Locator:
            def screenshot(self, **kwargs):
                page.screenshot_selector = selector
                page.screenshot_options = kwargs
                return page.script.get("png", _png())

        return _Locator()

    def close(self):
        self.closed = True


class _Context:
    """A Patchright context at its boundary: routes, request events, pages."""

    def __init__(self, script=None):
        self.script = script or {}
        self.routes = []
        self.handlers = {}
        self.fetched = []
        self.network = []
        self.pages = []

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def new_page(self):
        page = _Page(self, self.script)
        self.pages.append(page)
        for handler in self.handlers.get("page", []):
            handler(page)
        return page

    def popup(self):
        return self.new_page()

    def fetch(self, url, navigation=False):
        for handler in self.handlers.get("request", []):
            handler(SimpleNamespace(url=url, is_navigation_request=lambda: navigation))
        route = _Route(url, navigation, self)
        for _pattern, handler in self.routes:
            handler(route)
        self.fetched.append((url, route.outcome))
        return route.outcome


PUBLIC = {"tiles.openfreemap.org": ["93.184.216.34"], "elsewhere.example": ["93.184.216.35"]}


def _render(dr, script=None, envelope=None):
    checked, refusal = dr.validate_envelope(envelope or _envelope())
    assert refusal is None
    context = _Context(script)
    outcome = dr.render(context, checked, resolve=_resolver(PUBLIC))
    return context, outcome


class TestTheRenderOrigin:
    def test_the_document_is_served_at_the_root_with_its_policy(self, dr):
        context, outcome = _render(dr)

        assert outcome.reason is None
        page = context.pages[0]
        assert page.visited == [ORIGIN + "/"]
        _, status, headers, body = page.document
        assert status == 200
        assert body == DOCUMENT.encode()
        assert headers["content-type"].startswith("text/html")
        csp = headers["content-security-policy"]
        for directive in ("default-src 'none'", "script-src 'unsafe-inline'",
                          "style-src 'unsafe-inline'", "worker-src blob:",
                          "img-src data: blob: https:", "connect-src https:",
                          "frame-src 'none'", "form-action 'none'"):
            assert directive in csp, directive
        assert "http:" not in csp.replace("https:", "")

    def test_a_sent_asset_is_served_under_assets(self, dr):
        context, _ = _render(dr, {"during_render": lambda page: page.context.fetch(
            ORIGIN + "/assets/tiles/tile.png")})

        url, outcome = context.fetched[-1]
        assert outcome[0] == "fulfill"
        assert outcome[1] == 200
        assert outcome[2]["content-type"] == "image/png"
        assert outcome[3] == _png(4, 4)

    @pytest.mark.parametrize("path", ["/assets/missing.png", "/assets/", "/other", "/assets/tiles"])
    def test_any_other_path_on_the_origin_is_a_404(self, dr, path):
        context, outcome = _render(dr, {"during_render": lambda page: page.context.fetch(ORIGIN + path)})

        assert context.fetched[-1][1][:2] == ("fulfill", 404)
        assert outcome.reason is None

    def test_the_page_is_handed_the_payload_and_the_frame_is_captured(self, dr):
        context, outcome = _render(dr, {"during_render": lambda page: page.context.fetch(
            "https://tiles.openfreemap.org/styles/positron")})

        assert outcome.data == _png()
        page = context.pages[0]
        assert page.evaluated[0] == _envelope()["payload"]
        assert "istotaRender" in page.waited_for
        assert page.screenshot_selector == "[data-render-frame]"
        assert page.screenshot_options["type"] == "png"
        url = "https://tiles.openfreemap.org/styles/positron"
        assert context.fetched[-1] == (url, ("fulfill_response", url, 200))
        assert context.network == [(url, 0)]


class TestTheGuardDuringARender:
    def test_a_private_destination_is_aborted_and_fails_the_render(self, dr):
        context, outcome = _render(dr, {"during_render": lambda page: page.context.fetch(
            "https://10.0.0.5/secret")})

        assert (outcome.data, outcome.reason) == (None, "destination_blocked")
        assert context.fetched[-1] == ("https://10.0.0.5/secret", ("abort", "blockedbyclient"))

    def test_a_navigation_away_is_blocked_even_to_a_public_host(self, dr):
        context, outcome = _render(dr, {"during_render": lambda page: page.context.fetch(
            "https://elsewhere.example/", navigation=True)})

        assert outcome.reason == "destination_blocked"
        assert context.fetched[-1][1] == ("abort", "blockedbyclient")

    def test_a_navigation_to_an_asset_on_the_origin_is_blocked(self, dr):
        context, outcome = _render(dr, {"during_render": lambda page: page.context.fetch(
            ORIGIN + "/assets/tiles/tile.png", navigation=True)})

        assert outcome.reason == "destination_blocked"
        assert context.fetched[-1][1] == ("abort", "blockedbyclient")

    def test_a_popup_is_closed_and_fails_the_render(self, dr):
        context, outcome = _render(dr, {"during_render": lambda page: page.context.popup()})

        assert outcome.reason == "destination_blocked"
        assert context.pages[1].closed

    def test_a_download_fails_the_render(self, dr):
        def download(page):
            for handler in page.handlers["download"]:
                handler(object())

        _, outcome = _render(dr, {"during_render": download})
        assert outcome.reason == "destination_blocked"

    def test_a_redirect_to_a_private_address_is_never_fetched(self, dr):
        tile = "https://tiles.openfreemap.org/t/1.png"
        context, outcome = _render(dr, {
            "responses": {tile: (302, {"location": "https://10.0.0.5/secret"})},
            "during_render": lambda page: page.context.fetch(tile)})

        assert (outcome.data, outcome.reason) == (None, "destination_blocked")
        assert context.network == [(tile, 0)]
        assert context.fetched[-1] == (tile, ("abort", "blockedbyclient"))

    def test_a_redirect_between_public_hosts_is_followed_hop_by_hop(self, dr):
        tile = "https://tiles.openfreemap.org/t/1.png"
        moved = "https://elsewhere.example/t/1.png"
        context, outcome = _render(dr, {
            "responses": {tile: (301, {"location": "//elsewhere.example/t/1.png"})},
            "during_render": lambda page: page.context.fetch(tile)})

        assert outcome.reason is None
        assert context.network == [(tile, 0), (moved, 0)]
        assert context.fetched[-1] == (tile, ("fulfill_response", moved, 200))

    def test_a_redirect_chain_past_the_cap_fails_the_render(self, dr):
        hops = {f"https://elsewhere.example/{n}": (302, {"location": f"/{n + 1}"})
                for n in range(20)}
        context, outcome = _render(dr, {
            "responses": hops,
            "during_render": lambda page: page.context.fetch("https://elsewhere.example/0")})

        assert outcome.reason == "destination_blocked"
        assert len(context.network) == dr.MAX_REDIRECTS + 1
        assert context.fetched[-1][1] == ("abort", "blockedbyclient")

    def test_a_failed_fetch_is_a_network_error_not_a_violation(self, dr):
        class _Unreachable(_Route):
            def fetch(self, **kwargs):
                raise RuntimeError("net::ERR_CONNECTION_REFUSED")

        context = _Context({"during_render": lambda page: None})
        checked, _ = dr.validate_envelope(_envelope())
        dr.render(context, checked, resolve=_resolver(PUBLIC))
        (_pattern, handler) = context.routes[0]
        route = _Unreachable("https://elsewhere.example/x", False, context)
        handler(route)
        assert route.outcome == ("abort", "failed")

    def test_a_request_the_route_never_saw_still_fails_the_render(self, dr):
        """The request event is the backstop for a request no route handler saw."""
        def unrouted(page):
            for handler in page.context.handlers["request"]:
                handler(SimpleNamespace(url="http://192.168.1.1/",
                                        is_navigation_request=lambda: False))

        _, outcome = _render(dr, {"during_render": unrouted})
        assert outcome.reason == "destination_blocked"


class TestTheOutcome:
    @pytest.mark.parametrize("result,reason,detail", [
        ({"ok": False, "reason": "tile_error"}, "render_failed", "tile_error"),
        ({"ok": False, "reason": "timeout"}, "render_failed", "timeout"),
        ({"ok": False, "reason": "Failed at https://x.example/?k=secret"}, "render_failed", None),
        ({"ok": False}, "render_failed", None),
        (None, "render_failed", None),
        ({"hostTimeout": True}, "render_timeout", None),
    ])
    def test_a_page_that_could_not_draw(self, dr, result, reason, detail):
        _, outcome = _render(dr, {"result": result})
        assert (outcome.data, outcome.reason, outcome.detail) == (None, reason, detail)

    def test_a_document_with_no_renderer_is_a_timeout(self, dr):
        class TimeoutError(Exception):  # Patchright's own name
            pass

        def never(page):
            raise TimeoutError("waiting for function failed: timeout 9000ms exceeded")

        context, outcome = _render(dr, {"wait": never})
        assert outcome.reason == "render_timeout"
        assert not hasattr(context.pages[0], "screenshot_selector")

    def test_an_error_names_no_detail(self, dr):
        def boom(page):
            raise RuntimeError("https://tiles.example/{z}?api_key=secret")

        _, outcome = _render(dr, {"after_goto": boom})
        assert (outcome.reason, outcome.detail) == ("render_failed", None)

    def test_the_capture_is_returned_as_taken(self, dr):
        """Whether the picture is the right one is the caller's to check."""
        _, outcome = _render(dr, {"png": _png(10, 10)})
        assert outcome.data == _png(10, 10)

    def test_the_envelope_is_not_mutated_by_the_render(self, dr):
        envelope = _envelope()
        before = copy.deepcopy(envelope)
        _render(dr, envelope=envelope)
        assert envelope == before


class TestTheTemporaryContext:
    def test_it_is_a_new_context_closed_on_the_way_out(self, api, monkeypatch):  # noqa: F811
        monkeypatch.setattr(api.chrome, "_assert_pw_thread", lambda *a, **k: None)
        monkeypatch.setattr(api.chrome, "connect_cdp", lambda inst, **kw: None)
        inst = SimpleNamespace(pw_browser=MagicMock())
        context = inst.pw_browser.new_context.return_value

        with pytest.raises(RuntimeError):
            with api.chrome.temporary_context(inst, viewport={"width": 1, "height": 1}) as got:
                assert got is context
                raise RuntimeError("render failed")

        inst.pw_browser.new_context.assert_called_once_with(viewport={"width": 1, "height": 1})
        context.close.assert_called_once_with()


@pytest.fixture
def endpoint(api, monkeypatch):  # noqa: F811
    """The real endpoint over a context double; the page answers `ok`."""
    opened = []

    @contextmanager
    def temporary_context(inst, **options):
        context = _Context()
        opened.append((inst.user_id, options, context))
        yield context

    monkeypatch.setattr(api.chrome, "temporary_context", temporary_context)
    monkeypatch.setattr(api.document_render, "_resolve", lambda host, port: PUBLIC.get(host, []))
    api.opened = opened
    return api


class TestTheEndpoint:
    def test_a_render_answers_with_the_png(self, endpoint):
        response = post(endpoint, "/render-document", **_envelope())

        assert response.status_code == 200, response.get_data()
        assert response.mimetype == "image/png"
        assert response.get_data() == _png()
        user, options, _context = endpoint.opened[0]
        assert user == "alice"
        assert options["viewport"] == {"width": 640, "height": 360}

    def test_the_old_map_snapshot_route_is_gone(self, endpoint):
        response = post(endpoint, "/map-snapshot", **_envelope())
        assert response.status_code == 404

    def test_a_bad_envelope_opens_nothing(self, endpoint):
        response = post(endpoint, "/render-document", **_envelope(url="http://169.254.169.254/"))

        assert response.status_code == 400
        assert response.json["error"] == "bad_request"
        assert endpoint.opened == []

    def test_a_body_over_the_cap_is_refused_unread(self, endpoint):
        response = endpoint.app.test_client().post(
            "/render-document", data=b"{" + b" " * (endpoint.document_render.MAX_REQUEST_BYTES + 1),
            headers={"X-Istota-User": "alice", "Content-Type": "application/json"})
        assert response.status_code == 400
        assert endpoint.opened == []

    @pytest.mark.parametrize("reason,status", [
        ("destination_blocked", 502), ("render_failed", 502), ("render_timeout", 504),
    ])
    def test_a_render_failure_is_a_stable_reason(self, endpoint, monkeypatch, reason, status):
        monkeypatch.setattr(endpoint.document_render, "render",
                            lambda *a, **k: endpoint.document_render.Outcome(None, reason))

        response = post(endpoint, "/render-document", **_envelope())

        assert response.status_code == status
        assert response.json["error"] == reason
        assert "detail" not in response.json

    def test_the_pages_own_reason_rides_in_detail(self, endpoint, monkeypatch):
        monkeypatch.setattr(endpoint.document_render, "render",
                            lambda *a, **k: endpoint.document_render.Outcome(
                                None, "render_failed", "tile_error"))

        response = post(endpoint, "/render-document", **_envelope())

        assert response.status_code == 502
        assert (response.json["error"], response.json["detail"]) == ("render_failed", "tile_error")

    def test_an_exception_names_no_detail(self, endpoint, monkeypatch, caplog):
        def boom(*args, **kwargs):
            raise RuntimeError("https://tiles.example/{z}?api_key=secret 48.208176")

        monkeypatch.setattr(endpoint.document_render, "render", boom)

        response = post(endpoint, "/render-document", **_envelope())

        assert response.status_code == 502
        assert response.json["error"] == "render_failed"
        assert "secret" not in caplog.text and "48.208176" not in caplog.text
        assert "secret" not in response.get_data(as_text=True)

    def test_the_log_carries_no_document_payload_or_image(self, endpoint, caplog):
        envelope = _envelope(payload={"marker": "payload-marker-48.2", "viewport": {
            "width": 640, "height": 360}})
        post(endpoint, "/render-document", **envelope)
        assert "payload-marker" not in caplog.text
        assert "istotaRender=1" not in caplog.text

    def test_a_user_at_capacity_is_refused_and_no_session_is_evicted(self, endpoint):
        for _ in range(endpoint.MAX_SESSIONS):
            kept = post(endpoint, "/browse", url="https://example.com/", keep_session=True)
            assert kept.status_code == 200
        before = dict(endpoint._sessions)

        response = post(endpoint, "/render-document", **_envelope())

        assert response.status_code == 503
        assert response.json["error"] == "capacity"
        assert endpoint._sessions == before
        assert endpoint.opened == []

    def test_the_slot_is_given_back(self, endpoint, monkeypatch):
        monkeypatch.setattr(endpoint, "MAX_SESSIONS", 1)
        for _ in range(3):
            assert post(endpoint, "/render-document", **_envelope()).status_code == 200
        assert not endpoint._document_renders

    def test_the_briefings_identity_cannot_render(self, endpoint):
        response = endpoint.app.test_client().post(
            "/render-document", json=_envelope(),
            headers={"X-Istota-User": "__briefings__", "X-Istota-Owner": "x"})
        assert response.status_code == 403
        assert endpoint.opened == []
