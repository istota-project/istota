"""Bounded directory listings through pinned descriptors."""

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from istota.sandbox.confined import open_dir, open_regular

LIST_MAX_ENTRIES = 2000


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str
    size: int | None
    modified: float
    hidden: bool


@dataclass(frozen=True)
class Listing:
    entries: list[Entry]
    truncated: bool
    skipped_names: int = 0


class ListingError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def list_dir(root: Path, parts: list[str], *, limit: int = LIST_MAX_ENTRIES) -> Listing:
    fd = open_dir(root, *parts, raise_mount_errors=True)
    if fd is None:
        raise ListingError(404, "Folder not found.")
    entries = []
    truncated = False
    skipped_names = 0
    try:
        with os.scandir(fd) as scan:
            for index, entry in enumerate(scan):
                # Bound the scanned slice, including names we cannot return.
                if index >= limit:
                    truncated = True
                    break
                try:
                    entry.name.encode("utf-8")
                except UnicodeEncodeError:
                    skipped_names += 1
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    kind = "dir"
                elif stat.S_ISREG(info.st_mode):
                    kind = "file"
                elif stat.S_ISLNK(info.st_mode):
                    kind = "link"
                else:
                    kind = "other"
                entries.append(Entry(
                    name=entry.name, kind=kind,
                    size=info.st_size if kind == "file" else None,
                    modified=info.st_mtime, hidden=entry.name.startswith("."),
                ))
    finally:
        os.close(fd)
    entries.sort(key=lambda entry: (entry.kind != "dir", entry.name.casefold()))
    return Listing(entries, truncated, skipped_names)


def list_file(anchor: Path, parts: Sequence[str]) -> Listing:
    """List one authorized file without exposing its parent directory.

    The file is reached by a walk from ``anchor`` with no link followed.
    """
    fd = open_regular(anchor, parts, raise_mount_errors=True)
    if fd is None:
        raise ListingError(404, "File not found.")
    try:
        info = os.fstat(fd)
        leaf = parts[-1]
        entry = Entry(leaf, "file", info.st_size, info.st_mtime, leaf.startswith("."))
        return Listing([entry], False)
    finally:
        os.close(fd)
