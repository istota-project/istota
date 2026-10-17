"""A fake *arr stack behind one `httpx.MockTransport`, for the servarr tests.

Requests are routed by port, since the client connects to the checked address
and keeps the port: 8989 is sonarr, 7878 radarr, 8686 lidarr, 9696 prowlarr.
Every request is recorded, so a refusal can assert that nothing was sent.

The fixtures under ``tests/fixtures/servarr/`` are built from the public arr
API documentation and source, not captured from a running stack; each stage's
result file lists what was taken on trust.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "servarr"
HOST = "10.0.0.5"
PORTS = {"sonarr": 8989, "radarr": 7878, "lidarr": 8686, "prowlarr": 9696}
APP_BY_PORT = {port: app for app, port in PORTS.items()}
KEY = "never-shown-arr-sentinel"

#: Up to and including the API prefix, so an app under a URL base routes too.
_API_PREFIX = re.compile(r"^.*?/api/v\d+/")


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def api_path(request: httpx.Request) -> str:
    """A recorded request's path below the API prefix: ``album/monitor``."""
    return _API_PREFIX.sub("", request.url.path)


def url(app: str) -> str:
    return f"http://{HOST}:{PORTS[app]}"


class FakeArr:
    """Scripted answers keyed by ``(app, method, path)``; records every request.

    A route holds a list: each request takes the next item, and the last one
    repeats. An item is an `httpx.Response`, an exception to raise, or a
    callable taking the request. ``system/status`` answers each app's fixture
    unless a test scripts it.
    """

    def __init__(self):
        self.routes: dict[tuple[str, str, str], list] = {}
        self.requests: list[httpx.Request] = []

    def on(self, app: str, method: str, path: str, *answers) -> "FakeArr":
        self.routes[(app, method.upper(), path)] = list(answers)
        return self

    def json(self, app: str, path: str, data, *, method: str = "GET", status: int = 200):
        return self.on(app, method, path, httpx.Response(status, json=data))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        app = APP_BY_PORT.get(request.url.port, "?")
        path = _API_PREFIX.sub("", request.url.path)
        answers = self.routes.get((app, request.method, path))
        if answers is None:
            if request.method == "GET" and path == "system/status":
                return httpx.Response(200, json=fixture(f"system_status_{app}.json"))
            return httpx.Response(404, json={"message": "NotFound"})
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(request)
        return answer

    def sent(self, app: str | None = None, path: str | None = None) -> list[httpx.Request]:
        """The recorded requests, narrowed to one app and one API path."""
        out = []
        for request in self.requests:
            if app is not None and APP_BY_PORT.get(request.url.port) != app:
                continue
            if path is not None and _API_PREFIX.sub("", request.url.path) != path:
                continue
            out.append(request)
        return out
