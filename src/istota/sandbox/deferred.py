"""Bounded JSON transfer through task-writable directories.

Walk from the filesystem root without following links, including the deferred
directory itself. Keep the directory fd for each read/modify/write operation.
Configured deferred paths must contain only real directories; a symlink at any
component is refused. File publication uses the shared exclusive staging writer.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from istota.lib.atomic_write import replace_at
from istota.sandbox.confined import open_dir, open_or_create_dirs, open_regular_at

MAX_BYTES = 8 * 1024 * 1024
MISSING = object()


@contextmanager
def directory(path: Path, *, create: bool = False):
    path = path.absolute()
    root = Path(path.anchor)
    parts = path.parts[1:]
    if create:
        fd = open_or_create_dirs(root, parts)
    else:
        fd = open_dir(root, *parts, raise_errors=True)
    if fd is None:
        raise OSError("invalid deferred directory components")
    try:
        yield fd
    finally:
        os.close(fd)


def _read_at(fd: int, name: str):
    # Distinguish absence from an unsafe object without opening a FIFO.
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return MISSING
    source = open_regular_at(fd, name)
    if source is None:
        raise OSError("deferred file is not a readable regular file")
    with os.fdopen(source, "rb") as handle:
        if os.fstat(handle.fileno()).st_size > MAX_BYTES:
            raise ValueError("deferred file exceeds size limit")
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("deferred file exceeds size limit")
    return json.loads(data.decode("utf-8"))


def read_json(path: Path, *, missing=None):
    try:
        with directory(path.parent) as fd:
            data = _read_at(fd, path.name)
            return missing if data is MISSING else data
    except FileNotFoundError:
        return missing


def _write_at(fd: int, name: str, data) -> None:
    encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ValueError("deferred file exceeds size limit")
    replace_at(fd, name, encoded, mode=0o600)


def write_json(path: Path, data) -> None:
    with directory(path.parent, create=True) as fd:
        _write_at(fd, path.name, data)


def append_json(path: Path, entries: list) -> None:
    with directory(path.parent, create=True) as fd:
        try:
            existing = _read_at(fd, path.name)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            existing = None
        if not isinstance(existing, list):
            existing = []
        _write_at(fd, path.name, existing + entries)


def unlink(path: Path, *, missing_ok: bool = True) -> None:
    try:
        with directory(path.parent) as fd:
            os.unlink(path.name, dir_fd=fd)
    except FileNotFoundError:
        if not missing_ok:
            raise


def exists(path: Path) -> bool:
    try:
        with directory(path.parent) as fd:
            os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        return True
    except OSError:
        return False


def names(path: Path) -> list[str]:
    try:
        with directory(path) as fd:
            return os.listdir(fd)
    except OSError:
        return []
