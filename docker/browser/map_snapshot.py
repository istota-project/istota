"""A location share's map as a PNG: the request, the destination guard and the render.

`POST /map-snapshot` (browse_api.py) is how a Talk or WhatsApp share gets its
picture. The daemon sends the projected observation, the actor's resolved
basemap and the owner's display label; this module checks every field, opens
the web app's `/map-snapshot/` page in a fresh cookie-free context, hands it
the payload through `window.renderSnapshot`, and captures the 640 x 360 frame.

**The page is ours; everything it loads is not.** The basemap's style, tiles,
glyphs and sprites come from wherever the operator's provider points, so every
request the context makes is checked: the renderer's exact origin passes,
anything else must be http(s) to a host whose every address is public by
`sandbox/net_guard.py`'s rule (vendored as `lib/istota_net_guard.py`). A
document navigation anywhere but the renderer origin, a popup, a download, or a
request the route never saw (a redirect hop) fails the render: no PNG leaves.

Residual: the guard resolves a host and Chrome resolves it again, so a DNS
answer that changes between the two (rebinding) is not caught here. The
browser's own network is what bounds that: it is joined by the daemon alone.

Nothing here logs a coordinate, a URL or an exception's text: a tile URL can
carry the user's API key.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import socket
import struct
import time
from urllib.parse import urlsplit

from lib.istota_net_guard import ip_is_public

FRAME_SELECTOR = "[data-snapshot-frame]"
WIDTH = 640
HEIGHT = 360
MAX_PNG_BYTES = 1024 * 1024
#: Web Mercator's edge; `shareMap.ts`'s `isMercatorSafe` uses the same value.
MERCATOR_MAX_LAT = 85.0511
#: The whole render, page load to capture. The daemon gives up at 10 s.
RENDER_BUDGET_S = 9.0

#: Options for the fresh context, which shares no cookie or storage with the
#: user's profile. The viewport is larger than the frame, so the capture of the
#: frame element never depends on page margins.
CONTEXT_OPTIONS = {
    "viewport": {"width": 800, "height": 600},
    "device_scale_factor": 1,
    "accept_downloads": False,
    "service_workers": "block",
    "locale": "en-US",
    "timezone_id": "UTC",
}

PRECISIONS = ("exact", "area")
FRESHNESS = ("fresh", "fresh_clock_skew", "last_known")
PROVIDERS = ("openfreemap", "carto", "osm", "custom")
BASEMAP_KINDS = ("style", "raster")
_OBSERVATION_KEYS = frozenset((
    "precision", "lat", "lon", "accuracy_m", "observed_at", "freshness", "owner_user_id",
))
_BASEMAP_KEYS = frozenset(("provider", "kind", "light", "dark", "attribution"))
_REQUEST_KEYS = frozenset(("observation", "basemap", "owner"))
_MAX_ACCURACY_M = 100_000
_MAX_LABEL = 200
_MAX_URL = 2048
_MAX_ATTRIBUTION = 1000
_OBSERVED_AT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?"
)
# The one markup an attribution may carry: a plain http(s) link, as the
# resolver's own provider rows write it. MapLibre puts the string in as HTML.
_ATTRIBUTION_LINK = re.compile(r'<a href="https?://[^"<>\s]+">|</a>')
_URL_FORBIDDEN = re.compile(r"[\x00-\x20\x7f\"'<>\\`]")

#: What the page may answer with when it could not draw (`ShareMap`'s
#: `onReady` reasons plus the page's own). Anything else reads as render_failed.
_PAGE_TIMEOUT = "timeout"

_RENDER_JS = """async ([payload, ms]) => {
  const timer = new Promise((resolve) =>
    setTimeout(() => resolve({ ok: false, reason: 'timeout' }), ms));
  return await Promise.race([window.renderSnapshot(payload), timer]);
}"""
_READY_JS = "() => typeof window.renderSnapshot === 'function'"


def configured_render_url():
    """`ISTOTA_MAP_RENDER_URL`, or None when unset or not a plain http(s) URL."""
    value = os.environ.get("ISTOTA_MAP_RENDER_URL", "").strip()
    if not value or origin_of(value) is None:
        return None
    if urlsplit(value).username is not None:
        return None
    return value


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------

def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _plain_label(value):
    return (isinstance(value, str) and 0 < len(value) <= _MAX_LABEL
            and "<" not in value and ">" not in value
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _http_url(value):
    if not isinstance(value, str) or not value or len(value) > _MAX_URL:
        return False
    if _URL_FORBIDDEN.search(value):
        return False
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and bool(parts.hostname)
            and parts.username is None and parts.password is None)


def _attribution(value):
    if not isinstance(value, str) or len(value) > _MAX_ATTRIBUTION:
        return False
    rest = _ATTRIBUTION_LINK.sub("", value)
    return "<" not in rest and ">" not in rest


def _observation_problem(raw):
    if not isinstance(raw, dict) or set(raw) != _OBSERVATION_KEYS:
        return "bad_request"
    if raw["precision"] not in PRECISIONS or raw["freshness"] not in FRESHNESS:
        return "bad_request"
    lat, lon, accuracy = raw["lat"], raw["lon"], raw["accuracy_m"]
    if not _number(lat) or not _number(lon) or abs(lat) > 90 or abs(lon) > 180:
        return "bad_request"
    if accuracy is not None and (not _number(accuracy) or not 0 <= accuracy <= _MAX_ACCURACY_M):
        return "bad_request"
    if raw["precision"] == "area":
        # An area was rounded before it left the location module; a value
        # that is not is a payload this service will not draw a cell around.
        if accuracy is not None or round(lat, 2) != lat or round(lon, 2) != lon:
            return "bad_request"
    observed_at = raw["observed_at"]
    if not isinstance(observed_at, str) or not _OBSERVED_AT.fullmatch(observed_at):
        return "bad_request"
    if not _plain_label(raw["owner_user_id"]):
        return "bad_request"
    if abs(lat) > MERCATOR_MAX_LAT:
        return "not_mercator_safe"
    return None


def _basemap_problem(raw):
    if not isinstance(raw, dict) or not set(raw) <= _BASEMAP_KEYS:
        return "bad_request"
    if not {"provider", "kind", "light"} <= set(raw):
        return "bad_request"
    if raw["provider"] not in PROVIDERS or raw["kind"] not in BASEMAP_KINDS:
        return "bad_request"
    urls = [raw["light"]] + ([raw["dark"]] if raw.get("dark") else [])
    if not all(_http_url(url) for url in urls):
        return "bad_request"
    if raw["kind"] == "raster" and not all(
            all(token in url for token in ("{z}", "{x}", "{y}")) for url in urls):
        return "bad_request"
    if not _attribution(raw.get("attribution", "")):
        return "bad_request"
    return None


def validate_request(data):
    """`(payload, None)` for a request this service will draw, else `(None, reason)`.

    The keys are exact at every level, so a URL to visit, a session id or a
    style document has nowhere to ride. The payload is the request as sent:
    the same shape `parseSnapshotPayload` reads on the page.
    """
    if not isinstance(data, dict) or set(data) != _REQUEST_KEYS:
        return None, "bad_request"
    if not _plain_label(data["owner"]):
        return None, "bad_request"
    basemap = _basemap_problem(data["basemap"])
    if basemap:
        return None, basemap
    observation = _observation_problem(data["observation"])
    if observation:
        return None, observation
    return data, None


# ---------------------------------------------------------------------------
# The destination guard
# ---------------------------------------------------------------------------

def origin_of(url):
    """`scheme://host:port`, lowercased with the port explicit, or None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


