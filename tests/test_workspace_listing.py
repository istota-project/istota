"""Descriptor-relative workspace listings against real filesystem trees."""
import errno
import os
from contextlib import contextmanager

import pytest

from istota.workspace import listing


def test_metadata_order_links_and_special_files(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").touch()
    (root / "Zdir").mkdir()
    (root / "adir").mkdir()
    (root / "B.txt").write_text("hello")
    (root / ".hidden").touch()
    (root / "link").symlink_to(outside, target_is_directory=True)
    os.mkfifo(root / "pipe")
    result = listing.list_dir(root, [])
    assert [e.name for e in result.entries] == ["adir", "Zdir", ".hidden", "B.txt", "link", "pipe"]
    entries = {e.name: e for e in result.entries}
    assert entries["B.txt"].size == 5
    assert entries["B.txt"].modified == (root / "B.txt").stat().st_mtime
    assert entries[".hidden"].hidden and not entries["B.txt"].hidden
    assert entries["link"].kind == "link" and entries["link"].size is None
    assert entries["pipe"].kind == "other" and entries["adir"].size is None
    assert not result.truncated and result.skipped_names == 0
    with pytest.raises(listing.ListingError) as exc:
        listing.list_dir(root, ["link"])
    assert exc.value.status == 404


@pytest.mark.parametrize("parts", [[".."], ["."], [""], ["bad\0name"], ["a/b"], ["missing"]])
def test_invalid_or_missing_components(tmp_path, parts):
    with pytest.raises(listing.ListingError) as exc:
        listing.list_dir(tmp_path, parts)
    assert exc.value.status == 404


def test_symlinked_intermediate_component(tmp_path):
    (tmp_path / "real" / "child").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(listing.ListingError) as exc:
        listing.list_dir(tmp_path, ["link", "child"])
    assert exc.value.status == 404


def test_directory_swap_keeps_scanning_open_descriptor(tmp_path, monkeypatch):
    root = tmp_path / "home"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "safe").touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").touch()
    scandir = os.scandir

    def swap_then_scan(fd):
        (root / "notes").rename(root / "old")
        (root / "notes").symlink_to(outside, target_is_directory=True)
        return scandir(fd)

    monkeypatch.setattr(listing.os, "scandir", swap_then_scan)
    assert [e.name for e in listing.list_dir(root, ["notes"]).entries] == ["safe"]


def test_cap(tmp_path):
    for n in range(2000):
        (tmp_path / str(n)).touch()
    assert not listing.list_dir(tmp_path, []).truncated
    (tmp_path / "extra").touch()
    result = listing.list_dir(tmp_path, [])
    assert len(result.entries) == 2000
    assert result.truncated


def test_non_utf8_name_is_counted(tmp_path):
    try:
        fd = os.open(os.fsencode(tmp_path) + b"/bad-\xff", os.O_CREAT | os.O_WRONLY, 0o600)
    except OSError:
        pytest.skip("filesystem refuses non-UTF-8 filenames")
    os.close(fd)
    (tmp_path / "normal").touch()
    result = listing.list_dir(tmp_path, [])
    assert [e.name for e in result.entries] == ["normal"]
    assert result.skipped_names == 1


def test_entry_deleted_before_stat_is_skipped(tmp_path, monkeypatch):
    (tmp_path / "gone").touch()
    (tmp_path / "kept").touch()
    scandir = os.scandir

    @contextmanager
    def removing_scan(fd):
        with scandir(fd) as entries:
            def walk():
                for entry in entries:
                    if entry.name == "gone":
                        os.unlink("gone", dir_fd=fd)
                    yield entry
            yield walk()

    monkeypatch.setattr(listing.os, "scandir", removing_scan)
    assert [e.name for e in listing.list_dir(tmp_path, []).entries] == ["kept"]


@pytest.mark.parametrize("error", [errno.ENOTCONN, errno.EIO])
def test_overlay_mount_error_propagation_is_opt_in(tmp_path, monkeypatch, error):
    from istota.skills._loader import open_overlay_dir

    def fail(*args, **kwargs):
        raise OSError(error, "disconnected")

    monkeypatch.setattr(os, "open", fail)
    assert open_overlay_dir(tmp_path) is None
    with pytest.raises(OSError) as raised:
        open_overlay_dir(tmp_path, raise_mount_errors=True)
    assert raised.value.errno == error
