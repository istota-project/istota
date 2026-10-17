"""Run inside the browser image: render the built share-map bundle as a document, for real.

The bundle is bound at /bundle. Real Chromium loads it on the render origin
through the shipped `document_render.render`, with a raster basemap whose one
tile is a sent asset, so nothing leaves the container. The PNG goes to /out;
the last stdout line is a JSON report for the test on the host to read.
"""

import base64
import hashlib
import json
import struct
import zlib
from pathlib import Path

from patchright.sync_api import sync_playwright

import document_render

BUNDLE = Path("/bundle/share-map.html")
OUT = Path("/out")
TILE_COLOR = (214, 96, 40)


def _solid_png(size, color):
    row = b"\0" + bytes(color) * size
    raw = zlib.compress(row * size)

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw)
            + chunk(b"IEND", b""))


def _envelope(document, tiles):
    viewport = {"width": 640, "height": 360}
    return {
        "document": document,
        "document_sha256": hashlib.sha256(document.encode()).hexdigest(),
        "payload": {
            "observation": {
                "precision": "exact", "lat": 48.208176, "lon": 16.373819, "accuracy_m": 120.0,
                "observed_at": "2026-10-16T14:02:00+00:00", "freshness": "fresh",
                "owner_user_id": "alice",
            },
            "basemap": {
                "provider": "osm", "kind": "raster", "light": tiles, "dark": tiles,
                "attribution": "Test tiles",
            },
            "owner": "Alice",
            "viewport": viewport,
        },
        "assets": {
            "tile.png": {"content_type": "image/png",
                         "data_base64": base64.b64encode(_solid_png(256, TILE_COLOR)).decode()},
        },
        "viewport": viewport,
        "output": "png",
        "timeout_ms": 15000,
    }


def _draw(browser, raw):
    envelope, refusal = document_render.validate_envelope(raw)
    assert refusal is None, refusal
    context = browser.new_context(**document_render.context_options(envelope))
    try:
        return document_render.render(context, envelope)
    finally:
        context.close()


def main():
    document = BUNDLE.read_text(encoding="utf-8")
    report = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/google-chrome", headless=True,
            args=["--no-sandbox", "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
                  "--enable-webgl"],
        )
        # One URL with no placeholders: every tile is the one sent asset.
        drawn = _draw(browser, _envelope(document, f"{document_render.RENDER_ORIGIN}/assets/tile.png"))
        report["drawn"] = [drawn.reason, drawn.detail]
        if drawn.data is not None:
            (OUT / "map.png").write_bytes(drawn.data)
        # Tiles from the metadata address over https, which the page's CSP
        # allows: the guard must stop the render.
        blocked = _draw(browser, _envelope(document, "https://169.254.169.254/{z}/{x}/{y}.png"))
        report["blocked"] = blocked.reason
        browser.close()
    print(json.dumps(report))


main()
