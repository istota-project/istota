"""The shipped browser image: its card fill contract and document render,
exercised by real Chromium, and its split into a base stage and a Chrome layer."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image

from .conftest import (
    REPO, BuiltImage, _require_no_xdist, assert_ok, build_image, require_docker, run_in,
)

pytestmark = pytest.mark.image


@pytest.fixture(scope="session")
def browser_image(pytestconfig):
    _require_no_xdist(pytestconfig)
    dockerfile = REPO / "docker" / "browser" / "Dockerfile"
    tag = os.environ.get("ISTOTA_BROWSER_IMAGE_TAG")
    if tag:
        require_docker()
        return BuiltImage(tag, dockerfile, "linux/amd64")
    # The shipped Dockerfile installs Google's amd64 Chrome package.
    return build_image(dockerfile, dockerfile.parent,
                       platform="linux/amd64", prefix="browser")


@pytest.fixture(scope="session")
def render_bundles():
    """The built render bundles (`web/build-render`), as the istota image ships them.

    `ISTOTA_RENDER_BUNDLE_DIR` names a prebuilt directory instead, which is how
    the negative control hands over a bundle with its renderer unregistered.
    """
    override = os.environ.get("ISTOTA_RENDER_BUNDLE_DIR")
    if override:
        bundles = Path(override)
    else:
        if shutil.which("npm") is None:
            pytest.fail("npm is needed to build the render bundles", pytrace=False)
        result = subprocess.run(["npm", "--prefix", str(REPO / "web"), "run", "build:render"],
                                capture_output=True, text=True, timeout=900)
        assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]
        bundles = REPO / "web" / "build-render"
    assert (bundles / "share-map.html").is_file(), f"no share-map bundle in {bundles}"
    return bundles


def test_document_render_draws_the_share_map_bundle(browser_image, render_bundles, tmp_path):
    """The real bundle sent as a document, the real MapLibre, the real guard: a 640 x 360 map."""
    out = tmp_path / "out"
    out.mkdir()
    out.chmod(0o777)
    script = Path(__file__).with_name("document_render_run.py").read_text()
    result = run_in(browser_image, ["-c", script], entrypoint="python", timeout=600,
                    volumes=[(render_bundles, "/bundle", "ro"), (out, "/out", "rw")])
    report = json.loads(assert_ok(result, "document render").strip().splitlines()[-1])

    assert report["drawn"] == [None, None], report
    assert report["blocked"] == "destination_blocked", report
    with Image.open(out / "map.png") as picture:
        assert picture.size == (640, 360)
        pixels = picture.convert("RGB").getcolors(maxcolors=640 * 360)
    tile = (214, 96, 40)
    tiled = sum(count for count, color in pixels
                if all(abs(a - b) <= 3 for a, b in zip(color, tile)))
    # Tiles cover most of the frame; the pin, the accuracy circle and the
    # footer's text are what make it more than one flat colour.
    assert tiled > 0.3 * 640 * 360, tiled
    assert len(pixels) > 50, len(pixels)


def test_a_pdf_in_chromes_viewer_is_read_as_text(browser_image):
    """#850: real Chrome's PDF viewer is seen, and the child's limits hold on Linux.

    Negative control: `PDF_TEXT_CONTROL=html` serves the PDF route as HTML,
    which turns the `pdf_probe` assertion red.
    """
    script = Path(__file__).with_name("pdf_text_run.py").read_text()
    control = os.environ.get("PDF_TEXT_CONTROL", "")
    result = run_in(browser_image, ["-c", script], entrypoint="python", timeout=300,
                    env={"PDF_TEXT_CONTROL": control} if control else None)
    report = json.loads(assert_ok(result, "pdf text").strip().splitlines()[-1])

    assert report["html_probe"] is None, report
    assert report["pdf_probe"] == "https://docs.example/programme.pdf", report
    assert report["read"] == "PdfText", report
    assert "Programme deadline 30 June" in report["text"], report
    assert report["cookie"] == "sid=tab-cookie", report
    assert "Locked for editing only" in report["locked"], report
    assert report["bomb"]["ok"] is False, report
    assert report["bomb"]["error"] in ("pdf_too_complex", "pdf_unreadable"), report
    assert report["unbombed"]["ok"] is True, report
    assert "Padded but readable" in report["unbombed"]["text"], report


def test_browser_card_checkout(browser_image):
    script = Path(__file__).with_name("browser_checkout.py").read_text()
    result = run_in(browser_image, ["-c", script], entrypoint="python", timeout=180)
    assert_ok(result, "browser test checkout")


def test_browser_secret_capture_capability(browser_image):
    result = run_in(browser_image, ["-c", """
