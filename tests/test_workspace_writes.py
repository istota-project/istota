"""File creation must hold directory descriptors through every write."""

import os

import pytest

from istota.workspace.writes import write_new_file


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
    with pytest.raises(FileExistsError):
        write_new_file(root, ("note.txt",), b"replace")
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
    write_new_file(root, ("uploads", "note.txt"), b"private")
    assert list(outside.iterdir()) == []
    assert (held / "note.txt").read_bytes() == b"private"
