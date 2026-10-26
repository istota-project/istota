"""A loopback HTTP server for the feeds tests that fetch for real (#838).

Each path answers from `routes` as ``(status, headers, body)``, anything else
404, and every request path is recorded in `received`, so a test can tell a
refused fetch from one that reached the server.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FeedServer:
    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, dict, bytes]] = {}
        self.received: list[str] = []
        self.authorization: list[str | None] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                server.received.append(self.path)
                server.authorization.append(self.headers.get("Authorization"))
                status, headers, payload = server.routes.get(self.path, (404, {}, b""))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str, host: str = "localhost") -> str:
        return f"http://{host}:{self.port}{path}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
