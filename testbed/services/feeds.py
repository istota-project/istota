"""A static document server the feeds poller can be pointed at.

What this witnesses is the poller end to end against a real HTTP client:
`httpx.get` with the conditional-GET headers `_poll_rss` builds, feedparser
against bytes that arrived over a socket, the per-user SQLite write, and
`image_dedupe`. Every existing test of that path injects an `http_get` stub, so
nothing has ever checked that the headers the poller *sends* are the ones a
server reads, or that a 304 is honoured rather than parsed as an empty feed.

A stub rather than a real feed reader's server, on the spec's rule: the client
does not negotiate — it GETs a document and reads three headers — so the real
protocol is what this speaks. What a stub adds over a directory of files behind
nginx is the ability to *drive* the conditional-GET behaviour deliberately, and
to record which headers arrived.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler

from ..httpstub import LOOPBACK, HttpStub
from . import ServiceCall

#: The credential this stub publishes, and it authenticates nothing.
#:
#: Same shape as `model_endpoint.ENDPOINT_CREDENTIAL` and for the same reason.
#: Nothing in the feeds path can send a credential — `_poll_rss` builds two
#: conditional headers and a User-Agent and no more — so a stub that *checked*
#: one would fail every poll. `HttpStub.start` still requires a value for a
#: non-loopback bind, and the value is worth having anyway: it is one more name
#: the secret-isolation scenario can prove did not reach the model, and it
#: keeps the tier's record of what it has published on a shared network total.
#:
#: What it does not buy is protection. The listener serves only documents a
#: test registered in this process and holds nothing else, which is why an
#: unchecked credential is honest here and would not be on the forge.
FEEDS_CREDENTIAL = "unused-by-the-feeds-stub"

#: What `_poll_rss` sends when a feed has an `etag` and a `last_modified`.
IF_NONE_MATCH = "If-None-Match"
IF_MODIFIED_SINCE = "If-Modified-Since"


@dataclass
class Document:
    """One thing this server will hand out."""

    body: bytes
    content_type: str = "application/xml"
    etag: str = ""
    last_modified: str = ""
    #: Answer a matching conditional request with 304 rather than the body.
    #:
    #: Per document rather than per server: a scenario asserting that a repeat
    #: poll is cheap needs one feed that 304s, and a scenario asserting that a
    #: changed feed still comes through needs one beside it that does not.
    conditional_get: bool = True


class FeedsService(HttpStub):
    """A running document server and the record of what was fetched from it."""

    name = "feeds"

    def __init__(self) -> None:
        super().__init__()
        self._documents: dict[str, Document] = {}
        self._documents_lock = threading.Lock()

    # -- the `Service` members --------------------------------------------

    def config_env(self) -> dict[str, str]:
        """Nothing, and that is not an omission.

        A feed's URL is a *row* — `feeds.url` in the user's own
        `modules/{user}/feeds.db` — not a config value, so there is no variable
        for `render-config.sh` to read that could point the daemon here. The
        scenario seeds the rows through the shipped `feeds add` CLI inside the
        container, against `container_url`.

        The module switch is not here either, and that is deliberate rather
        than an oversight in the other direction: `ISTOTA_FEEDS_ENABLED` says
        the *module* is on, which is a property of the profile and not of this
        server, and it is what `FULL_MODULE_SWITCHES` already derives from a
        profile's service list on the other shape. The `feeds` profile carries
        it in `Profile.config`, where the two-file rule still checks it.
        """
        return {}

    def reset(self) -> None:
        """Forget every document and every recorded fetch.

        Total rather than selective, because a document is per-scenario by
        construction: each one registers what it wants served immediately after
        the reset, and a leftover from the previous test is a feed the poller
        would fetch again — the poller polls every row that is due, not the row
        this test cares about.
        """
        with self._documents_lock:
            self._documents.clear()
        super().reset()

    # -- what it serves ---------------------------------------------------

    def add(
        self,
        path: str,
        body: bytes | str,
        *,
        content_type: str = "application/xml",
        etag: str = "",
        last_modified: str = "",
        conditional_get: bool = True,
    ) -> str:
        """Register one document and return the URL a container reaches it on.

        Returning the URL rather than making the caller build it is what keeps
        the seeded DB row and the served path from drifting: the row is written
        from this return value.
        """
        if not path.startswith("/"):
            path = "/" + path
        document = Document(
            body=body.encode() if isinstance(body, str) else body,
            content_type=content_type,
            etag=etag,
            last_modified=last_modified,
            conditional_get=conditional_get,
        )
        with self._documents_lock:
            self._documents[path] = document
        return f"{self.container_url}{path}"

    def replace(self, path: str, body: bytes | str, *, etag: str = "") -> None:
        """Change what an already-registered path serves.

        For the second half of a conditional-GET scenario: the same feed, a new
        `ETag`, and a body carrying an entry the first poll did not see.
        """
        if not path.startswith("/"):
            path = "/" + path
        with self._documents_lock:
            document = self._documents.get(path)
            if document is None:
                raise KeyError(f"{path!r} was never registered")
            document.body = body.encode() if isinstance(body, str) else body
            document.etag = etag

    def fetches(self, path: str = "") -> list[ServiceCall]:
        """Recorded GETs, optionally for one exact path, oldest first."""
        gets = self.calls_matching(method="GET")
        if not path:
            return gets
        if not path.startswith("/"):
            path = "/" + path
        return [call for call in gets if call.path == path]


def serve(
    *,
    port: int = 0,
    host: str = LOOPBACK,
    credential: str | None = None,
) -> FeedsService:
    """Start a document server on an ephemeral port.

    `host` defaults to loopback and only the deployment tier overrides it, for
    the reason every stub here does: an ordinary `uv run pytest` has no use for
    a listener on every interface, and on macOS it raises the
    incoming-connections prompt where the run looks hung.
    """
    stub = FeedsService()

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 5

        def log_message(self, *args) -> None:
            """Silence; see `model_endpoint._Handler.log_message`."""

        def handle_error(self, request, client_address) -> None:
            """Silence too; see `model_endpoint._Handler.handle_error`."""

        def _send(self, status: int, body: bytes, content_type: str = "") -> None:
            self.send_response(status)
            if content_type:
                self.send_header("content-type", content_type)
            # Always, including on 304 — where it must be zero. A 304 carries
            # no body by definition, and an HTTP/1.1 keep-alive connection with
            # no length and no body leaves the client waiting for one.
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            stub.record(
                ServiceCall(
                    method="GET",
                    path=path,
                    headers={key: value for key, value in self.headers.items()},
                )
            )
            with stub._documents_lock:
                document = stub._documents.get(path)
                if document is not None:
                    # Copied under the lock: `replace` mutates a live document,
                    # and a handler thread reading it field by field afterwards
                    # could serve one poll's body with the next poll's ETag.
                    document = Document(
                        body=document.body,
                        content_type=document.content_type,
                        etag=document.etag,
                        last_modified=document.last_modified,
                        conditional_get=document.conditional_get,
                    )

            if document is None:
                self._send(404, b"no such document", "text/plain")
                return

            if document.conditional_get and _matches(self.headers, document):
                self._send(304, b"")
                return

            self._send_document(document)

        def _send_document(self, document: Document) -> None:
            self.send_response(200)
            self.send_header("content-type", document.content_type)
            if document.etag:
                self.send_header("ETag", document.etag)
            if document.last_modified:
                self.send_header("Last-Modified", document.last_modified)
            self.send_header("content-length", str(len(document.body)))
            self.end_headers()
            self.wfile.write(document.body)

    stub.start(_Handler, host=host, port=port, credential=credential)
    return stub


def _matches(headers, document: Document) -> bool:
    """Whether this request's validators say the client already has it.

    Either validator is enough, which is what RFC 9110 says and what a real
    server does: `_poll_rss` sends both when it has both, and a server
    demanding both to agree would serve a full body to a client that was
    correctly up to date.
    """
    if document.etag and headers.get(IF_NONE_MATCH) == document.etag:
        return True
    if (
        document.last_modified
        and headers.get(IF_MODIFIED_SINCE) == document.last_modified
    ):
        return True
    return False
