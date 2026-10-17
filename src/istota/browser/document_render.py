"""Render a web bundle to an image through the browser service's ``POST /render-document``.

The daemon pushes a self-contained HTML document and the payload it draws;
the browser serves it on its own render origin, calls
``window.istotaRender(payload)``, and answers with a PNG of the frame. The
browser never connects to istota, so the page draws only what it is sent.

A bundle is one file, ``<render dir>/<name>.html``, built by
``web/vite.render.config.ts`` from ``web/src/render/<name>/main.ts``; the
render dir's ``manifest.json`` names each file's sha256, and a file that does
not match is never sent. Adding a renderer is a new entry directory there and
a caller of :func:`render_document`; nothing here changes.

This module does the transport and maps every failure to a stable reason:
``bad_request`` (the caller's arguments, before any call), ``render_unavailable``
(no bundle, no browser, an older browser image), ``capacity``,
``render_timeout``, ``render_failed``. What the payload means, and whether the
picture that comes back is the right one, is the caller's to check. Nothing
logged here carries the payload, the document or the image.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from istota.browser.admission import (
    BrowserAdmissionUnconfigured,
    BrowserQueueTimeout,
    browser_admission,
    browser_call,
)
from istota.browser.owner import browser_headers
from istota.webui.static_dir import resolve_render_dir

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger(__name__)

ENDPOINT = "/render-document"
OUTPUT = "png"
#: The browser service's own caps (docker/browser/document_render.py).
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_ASSET_BYTES = 2 * 1024 * 1024
MAX_VIEWPORT = 2000
MAX_TIMEOUT_MS = 15_000
#: How much sooner the page's budget ends than the HTTP call's, so the
#: browser answers `render_timeout` before this side gives up on it.
PAGE_MARGIN_S = 1.0
DEFAULT_ADMISSION_WAIT_S = 2.0

_BUNDLE_NAME = re.compile(r"[a-z0-9-]{1,40}")
_PAGE_DETAIL = re.compile(r"[a-z_]{1,40}")
_PASSED_ON = frozenset({"render_unavailable", "capacity", "render_timeout"})


@dataclass(frozen=True)
class DocumentRender:
    """The image, or a stable reason. `detail` is the page's own `[a-z_]` reason, if any."""

    data: bytes | None
    reason: str | None
    detail: str | None = None


# path -> (mtime_ns, size, payload). The manifest and the bundles both.
_cache: dict[Path, tuple[int, int, object]] = {}
_warned: set[tuple[str, str]] = set()
_lock = threading.Lock()


def reset_cache() -> None:
    """Forget every cached file and every once-only warning (tests)."""
    with _lock:
        _cache.clear()
        _warned.clear()


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _cached(path: Path, parse):
    """`parse(bytes)` for the file at `path`, re-read only when its mtime or size changes."""
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    with _lock:
        hit = _cache.get(path)
        if hit is not None and hit[:2] == key:
            return hit[2]
    value = parse(_read_bytes(path))
    with _lock:
        _cache[path] = (*key, value)
    return value


def _warn_once(bundle: str, reason: str) -> None:
    with _lock:
        if (bundle, reason) in _warned:
            return
        _warned.add((bundle, reason))
    logger.warning("document_render.bundle_unavailable bundle=%s reason=%s", bundle, reason)


def _parse_manifest(data: bytes) -> dict:
    manifest = json.loads(data)
    return manifest if isinstance(manifest, dict) else {}


def _parse_bundle(data: bytes) -> tuple[str, str]:
    return data.decode("utf-8"), hashlib.sha256(data).hexdigest()


def load_bundle(bundle: str) -> tuple[str, str] | None:
    """The bundle's document and its sha256, or None when it is missing or not the manifest's."""
    directory = resolve_render_dir()
    try:
        manifest = _cached(directory / "manifest.json", _parse_manifest)
    except (OSError, ValueError):
        _warn_once(bundle, "no_manifest")
        return None
    expected = manifest.get(bundle)
    if not isinstance(expected, str):
        _warn_once(bundle, "not_in_manifest")
        return None
    try:
        document, digest = _cached(directory / f"{bundle}.html", _parse_bundle)
    except (OSError, UnicodeDecodeError):
        _warn_once(bundle, "no_bundle")
        return None
    if digest != expected:
        _warn_once(bundle, "digest_mismatch")
        return None
    return document, digest


def _viewport_ok(viewport) -> bool:
    if not isinstance(viewport, dict) or set(viewport) != {"width", "height"}:
        return False
    return all(
        isinstance(viewport[side], int) and not isinstance(viewport[side], bool)
        and 1 <= viewport[side] <= MAX_VIEWPORT
        for side in ("width", "height")
    )


