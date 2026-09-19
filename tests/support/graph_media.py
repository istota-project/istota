"""A stubbed Meta Graph media API, shared by the two suites that drive it.

`tests/test_whatsapp_cloud_media.py` owns the fetch itself and
`tests/test_whatsapp_media_precheck.py` owns the lock-ordering property on both
adapters, so the stub lives here rather than in either — a second copy would be
a second answer to "what does Meta return", which is the one thing these tests
rest on.

The recorded requests are the instrument. Two of the properties this surface
claims are absences — a stranger's media is never fetched, and a file Meta
itself says is too large is refused before a byte moves — and the only honest
way to assert an absence is to ask the transport what it was asked for.
"""

from __future__ import annotations

import httpx

MEDIA_ID = "1122334455667788"
MEDIA_URL = "https://media.example.invalid/asset/1122334455667788"

#: The smallest thing `image_sniff.sniff_decodable` calls a PNG, padded so a
#: staged file has a plausible size.
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64


class Graph:
    """Meta's two calls — the media-url lookup and the CDN download."""

    def __init__(
        self,
        *,
        body: bytes = PNG,
        declared_size: int | None = None,
        mime: str = "image/jpeg",
        lookup_status: int = 200,
        url: str = MEDIA_URL,
        download_status: int = 200,
    ):
        self.body = body
        self.declared_size = len(body) if declared_size is None else declared_size
        self.mime = mime
        self.lookup_status = lookup_status
        self.url = url
        self.download_status = download_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url).startswith("https://media."):
            if self.download_status != 200:
                return httpx.Response(self.download_status, content=b"nope")
            return httpx.Response(200, content=self.body)
        if self.lookup_status != 200:
            return httpx.Response(
                self.lookup_status,
                json={"error": {"message": "media not found", "code": 100}},
            )
        return httpx.Response(200, json={
            "id": MEDIA_ID,
            "url": self.url,
            "mime_type": self.mime,
            "sha256": "0" * 64,
            "file_size": self.declared_size,
            "messaging_product": "whatsapp",
        })

    @property
    def paths(self) -> list[str]:
        return [str(request.url) for request in self.requests]

    @property
    def downloads(self) -> list[str]:
        return [path for path in self.paths if path.startswith("https://media.")]


def build_client(config, graph: Graph):
    """A real `WhatsAppClient` over a transport this stub answers."""
    from istota.transport.whatsapp.client import WhatsAppClient

    return WhatsAppClient(
        config, session=httpx.AsyncClient(transport=httpx.MockTransport(graph)),
    )


def install_client(monkeypatch, graph: Graph) -> None:
    """Replace the seam `stage_cloud_media` builds its client through.

    The staging step imports `make_client` at call time, so patching the
    attribute on `client` is what production's own lookup then finds.
    """
    from istota.transport.whatsapp import client as client_module

    monkeypatch.setattr(
        client_module, "make_client", lambda cfg: build_client(cfg, graph),
    )
