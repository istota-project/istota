"""scripts/apps-escape-fixture.py: a fixture folder built the way an install builds it."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from istota.apps import kit
from istota.apps.document import csp_for
from tests.support.apps_build import write_build

REPO = Path(__file__).resolve().parent.parent
CONTRACT = REPO / "tests" / "fixtures" / "apps" / "escape-contract"


def _load_script():
    spec = importlib.util.spec_from_file_location("apps_escape_fixture", REPO / "scripts" / "apps-escape-fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    directory = tmp_path / "build-apps"
    directory.mkdir()
    write_build(directory)
    monkeypatch.setenv("ISTOTA_WEB_APPS_DIR", str(directory))
    kit.reset_cache()
    yield directory
    kit.reset_cache()


def test_builds_the_contract_fixture_with_its_csp(apps_dir, tmp_path):
    out = tmp_path / "out"
    assert _load_script().main(["--app", str(CONTRACT), "--out", str(out)]) == 0

    html = (out / "frame.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html><script>(() => { delete window.RTCPeerConnection; })();")
    assert "window.istota.kv.set('done', true)" in html
    csp = (out / "csp.txt").read_text(encoding="utf-8").strip()
    assert csp.startswith("default-src 'none'; script-src 'sha256-")
    assert csp.endswith("sandbox allow-scripts")
    assert csp.count("'sha256-") == 4


def test_the_csp_is_the_frame_routes(apps_dir, tmp_path):
    from istota.apps.document import _inline_scripts, script_hash

    out = tmp_path / "out"
    _load_script().main(["--app", str(CONTRACT), "--out", str(out)])
    html = (out / "frame.html").read_text(encoding="utf-8")
    hashes = tuple(script_hash(text) for text in _inline_scripts(html))
    assert (out / "csp.txt").read_text(encoding="utf-8").strip() == csp_for(hashes)


def test_no_kit_is_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ISTOTA_WEB_APPS_DIR", str(tmp_path / "missing"))
    kit.reset_cache()
    try:
        assert _load_script().main(["--app", str(CONTRACT), "--out", str(tmp_path / "out")]) == 2
    finally:
        kit.reset_cache()
    assert "build:apps" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_a_folder_the_builder_refuses_is_exit_1(apps_dir, tmp_path, capsys):
    folder = tmp_path / "wrong-name"
    folder.mkdir()
    (folder / "manifest.toml").write_text('id = "other"\nname = "X"\nicon = "x"\n', encoding="utf-8")
    (folder / "index.html").write_text("<!doctype html><p>x</p>", encoding="utf-8")
    assert _load_script().main(["--app", str(folder), "--out", str(tmp_path / "out")]) == 1
    assert "must equal the folder name" in capsys.readouterr().err
