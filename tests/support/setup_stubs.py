"""HTTP stubs for the first-run setup's connection checks.

Built on `testbed.httpstub.HttpStub`, bound to loopback, each answering only
what one check asks: the Nextcloud screen's six requests, a CalDAV PROPFIND,
and a model endpoint that refuses. The model check's success case runs against
the testbed's scripted endpoint instead (`serve_script`).

Every stub records each request's auth as a shape (`ServiceCall.auth`) and
keeps the raw header apart in `auth_headers`, so a test can say the
credential it typed is the one that arrived, without the value reaching a
failure report through `calls`.
"""

from __future__ import annotations

import base64
import json
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from testbed.httpstub import HttpStub
from testbed.services import ServiceCall


def _auth_shape(header: str) -> str:
    scheme, _, value = header.partition(" ")
    return f"{scheme} len={len(value)}" if header else ""


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class _Recording(HttpStub):
    def __init__(self) -> None:
        super().__init__()
        self.auth_headers: list[str] = []

    def _record(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parts = urlsplit(handler.path)
        header = handler.headers.get("authorization", "")
        with self._lock:
            self.auth_headers.append(header)
        self.record(ServiceCall(
            method=method, path=parts.path, auth=_auth_shape(header),
            query=parse_qs(parts.query),
        ))

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 5

            def log_message(self, *args) -> None:
                pass

            def handle_error(self, request, client_address) -> None:
                pass

            def _drain(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)

            def _reply(self, status: int, body: bytes, content_type: str, extra=None) -> None:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._drain()
                stub._record(self, "GET")
                stub.answer(self, "GET")

            def do_POST(self) -> None:
                self._drain()
                stub._record(self, "POST")
                stub.answer(self, "POST")

            def do_PROPFIND(self) -> None:
                self._drain()
                stub._record(self, "PROPFIND")
                stub.answer(self, "PROPFIND")

        return Handler

    def serve(self):
        self.start(self._handler())
        return self


def _ocs(data, *, status: int = 200, message: str = "OK") -> bytes:
    ok = 200 <= status < 300
    return json.dumps({"ocs": {
        "meta": {"status": "ok" if ok else "failure", "statuscode": 200 if ok else 997, "message": message},
        "data": data,
    }}).encode()


_MULTISTATUS = (
    b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>'
    b"<d:href>/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/>"
    b"</d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
    b"</d:response></d:multistatus>"
)


class FakeNextcloud(_Recording):
    """Capabilities, `/cloud/user`, the account's WebDAV root and sharee search."""

    def __init__(
        self, *, username: str = "istota", password: str = "app-pass",
        account_id: str | None = None, groups: tuple[str, ...] = (),
        talk: bool = True, sharees: tuple[str, ...] = (), files_status: int = 207,
        error_body: str = "",
    ) -> None:
        super().__init__()
        self.username = username
        self.password = password
        self.account_id = account_id or username
        self.groups = list(groups)
        self.talk = talk
        self.sharees = list(sharees)
        self.files_status = files_status
        #: Text put into every refusal, so a test can say it never reached the user.
        self.error_body = error_body

    def answer(self, h, method: str) -> None:
        path = urlsplit(h.path).path
        if h.headers.get("authorization", "") != basic(self.username, self.password):
            h._reply(401, _ocs([], status=401, message=f"Unauthorised {self.error_body}"),
                     "application/json")
            return
        if method == "GET" and path == "/ocs/v2.php/cloud/capabilities":
            caps = {"core": {"webdav-root": "remote.php/webdav"}, "files_sharing": {"api_enabled": True}}
            if self.talk:
                caps["spreed"] = {"features": ["chat-v2"]}
            h._reply(200, _ocs({"version": {"string": "30.0.1"}, "capabilities": caps}), "application/json")
        elif method == "GET" and path == "/ocs/v2.php/cloud/user":
            h._reply(200, _ocs({"id": self.account_id, "groups": self.groups}), "application/json")
        elif method == "PROPFIND" and path == f"/remote.php/dav/files/{self.username}/":
            if self.files_status == 207:
                h._reply(207, _MULTISTATUS, "application/xml")
            else:
                h._reply(self.files_status, self.error_body.encode(), "text/plain")
        elif method == "GET" and path == "/ocs/v2.php/apps/files_sharing/api/v1/sharees":
            search = (parse_qs(urlsplit(h.path).query).get("search") or [""])[0]
            exact = [
                {"label": uid, "value": {"shareType": 0, "shareWith": uid}}
                for uid in self.sharees if uid == search
            ]
            h._reply(200, _ocs({"exact": {"users": exact}, "users": []}), "application/json")
        else:
            h._reply(404, self.error_body.encode(), "text/plain")


class CalendarServer(_Recording):
    """PROPFIND with basic auth: 207 for the right pair, 401 otherwise."""

    def __init__(self, *, username: str, password: str, error_body: str = "") -> None:
        super().__init__()
        self.username = username
        self.password = password
        self.error_body = error_body

    def answer(self, h, method: str) -> None:
        if method != "PROPFIND":
            h._reply(405, b"", "text/plain")
        elif h.headers.get("authorization", "") != basic(self.username, self.password):
            h._reply(401, self.error_body.encode(), "text/plain",
                     {"www-authenticate": 'Basic realm="calendar"'})
        else:
            h._reply(207, _MULTISTATUS, "application/xml")


class RefusingModel(_Recording):
    """An OpenAI-compatible endpoint answering every completion with `status`."""

    def __init__(self, *, status: int, body: str = "", delay: float = 0.0) -> None:
        super().__init__()
        self.status = status
        self.body = body
        self.delay = delay

    @property
    def base_url(self) -> str:
        return f"{self.url}/v1"

    def answer(self, h, method: str) -> None:
        if self.delay:
            time.sleep(self.delay)
        h._reply(self.status, json.dumps({"error": {"message": self.body}}).encode(), "application/json")
