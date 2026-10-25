"""`istota.apps.install`: pending installs, approval, versions, uninstall, grants and starter apps.

Real files throughout: drafts are folders in a `tmp_path` workspace, the kit
and the starter apps are a build directory named by `ISTOTA_WEB_APPS_DIR`, and
KV data lives in a real framework database.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from istota import db
from istota.apps import db as apps_db
from istota.apps import install, kit
from istota.apps.drafts import DraftNotFound
from istota.apps.kit import KitMissing
from istota.apps.manifest import ManifestError
from istota.config import Config
from tests.support.apps_build import KIT_FILES
from tests.support.apps_build import write_build as _write_build

USER = "alice"

INDEX = "<!doctype html><html><head><title>x</title></head><body><p>v1</p></body></html>\n"


def _manifest_text(app_id="board", capabilities=(), name="Board"):
    caps = ", ".join(f'"{c}"' for c in capabilities)
    return f'id = "{app_id}"\nname = "{name}"\nicon = "layout"\ncapabilities = [{caps}]\n'


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    directory = tmp_path / "build-apps"
    directory.mkdir()
    _write_build(directory)
    monkeypatch.setenv("ISTOTA_WEB_APPS_DIR", str(directory))
    kit.reset_cache()
    yield directory
    kit.reset_cache()


@pytest.fixture
def config(tmp_path, apps_dir):
    cfg = Config(db_path=tmp_path / "istota.db", workspace_path=tmp_path / "ws")
    db.init_db(cfg.db_path)
    return cfg


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "modules" / USER / "apps.db"
    apps_db.init_db(path)
    with apps_db.connect(path) as c:
        yield c


@pytest.fixture
def apps_root(config):
    root = config.workspace_root(USER) / config.bot_dir_name / "apps"
    root.mkdir(parents=True)
    return root


def _draft(apps_root, app_id="board", capabilities=(), index=INDEX):
    folder = apps_root / app_id
    folder.mkdir(exist_ok=True)
    (folder / "manifest.toml").write_text(_manifest_text(app_id, capabilities))
    (folder / "index.html").write_text(index)
    return folder


def _install(config, conn, apps_root, *, app_id="board", capabilities=(), index=INDEX, ack=None):
    _draft(apps_root, app_id, capabilities, index)
    pending_id = install.create_pending(config, USER, conn, app_id, origin="draft")
    if ack is None:
        ack = [c for c in capabilities if c not in ("kv:own", "files:own")]
    return install.approve(conn, pending_id, new_capabilities_ack=ack)


class TestCreatePending:
    def test_builds_the_draft_into_a_stored_pending_install(self, config, conn, apps_root):
        _draft(apps_root, capabilities=["calendar:read"])
        pending_id = install.create_pending(config, USER, conn, "board", origin="draft")
        pending = apps_db.get_pending(conn, pending_id)
        assert pending["app_id"] == "board"
        assert pending["origin"] == "draft"
        assert pending["manifest"]["capabilities"] == ["kv:own", "files:own", "calendar:read"]
        assert pending["manifest"]["name"] == "Board"
        assert pending["source"]["index.html"] == INDEX
        assert KIT_FILES["kit/guard.js"].encode() in pending["document"]
        assert pending["csp"].startswith("default-src 'none'; script-src 'sha256-")
        assert pending["content_hash"] == hashlib.sha256(pending["document"]).hexdigest()

    def test_later_edits_to_the_folder_change_nothing(self, config, conn, apps_root):
        folder = _draft(apps_root)
        pending_id = install.create_pending(config, USER, conn, "board", origin="draft")
        before = apps_db.get_pending(conn, pending_id)["document"]
        (folder / "index.html").write_text("<p>changed</p>")
        assert apps_db.get_pending(conn, pending_id)["document"] == before

    def test_a_second_pending_install_replaces_the_first(self, config, conn, apps_root):
        _draft(apps_root)
        first = install.create_pending(config, USER, conn, "board", origin="draft")
        second = install.create_pending(config, USER, conn, "board", origin="draft")
        assert apps_db.get_pending(conn, first) is None
        assert apps_db.get_pending(conn, second) is not None

    def test_manifest_warnings_are_kept_with_the_document_warnings(self, config, conn, apps_root):
        folder = _draft(apps_root)
        (folder / "manifest.toml").write_text(_manifest_text() + 'future = "x"\n')
        pending_id = install.create_pending(config, USER, conn, "board", origin="draft")
        assert "manifest.toml: unknown key future is ignored." in apps_db.get_pending(conn, pending_id)["warnings"]

    def test_a_manifest_naming_another_folder_is_refused(self, config, conn, apps_root):
        folder = _draft(apps_root)
        (folder / "manifest.toml").write_text(_manifest_text("other"))
        with pytest.raises(ManifestError):
            install.create_pending(config, USER, conn, "board", origin="draft")
        assert apps_db.list_pending(conn) == []

    def test_a_draft_without_a_manifest_is_refused(self, config, conn, apps_root):
        folder = _draft(apps_root)
        (folder / "manifest.toml").unlink()
        with pytest.raises(ManifestError):
            install.create_pending(config, USER, conn, "board", origin="draft")

    def test_a_missing_draft(self, config, conn, apps_root):
        with pytest.raises(DraftNotFound):
            install.create_pending(config, USER, conn, "board", origin="draft")

    def test_an_unbuilt_kit_is_refused(self, config, conn, apps_root, apps_dir):
        _draft(apps_root)
        (apps_dir / "manifest.json").unlink()
        kit.reset_cache()
        with pytest.raises(KitMissing):
            install.create_pending(config, USER, conn, "board", origin="draft")


class TestApprove:
    def test_a_first_install_becomes_version_one_with_its_grants(self, config, conn, apps_root):
        version = _install(config, conn, apps_root)
        assert version == 1
        app = apps_db.get_app(conn, "board")
        assert app["current_version"] == 1
        assert (app["name"], app["icon"], app["origin"]) == ("Board", "layout", "draft")
        assert apps_db.get_grants(conn, "board", 1) == {"files:own": None, "kv:own": None}
        assert apps_db.list_pending(conn) == []
        assert apps_db.get_version(conn, "board", 1)["approved_at"] is not None

    def test_a_new_capability_must_be_acknowledged(self, config, conn, apps_root):
        _draft(apps_root, capabilities=["calendar:read"])
        pending_id = install.create_pending(config, USER, conn, "board", origin="draft")
        with pytest.raises(install.StaleApproval):
            install.approve(conn, pending_id, new_capabilities_ack=[])
        assert apps_db.get_app(conn, "board") is None
        assert apps_db.get_pending(conn, pending_id) is not None

        assert install.approve(conn, pending_id, new_capabilities_ack=["calendar:read"]) == 1
        assert apps_db.get_grants(conn, "board", 1)["calendar:read"] is None

    def test_an_ack_naming_more_than_is_new_is_stale(self, config, conn, apps_root):
        _draft(apps_root)
        pending_id = install.create_pending(config, USER, conn, "board", origin="draft")
        with pytest.raises(install.StaleApproval):
            install.approve(conn, pending_id, new_capabilities_ack=["calendar:read"])

    def test_the_same_capabilities_again_need_no_ack(self, config, conn, apps_root):
        _install(config, conn, apps_root, capabilities=["calendar:read"])
        version = _install(
            config, conn, apps_root, capabilities=["calendar:read"], index=INDEX.replace("v1", "v2"), ack=[],
        )
        assert version == 2
        assert apps_db.get_app(conn, "board")["current_version"] == 2

    def test_new_against_the_current_version_only(self, config, conn, apps_root):
        _install(config, conn, apps_root)
        _draft(apps_root, capabilities=["calendar:read", "kv:read:notes"], index=INDEX.replace("v1", "v2"))
        pending_id = install.create_pending(config, USER, conn, "board", origin="draft")
        assert install.new_capabilities(conn, "board", apps_db.get_pending(conn, pending_id)["manifest"]) == [
            "calendar:read", "kv:read:notes",
        ]
        with pytest.raises(install.StaleApproval):
            install.approve(conn, pending_id, new_capabilities_ack=["calendar:read"])
        assert install.approve(
            conn, pending_id, new_capabilities_ack=["kv:read:notes", "calendar:read"],
        ) == 2
        assert apps_db.get_grants(conn, "board", 2)["kv:read:notes"] == "notes"

    def test_an_unknown_pending_install(self, conn):
        with pytest.raises(install.NotFound):
            install.approve(conn, "nope", new_capabilities_ack=[])


class TestRollbackAndDelete:
    def _two_versions(self, config, conn, apps_root):
        _install(config, conn, apps_root)
        _install(config, conn, apps_root, capabilities=["calendar:read"], index=INDEX.replace("v1", "v2"))

    def test_rollback_restores_the_old_versions_grants(self, config, conn, apps_root):
        self._two_versions(config, conn, apps_root)
        assert "calendar:read" in apps_db.get_grants(conn, "board", 2)
        install.rollback(conn, "board", 1)
        app = apps_db.get_app(conn, "board")
        assert app["current_version"] == 1
        assert apps_db.get_grants(conn, "board", app["current_version"]) == {"files:own": None, "kv:own": None}

    def test_rollback_to_an_unknown_version(self, config, conn, apps_root):
        _install(config, conn, apps_root)
        with pytest.raises(install.NotFound):
            install.rollback(conn, "board", 7)
        with pytest.raises(install.NotFound):
            install.rollback(conn, "other", 1)

    def test_delete_a_non_current_version_and_its_document(self, config, conn, apps_root):
        self._two_versions(config, conn, apps_root)
        old_hash = apps_db.get_version(conn, "board", 1)["content_hash"]
        install.delete_version(conn, "board", 1)
        assert apps_db.get_version(conn, "board", 1) is None
        assert apps_db.get_grants(conn, "board", 1) == {}
        assert apps_db.get_document(conn, old_hash) is None
        assert apps_db.get_app(conn, "board")["current_version"] == 2

    def test_the_current_version_cannot_be_deleted(self, config, conn, apps_root):
        self._two_versions(config, conn, apps_root)
        with pytest.raises(install.CurrentVersionError):
            install.delete_version(conn, "board", 2)
        with pytest.raises(install.NotFound):
            install.delete_version(conn, "board", 9)


class TestUninstall:
    @pytest.fixture
    def installed(self, config, conn, apps_root):
        _install(config, conn, apps_root)
        data = apps_root / "board" / "data"
        (data / "sub").mkdir(parents=True)
        (data / "notes.txt").write_text("mine")
        (data / "sub" / "deep.txt").write_text("deep")
        with db.get_db(config.db_path) as fw:
            db.kv_set(fw, USER, "app.board", "columns", "[]")
            db.kv_set(fw, USER, "app.boardx", "k", "1")
            db.kv_set(fw, USER, "notes", "k", "1")
            db.kv_set(fw, "bob", "app.board", "k", "1")
        return apps_root / "board"

    def _kv(self, config, user, namespace):
        with db.get_db(config.db_path) as fw:
            return db.kv_list(fw, user, namespace)

    def test_without_data_keeps_kv_and_files(self, config, conn, installed):
        install.uninstall(config, USER, conn, "board", delete_data=False)
        assert apps_db.get_app(conn, "board") is None
        assert apps_db.list_versions(conn, "board") == []
        assert conn.execute("SELECT COUNT(*) FROM app_documents").fetchone()[0] == 0
        assert len(self._kv(config, USER, "app.board")) == 1
        assert (installed / "data" / "notes.txt").read_text() == "mine"
        assert (installed / "manifest.toml").exists()

    def test_with_data_removes_the_namespace_and_the_data_folder(self, config, conn, installed):
        install.uninstall(config, USER, conn, "board", delete_data=True)
        assert apps_db.get_app(conn, "board") is None
        assert self._kv(config, USER, "app.board") == []
        assert len(self._kv(config, USER, "app.boardx")) == 1
        assert len(self._kv(config, USER, "notes")) == 1
        assert len(self._kv(config, "bob", "app.board")) == 1
        assert not (installed / "data").exists()
        assert (installed / "manifest.toml").exists()
        assert (installed / "index.html").exists()

    def test_a_link_in_the_data_folder_is_removed_not_followed(self, config, conn, installed, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        os.symlink(outside, installed / "data" / "link")
        install.uninstall(config, USER, conn, "board", delete_data=True)
        assert not (installed / "data").exists()
        assert (outside / "keep.txt").read_text() == "keep"

    def test_a_data_folder_that_is_a_link_is_not_followed(self, config, conn, installed, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        for path in sorted((installed / "data").rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        (installed / "data").rmdir()
        os.symlink(outside, installed / "data")
        install.uninstall(config, USER, conn, "board", delete_data=True)
        assert (outside / "keep.txt").read_text() == "keep"
        assert apps_db.get_app(conn, "board") is None

    def test_an_unknown_app(self, config, conn):
        with pytest.raises(install.NotFound):
            install.uninstall(config, USER, conn, "board", delete_data=False)


class TestGrant:
    def test_grants_a_requested_capability_on_the_current_version(self, config, conn, apps_root):
        _install(config, conn, apps_root, capabilities=["calendar:read"])
        conn.execute("DELETE FROM app_grants WHERE capability = 'calendar:read'")
        install.grant(conn, "board", 1, "calendar:read", None, bot_dir=config.bot_dir_name)
        assert apps_db.get_grants(conn, "board", 1)["calendar:read"] is None

    def test_a_files_read_scope_may_be_changed(self, config, conn, apps_root):
        _install(config, conn, apps_root, capabilities=["files:read:notes"])
        install.grant(conn, "board", 1, "files:read:notes", "notes/aviation", bot_dir=config.bot_dir_name)
        assert apps_db.get_grants(conn, "board", 1)["files:read:notes"] == "notes/aviation"

    def test_a_scope_into_a_denied_folder_is_refused(self, config, conn, apps_root):
        _install(config, conn, apps_root, capabilities=["files:read:notes"])
        with pytest.raises(install.GrantError):
            install.grant(conn, "board", 1, "files:read:notes", "memories", bot_dir=config.bot_dir_name)
        assert apps_db.get_grants(conn, "board", 1)["files:read:notes"] == "notes"

    def test_other_scopes_are_fixed(self, config, conn, apps_root):
        _install(config, conn, apps_root, capabilities=["kv:read:notes"])
        with pytest.raises(install.GrantError):
            install.grant(conn, "board", 1, "kv:read:notes", "secrets", bot_dir=config.bot_dir_name)

    def test_a_capability_the_manifest_does_not_request(self, config, conn, apps_root):
        _install(config, conn, apps_root)
        with pytest.raises(install.GrantError):
            install.grant(conn, "board", 1, "calendar:read", None, bot_dir=config.bot_dir_name)

    def test_only_the_current_version(self, config, conn, apps_root):
        _install(config, conn, apps_root, capabilities=["calendar:read"])
        _install(config, conn, apps_root, capabilities=["calendar:read"], index=INDEX.replace("v1", "v2"), ack=[])
        with pytest.raises(install.GrantError):
            install.grant(conn, "board", 1, "calendar:read", None, bot_dir=config.bot_dir_name)


def _starter(capabilities=("calendar:read",), index=INDEX, name="Board"):
    return {"board": {"manifest.toml": _manifest_text("board", capabilities, name), "index.html": index}}


class TestStarterApps:
    @pytest.fixture
    def starters(self, apps_dir, monkeypatch):
        monkeypatch.setattr(install, "STARTER_APPS", ("board",))
        _write_build(apps_dir, _starter())
        kit.reset_cache()
        return apps_dir

    def test_a_build_without_the_starter_apps_seeds_nothing(self, config, conn):
        statuses = install.ensure_starter_apps(config, USER, conn)
        assert statuses == {app_id: "unavailable" for app_id in install.STARTER_APPS}
        assert apps_db.list_apps(conn) == []

    def test_seeds_version_one_with_only_the_always_granted(self, config, conn, starters):
        assert install.ensure_starter_apps(config, USER, conn) == {"board": "installed"}
        app = apps_db.get_app(conn, "board")
        assert (app["origin"], app["current_version"]) == ("starter", 1)
        assert apps_db.get_grants(conn, "board", 1) == {"files:own": None, "kv:own": None}
        assert apps_db.list_pending(conn) == []
        state = apps_db.get_starter_state(conn, "board")
        assert state["installed_hash"] == apps_db.get_version(conn, "board", 1)["content_hash"]
        assert state["dismissed_at"] is None
        assert install.ungranted(conn, "board") == ["calendar:read"]

    def test_an_unchanged_build_adds_no_version(self, config, conn, starters):
        install.ensure_starter_apps(config, USER, conn)
        assert install.ensure_starter_apps(config, USER, conn) == {"board": "current"}
        assert [v["version"] for v in apps_db.list_versions(conn, "board")] == [1]

    def test_an_upgrade_carries_grants_and_leaves_a_new_capability_ungranted(self, config, conn, starters):
        install.ensure_starter_apps(config, USER, conn)
        install.grant(conn, "board", 1, "calendar:read", None, bot_dir=config.bot_dir_name)
        conn.commit()
        _write_build(starters, _starter(("calendar:read", "kv:read:notes"), INDEX.replace("v1", "v2")))
        assert install.ensure_starter_apps(config, USER, conn) == {"board": "upgraded"}
        assert apps_db.get_app(conn, "board")["current_version"] == 2
        assert apps_db.get_grants(conn, "board", 2) == {
            "calendar:read": None, "files:own": None, "kv:own": None,
        }
        assert install.ungranted(conn, "board") == ["kv:read:notes"]

    def test_an_uninstalled_starter_is_not_reinstalled_or_upgraded(self, config, conn, starters):
        install.ensure_starter_apps(config, USER, conn)
        install.uninstall(config, USER, conn, "board", delete_data=False)
        conn.commit()
        assert apps_db.get_starter_state(conn, "board")["dismissed_at"] is not None
        assert install.ensure_starter_apps(config, USER, conn) == {"board": "dismissed"}
        _write_build(starters, _starter(index=INDEX.replace("v1", "v2")))
        assert install.ensure_starter_apps(config, USER, conn) == {"board": "dismissed"}
        assert apps_db.get_app(conn, "board") is None

    def test_reinstall_clears_the_dismissal(self, config, conn, starters):
        install.ensure_starter_apps(config, USER, conn)
        install.uninstall(config, USER, conn, "board", delete_data=False)
        conn.commit()
        assert install.reinstall_starter(config, USER, conn, "board") == "installed"
        assert apps_db.get_app(conn, "board")["current_version"] == 1
        assert apps_db.get_starter_state(conn, "board")["dismissed_at"] is None

    def test_reinstall_of_an_app_that_is_not_a_starter(self, config, conn, starters):
        with pytest.raises(install.NotFound):
            install.reinstall_starter(config, USER, conn, "tides")

    def test_an_app_the_user_installed_under_the_same_id_is_left_alone(self, config, conn, apps_root, starters):
        _install(config, conn, apps_root)
        conn.commit()
        assert install.ensure_starter_apps(config, USER, conn) == {"board": "conflict"}
        assert apps_db.get_app(conn, "board")["origin"] == "draft"
        assert [v["version"] for v in apps_db.list_versions(conn, "board")] == [1]

    def test_an_unbuilt_kit_leaves_starters_unavailable(self, config, conn, starters):
        (starters / "manifest.json").unlink()
        kit.reset_cache()
        assert install.ensure_starter_apps(config, USER, conn) == {"board": "unavailable"}
        assert apps_db.list_apps(conn) == []

    def test_a_broken_starter_fails_alone(self, config, conn, starters, monkeypatch):
        monkeypatch.setattr(install, "STARTER_APPS", ("board", "ledger"))
        files = _starter()
        files["ledger"] = {"manifest.toml": _manifest_text("wrong"), "index.html": INDEX}
        _write_build(starters, files)
        kit.reset_cache()
        assert install.ensure_starter_apps(config, USER, conn) == {"board": "installed", "ledger": "failed"}
        assert apps_db.get_app(conn, "board") is not None
