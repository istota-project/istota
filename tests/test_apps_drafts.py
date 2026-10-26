"""`istota.apps.drafts`: app folders in the workspace, read by descriptor with real symlinks."""

from __future__ import annotations

import os

import pytest

from istota.apps.document import build_document
from istota.apps.kit import Kit
from istota.apps.manifest import parse_manifest
from istota.apps.drafts import (
    MAX_DRAFT_FILES,
    DraftError,
    DraftNotFound,
    DraftSummary,
    list_drafts,
    read_draft,
)
from istota.config import AppsConfig, Config

USER = "alice"


@pytest.fixture
def config(tmp_path):
    return Config(db_path=tmp_path / "istota.db", workspace_path=tmp_path / "ws")


@pytest.fixture
def apps_root(config):
    root = config.workspace_root(USER) / config.bot_dir_name / "apps"
    root.mkdir(parents=True)
    return root


def _draft(apps_root, app_id="board", files=None):
    folder = apps_root / app_id
    folder.mkdir()
    files = files if files is not None else {
        "manifest.toml": f'id = "{app_id}"\nname = "My Board"\nicon = "layout"\n',
        "index.html": "<p>x</p>",
        "js/app.js": "1;",
    }
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return folder


class TestReadDraft:
    def test_reads_every_file_by_relative_path(self, config, apps_root):
        _draft(apps_root)
        assert read_draft(config, USER, "board") == {
            "manifest.toml": b'id = "board"\nname = "My Board"\nicon = "layout"\n',
            "index.html": b"<p>x</p>",
            "js/app.js": b"1;",
        }

    def test_data_preview_and_dot_entries_are_not_read(self, config, apps_root):
        folder = _draft(apps_root)
        (folder / "data").mkdir()
        (folder / "data" / "big.bin").write_bytes(b"x" * 1000)
        (folder / "preview").mkdir()
        (folder / "preview" / "a.png").write_bytes(b"x")
        (folder / ".git").mkdir()
        (folder / ".git" / "HEAD").write_text("ref")
        (folder / ".DS_Store").write_bytes(b"x")
        os.symlink("/etc/passwd", folder / ".link")
        assert set(read_draft(config, USER, "board")) == {"manifest.toml", "index.html", "js/app.js"}

    def test_a_nested_data_folder_is_read(self, config, apps_root):
        folder = _draft(apps_root)
        (folder / "js" / "data").mkdir()
        (folder / "js" / "data" / "x.js").write_text("2;")
        assert "js/data/x.js" in read_draft(config, USER, "board")

    def test_a_missing_draft(self, config, apps_root):
        with pytest.raises(DraftNotFound):
            read_draft(config, USER, "board")

    def test_the_draft_folder_is_a_symlink(self, config, apps_root, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "index.html").write_text("secret")
        os.symlink(elsewhere, apps_root / "board")
        with pytest.raises(DraftError) as caught:
            read_draft(config, USER, "board")
        assert not isinstance(caught.value, DraftNotFound)
        assert "link" in str(caught.value)

    def test_a_symlinked_file_inside(self, config, apps_root, tmp_path):
        folder = _draft(apps_root)
        secret = tmp_path / "secret.js"
        secret.write_text("secret")
        os.symlink(secret, folder / "js" / "lib.js")
        with pytest.raises(DraftError) as caught:
            read_draft(config, USER, "board")
        assert "js/lib.js" in str(caught.value)

    def test_a_symlink_pointing_back_inside_is_refused_too(self, config, apps_root):
        folder = _draft(apps_root)
        os.symlink("app.js", folder / "js" / "alias.js")
        with pytest.raises(DraftError):
            read_draft(config, USER, "board")

    def test_a_symlinked_folder_inside(self, config, apps_root, tmp_path):
        folder = _draft(apps_root)
        other = tmp_path / "other"
        other.mkdir()
        (other / "x.js").write_text("secret")
        os.symlink(other, folder / "vendor")
        with pytest.raises(DraftError):
            read_draft(config, USER, "board")

    def test_a_symlinked_apps_folder(self, config, tmp_path):
        bot_dir = config.workspace_root(USER) / config.bot_dir_name
        bot_dir.mkdir(parents=True)
        real = tmp_path / "real-apps"
        real.mkdir()
        _draft(real)
        os.symlink(real, bot_dir / "apps")
        with pytest.raises(DraftError):
            read_draft(config, USER, "board")

    def test_a_fifo_is_refused(self, config, apps_root):
        folder = _draft(apps_root)
        os.mkfifo(folder / "pipe.js")
        with pytest.raises(DraftError):
            read_draft(config, USER, "board")

    def test_too_many_files(self, config, apps_root):
        files = {f"f{i}.js": "1" for i in range(MAX_DRAFT_FILES + 1)}
        _draft(apps_root, files=files)
        with pytest.raises(DraftError) as caught:
            read_draft(config, USER, "board")
        assert str(MAX_DRAFT_FILES) in str(caught.value)

    def test_too_many_bytes(self, tmp_path, apps_root):
        config = Config(
            db_path=tmp_path / "istota.db", workspace_path=tmp_path / "ws",
            apps=AppsConfig(bundle_max_bytes=100),
        )
        _draft(apps_root, files={"index.html": "x" * 150, "a.js": "y" * 51})
        with pytest.raises(DraftError):
            read_draft(config, USER, "board")

    def test_just_under_the_byte_cap(self, tmp_path, apps_root):
        config = Config(
            db_path=tmp_path / "istota.db", workspace_path=tmp_path / "ws",
            apps=AppsConfig(bundle_max_bytes=100),
        )
        _draft(apps_root, files={"index.html": "x" * 150, "a.js": "y" * 50})
        assert len(read_draft(config, USER, "board")) == 2

    @pytest.mark.parametrize("app_id", ["..", ".", "Board", "a/b", "", "x" * 41])
    def test_an_invalid_app_id(self, config, apps_root, app_id):
        with pytest.raises(DraftError):
            read_draft(config, USER, app_id)

    def test_no_workspace(self, tmp_path):
        config = Config(db_path=tmp_path / "istota.db")
        with pytest.raises(DraftError):
            read_draft(config, USER, "board")


