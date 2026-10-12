"""File creation must hold directory descriptors through every write."""

import os

import pytest

from istota.workspace.writes import write_new_file
from istota.workspace import writes
from istota.workspace.listing import ListingError


@pytest.mark.parametrize("existing", ["file", "symlink"])
def test_new_file_does_not_overwrite_an_existing_leaf(tmp_path, existing):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    leaf = root / "note.txt"
    if existing == "symlink":
        leaf.symlink_to(target)
    else:
        leaf.write_bytes(b"keep")
    assert write_new_file(root, [], "note.txt", [b"replace"], max_bytes=100) == "note (2).txt"
    assert (root / "note (2).txt").read_bytes() == b"replace"
    assert leaf.read_bytes() == target.read_bytes() == b"keep"


def test_directory_swapped_after_open_cannot_redirect_the_write(tmp_path, monkeypatch):
    root = tmp_path / "root"
    parent = root / "uploads"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    held = root / "held"
    real_open = os.open

    def open_then_swap(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == "uploads":
            parent.rename(held)
            parent.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(os, "open", open_then_swap)
    write_new_file(root, ["uploads"], "note.txt", [b"private"], max_bytes=100)
    assert list(outside.iterdir()) == []
    assert (held / "note.txt").read_bytes() == b"private"


def test_create_numbering_and_ceiling(tmp_path):
    for n in range(1, 101):
        name = write_new_file(tmp_path, [], "note.txt", [b"a", b"b"], max_bytes=2)
        assert name == ("note.txt" if n == 1 else f"note ({n}).txt")
    with pytest.raises(ListingError) as exc:
        write_new_file(tmp_path, [], "note.txt", [b"c"], max_bytes=2)
    assert exc.value.status == 409
    assert (tmp_path / "note (3).txt").read_bytes() == b"ab"


def test_over_cap_stops_reading_and_unlinks(tmp_path):
    def chunks():
        yield b"ab"
        yield b"cd"
        pytest.fail("must stop consuming when cap is exceeded")
    with pytest.raises(ListingError) as exc:
        write_new_file(tmp_path, [], "note.txt", chunks(), max_bytes=3)
    assert exc.value.status == 413
    assert list(tmp_path.iterdir()) == []


def test_interrupted_source_unlinks(tmp_path):
    def chunks():
        yield b"partial"
        raise OSError("source failed")
    with pytest.raises(OSError):
        write_new_file(tmp_path, [], "note.txt", chunks(), max_bytes=100)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("parts", [["link"], [".."], [""], ["bad\0name"]])
def test_refuses_parent_components(tmp_path, parts):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(ListingError) as exc:
        write_new_file(tmp_path, parts, "note.txt", [b"x"], max_bytes=100)
    assert exc.value.status == 404
    assert list(outside.iterdir()) == []


def test_make_folder_and_conflict(tmp_path):
    assert writes.make_folder(tmp_path, [], "Notes") == "Notes"
    assert (tmp_path / "Notes").is_dir()
    with pytest.raises(ListingError) as exc:
        writes.make_folder(tmp_path, [], "Notes")
    assert exc.value.status == 409


def test_empty_name_fallback(tmp_path):
    assert write_new_file(tmp_path, [], "...", [b"x"], max_bytes=100) == "upload"


def test_delete_file_and_empty_folder(tmp_path):
    (tmp_path / "note.txt").write_text("remove")
    (tmp_path / "empty").mkdir()
    writes.delete_entry(tmp_path, [], "note.txt")
    writes.delete_entry(tmp_path, [], "empty")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["..", ".", "", "../outside", "bad\0name"])
def test_delete_refuses_invalid_leaf(tmp_path, name):
    with pytest.raises(ListingError) as exc:
        writes.delete_entry(tmp_path, [], name)
    assert exc.value.status == 422


def test_delete_refuses_links_and_nonempty_folders(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside)
    for parts, name in [([], "link"), (["link"], "keep.txt"), (["..", "outside"], "keep.txt")]:
        with pytest.raises(ListingError):
            writes.delete_entry(root, parts, name)
    with pytest.raises(ListingError) as exc:
        writes.delete_entry(tmp_path, [], "outside")
    assert exc.value.status == 409
    assert (outside / "keep.txt").read_text() == "keep"


def test_delete_pins_parent_before_directory_swap(tmp_path, monkeypatch):
    parent = tmp_path / "root" / "notes"
    parent.mkdir(parents=True)
    (parent / "note.txt").write_text("remove")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("keep")
    real_open = os.open
    held = parent.with_name("held")

    def open_then_swap(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == "notes":
            parent.rename(held)
            parent.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(os, "open", open_then_swap)
    writes.delete_entry(tmp_path / "root", ["notes"], "note.txt")
    assert not (held / "note.txt").exists()
    assert (outside / "note.txt").read_text() == "keep"
