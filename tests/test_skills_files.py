"""Tests for skills/files.py module."""


import os

import pytest

from istota.config import Config
from istota.skills.files import (
    copy_to_local,
    get_local_path,
    list_files,
    mkdir,
    move_file,
    path_exists,
    read_text,
    write_text,
)


@pytest.fixture
def mount_config(tmp_path):
    """Config using local mount path."""
    mount_dir = tmp_path / "mount"
    mount_dir.mkdir()
    return Config(
        workspace_path=mount_dir,
    )


# --- Mount-aware file operations ---


class TestMountFileOps:
    def test_list_files(self, mount_config):
        mount = mount_config.workspace_path
        (mount / "subdir").mkdir()
        (mount / "subdir" / "a.txt").write_text("hello")
        (mount / "subdir" / "b.txt").write_text("world")
        (mount / "subdir" / "nested").mkdir()

        items = list_files(mount_config, "subdir")
        names = {item["name"] for item in items}
        assert "a.txt" in names
        assert "b.txt" in names
        assert "nested" in names

        # Check is_dir flag
        dirs = {item["name"] for item in items if item["is_dir"]}
        files = {item["name"] for item in items if not item["is_dir"]}
        assert "nested" in dirs
        assert "a.txt" in files

    def test_list_files_nonexistent(self, mount_config):
        with pytest.raises(RuntimeError, match="Path not found"):
            list_files(mount_config, "nonexistent")

    def test_read_text(self, mount_config):
        mount = mount_config.workspace_path
        (mount / "doc.txt").write_text("file content here")

        content = read_text(mount_config, "doc.txt")
        assert content == "file content here"

    def test_read_text_nonexistent(self, mount_config):
        with pytest.raises(RuntimeError, match="File not found"):
            read_text(mount_config, "missing.txt")

    def test_write_text(self, mount_config):
        write_text(mount_config, "output/result.txt", "test output")

        mount = mount_config.workspace_path
        assert (mount / "output" / "result.txt").read_text() == "test output"

    def test_write_text_creates_parents(self, mount_config):
        write_text(mount_config, "deep/nested/dir/file.txt", "content")

        mount = mount_config.workspace_path
        assert (mount / "deep" / "nested" / "dir" / "file.txt").exists()

    def test_mkdir(self, mount_config):
        result = mkdir(mount_config, "new_folder/sub")

        assert result is True
        assert (mount_config.workspace_path / "new_folder" / "sub").is_dir()

    def test_path_exists_true(self, mount_config):
        mount = mount_config.workspace_path
        (mount / "exists.txt").write_text("yes")

        assert path_exists(mount_config, "exists.txt") is True

    def test_path_exists_false(self, mount_config):
        assert path_exists(mount_config, "nope.txt") is False

    def test_move_file(self, mount_config):
        mount = mount_config.workspace_path
        (mount / "source.txt").write_text("data")

        result = move_file(mount_config, "source.txt", "dest/moved.txt")

        assert result is True
        assert not (mount / "source.txt").exists()
        assert (mount / "dest" / "moved.txt").read_text() == "data"

    def test_get_local_path(self, mount_config):
        result = get_local_path(mount_config, "/alice/TODO.txt")
        expected = mount_config.workspace_path / "alice" / "TODO.txt"
        assert result == expected

    def test_get_local_path_no_mount(self):
        config = Config(workspace_path=None)
        result = get_local_path(config, "/alice/TODO.txt")
        assert result is None


class TestNoWorkspace:
    def test_reads_and_writes_refuse(self):
        config = Config(workspace_path=None)
        with pytest.raises(RuntimeError, match="No workspace"):
            read_text(config, "/doc.txt")
        with pytest.raises(RuntimeError, match="No workspace"):
            write_text(config, "/doc.txt", "content")
        with pytest.raises(RuntimeError, match="No workspace"):
            list_files(config, "/")

    def test_predicates_answer_false(self):
        config = Config(workspace_path=None)
        assert path_exists(config, "/doc.txt") is False
        assert mkdir(config, "/new_dir") is False
        assert move_file(config, "/a.txt", "/b.txt") is False


def _xattr_eio(monkeypatch):
    """`listxattr` as the rclone workspace mount answered it for files (#694)."""
    import errno
    import os

    def _raise(path, *a, **kw):
        raise OSError(errno.EIO, "Input/output error", str(path))

    monkeypatch.setattr(os, "listxattr", _raise)


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="listxattr is Linux-only")
def test_copy_to_local_survives_a_mount_whose_xattr_calls_fail(mount_config, tmp_path, monkeypatch):
    _xattr_eio(monkeypatch)
    (mount_config.workspace_path / "report.pdf").write_bytes(b"%PDF-1.4\n")
    local = tmp_path / "local" / "report.pdf"
    copy_to_local(mount_config, "/report.pdf", local)
    assert local.read_bytes() == b"%PDF-1.4\n"