def _encoded_assets(assets) -> dict | None:
    """The envelope's `assets`, or None when they are over the cap or out of shape."""
    encoded = {}
    total = 0
    for path, item in assets.items():
        if not isinstance(path, str) or not isinstance(item, tuple) or len(item) != 2:
            return None
        content_type, data = item
        if not isinstance(content_type, str) or not isinstance(data, (bytes, bytearray)):
            return None
        total += len(data)
        if total > MAX_ASSET_BYTES:
            return None
        encoded[path] = {"content_type": content_type,
                         "data_base64": base64.b64encode(bytes(data)).decode("ascii")}
    return encoded


def _refused(bundle: str, reason: str, detail: str | None = None) -> DocumentRender:
    logger.info("document_render.no_render bundle=%s reason=%s%s", bundle, reason,
                f" detail={detail}" if detail else "")
    return DocumentRender(None, reason, detail)


def _from_answer(bundle: str, response: httpx.Response) -> DocumentRender:
    if response.status_code == 200:
        if response.headers.get("content-type", "").split(";")[0].strip() == "image/png":
            return DocumentRender(response.content, None)
        return _refused(bundle, "render_failed")
    if response.status_code == 404:
        # A browser image older than the endpoint.
        return _refused(bundle, "render_unavailable")
    if response.status_code == 503:
        # The pool refuses before the endpoint runs, with its own message as
        # `error`, so any 503 not naming itself unavailable is a capacity refusal.
        try:
            named = response.json().get("error")
        except (ValueError, AttributeError):
            named = None
        return _refused(bundle, "render_unavailable" if named == "render_unavailable" else "capacity")
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    error = body.get("error")
    detail = body.get("detail")
    if not (isinstance(detail, str) and _PAGE_DETAIL.fullmatch(detail)):
        detail = None
    if error in _PASSED_ON:
        return _refused(bundle, error)
    return _refused(bundle, "render_failed", detail)


def render_document(
    config: "Config",
    *,
    actor_user_id: str,
    bundle: str,
    payload: dict,
    viewport: dict,
    timeout_s: float,
    assets: dict[str, tuple[str, bytes]] | None = None,
    admission_wait_s: float = DEFAULT_ADMISSION_WAIT_S,
) -> DocumentRender:
    """Render `bundle` drawing `payload` at `viewport`, as `actor_user_id`. Never raises.

    `timeout_s` bounds the whole call, admission included; `admission_wait_s`
    is how long to wait for the browser's admission lock. `assets` are served
    to the page at `/assets/<path>`, as `{path: (content_type, bytes)}`.
    """
    if not isinstance(bundle, str) or not _BUNDLE_NAME.fullmatch(bundle):
        return _refused("-", "bad_request")
    if not isinstance(payload, dict) or not _viewport_ok(viewport):
        return _refused(bundle, "bad_request")
    try:
        payload_size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return _refused(bundle, "bad_request")
    if payload_size > MAX_PAYLOAD_BYTES:
        return _refused(bundle, "bad_request")
    encoded_assets = _encoded_assets(assets or {})
    if encoded_assets is None:
        return _refused(bundle, "bad_request")
    browser = getattr(config, "browser", None)
    if browser is None or not getattr(browser, "enabled", False):
        return _refused(bundle, "render_unavailable")
    try:
        headers = browser_headers(actor_user_id)
    except ValueError:
        return _refused(bundle, "render_unavailable")
    loaded = load_bundle(bundle)
    if loaded is None:
        return DocumentRender(None, "render_unavailable")
    document, digest = loaded
    envelope = {
        "document": document,
        "document_sha256": digest,
        "payload": payload,
        "viewport": {"width": viewport["width"], "height": viewport["height"]},
        "output": OUTPUT,
    }
    if encoded_assets:
        envelope["assets"] = encoded_assets
    started = time.monotonic()
    try:
        with browser_admission(db_path=config.db_path, queue_timeout=admission_wait_s):
            remaining = max(1.0, timeout_s - (time.monotonic() - started))
            envelope["timeout_ms"] = max(
                1, min(MAX_TIMEOUT_MS, int((remaining - PAGE_MARGIN_S) * 1000)))
            response = browser_call(
                "post", f"{browser.api_url}{ENDPOINT}",
                headers=headers, json=envelope, timeout=remaining,
            )
    except BrowserQueueTimeout:
        return _refused(bundle, "capacity")
    except BrowserAdmissionUnconfigured:
        return _refused(bundle, "render_unavailable")
    except httpx.TimeoutException:
        return _refused(bundle, "render_timeout")
    except httpx.HTTPError:
        return _refused(bundle, "render_unavailable")
    except Exception as error:  # noqa: BLE001 - a render never fails its caller
        logger.warning("document_render.no_render bundle=%s reason=render_failed error=%s",
                       bundle, type(error).__name__)
        return DocumentRender(None, "render_failed")
    return _from_answer(bundle, response)
