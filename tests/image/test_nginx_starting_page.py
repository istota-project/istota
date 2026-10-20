"""The starting-up page, as the shipped nginx serves it.

`tests/test_nginx_starting_page.py` reads the template's text; this runs the
shipped nginx image against the shipped `docker/nginx/` directory on a Docker
network of its own, so `resolver 127.0.0.11` answers as it does in the stack.
With no `web` on the network nginx's own 502 must become the page, and only
under `/istota/`. With a `web` that answers 503 itself, that answer must pass
through untouched, which is what keeps the page's poll from waiting forever on
an error the app means.
"""

from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from tests.image.test_nginx_ingress import NGINX_DIR, SCRIPT, _nginx_image

pytestmark = pytest.mark.image

WEB_PORT = "8766"


def _docker(*args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check:
        assert result.returncode == 0, f"docker {' '.join(args)}\n{result.stdout}\n{result.stderr}"
    return result


@pytest.fixture(scope="module")
def network():
    import shutil

    if shutil.which("docker") is None:
        pytest.skip("no docker CLI")
    name = f"istota-starting-{uuid.uuid4().hex[:8]}"
    _docker("network", "create", name)
    yield name
    _docker("network", "rm", name, check=False)


def _start_nginx(network: str, *extra: str) -> str:
    """The shipped nginx in local ingress; `extra` is more `docker run` options."""
    name = f"istota-starting-nginx-{uuid.uuid4().hex[:8]}"
    _docker(
        "run", "-d", "--name", name, "--network", network,
        "-v", f"{NGINX_DIR}:/etc/istota-nginx:ro",
        "-v", f"{SCRIPT}:/docker-entrypoint.d/15-istota-ingress.sh:ro",
        "-e", "INGRESS=local", "-e", "DOMAIN=localhost", "-e", f"WEB_PORT={WEB_PORT}",
        *extra,
        _nginx_image(),
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        probe = _docker("exec", name, "curl", "-s", "-o", "/dev/null", "http://localhost/",
                        check=False)
        if probe.returncode == 0:
            return name
        time.sleep(0.5)
    logs = _docker("logs", name, check=False)
    _docker("rm", "-f", name, check=False)
    pytest.fail(f"nginx did not start\n{logs.stdout}\n{logs.stderr}")


def _get(container: str, path: str) -> tuple[int, dict[str, str], str]:
    out = _docker("exec", container, "curl", "-s", "-i", f"http://localhost{path}").stdout
    head, _, body = out.replace("\r\n", "\n").partition("\n\n")
    lines = head.splitlines()
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return status, headers, body


@pytest.fixture
def nginx_without_web(network):
    name = _start_nginx(network)
    yield name
    _docker("rm", "-f", name, check=False)


def _start_web(network: str, status: int, body: str) -> str:
    """A stand-in `web` on the network, answering every request with `status`."""
    web = f"istota-starting-web-{uuid.uuid4().hex[:8]}"
    conf = f"server {{ listen {WEB_PORT}; location / {{ return {status} {body}; }} }}"
    _docker(
        "run", "-d", "--name", web, "--network", network, "--network-alias", "web",
        "--entrypoint", "sh", _nginx_image(), "-c",
        f"printf '%s' '{conf}' > /etc/nginx/conf.d/default.conf && exec nginx -g 'daemon off;'",
    )
    return web


@pytest.fixture
def nginx_with_a_failing_web(network):
    web = _start_web(network, 503, "app-own-503")
    name = _start_nginx(network)
    yield name
    _docker("rm", "-f", name, web, check=False)


def test_the_web_ui_gets_the_page_while_web_is_down(nginx_without_web):
    status, headers, body = _get(nginx_without_web, "/istota/chat")

    assert status == 503
    assert headers.get("x-istota-upstream") == "unavailable"
    assert headers.get("retry-after") == "5"
    assert headers.get("cache-control") == "no-store"
    assert headers.get("x-robots-tag") == "noindex, nofollow"
    assert headers.get("content-type", "").startswith("text/html")
    assert "<h1>Starting up</h1>" in body


def test_the_webhooks_keep_nginxs_plain_error(nginx_without_web):
    status, headers, body = _get(nginx_without_web, "/webhooks/overland")

    assert status == 502
    assert "x-istota-upstream" not in headers
    assert "Starting up" not in body


def test_the_page_is_not_reachable_directly(nginx_without_web):
    status, _, body = _get(nginx_without_web, "/_istota_starting")

    assert status == 404
    assert "Starting up" not in body


def test_an_error_the_app_sends_passes_through(nginx_with_a_failing_web):
    status, headers, body = _get(nginx_with_a_failing_web, "/istota/chat")

    assert status == 503
    assert "x-istota-upstream" not in headers
    assert "app-own-503" in body
