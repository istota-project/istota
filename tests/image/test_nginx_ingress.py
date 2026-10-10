"""The compose nginx's configuration, rendered and checked by nginx itself.

`docker/nginx/istota-ingress.sh` renders the server blocks for the stack's
ingress mode (`INGRESS` in host.env) around the one location template, and the
stock nginx image runs it before nginx starts. Nothing in the tree ran `nginx
-t` against either nginx template before this (the Ansible one says so); a
default-suite test reading the text cannot see a directive nginx refuses, an
include that resolves to nothing, or a variable envsubst ate.

So each case here runs the shipped script in the shipped compose file's nginx
image, against the shipped `docker/nginx/` directory, and then `nginx -t`:
every ingress mode, each with and without a certificate where that changes what
is rendered, and each with the two `root.conf` variants a stack can carry (the
shipped one, and the full test tier's Nextcloud fixture). The refusals are
cases too: a mode the script will not render must stop the container, since
the image's entrypoint stops on a failed configuration script.

Image tier rather than smoke: it needs Docker and the nginx image, not a stack.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from testbed import certs

pytestmark = pytest.mark.image

REPO = Path(__file__).resolve().parents[2]
NGINX_DIR = REPO / "docker" / "nginx"
SCRIPT = NGINX_DIR / "istota-ingress.sh"
NEXTCLOUD_LOCATIONS = REPO / "testbed" / "compose" / "nextcloud-locations.conf"
DOMAIN = "istota.example.test"

ROOT_VARIANTS = {
    "shipped": None,
    "nextcloud-fixture": NEXTCLOUD_LOCATIONS,
}


def _nginx_image() -> str:
    compose = yaml.safe_load((REPO / "docker" / "docker-compose.yml").read_text())
    return compose["services"]["nginx"]["image"]


@pytest.fixture(scope="module")
def nginx_image():
    if shutil.which("docker") is None:
        pytest.skip("no docker CLI")
    image = _nginx_image()
    pulled = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True, timeout=60,
    )
    if pulled.returncode != 0:
        result = subprocess.run(["docker", "pull", image], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            pytest.skip(f"could not pull {image}: {result.stderr[-400:]}")
    return image


@pytest.fixture(scope="module")
def cert_pair(tmp_path_factory) -> tuple[Path, Path]:
    crt, key = certs.generate_self_signed(tmp_path_factory.mktemp("cert"), sans=(DOMAIN,))
    return crt, key


def _cert_dir(tmp_path: Path, cert_pair, layout: str) -> Path:
    """A certificates directory the way the VM lays it out for each source."""
    root = tmp_path / "certs"
    target = root / "live" / DOMAIN if layout == "acme" else root
    target.mkdir(parents=True, exist_ok=True)
    if layout != "none":
        shutil.copy(cert_pair[0], target / "fullchain.pem")
        shutil.copy(cert_pair[1], target / "privkey.pem")
    return root


def _render(nginx_image, tmp_path, *, env: dict[str, str], cert_layout: str,
            cert_pair, root: Path | None) -> subprocess.CompletedProcess:
    argv = [
        "docker", "run", "--rm",
        "-v", f"{NGINX_DIR}:/etc/istota-nginx:ro",
        "-v", f"{SCRIPT}:/docker-entrypoint.d/15-istota-ingress.sh:ro",
        "-v", f"{_cert_dir(tmp_path, cert_pair, cert_layout)}:/etc/nginx/certs:ro",
    ]
    if root is not None:
        argv += ["-v", f"{root}:/etc/istota-testbed/root-locations.conf:ro",
                 "-e", "ISTOTA_NGINX_ROOT_LOCATIONS=/etc/istota-testbed/root-locations.conf"]
    for name, value in env.items():
        argv += ["-e", f"{name}={value}"]
    argv += [
        "--entrypoint", "sh", nginx_image, "-c",
        "/docker-entrypoint.d/15-istota-ingress.sh && nginx -t 2>&1 && "
        "echo '=== conf.d' && cat /etc/nginx/conf.d/default.conf && "
        "echo '=== root' && cat /etc/nginx/istota/root.conf",
    ]
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def _ok(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 0, f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    assert "test is successful" in result.stdout, result.stdout
    return result.stdout


#: (case id, environment, certificate layout, what the rendered conf.d must
#: carry, what it must not).
MODES = [
    ("local", {"INGRESS": "local", "DOMAIN": "localhost:8080"}, "none",
     ["listen 80 default_server;", "set $forwarded_proto $scheme;"], ["ssl", "allow "]),
    ("proxied-plain",
     {"INGRESS": "proxied", "DOMAIN": DOMAIN, "UPSTREAM_PROXY": "10.0.0.5,192.168.10.0/24"},
     "none",
     ["listen 80 default_server;", "allow 10.0.0.5;", "allow 192.168.10.0/24;", "deny all;",
      "set $forwarded_proto $istota_upstream_proto;"],
     ["ssl_certificate", "listen 443"]),
    ("proxied-files",
     {"INGRESS": "proxied", "DOMAIN": DOMAIN, "UPSTREAM_PROXY": "10.0.0.5",
      "TLS_CERT_SOURCE": "files"},
     "files",
     ["listen 443 ssl default_server;", "allow 10.0.0.5;", "deny all;"],
     ["listen 80 "]),
    ("direct-acme-before-the-first-certificate",
     {"INGRESS": "direct", "DOMAIN": DOMAIN, "TLS_CERT_SOURCE": "acme"}, "none",
     ["listen 80 default_server;", "/.well-known/acme-challenge/", "return 301 https://"],
     ["listen 443"]),
    ("direct-acme",
     {"INGRESS": "direct", "DOMAIN": DOMAIN, "TLS_CERT_SOURCE": "acme"}, "acme",
     ["listen 80 default_server;", "listen 443 ssl default_server;", "http2 on;",
      'Strict-Transport-Security "max-age=31536000"', "set $forwarded_proto https;"],
     ["includeSubDomains", "preload"]),
    ("direct-files",
     {"INGRESS": "direct", "DOMAIN": DOMAIN, "TLS_CERT_SOURCE": "files"}, "files",
     ["listen 443 ssl default_server;", "/.well-known/acme-challenge/"],
     []),
]


@pytest.mark.parametrize("root_variant", sorted(ROOT_VARIANTS))
@pytest.mark.parametrize(
    "case, env, cert_layout, present, absent", MODES, ids=[mode[0] for mode in MODES],
)
def test_every_mode_renders_a_configuration_nginx_accepts(
    nginx_image, cert_pair, tmp_path, case, env, cert_layout, present, absent, root_variant,
):
    output = _ok(_render(
        nginx_image, tmp_path, env=env, cert_layout=cert_layout, cert_pair=cert_pair,
        root=ROOT_VARIANTS[root_variant],
    ))
    conf = output.split("=== conf.d", 1)[1].split("=== root", 1)[0]
    # The directives, not the comments that explain them.
    conf = "\n".join(line for line in conf.splitlines() if not line.strip().startswith("#"))

    for text in present:
        assert text in conf, f"{case}: {text!r} missing from\n{conf}"
    for text in absent:
        assert text not in conf, f"{case}: {text!r} present in\n{conf}"


def test_the_tls_floor_is_tls_1_2(nginx_image, cert_pair, tmp_path):
    result = subprocess.run(
        ["docker", "run", "--rm",
         "-v", f"{NGINX_DIR}:/etc/istota-nginx:ro",
         "-v", f"{SCRIPT}:/docker-entrypoint.d/15-istota-ingress.sh:ro",
         "-v", f"{_cert_dir(tmp_path, cert_pair, 'files')}:/etc/nginx/certs:ro",
         "-e", "INGRESS=direct", "-e", f"DOMAIN={DOMAIN}", "-e", "TLS_CERT_SOURCE=files",
         "--entrypoint", "sh", nginx_image, "-c",
         "/docker-entrypoint.d/15-istota-ingress.sh >/dev/null && cat /etc/nginx/istota/tls.conf"],
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in result.stdout
    assert "ssl_certificate /etc/nginx/certs/fullchain.pem;" in result.stdout


def test_the_fixture_root_serves_nextcloud_and_the_shipped_one_does_not(
    nginx_image, cert_pair, tmp_path,
):
    env = {"INGRESS": "local", "DOMAIN": "localhost"}
    shipped = _ok(_render(nginx_image, tmp_path, env=env, cert_layout="none",
                          cert_pair=cert_pair, root=None))
    fixture = _ok(_render(nginx_image, tmp_path, env=env, cert_layout="none",
                          cert_pair=cert_pair, root=NEXTCLOUD_LOCATIONS))

    assert "nextcloud" not in shipped.split("=== root", 1)[1]
    assert "return 404;" in shipped.split("=== root", 1)[1]
    assert "nextcloud:80" in fixture.split("=== root", 1)[1]


@pytest.mark.parametrize(
    "env, cert_layout, says",
    [
        ({"INGRESS": "proxied", "DOMAIN": DOMAIN}, "none", "needs UPSTREAM_PROXY"),
        ({"INGRESS": "proxied", "DOMAIN": DOMAIN, "UPSTREAM_PROXY": "0.0.0.0/0"}, "none",
         "allows every address"),
        ({"INGRESS": "proxied", "DOMAIN": DOMAIN, "UPSTREAM_PROXY": "10.0.0.5; allow all"},
         "none", "is not an address"),
        ({"INGRESS": "proxied", "DOMAIN": DOMAIN, "UPSTREAM_PROXY": "10.0.0.5",
          "TLS_CERT_SOURCE": "acme"}, "acme", "files or none"),
        ({"INGRESS": "proxied", "DOMAIN": DOMAIN, "UPSTREAM_PROXY": "10.0.0.5",
          "TLS_CERT_SOURCE": "files"}, "none", "no fullchain.pem"),
        ({"INGRESS": "direct", "DOMAIN": DOMAIN}, "none", "needs TLS_CERT_SOURCE"),
        ({"INGRESS": "local", "DOMAIN": "bad;name"}, "none", "is not a hostname"),
        ({"INGRESS": "sideways"}, "none", "is not one of"),
    ],
    ids=["proxied-no-upstream", "proxied-everyone", "proxied-injection", "proxied-acme",
         "proxied-files-missing", "direct-no-source", "bad-domain", "unknown-mode"],
)
def test_a_mode_it_will_not_render_stops_the_container(
    nginx_image, cert_pair, tmp_path, env, cert_layout, says,
):
    result = _render(nginx_image, tmp_path, env=env, cert_layout=cert_layout,
                     cert_pair=cert_pair, root=None)

    assert result.returncode != 0, result.stdout
    assert says in result.stderr, result.stderr
    assert "test is successful" not in result.stdout
