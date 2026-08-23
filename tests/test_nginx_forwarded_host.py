"""What authority a redirect out of the web UI ends up carrying.

`StaticFiles(html=True)` answers a directory URL with a 307 that adds the
trailing slash, and Starlette builds that `Location` as
``{scope['scheme']}://{Host header}{path}/`` — an absolute URL whose authority
is entirely whatever the reverse proxy chose to forward. Both halves of that
authority were wrong.

**The port.** All four nginx sources forwarded ``Host: $host``. `$host` is the
client's host *normalized*, and the normalization strips the port, so a stack
published on anything but the scheme's default port handed the upstream a bare
hostname. On the Docker stack at ``:8282``, a direct navigation to
``/istota/chat`` answered ``http://localhost/istota/chat/`` and landed on
nothing. Clicking through the app never reaches this path — SvelteKit routes
client-side — so it only ever broke deep links, bookmarks and a reload, which is
how it survived. `$http_host` is the header verbatim.

**The scheme.** No deployment starts uvicorn with ``--proxy-headers`` and
nothing in the app reads ``X-Forwarded-Proto``, so ``scope['scheme']`` is always
``http``. A TLS deployment therefore answered a directory URL with a plaintext
one and recovered only because the port-80 server block redirects back.

The two fixes are independent on purpose. The nginx one is correct proxying and
fixes every URL an upstream derives from the Host header, not just this
redirect. The app one drops the authority from the `Location` altogether, and is
the half that holds when a proxy nobody in this repo wrote is in front.

What this file cannot see: whether a deployed nginx actually runs one of these
templates. It asserts on the sources the repo ships.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Every place the repo writes a `proxy_set_header Host`. Two are configs that
#: get deployed; two are documentation an operator copies into their own nginx,
#: which is a source of the same defect in a deployment this repo never sees.
HOST_HEADER_SOURCES = {
    "docker": REPO / "docker" / "nginx" / "default.conf.template",
    "ansible": REPO / "deploy" / "ansible" / "templates" / "istota.conf.j2",
    "docs-web": REPO / "docs" / "features" / "web-interface.md",
    "docs-location": REPO / "docs" / "features" / "location.md",
}

HOST_HEADER = re.compile(r"proxy_set_header\s+Host\s+(\S+?)\s*;")


class TestTheProxiesForwardThePort:
    def test_every_source_is_present(self):
        """A renamed or deleted file must fail here rather than sweep zero
        matches and report a clean pass."""
        for name, path in HOST_HEADER_SOURCES.items():
            assert path.is_file(), f"{name}: {path} is gone"

    @pytest.mark.parametrize("name", sorted(HOST_HEADER_SOURCES))
    def test_the_host_header_is_forwarded_verbatim(self, name):
        """`$host` drops the port; `$http_host` is the header as sent."""
        text = HOST_HEADER_SOURCES[name].read_text()
        found = HOST_HEADER.findall(text)
        assert found, f"{name}: no `proxy_set_header Host` at all"
        assert set(found) == {"$http_host"}, f"{name}: forwards {sorted(set(found))}"

    def test_the_scheme_redirect_still_normalizes_the_port_away(self):
        """The Ansible template's port-80 block is the one place `$host` is
        right: it redirects *to* the scheme default, where carrying a port over
        would produce `https://host:80/...`. A sweep of this file must not
        take it with the rest."""
        text = HOST_HEADER_SOURCES["ansible"].read_text()
        assert "return 301 https://$host$request_uri;" in text


try:
    import authlib  # noqa: F401 - probe: `istota.web_app` needs it to import
    import fastapi  # noqa: F401 - probe
    _has_web_deps = True
except ImportError:  # pragma: no cover - the lean install, which has no web
    _has_web_deps = False

#: Only the two classes below need the app. The config sweep above reads files
#: and must still run on an install with no web extra.
_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed"
)


@_needs_web_deps
class TestTheDirectoryRedirectCarriesNoAuthority:
    """The app half, which does not depend on the proxy being configured well."""

    @pytest.fixture
    def client(self, tmp_path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from istota.web_app import _CacheHeaderStatics

        root = tmp_path / "build"
        (root / "chat").mkdir(parents=True)
        (root / "chat" / "index.html").write_text("<html>chat</html>")
        (root / "index.html").write_text("<html>shell</html>")

        app = FastAPI()
        app.mount("/istota", _CacheHeaderStatics(directory=str(root), html=True))
        return TestClient(app)

    @pytest.mark.parametrize("host", ["localhost:8282", "localhost", "istota.example"])
    def test_the_location_is_a_bare_path(self, client, host):
        """Whatever the proxy forwarded as Host, the client resolves the
        redirect against the URL it actually asked for — so a proxy that
        dropped the port, or one that never saw the real scheme, cannot send
        the browser somewhere that does not exist."""
        response = client.get(
            "/istota/chat", headers={"host": host}, follow_redirects=False
        )

        assert response.status_code == 307
        assert response.headers["location"] == "/istota/chat/"

    def test_the_redirect_still_reaches_the_page(self, client):
        """A relative `Location` is only correct if clients follow it."""
        response = client.get("/istota/chat", headers={"host": "localhost:8282"})

        assert response.status_code == 200
        assert "chat" in response.text

    def test_a_file_response_is_untouched(self, client):
        """`get_response` wraps every static response, not just redirects."""
        response = client.get("/istota/", headers={"host": "localhost:8282"})

        assert response.status_code == 200
        assert "shell" in response.text
        assert response.headers["cache-control"] == "no-cache"


@_needs_web_deps
class TestRelativeLocation:
    """The rewrite itself, including the two cases it declines."""

    def test_an_absolute_url_loses_its_authority(self):
        from istota.web_app import _relative_location

        assert _relative_location("http://h:8282/istota/chat/") == "/istota/chat/"

    def test_query_and_fragment_survive(self):
        from istota.web_app import _relative_location

        assert _relative_location("https://h/p?a=1#f") == "/p?a=1#f"

    def test_an_already_relative_location_is_declined(self):
        from istota.web_app import _relative_location

        assert _relative_location("/istota/chat/") is None

    def test_a_protocol_relative_path_is_declined(self):
        """`//evil.example/` is a *reference to another host*, not a path. Its
        first segment would be read as an authority, so a request under a
        doubled slash must keep the absolute form it already had."""
        from istota.web_app import _relative_location

        assert _relative_location("http://h//evil.example/x/") is None