import browse_api
with browse_api.app.test_client() as client:
    response = client.get('/health')
    assert response.status_code == 200
    assert response.get_json()['secret_capture'] is True
    assert response.get_json()['secret_capture_download'] is True
    assert response.get_json()['secret_discovery'] is True
"""], entrypoint="python", timeout=60)
    assert_ok(result, "browser secret capture capability")


# --- the base and Chrome stages ----------------------------------------------
#
# A release ships istota-browser-base, built with `--target base`, because
# Chrome may not be redistributed; the VM's `istota-stack local-layers` builds
# the chrome stage on it with BROWSER_BASE naming that image. Both natively:
# what is asserted is the split, not the architecture.

BROWSER_DOCKERFILE = REPO / "docker" / "browser" / "Dockerfile"


@pytest.fixture(scope="session")
def browser_base(pytestconfig):
    _require_no_xdist(pytestconfig)
    return build_image(BROWSER_DOCKERFILE, BROWSER_DOCKERFILE.parent,
                       prefix="browser-base", target="base")


@pytest.fixture(scope="session")
def browser_chrome_layer(browser_base):
    return build_image(BROWSER_DOCKERFILE, BROWSER_DOCKERFILE.parent,
                       prefix="browser-chrome-layer", target="chrome",
                       build_args={"BROWSER_BASE": browser_base.tag})


def _layers(image: BuiltImage) -> list[str]:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .RootFS.Layers}}", image.tag],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(result.stdout)


def test_the_base_stage_carries_no_chrome(browser_base):
    result = run_in(browser_base, ["-c", """
set -e
if dpkg -s google-chrome-stable >/dev/null 2>&1; then echo "google-chrome-stable is installed"; exit 1; fi
for path in /opt/google /usr/bin/google-chrome /etc/apt/sources.list.d/google-chrome.list; do
    if [ -e "$path" ]; then echo "$path exists"; exit 1; fi
done
if ls /*.deb /tmp/*.deb 2>/dev/null; then echo "a .deb was left behind"; exit 1; fi
dpkg -s libnss3 libgbm1 >/dev/null
"""])
    assert_ok(result, "the browser base stage")


def test_the_chrome_layer_is_one_layer_on_the_base(browser_base, browser_chrome_layer):
    base, layered = _layers(browser_base), _layers(browser_chrome_layer)
    assert layered[:-1] == base
    assert len(layered) == len(base) + 1


def test_chrome_runs_in_the_layer(browser_chrome_layer):
    result = run_in(browser_chrome_layer, ["-c", "google-chrome --version && apt-get check"])
    assert_ok(result, "Chrome in the local layer")
    assert "Google Chrome" in result.stdout


def test_the_browser_service_starts_on_the_layer(browser_chrome_layer):
    """The image's own entrypoint, as compose starts the browser service."""
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "--init", "--shm-size", "1g", browser_chrome_layer.tag],
        capture_output=True, text=True, timeout=60,
    )
    assert started.returncode == 0, started.stderr
    container = started.stdout.strip()
    try:
        deadline = time.monotonic() + 90
        live = None
        while time.monotonic() < deadline:
            live = subprocess.run(
                ["docker", "exec", container, "curl", "-sf", "http://localhost:9224/live"],
                capture_output=True, text=True, timeout=30,
            )
            if live.returncode == 0:
                break
            time.sleep(2)
        assert live is not None and live.returncode == 0, subprocess.run(
            ["docker", "logs", container], capture_output=True, text=True, timeout=30,
        ).stdout[-3000:]
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=60)
