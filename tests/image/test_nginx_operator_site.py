"""The operator's static site (#804), as the shipped nginx serves it.

With host.env's SITE_DIR set, `istota-stack` passes it to compose, which
mounts it read-only at `/srv/site` on nginx and sets `ISTOTA_SITE=1`, and the
ingress script renders `root-locations-site.conf` in place of the redirect.
This runs the shipped image with that mount and that variable and asks it over
HTTP: the site answers at `/`, a missing file and a hidden one are 404, and
`/istota/` and `/webhooks/` still reach `web`. Unset, the rendered `root.conf`
must be the shipped `root-locations.conf` byte for byte, so an existing install
sees no change.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid

import pytest

from tests.image.test_nginx_ingress import NGINX_DIR, SCRIPT, _nginx_image
from tests.image.test_nginx_starting_page import _docker, _get, _start_nginx, _start_web

pytestmark = pytest.mark.image


@pytest.fixture(scope="module")
def network():
    if shutil.which("docker") is None:
        pytest.skip("no docker CLI")
    name = f"istota-site-{uuid.uuid4().hex[:8]}"
    _docker("network", "create", name)
    yield name
    _docker("network", "rm", name, check=False)


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    (root / "css").mkdir(parents=True)
    (root / "index.html").write_text("<h1>operator home</h1>\n")
    (root / "robots.txt").write_text("User-agent: *\n")
    (root / "css" / "site.css").write_text("body { color: black; }\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    root.chmod(0o755)
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    return root


@pytest.fixture
def nginx_with_a_site(network, site):
    web = _start_web(network, 200, "from-web")
    name = _start_nginx(network, "-v", f"{site}:/srv/site:ro", "-e", "ISTOTA_SITE=1")
    yield name
    _docker("rm", "-f", name, web, check=False)


def test_the_site_answers_at_the_root(nginx_with_a_site):
    status, _, body = _get(nginx_with_a_site, "/")
    assert status == 200
    assert "operator home" in body

    status, _, body = _get(nginx_with_a_site, "/index.html")
    assert status == 200
    assert "operator home" in body


def test_a_static_file_is_served(nginx_with_a_site):
    status, headers, body = _get(nginx_with_a_site, "/css/site.css")
    assert status == 200
    assert headers.get("content-type", "").startswith("text/css")
    assert "color: black" in body

    status, _, _ = _get(nginx_with_a_site, "/robots.txt")
    assert status == 200


@pytest.mark.parametrize("path", ["/missing.html", "/.git/config", "/.git/", "/css/"])
def test_a_missing_or_hidden_file_is_404(nginx_with_a_site, path):
    status, _, body = _get(nginx_with_a_site, path)
    assert status == 404
    assert "[core]" not in body


def test_a_directory_redirect_keeps_the_clients_port(nginx_with_a_site):
    status, headers, _ = _get(nginx_with_a_site, "/css")
    assert status == 301
    assert headers.get("location") == "/css/"


def test_the_site_may_be_indexed_and_the_app_may_not(nginx_with_a_site):
    _, headers, _ = _get(nginx_with_a_site, "/")
    assert "x-robots-tag" not in headers

    _, headers, _ = _get(nginx_with_a_site, "/istota/chat")
    assert headers.get("x-robots-tag") == "noindex, nofollow"


def test_a_site_with_no_index_is_404_at_the_root(network, site):
    (site / "index.html").unlink()
    web = _start_web(network, 200, "from-web")
    name = _start_nginx(network, "-v", f"{site}:/srv/site:ro", "-e", "ISTOTA_SITE=1")
    try:
        status, _, _ = _get(name, "/")
        assert status == 404
    finally:
        _docker("rm", "-f", name, web, check=False)


@pytest.mark.parametrize("path", ["/istota/chat", "/webhooks/overland"])
def test_the_apps_locations_still_reach_web(nginx_with_a_site, path):
    status, _, body = _get(nginx_with_a_site, path)
    assert status == 200
    assert "from-web" in body


def _rendered_root(*extra: str) -> str:
    result = subprocess.run(
        ["docker", "run", "--rm",
         "-v", f"{NGINX_DIR}:/etc/istota-nginx:ro",
         "-v", f"{SCRIPT}:/docker-entrypoint.d/15-istota-ingress.sh:ro",
         "-e", "INGRESS=local", "-e", "DOMAIN=localhost", *extra,
         "--entrypoint", "sh", _nginx_image(), "-c",
         "/docker-entrypoint.d/15-istota-ingress.sh >/dev/null && nginx -t 2>/dev/null "
         "&& cat /etc/nginx/istota/root.conf"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_unset_renders_the_shipped_root_unchanged(network):
    assert _rendered_root() == (NGINX_DIR / "root-locations.conf").read_text()
    assert _rendered_root("-e", "ISTOTA_SITE=") == (NGINX_DIR / "root-locations.conf").read_text()


def test_set_renders_the_site_locations(network, site):
    rendered = _rendered_root("-v", f"{site}:/srv/site:ro", "-e", "ISTOTA_SITE=1")
    assert rendered == (NGINX_DIR / "root-locations-site.conf").read_text()
