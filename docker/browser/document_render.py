"""A document the daemon sends, rendered to an image: the envelope, the render origin, the guard.

`POST /render-document` (browse_api.py) takes a self-contained HTML document,
the JSON payload it draws, and optionally a few assets, and answers with a PNG
of the document's `[data-render-frame]`. The browser never connects to istota:
everything the page draws arrives in the request. The first document is the
location share map; the shape is the same for any later one (a chart, a
preview), which is a new web bundle and a new caller, not a change here.

This module holds nothing specific to any renderer. It checks the envelope's
shape and never reads the payload, whose meaning is the caller's.

**The page is ours; everything it loads is not.** The document is served from
`https://render.istota.invalid/` by the route handler, with a
Content-Security-Policy allowing inline script and style, `blob:` workers,
`data:`/`blob:` images and `https:` connections and images, and nothing else.
The sent assets are served at `/assets/<path>`; any other path on that origin
is a 404. Every other request (a basemap's tiles, its style) must pass
`sandbox/net_guard.py`'s public-address rule, vendored as
`lib/istota_net_guard.py`. A navigation anywhere but the document, a popup, a
download, or a request the route never saw (a redirect hop) fails the render:
no image leaves.

Residual: the guard resolves a host and Chrome resolves it again, so a DNS
answer that changes between the two (rebinding) is not caught here. The
browser's own network is what bounds that: it is joined by the daemon alone.

Nothing here logs the payload, the document, the image, a URL or an
exception's text: a tile URL can carry the user's API key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from lib.istota_net_guard import ip_is_public

RENDER_ORIGIN = "https://render.istota.invalid"
DOCUMENT_URL = RENDER_ORIGIN + "/"
ASSET_PREFIX = "/assets/"
FRAME_SELECTOR = "[data-render-frame]"

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_ASSET_BYTES = 2 * 1024 * 1024
MAX_ASSETS = 64
MAX_VIEWPORT = 2000
MAX_TIMEOUT_MS = 15_000
#: The whole request body: a full document, base64 assets and the payload.
MAX_REQUEST_BYTES = 8 * 1024 * 1024
OUTPUTS = ("png",)

#: What a sent asset may be. No HTML, script or SVG: an asset is fetched by
#: the page, and a navigation to one is refused besides.
ASSET_TYPES = frozenset({
    "image/png", "image/jpeg", "image/webp", "image/gif",
    "font/woff2", "font/woff", "font/ttf", "font/otf",
    "application/json", "text/css",
})
_ASSET_PATH = re.compile(r"[a-z0-9._/-]{1,200}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PAGE_REASON = re.compile(r"[a-z_]{1,40}")

_ENVELOPE_KEYS = frozenset(("document", "document_sha256", "payload", "viewport", "output",
                            "timeout_ms"))
_OPTIONAL_KEYS = frozenset(("assets",))

CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'none'",
    "script-src 'unsafe-inline'",
    "style-src 'unsafe-inline'",
    "worker-src blob:",
    "img-src data: blob: https:",
    "connect-src https:",
    "font-src https: data:",
    "frame-src 'none'",
    "form-action 'none'",
    "base-uri 'none'",
    "object-src 'none'",
))

_RENDER_JS = """async ([payload, ms]) => {
  const timer = new Promise((resolve) =>
    setTimeout(() => resolve({ hostTimeout: true }), ms));
  return await Promise.race([window.istotaRender(payload), timer]);
}"""
_READY_JS = "() => typeof window.istotaRender === 'function'"


@dataclass(frozen=True)
class Envelope:
    """A checked request. `document` is the UTF-8 bytes the digest was taken over."""

    document: bytes
    payload: dict
    assets: dict
    viewport: dict
    output: str
    timeout_ms: int


@dataclass(frozen=True)
class Outcome:
    """The image, or a stable reason. `detail` is the page's own `[a-z_]` reason, if any."""

    data: bytes | None
    reason: str | None
    detail: str | None = None


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------