class TestListDrafts:
    def test_lists_app_folders(self, config, apps_root):
        _draft(apps_root, "board")
        _draft(apps_root, "tides", files={"index.html": "<p>x</p>"})
        assert list_drafts(config, USER) == [
            DraftSummary(app_id="board", name="My Board", has_manifest=True),
            DraftSummary(app_id="tides", name=None, has_manifest=False),
        ]

    def test_skips_links_files_and_bad_names(self, config, apps_root, tmp_path):
        _draft(apps_root, "board")
        other = tmp_path / "other"
        other.mkdir()
        os.symlink(other, apps_root / "linked")
        (apps_root / "loose.txt").write_text("x")
        (apps_root / "Bad_Name").mkdir()
        (apps_root / ".hidden").mkdir()
        assert [d.app_id for d in list_drafts(config, USER)] == ["board"]

    def test_a_symlinked_manifest_is_not_read(self, config, apps_root, tmp_path):
        folder = _draft(apps_root, "board", files={"index.html": "x"})
        secret = tmp_path / "secret.toml"
        secret.write_text('name = "Secret"\n')
        os.symlink(secret, folder / "manifest.toml")
        assert list_drafts(config, USER) == [DraftSummary(app_id="board", name=None, has_manifest=False)]

    def test_an_unparseable_manifest_has_no_name(self, config, apps_root):
        _draft(apps_root, "board", files={"manifest.toml": "id = \n"})
        assert list_drafts(config, USER) == [DraftSummary(app_id="board", name=None, has_manifest=True)]

    def test_no_apps_folder(self, config):
        assert list_drafts(config, USER) == []

    def test_no_workspace(self, tmp_path):
        assert list_drafts(Config(db_path=tmp_path / "istota.db"), USER) == []


def test_a_draft_on_disk_builds_into_a_document(config, apps_root):
    _draft(apps_root, files={
        "manifest.toml": 'id = "board"\nname = "Board"\nicon = "layout"\n',
        "index.html": '<html><head></head><body><script src="js/app.js"></script></body></html>',
        "js/app.js": "1;",
        "data/state.json": "{}",
    })
    files = read_draft(config, USER, "board")
    manifest = parse_manifest(files["manifest.toml"].decode(), folder_name="board", bot_dir=config.bot_dir_name)
    built = build_document(files, manifest, Kit("g;", "c;", "k;", "x{}"), max_bytes=config.apps.bundle_max_bytes)
    assert built.html.endswith(b"<body><script>1;</script></body></html>")
    assert built.warnings == ()