def _resolve(host, port):
    """Every address `host` resolves to, as strings; empty on any failure."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError, ValueError):
        return []
    return [info[4][0].split("%", 1)[0] for info in infos]


def destination_allowed(url, render_origin, *, resolve=None):
    """Whether the capture context may fetch `url`.

    The renderer's exact origin passes, and so do `data:` and `blob:`, which
    reach no network. Anything else must be http(s), carry no credentials, and
    resolve only to public addresses.
    """
    resolve = resolve or _resolve
    if not isinstance(url, str):
        return False
    scheme = url.split(":", 1)[0].lower()
    if scheme in ("data", "blob"):
        return True
    origin = origin_of(url)
    if origin is None:
        return False
    if origin == render_origin:
        return True
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        return False
    host = parts.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return ip_is_public(literal)
    addresses = resolve(host, parts.port or (443 if parts.scheme == "https" else 80))
    if not addresses:
        return False
    for address in addresses:
        try:
            if not ip_is_public(ipaddress.ip_address(address)):
                return False
        except ValueError:
            return False
    return True


# ---------------------------------------------------------------------------
# The render
# ---------------------------------------------------------------------------

def png_problem(data):
    """None for a PNG of exactly the frame's size and under the cap, else `bad_png`."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_PNG_BYTES:
        return "bad_png"
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return "bad_png"
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (WIDTH, HEIGHT):
        return "bad_png"
    return None