def _int_in(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _asset_path_ok(path):
    if not isinstance(path, str) or not _ASSET_PATH.fullmatch(path):
        return False
    if path.startswith("/") or path.endswith("/") or "//" in path or ".." in path:
        return False
    return True


def _assets(raw):
    """`{path: (content_type, bytes)}`, or None when anything is out of shape."""
    if not isinstance(raw, dict) or len(raw) > MAX_ASSETS:
        return None
    checked = {}
    total = 0
    for path, item in raw.items():
        if not _asset_path_ok(path):
            return None
        if not isinstance(item, dict) or set(item) != {"content_type", "data_base64"}:
            return None
        content_type, encoded = item["content_type"], item["data_base64"]
        if content_type not in ASSET_TYPES or not isinstance(encoded, str):
            return None
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return None
        total += len(data)
        if total > MAX_ASSET_BYTES:
            return None
        checked[path] = (content_type, data)
    return checked


def validate_envelope(data):
    """`(Envelope, None)` for a request this service will render, else `(None, "bad_request")`.

    The keys are exact, so a URL to visit or a session id has nowhere to ride.
    The payload must be a JSON object under the cap and is otherwise opaque.
    """
    refused = (None, "bad_request")
    if not isinstance(data, dict):
        return refused
    keys = set(data)
    if not _ENVELOPE_KEYS <= keys or not keys <= _ENVELOPE_KEYS | _OPTIONAL_KEYS:
        return refused
    document = data["document"]
    if not isinstance(document, str):
        return refused
    try:
        encoded = document.encode("utf-8")
    except UnicodeEncodeError:
        return refused
    if len(encoded) > MAX_DOCUMENT_BYTES:
        return refused
    digest = data["document_sha256"]
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        return refused
    if hashlib.sha256(encoded).hexdigest() != digest:
        return refused
    payload = data["payload"]
    if not isinstance(payload, dict):
        return refused
    try:
        size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return refused
    if size > MAX_PAYLOAD_BYTES:
        return refused
    viewport = data["viewport"]
    if not isinstance(viewport, dict) or set(viewport) != {"width", "height"}:
        return refused
    if not all(_int_in(viewport[side], 1, MAX_VIEWPORT) for side in ("width", "height")):
        return refused
    if data["output"] not in OUTPUTS:
        return refused
    if not _int_in(data["timeout_ms"], 1, MAX_TIMEOUT_MS):
        return refused
    assets = _assets(data.get("assets", {}))
    if assets is None:
        return refused
    return Envelope(
        document=encoded,
        payload=payload,
        assets=assets,
        viewport={"width": viewport["width"], "height": viewport["height"]},
        output=data["output"],
        timeout_ms=data["timeout_ms"],
    ), None


def context_options(envelope):
    """Options for the fresh context, which shares no cookie or storage with the user's profile."""
    return {
        "viewport": dict(envelope.viewport),
        "device_scale_factor": 1,
        "accept_downloads": False,
        "service_workers": "block",
        "locale": "en-US",
        "timezone_id": "UTC",
    }


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


_RENDER_ORIGIN_KEY = origin_of(RENDER_ORIGIN)


def on_render_origin(url):
    return isinstance(url, str) and origin_of(url) == _RENDER_ORIGIN_KEY


def _resolve(host, port):
    """Every address `host` resolves to, as strings; empty on any failure."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError, ValueError):
        return []
    return [info[4][0].split("%", 1)[0] for info in infos]


def destination_allowed(url, *, resolve=None):
    """Whether the render context may fetch `url` from the network.

    `data:` and `blob:` reach no network and pass. Anything else must be
    http(s), carry no credentials, and resolve only to public addresses. The
    render origin is never fetched: the route serves it, so here it fails.
    """
    resolve = resolve or _resolve
    if not isinstance(url, str):
        return False
    scheme = url.split(":", 1)[0].lower()
    if scheme in ("data", "blob"):
        return True
    if origin_of(url) is None or on_render_origin(url):
        return False
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

def _path_of(url):
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return None


def _serve(route, envelope, path):
    """Answer a request on the render origin: the document, a sent asset, or a 404."""
    if path == "/":
        route.fulfill(status=200, headers={
            "content-type": "text/html; charset=utf-8",
            "content-security-policy": CONTENT_SECURITY_POLICY,
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        }, body=envelope.document)
        return
    if path.startswith(ASSET_PREFIX) and path[len(ASSET_PREFIX):] in envelope.assets:
        content_type, body = envelope.assets[path[len(ASSET_PREFIX):]]
        route.fulfill(status=200, headers={
            "content-type": content_type,
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        }, body=body)
        return
    route.fulfill(status=404, headers={"content-type": "text/plain"}, body=b"not found")


def _close_quietly(page):
    try:
        page.close()
    except Exception:
        pass


def _page_failure(result):
    if isinstance(result, dict) and result.get("hostTimeout") is True:
        return Outcome(None, "render_timeout")
    reason = result.get("reason") if isinstance(result, dict) else None
    detail = reason if isinstance(reason, str) and _PAGE_REASON.fullmatch(reason) else None
    return Outcome(None, "render_failed", detail)


def render(context, envelope, *, resolve=None, clock=time.monotonic):
    """Render `envelope` in `context`, a fresh one the caller closes.

    Reasons: `destination_blocked`, `render_timeout`, `render_failed`.
    """
    resolve = resolve or _resolve
    resolved = {}
    violations = []

    def cached_resolve(host, port):
        key = (host, port)
        if key not in resolved:
            resolved[key] = resolve(host, port)
        return resolved[key]

    def allowed(url):
        return on_render_origin(url) or destination_allowed(url, resolve=cached_resolve)

    def on_request(request):
        if not allowed(request.url):
            violations.append("request")

    def on_route(route):
        request = route.request
        url = request.url
        try:
            navigation = bool(request.is_navigation_request())
        except Exception:
            navigation = True
        if navigation and not (on_render_origin(url) and _path_of(url) == "/"):
            violations.append("navigation")
            route.abort("blockedbyclient")
        elif on_render_origin(url):
            _serve(route, envelope, _path_of(url))
        elif destination_allowed(url, resolve=cached_resolve):
            route.continue_()
        else:
            violations.append("route")
            route.abort("blockedbyclient")

    deadline = clock() + envelope.timeout_ms / 1000

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
            if getattr(frame, "parent_frame", None) is not None:
                return
            if not (on_render_origin(frame.url) and _path_of(frame.url) == "/"):
                violations.append("navigated")

        def on_download(download):
            violations.append("download")

        # After new_page, so the context's own first page is not a popup.
        context.on("page", on_popup)
        page.on("framenavigated", on_navigated)
        page.on("download", on_download)

        page.goto(DOCUMENT_URL, wait_until="load", timeout=remaining_ms())
        page.wait_for_function(_READY_JS, timeout=remaining_ms())
        result = page.evaluate(_RENDER_JS, [envelope.payload, remaining_ms()])
        if violations:
            return Outcome(None, "destination_blocked")
        if not isinstance(result, dict) or result.get("ok") is not True:
            return _page_failure(result)
        image = page.locator(FRAME_SELECTOR).screenshot(
            type="png", timeout=remaining_ms(), animations="disabled",
        )
    except Exception as error:
        # Patchright raises its own TimeoutError; the name is the test, since
        # its module is not importable where this is unit-tested.
        if violations:
            return Outcome(None, "destination_blocked")
        if type(error).__name__ == "TimeoutError":
            return Outcome(None, "render_timeout")
        return Outcome(None, "render_failed")
    if violations:
        return Outcome(None, "destination_blocked")
    return Outcome(bytes(image), None)
