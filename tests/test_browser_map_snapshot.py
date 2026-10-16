"""The browser service's `/map-snapshot`: validation, the destination guard, the render, the endpoint.

The render runs against a context double at Patchright's boundary (route,
request, page and navigation callbacks); the real Chromium render of the built
page is the image tier's (`tests/image/test_browser_image.py`).
"""

from __future__ import annotations

import copy
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
RENDER_URL = "http://web:8766/istota/map-snapshot/"


@pytest.fixture
def snap(monkeypatch):
    monkeypatch.syspath_prepend(str(BROWSER))
    for name in ("lib", "lib.istota_net_guard"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location("_map_snapshot", BROWSER / "map_snapshot.py")
    module = importlib.util.module_from_spec(spec)
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


def _request(**changes):
    body = {
        "observation": {
            "precision": "exact", "lat": 48.208176, "lon": 16.373819, "accuracy_m": 12.0,
            "observed_at": "2026-10-16T14:02:00+00:00", "freshness": "fresh",
            "owner_user_id": "alice",
        },
        "basemap": {
            "provider": "openfreemap", "kind": "style",
            "light": "https://tiles.openfreemap.org/styles/positron",
            "dark": "https://tiles.openfreemap.org/styles/dark",
            "attribution": "",
        },
        "owner": "Alice",
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


_DELETE = object()


class TestTheRequest:
    def test_a_well_formed_request_passes_unchanged(self, snap):
        payload, refusal = snap.validate_request(_request())
        assert refusal is None
        assert payload == _request()

    def test_the_raster_shape_and_its_attribution_pass(self, snap):
        payload, refusal = snap.validate_request(_request(**{
            "basemap.provider": "carto", "basemap.kind": "raster",
            "basemap.light": "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png?api_key=k%26x",
            "basemap.dark": "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
            "basemap.attribution": ('&copy; <a href="https://carto.com/">CARTO</a> '
                                    '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>'),
        }))
        assert refusal is None
        assert payload["basemap"]["kind"] == "raster"

    def test_an_area_share_carries_rounded_values_and_no_accuracy(self, snap):
        _, refusal = snap.validate_request(_request(**{
            "observation.precision": "area", "observation.lat": 48.21,
            "observation.lon": 16.37, "observation.accuracy_m": None}))
        assert refusal is None

    @pytest.mark.parametrize("changes", [
        {"url": "http://169.254.169.254/"},
        {"session_id": "abc12345"},
        {"observation.url": "https://example.com/"},
        {"basemap.style_json": "{}"},
        {"owner": _DELETE},
        {"observation.precision": "street"},
        {"observation.lat": 91.0},
        {"observation.lat": float("inf")},
        {"observation.lat": True},
        {"observation.lon": -180.5},
        {"observation.lat": "48.2"},
        {"observation.accuracy_m": -1},
        {"observation.accuracy_m": 10_000_000},
        {"observation.freshness": "stale"},
        {"observation.observed_at": "yesterday"},
        {"observation.owner_user_id": "<img src=x onerror=alert(1)>"},
        {"owner": "<b>Alice</b>"},
        {"owner": "Alice\nBob"},
        {"observation.precision": "area", "observation.accuracy_m": None,
         "observation.lat": 48.2081},
        {"observation.precision": "area", "observation.lat": 48.21, "observation.lon": 16.37},
        {"basemap.provider": "mapbox"},
        {"basemap.kind": "vector"},
        {"basemap.light": "javascript:alert(1)"},
        {"basemap.light": "file:///etc/passwd"},
        {"basemap.light": "https://user:pass@tiles.example/style"},
        {"basemap.light": 'https://tiles.example/style" onload="x'},
        {"basemap.kind": "raster", "basemap.light": "https://tiles.example/{z}/{x}.png"},
        {"basemap.attribution": '<a href="javascript:alert(1)">x</a>'},
        {"basemap.attribution": '<img src=x onerror=alert(1)>'},
        {"basemap.attribution": '<a href="https://x.example/" onclick="y">x</a>'},
    ], ids=lambda changes: ",".join(changes))
    def test_a_request_out_of_shape_is_refused(self, snap, changes):
        payload, refusal = snap.validate_request(_request(**changes))
        assert payload is None
        assert refusal == "bad_request"

    @pytest.mark.parametrize("body", [None, [], "x", {"observation": None}])
    def test_a_body_that_is_not_the_request(self, snap, body):
        assert snap.validate_request(body) == (None, "bad_request")

    def test_a_latitude_outside_web_mercator_is_its_own_refusal(self, snap):
        assert snap.validate_request(_request(**{"observation.lat": 85.06})) == (
            None, "not_mercator_safe")


def _resolver(table):
    def resolve(host, port):
        return table.get(host, [])
    return resolve


class TestTheDestinationGuard:
    ORIGIN = "http://web:8766"

    @pytest.mark.parametrize("url", [
        "http://web:8766/istota/map-snapshot/",
        "http://web:8766/istota/_app/immutable/chunks/maplibre.js",
        "HTTP://WEB:8766/istota/",
        "data:image/png;base64,AAAA",
        "blob:http://web:8766/1234",
    ])
    def test_the_renderer_origin_and_local_schemes_pass(self, snap, url):
        assert snap.destination_allowed(url, self.ORIGIN, resolve=_resolver({}))

    def test_a_host_resolving_only_to_public_addresses_passes(self, snap):
        resolve = _resolver({"tiles.openfreemap.org": ["93.184.216.34", "2606:2800:220:1::1"]})
        assert snap.destination_allowed(
            "https://tiles.openfreemap.org/styles/positron", self.ORIGIN, resolve=resolve)

    @pytest.mark.parametrize("url,table", [
        ("http://web:8767/istota/", {"web": ["172.18.0.4"]}),
        ("https://web:8766/istota/", {"web": ["172.18.0.4"]}),
        ("http://daemon:8765/", {"daemon": ["172.18.0.3"]}),
        ("http://169.254.169.254/latest/meta-data/", {}),
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
    ])
    def test_everything_else_needs_a_public_address(self, snap, url, table):
        assert not snap.destination_allowed(url, self.ORIGIN, resolve=_resolver(table))

    def test_the_rule_is_the_vendored_net_guard(self, snap):
        # The vendored leaf is what decides, not a second blocklist here.
        assert snap.ip_is_public(ipaddress.ip_address("100.64.0.1")) is False
        assert snap.ip_is_public.__module__ == "lib.istota_net_guard"


class _Route:
    def __init__(self, url, navigation=False):
        self.request = SimpleNamespace(url=url, is_navigation_request=lambda: navigation)
        self.outcome = None

    def continue_(self):
        self.outcome = "continue"

    def abort(self, reason="failed"):
        self.outcome = ("abort", reason)


class _Page:
    def __init__(self, context, script):
        self.context = context
        self.script = script
        self.handlers = {}
        self.visited = []
        self.main_frame = object()
        self.closed = False

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def goto(self, url, **kwargs):
        self.visited.append(url)
        self.context.fetch(url, navigation=True)
        for handler in self.handlers.get("framenavigated", []):
            handler(SimpleNamespace(url=url, parent_frame=None))
        self.script.get("after_goto", lambda page: None)(self)

    def wait_for_function(self, expression, **kwargs):
        pass

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
        route = _Route(url, navigation)
        for _pattern, handler in self.routes:
            handler(route)
        self.fetched.append((url, route.outcome))
        return route.outcome


PUBLIC = {"tiles.openfreemap.org": ["93.184.216.34"], "web": ["172.18.0.4"],
          "elsewhere.example": ["93.184.216.35"]}


def _render(snap, script=None, payload=None):
    context = _Context(script)
    png, reason = snap.render(context, RENDER_URL, payload or _request(),
                              resolve=_resolver(PUBLIC))
    return context, png, reason


class TestTheRender:
    def test_the_page_draws_the_payload_and_the_frame_is_captured(self, snap):
        context, png, reason = _render(snap, {
            "during_render": lambda page: page.context.fetch(
                "https://tiles.openfreemap.org/styles/positron"),
        })

        assert reason is None
        assert png == _png()
        page = context.pages[0]
        assert page.visited == [RENDER_URL]
        assert page.evaluated[0] == _request()
        assert page.screenshot_selector == "[data-snapshot-frame]"
        assert context.fetched[-1] == ("https://tiles.openfreemap.org/styles/positron",
                                       "continue")

    def test_a_private_destination_is_aborted_and_fails_the_render(self, snap):
        context, png, reason = _render(snap, {
            "during_render": lambda page: page.context.fetch("http://10.0.0.5/secret"),
        })

        assert (png, reason) == (None, "destination_blocked")
        assert context.fetched[-1] == ("http://10.0.0.5/secret", ("abort", "blockedbyclient"))

    def test_a_navigation_away_from_the_shell_is_blocked_even_to_a_public_host(self, snap):
        context, png, reason = _render(snap, {
            "during_render": lambda page: page.context.fetch(
                "https://elsewhere.example/", navigation=True),
        })

        assert (png, reason) == (None, "destination_blocked")
        assert context.fetched[-1][1] == ("abort", "blockedbyclient")

    def test_a_popup_is_closed_and_fails_the_render(self, snap):
        context, png, reason = _render(snap, {
            "during_render": lambda page: page.context.popup(),
        })

        assert (png, reason) == (None, "destination_blocked")
        assert context.pages[1].closed

    def test_a_request_the_route_never_saw_still_fails_the_render(self, snap):
        """A redirect hop or anything else the route handler is not shown."""
        def unrouted(page):
            for handler in page.context.handlers["request"]:
                handler(SimpleNamespace(url="http://192.168.1.1/",
                                        is_navigation_request=lambda: False))

        _, png, reason = _render(snap, {"during_render": unrouted})

        assert (png, reason) == (None, "destination_blocked")

    @pytest.mark.parametrize("result,reason", [
        ({"ok": False, "reason": "timeout"}, "render_timeout"),
        ({"ok": False, "reason": "tile_error"}, "render_failed"),
        ({"ok": False, "reason": "webgl_unavailable"}, "render_failed"),
        (None, "render_failed"),
    ])
    def test_a_page_that_could_not_draw(self, snap, result, reason):
        _, png, got = _render(snap, {"result": result})
        assert (png, got) == (None, reason)

    @pytest.mark.parametrize("png", [b"", b"GIF89a", _png(1280, 720), _png(640, 359)])
    def test_a_capture_that_is_not_the_frame(self, snap, png):
        _, got, reason = _render(snap, {"png": png})
        assert (got, reason) == (None, "bad_png")

    def test_a_timeout_anywhere_is_a_timeout(self, snap):
        class TimeoutError(Exception):  # Patchright's own name
            pass

        def slow(page):
            raise TimeoutError("Timeout 5000ms exceeded waiting for https://tiles.example/k=secret")

        _, png, reason = _render(snap, {"after_goto": slow})
        assert (png, reason) == (None, "render_timeout")

    def test_the_payload_is_not_mutated_by_the_render(self, snap):
        payload = _request()
        before = copy.deepcopy(payload)
        _render(snap, payload=payload)
        assert payload == before


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
    monkeypatch.setenv("ISTOTA_MAP_RENDER_URL", RENDER_URL)
    opened = []

    @contextmanager
    def temporary_context(inst, **options):
        context = _Context()
        opened.append((inst.user_id, options, context))
        yield context

    monkeypatch.setattr(api.chrome, "temporary_context", temporary_context)
    monkeypatch.setattr(api.map_snapshot, "_resolve",
                        lambda host, port: PUBLIC.get(host, []))
    api.opened = opened
    return api


class TestTheEndpoint:
    def test_a_render_answers_with_the_png(self, endpoint):
        response = post(endpoint, "/map-snapshot", **_request())

        assert response.status_code == 200, response.get_data()
        assert response.mimetype == "image/png"
        assert response.get_data() == _png()
        user, options, _context = endpoint.opened[0]
        assert user == "alice"
        assert options == endpoint.map_snapshot.CONTEXT_OPTIONS

    def test_without_a_renderer_url_nothing_is_opened(self, endpoint, monkeypatch):
        monkeypatch.delenv("ISTOTA_MAP_RENDER_URL")

        response = post(endpoint, "/map-snapshot", **_request())

        assert response.status_code == 503
        assert response.json["error"] == "render_unavailable"
        assert endpoint.opened == []

    def test_a_bad_request_opens_nothing(self, endpoint):
        response = post(endpoint, "/map-snapshot", **_request(session_id="abc12345"))

        assert response.status_code in (400, 404)
        assert endpoint.opened == []

    def test_a_render_failure_is_a_stable_reason(self, endpoint, monkeypatch):
        monkeypatch.setattr(endpoint.map_snapshot, "render",
                            lambda *a, **k: (None, "destination_blocked"))

        response = post(endpoint, "/map-snapshot", **_request())

        assert response.status_code == 502
        assert response.json["error"] == "destination_blocked"

    def test_an_exception_names_no_detail(self, endpoint, monkeypatch, caplog):
        def boom(*args, **kwargs):
            raise RuntimeError("https://tiles.example/{z}?api_key=secret 48.208176")

        monkeypatch.setattr(endpoint.map_snapshot, "render", boom)

        response = post(endpoint, "/map-snapshot", **_request())

        assert response.status_code == 502
        assert response.json["error"] == "render_failed"
        assert "secret" not in caplog.text and "48.208176" not in caplog.text
        assert "secret" not in response.get_data(as_text=True)

    def test_a_user_at_capacity_is_refused_and_no_session_is_evicted(self, endpoint):
        for _ in range(endpoint.MAX_SESSIONS):
            kept = post(endpoint, "/browse", url="https://example.com/", keep_session=True)
            assert kept.status_code == 200
        before = dict(endpoint._sessions)

        response = post(endpoint, "/map-snapshot", **_request())

        assert response.status_code == 503
        assert response.json["error"] == "capacity"
        assert endpoint._sessions == before
        assert endpoint.opened == []

    def test_the_slot_is_given_back(self, endpoint, monkeypatch):
        monkeypatch.setattr(endpoint, "MAX_SESSIONS", 1)
        for _ in range(3):
            assert post(endpoint, "/map-snapshot", **_request()).status_code == 200
        assert not endpoint._map_renders

    def test_the_briefings_identity_cannot_render(self, endpoint):
        response = endpoint.app.test_client().post(
            "/map-snapshot", json=_request(),
            headers={"X-Istota-User": "__briefings__", "X-Istota-Owner": "x"})
        assert response.status_code == 403
        assert endpoint.opened == []