def _close_quietly(page):
    try:
        page.close()
    except Exception:
        pass


def render(context, render_url, payload, *, resolve=None, clock=time.monotonic):
    """Draw `payload` on the renderer page in `context`; `(png, None)` or `(None, reason)`.

    `context` is a fresh one the caller closes. Reasons: `destination_blocked`,
    `render_timeout`, `render_failed`, `bad_png`.
    """
    render_origin = origin_of(render_url)
    resolve = resolve or _resolve
    resolved = {}
    violations = []

    def cached_resolve(host, port):
        key = (host, port)
        if key not in resolved:
            resolved[key] = resolve(host, port)
        return resolved[key]

    def allowed(url):
        return destination_allowed(url, render_origin, resolve=cached_resolve)

    def on_request(request):
        if not allowed(request.url):
            violations.append("request")

    def on_route(route):
        request = route.request
        navigation = False
        try:
            navigation = bool(request.is_navigation_request())
        except Exception:
            navigation = True
        if navigation and origin_of(request.url) != render_origin:
            violations.append("navigation")
            route.abort("blockedbyclient")
        elif allowed(request.url):
            route.continue_()
        else:
            violations.append("route")
            route.abort("blockedbyclient")

    deadline = clock() + RENDER_BUDGET_S

    def remaining_ms():
        return max(1, int((deadline - clock()) * 1000))

    try:
        context.on("request", on_request)
        context.route("**/*", on_route)
        page = context.new_page()

        def on_popup(other):
            violations.append("popup")
            _close_quietly(other)

        def on_navigated(frame):
            if getattr(frame, "parent_frame", None) is None and origin_of(frame.url) != render_origin:
                violations.append("navigated")

        def on_download(download):
            violations.append("download")

        # After new_page, so the context's own first page is not a popup.
        context.on("page", on_popup)
        page.on("framenavigated", on_navigated)
        page.on("download", on_download)

        page.goto(render_url, wait_until="load", timeout=remaining_ms())
        page.wait_for_function(_READY_JS, timeout=remaining_ms())
        result = page.evaluate(_RENDER_JS, [payload, remaining_ms()])
        if violations:
            return None, "destination_blocked"
        if not isinstance(result, dict) or result.get("ok") is not True:
            reason = result.get("reason") if isinstance(result, dict) else None
            return None, "render_timeout" if reason == _PAGE_TIMEOUT else "render_failed"
        png = page.locator(FRAME_SELECTOR).screenshot(
            type="png", timeout=remaining_ms(), animations="disabled",
        )
    except Exception as error:
        # Patchright raises its own TimeoutError; the name is the test, since
        # its module is not importable where this is unit-tested.
        if violations:
            return None, "destination_blocked"
        if type(error).__name__ == "TimeoutError":
            return None, "render_timeout"
        return None, "render_failed"
    if violations:
        return None, "destination_blocked"
    problem = png_problem(png)
    if problem:
        return None, problem
    return bytes(png), None
