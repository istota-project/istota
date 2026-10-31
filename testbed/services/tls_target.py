"""An allowlisted host that speaks TLS, for the network witness to reach.

The CONNECT proxy carries only TLS whose ClientHello names the CONNECT host, and
every other stub here speaks plain HTTP, so none of them can answer through a
tunnel. This one can: a real `curl` over a real handshake, ending in a status the
stub recorded, which is the only evidence that bytes crossed the tunnel. A closed
connection looks the same whether the proxy refused the hello or passed it on.

The configured forge stays plain HTTP because `https` would make it a bound
credential host, and then the broker would intercept the tunnel rather than
pass it through.
"""

from __future__ import annotations

import ssl
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from ..certs import generate_self_signed
from ..httpstub import FROM_CONTAINER, LOOPBACK, HttpStub
from . import ServiceCall

#: Named for `HttpStub.start`'s rule. Nothing is checked against it, since the
#: stub serves nothing but a status.
TLS_TARGET_CREDENTIAL = "unused-by-the-tls-stub"


class TlsTargetService(HttpStub):
    """A running TLS stub and the record of what reached it."""

    name = "tls"

    @property
    def url(self) -> str:
        return f"https://{LOOPBACK}:{self.port}"

    @property
    def container_url(self) -> str:
        return f"https://{FROM_CONTAINER}:{self.port}"

    def config(self) -> dict:
        """Allowlist this stub, which also lets the name reach a private address."""
        return {"security": {"network": {"extra_hosts": [f"{FROM_CONTAINER}:{self.port}"]}}}


def serve(
    cert_dir: Path,
    *,
    port: int = 0,
    host: str = LOOPBACK,
    credential: str | None = None,
) -> TlsTargetService:
    """Start the stub on an ephemeral port, with a certificate in `cert_dir`."""
    stub = TlsTargetService()
    crt, key = generate_self_signed(cert_dir, sans=(FROM_CONTAINER, LOOPBACK))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(crt, key)

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 5

        def log_message(self, *args) -> None:
            """Silence; the default writes a line per request to stderr."""

        def do_GET(self) -> None:
            stub.record(ServiceCall(method="GET", path=self.path.split("?", 1)[0]))
            self.send_response(204)
            self.end_headers()

    stub.start(_Handler, host=host, port=port, credential=credential, tls=context)
    return stub
