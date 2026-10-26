"""`istota.apps.kit`: the house kit read off the apps build, checked against its manifest.

The build directory is a real one under `tmp_path`, named by `ISTOTA_WEB_APPS_DIR`
as the image sets it, laid out the way `web/vite.apps.config.ts` writes it.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from istota.apps import kit
from istota.apps.kit import KIT_FILES, Kit, KitMissing, load_kit

FILES = {
    "kit/guard.js": "(() => { delete window.RTCPeerConnection; })();\n",
    "kit/istota-app.js": "(() => { globalThis.istota = {}; })();\n",
    "kit/components.js": "(() => { customElements.define('istota-x', class extends HTMLElement {}); })();\n",
    "kit/istota.css": ":root { --space-1: 0.25rem; }\n",
}


def _write_build(directory, files=FILES, manifest=None):
    for relative, text in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if manifest is None:
        manifest = {
            relative: hashlib.sha256(text.encode()).hexdigest() for relative, text in files.items()
        }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    directory = tmp_path / "build-apps"
    directory.mkdir()
    monkeypatch.setenv("ISTOTA_WEB_APPS_DIR", str(directory))
    kit.reset_cache()
    yield directory
    kit.reset_cache()


def test_the_four_files_are_the_manifest_keys():
    assert set(KIT_FILES) == set(FILES)


def test_loads_the_four_files(apps_dir):
    _write_build(apps_dir)
    loaded = load_kit()
    assert loaded == Kit(
        guard_js=FILES["kit/guard.js"],
        client_js=FILES["kit/istota-app.js"],
        components_js=FILES["kit/components.js"],
        css=FILES["kit/istota.css"],
    )


def test_cached_for_the_process(apps_dir):
    _write_build(apps_dir)
    first = load_kit()
    (apps_dir / "kit" / "guard.js").unlink()
    assert load_kit() is first


def test_a_missing_directory_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_WEB_APPS_DIR", str(tmp_path / "nowhere"))
    kit.reset_cache()
    with pytest.raises(KitMissing) as raised:
        load_kit()
    assert str(raised.value) == "the apps kit is not built"
    assert raised.value.reason == "no_manifest"


def test_a_missing_file_raises(apps_dir):
    _write_build(apps_dir)
    (apps_dir / "kit" / "components.js").unlink()
    with pytest.raises(KitMissing) as raised:
        load_kit()
    assert raised.value.reason == "no_file"


def test_a_file_the_manifest_does_not_name_raises(apps_dir):
    manifest = {
        relative: hashlib.sha256(text.encode()).hexdigest()
        for relative, text in FILES.items()
        if relative != "kit/istota.css"
    }
    _write_build(apps_dir, manifest=manifest)
    with pytest.raises(KitMissing) as raised:
        load_kit()
    assert raised.value.reason == "not_in_manifest"


def test_a_hash_mismatch_raises(apps_dir):
    _write_build(apps_dir)
    (apps_dir / "kit" / "guard.js").write_text("(() => {})();\n", encoding="utf-8")
    with pytest.raises(KitMissing) as raised:
        load_kit()
    assert raised.value.reason == "digest_mismatch"


def test_a_manifest_that_is_not_json_raises(apps_dir):
    _write_build(apps_dir)
    (apps_dir / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(KitMissing) as raised:
        load_kit()
    assert raised.value.reason == "no_manifest"


def test_a_failure_is_not_cached(apps_dir):
    with pytest.raises(KitMissing):
        load_kit()
    _write_build(apps_dir)
    assert load_kit().css == FILES["kit/istota.css"]


class TestAppsDirResolution:
    def test_the_env_override_wins(self, tmp_path, monkeypatch):
        from istota.webui.static_dir import resolve_apps_dir
        monkeypatch.setenv("ISTOTA_WEB_APPS_DIR", str(tmp_path / "apps"))
        assert resolve_apps_dir() == tmp_path / "apps"

    def test_the_checkout_beside_the_render_build(self, monkeypatch):
        from istota.webui.static_dir import resolve_apps_dir, resolve_render_dir
        monkeypatch.delenv("ISTOTA_WEB_APPS_DIR", raising=False)
        monkeypatch.delenv("ISTOTA_WEB_RENDER_DIR", raising=False)
        assert resolve_apps_dir() == resolve_render_dir().parent / "build-apps"

    def test_the_image_sets_the_variable(self):
        from pathlib import Path
        dockerfile = (Path(__file__).resolve().parent.parent / "docker" / "istota" / "Dockerfile").read_text()
        assert "COPY --from=web-builder /app/web/build-apps /app/web/build-apps" in dockerfile
        assert "ENV ISTOTA_WEB_APPS_DIR=/app/web/build-apps" in dockerfile
