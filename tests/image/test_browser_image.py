"""The shipped browser's card fill contract and document render, exercised by real Chromium."""

import json
import os
import shutil
import subprocess
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
