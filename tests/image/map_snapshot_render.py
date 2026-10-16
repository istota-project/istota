"""Run inside the browser image: draw a share map from the built web page, for real.

The built web bundle is bound at /site and served on loopback under /istota/,
as the web app serves it, beside a deterministic tile server on the same
origin. Real Chromium loads `/istota/map-snapshot/`, and the shipped
`map_snapshot.render` drives it. The PNG goes to /out; the last stdout line is
a JSON report for the test on the host to read.
"""

import http.server
import json
import struct
import threading
import zlib
from pathlib import Path

from patchright.sync_api import sync_playwright

import map_snapshot

SITE = Path("/site")
OUT = Path("/out")
TILE_COLOR = (214, 96, 40)
_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
          ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
          ".woff2": "font/woff2", ".webmanifest": "application/manifest+json"}


def _solid_png(size, color):
    row = b"\0" + bytes(color) * size
    raw = zlib.compress(row * size)

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw)
            + chunk(b"IEND", b""))


TILE = _solid_png(256, TILE_COLOR)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/tiles/"):
            return self._send(TILE, "image/png")
        if not path.startswith("/istota/"):
            return self._send(b"not found", "text/plain", 404)
        target = SITE / path[len("/istota/"):]
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return self._send(b"not found", "text/plain", 404)
        return self._send(target.read_bytes(),
                          _TYPES.get(target.suffix, "application/octet-stream"))

    def _send(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _payload(tiles):
    return {
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
    }


def _draw(browser, render_url, payload):
    checked, refusal = map_snapshot.validate_request(payload)
    assert refusal is None, refusal
    context = browser.new_context(**map_snapshot.CONTEXT_OPTIONS)
    try:
        return map_snapshot.render(context, render_url, checked)
    finally:
        context.close()


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    render_url = f"{origin}/istota/map-snapshot/"
    report = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/google-chrome", headless=True,
            args=["--no-sandbox", "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
                  "--enable-webgl"],
        )
        png, reason = _draw(browser, render_url, _payload(f"{origin}/tiles/{{z}}/{{x}}/{{y}}.png"))
        report["drawn"] = reason
        if png is not None:
            (OUT / "map.png").write_bytes(png)
        # Tiles from the metadata address: the guard must stop the render.
        _, blocked = _draw(browser, render_url,
                           _payload("http://169.254.169.254/{z}/{x}/{y}.png"))
        report["blocked"] = blocked
        browser.close()
    server.shutdown()
    print(json.dumps(report))


main()
