"""The starter apps as they ship: each builds cleanly and is seeded for a user.

These read the real folders under `web/src/apps/starter/` (what `npm run
build:apps` copies, tests left out) and the real kit scripts, so a starter app
that picks up a validator warning, or stops building, fails here rather than at
a user's first visit to Apps.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from istota import db
from istota.apps import db as apps_db
from istota.apps import install, kit
from istota.apps.document import build_document
from istota.apps.kit import Kit
from istota.apps.manifest import parse_manifest
from istota.config import AppsConfig, Config
from tests.support.apps_build import shipped_starter, write_build

USER = "alice"
KIT_SOURCE = Path(__file__).resolve().parents[1] / "web" / "src" / "apps" / "kit"


def _real_kit() -> Kit:
    # The stylesheet is the unbuilt source; nothing here depends on its tokens.
    read = lambda name: (KIT_SOURCE / name).read_text(encoding="utf-8")  # noqa: E731
    return Kit(
        guard_js=read("guard.js"),
        client_js=read("istota-app.js"),
        components_js=read("components.js"),
        css=read("istota.css"),
    )


def test_board_and_ledger_are_starter_apps():
    assert install.STARTER_APPS[:2] == ("board", "ledger")


@pytest.mark.parametrize("app_id", install.STARTER_APPS)
def test_the_packaged_app_builds_with_no_warnings(app_id):
    files = {path: text.encode("utf-8") for path, text in shipped_starter(app_id).items()}
    manifest = parse_manifest(files["manifest.toml"].decode(), folder_name=app_id, bot_dir="istota")
    assert manifest.warnings == ()
    built = build_document(files, manifest, _real_kit(), max_bytes=AppsConfig().bundle_max_bytes)
    assert built.warnings == ()
    # Every file it ships is inlined: nothing is left for the frame to fetch.
    assert set(built.source) == set(files)
    assert b"<script src=" not in built.html
    assert b"<link" not in built.html


def test_the_board_manifest():
    files = shipped_starter("board")
    manifest = parse_manifest(files["manifest.toml"], folder_name="board", bot_dir="istota")
    assert (manifest.name, manifest.icon, manifest.narrowest) == ("Board", "kanban", "phone")
    assert sorted(c.name for c in manifest.capabilities) == ["files:own", "kv:own"]


def test_the_ledger_manifest():
    files = shipped_starter("ledger")
    manifest = parse_manifest(files["manifest.toml"], folder_name="ledger", bot_dir="istota")
    assert (manifest.name, manifest.icon, manifest.narrowest) == ("Ledger of guesses", "target", "phone")
    assert sorted(c.name for c in manifest.capabilities) == ["files:own", "kv:own"]


@pytest.fixture
def config(tmp_path, monkeypatch):
    directory = tmp_path / "build-apps"
    directory.mkdir()
    write_build(directory, {app_id: shipped_starter(app_id) for app_id in install.STARTER_APPS})
    monkeypatch.setenv("ISTOTA_WEB_APPS_DIR", str(directory))
    kit.reset_cache()
    cfg = Config(db_path=tmp_path / "istota.db", workspace_path=tmp_path / "ws")
    db.init_db(cfg.db_path)
    yield cfg
    kit.reset_cache()


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "modules" / USER / "apps.db"
    apps_db.init_db(path)
    with apps_db.connect(path) as c:
        yield c


def test_seeding_installs_every_shipped_starter_app(config, conn):
    statuses = install.ensure_starter_apps(config, USER, conn)
    assert statuses == {app_id: "installed" for app_id in install.STARTER_APPS}
    board = apps_db.get_app(conn, "board")
    assert (board["name"], board["icon"], board["origin"], board["current_version"]) == (
        "Board", "kanban", "starter", 1,
    )
    assert apps_db.get_grants(conn, "board", 1) == {"files:own": None, "kv:own": None}
    # Board needs nothing beyond its own storage, so it opens with no capability screen.
    assert install.ungranted(conn, "board") == []
    version = apps_db.get_version(conn, "board", 1)
    assert version["warnings"] == []
    ledger = apps_db.get_app(conn, "ledger")
    assert (ledger["name"], ledger["icon"], ledger["origin"], ledger["current_version"]) == (
        "Ledger of guesses", "target", "starter", 1,
    )
    # Ledger keeps its guesses in its own namespace too, so it asks nothing on first open.
    assert apps_db.get_grants(conn, "ledger", 1) == {"files:own": None, "kv:own": None}
    assert install.ungranted(conn, "ledger") == []
    assert apps_db.get_version(conn, "ledger", 1)["warnings"] == []
    again = install.ensure_starter_apps(config, USER, conn)
    assert (again["board"], again["ledger"]) == ("current", "current")
