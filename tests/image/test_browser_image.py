"""The shipped browser's card fill contract and share-map render, exercised by real Chromium."""

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
def snapshot_site():
    """The built web bundle, whose `/map-snapshot/` page the render loads.

    `ISTOTA_MAP_SNAPSHOT_SITE` names a prebuilt one instead, which is how the
    negative control hands over a bundle with MapLibre's chunk removed.
    """
    override = os.environ.get("ISTOTA_MAP_SNAPSHOT_SITE")
    if override:
        site = Path(override)
    else:
        if shutil.which("npm") is None:
            pytest.fail("npm is needed to build the web bundle the map render loads",
                        pytrace=False)
        result = subprocess.run(["npm", "--prefix", str(REPO / "web"), "run", "build"],
                                capture_output=True, text=True, timeout=900)
        assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]
        site = REPO / "web" / "build"
    assert (site / "map-snapshot" / "index.html").is_file(), f"no map-snapshot page in {site}"
    return site


def test_map_snapshot_renders_the_built_page(browser_image, snapshot_site, tmp_path):
    """The real page, the real MapLibre, the real guard: a 640 x 360 map with tiles on it."""
    out = tmp_path / "out"
    out.mkdir()
    out.chmod(0o777)
    script = Path(__file__).with_name("map_snapshot_render.py").read_text()
    result = run_in(browser_image, ["-c", script], entrypoint="python", timeout=600,
                    volumes=[(snapshot_site, "/site", "ro"), (out, "/out", "rw")])
    report = json.loads(assert_ok(result, "map snapshot render").strip().splitlines()[-1])

    assert report["drawn"] is None, report
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
