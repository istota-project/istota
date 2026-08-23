"""An ntfy server that records the bytes it was sent.

What this witnesses is not "a notification was delivered" — a unit test can
assert that against a patched `httpx.post`, and several do. It is that a push
*leaves the container* with correctly encoded headers. `ntfy_headers.py` exists
because RFC 2047 encoding of a header value is easy to get wrong (ISSUE-213: a
non-ASCII title raised inside httpx and took the whole notification with it),
and every existing test asserts on that function's return value rather than on
what arrived over a socket.

A stub rather than a real ntfy container, and the spec's rule is what decides:
do not stub a service whose client negotiates with it. ntfy's client does not —
it POSTs a body with headers and reads a status — so the real protocol *is* one
POST, and a recording stub sees the bytes better than a real server would.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler

from ..httpstub import LOOPBACK, HttpStub
from . import ServiceCall

#: The topic the fixture seeds and the daemon publishes to.
#:
#: Any string works; ntfy topics are namespaces rather than registered objects.
#: A fixed one keeps the assertion able to name the path it expected.
NTFY_TOPIC = "istota-testbed"

#: The bearer token the fixture seeds into the secrets store and this stub
#: expects. On the service rather than in a fixture, for the reason
#: `FORGE_TOKEN` is on the forge: a service's own credential belongs to it, and
#: the secret-isolation scenario reads every stub's credential off the service
#: rather than keeping a second list that can drift.
NTFY_TOKEN = "ntfy-testbed-token-not-a-real-credential"


class NtfyService(HttpStub):
    """A running ntfy stub and the record of what was pushed to it."""

    name = "ntfy"

    def __init__(self, topic: str = NTFY_TOPIC) -> None:
        super().__init__()
        self.topic = topic

    @property
    def topic_url(self) -> str:
        """What goes into the user's `server_url` secret.

        The *server*, not the topic: `_post_ntfy_blocking` builds
        `{server_url}/{topic}` itself, so a `server_url` carrying the topic
        would have the daemon POST to `/{topic}/{topic}` and the assertion
        would fail on a path nobody wrote.
        """
        return self.container_url

    def config_env(self) -> dict[str, str]:
        """Nothing, and that is not an omission.

        ntfy is a *per-user connected service* held in the encrypted `secrets`
        table, not a config block — the global `[ntfy]` block was retired, and
        `ntfy_settings` resolves `server_url`, `topic` and the credentials per
        user out of the store. So there is no `ISTOTA_NTFY_*` variable for
        `render-config.sh` to read and none for this to return; the fixture
        points the daemon here with `istota secret ensure` inside the container,
        against `container_url`.

        Said out loud because an empty `config_env` invites the reader to think
        it was forgotten, and the two-file rule this method usually satisfies
        (`render-config.sh` reads it *and* `docker-compose.yml` passes it
        through) has nothing to check here.
        """
        return {}

    def pushes(self, topic: str = "") -> list[ServiceCall]:
        """Recorded POSTs to one topic, newest last.

        A named accessor rather than `calls_matching`, because the path is
        `/{topic}` and a caller matching on a substring would also match a
        topic that merely contains this one.
        """
        wanted = f"/{topic or self.topic}"
        return [
            call for call in self.calls_matching(method="POST") if call.path == wanted
        ]

    def header(self, name: str, *, topic: str = "") -> str:
        """One header off the most recent push, as it arrived on the wire.

        Case-insensitive, because a client picks the capitalization and an
        assertion should not have to know which. Returns `""` when the header
        was absent, so a scenario asserting a value gets a readable comparison
        rather than a `KeyError` from inside a helper.
        """
        pushes = self.pushes(topic)
        if not pushes:
            return ""
        wanted = name.lower()
        for key, value in pushes[-1].headers.items():
            if key.lower() == wanted:
                return value
        return ""


def serve(
    *,
    port: int = 0,
    host: str = LOOPBACK,
    credential: str | None = None,
    topic: str = NTFY_TOPIC,
) -> NtfyService:
    """Start an ntfy stub on an ephemeral port.

    `host` defaults to loopback and only the deployment tier overrides it, for
    the reason every stub here does: binding all interfaces on an ordinary
    `uv run pytest` publishes a listener the default suite has no use for, and
    raises the macOS incoming-connections prompt where the run looks hung.

    The token is *checked*, unlike the scripted endpoint's. There is a real
    credential in play — the fixture seeds it into the secrets store and the
    daemon sends it as `Authorization: Bearer …` — so a stub that accepted
    anything would let a scenario pass on a deployment that had lost the header
    entirely, and "the push arrived" would stop meaning "the push arrived
    authenticated".
    """
    stub = NtfyService(topic=topic)

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 5

        def log_message(self, *args) -> None:
            """Silence; the default writes a line per request to stderr, and
            pytest attaches all of it to whichever test is running."""

        def handle_error(self, request, client_address) -> None:
            """Silence too. An unhandled exception in a handler thread
            otherwise prints a traceback into an unrelated test's output."""

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            try:
                body = self.rfile.read(length)
            except OSError:
                return

            # Recorded *before* the credential check, so a push that arrived
            # with the wrong token is visible to a scenario rather than being
            # a 401 with no record of what was sent.
            #
            # `self.headers` is parsed by `email.parser`, which keeps the name
            # as it arrived and decodes the value as latin-1. RFC 2047 encoding
            # produces pure ASCII by construction, so an encoded `Title` comes
            # back byte-identical to what was written — which is the whole
            # point of recording it here rather than asserting on the function
            # that produced it.
            stub.record(
                ServiceCall(
                    method="POST",
                    path=self.path.split("?", 1)[0],
                    auth=_auth_shape(self.headers.get("Authorization", "")),
                    body=body,
                    headers={key: value for key, value in self.headers.items()},
                )
            )

            expected = stub.credential
            if expected and self.headers.get("Authorization") != f"Bearer {expected}":
                self._reply(401, {"code": 40101, "error": "unauthorized"})
                return

            self._reply(200, {"id": "scripted", "topic": self.path.lstrip("/")})

    stub.start(_Handler, host=host, port=port, credential=credential)
    return stub


def _auth_shape(header: str) -> str:
    """Scheme and length, never the value.

    `ServiceCall.auth` is a shape string on every service, for the reason the
    protocol's docstring gives: a fixture that stores real-looking tokens in a
    list that gets printed into failure output is a liability on a public repo.
    Here it also keeps the credential out of `describe()`, which
    `Stack.diagnostics` prints on any failure in the profile.
    """
    if not header:
        return ""
    scheme, _, value = header.partition(" ")
    return f"{scheme} len={len(value)}"
