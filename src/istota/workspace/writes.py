"""Descriptor-relative writes below a trusted workspace or temp root."""

import os
import errno
import stat
from pathlib import Path
from collections.abc import Iterable

from istota.lib.filenames import (
    DEFAULT_MAX_STEM, filename_parts, numbered_name, safe_filename,
)
from istota.skills._loader import open_overlay_dir
from istota.workspace.listing import ListingError


def _validate_parts(parts: list[str] | tuple[str, ...]) -> None:
    for part in parts:
        if not part or "/" in part or "\0" in part or part in (".", ".."):
            raise ValueError("invalid path component")


def open_or_create_dirs(root: Path, parts: list[str] | tuple[str, ...]) -> int:
    """Open directories below a trusted root, creating missing components.

    Only the configured root may follow symlinks. Every component below it is
    opened relative to its parent descriptor with O_NOFOLLOW. The caller owns
    the returned descriptor; ordinary filesystem refusals raise OSError.
    """
    _validate_parts(parts)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            nxt = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd,
            )
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_new_file(
    root: Path, parts: list[str], filename: str, chunks: Iterable[bytes],
    *, max_bytes: int, numbered: bool = True, max_stem: int = DEFAULT_MAX_STEM,
) -> str:
    """Write chunks through a pinned parent, removing incomplete uploads.

    ``numbered=False`` writes the name as given or not at all: an existing
    entry raises ``FileExistsError``, for a caller that owns the destination
    and treats a concurrent writer's copy as the answer. ``max_stem`` is for a
    caller whose name is already sanitised with a wider stem (the inbox).
    """
    dir_fd = open_overlay_dir(root, *parts, raise_mount_errors=True)
    if dir_fd is None:
        raise ListingError(404, "Folder not found.")
    try:
        stem, ext = filename_parts(
            safe_filename(filename, fallback="upload", max_stem=max_stem),
            max_stem=max_stem,
        )
        for n in range(1, 101 if numbered else 2):
            name = numbered_name(stem, ext, n)
            try:
                fd = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644, dir_fd=dir_fd,
                )
                break
            except FileExistsError:
                if not numbered:
                    raise
                continue
        else:
            raise ListingError(409, "Too many files with this name.")
        try:
            with os.fdopen(fd, "wb") as file:
                size = 0
                for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise ListingError(413, f"file exceeds {max_bytes / (1024 * 1024):g} MB")
                    file.write(chunk)
        except BaseException:
            os.unlink(name, dir_fd=dir_fd)
            raise
    finally:
        os.close(dir_fd)
    return name


def make_folder(root: Path, parts: list[str], name: str) -> str:
    fd = open_overlay_dir(root, *parts, raise_mount_errors=True)
    if fd is None:
        raise ListingError(404, "Folder not found.")
    try:
        name = safe_filename(name, fallback="folder")
        try:
            os.mkdir(name, 0o755, dir_fd=fd)
        except FileExistsError as exc:
            raise ListingError(409, "A file or folder already exists.") from exc
    finally:
        os.close(fd)
    return name


def delete_entry(root: Path, parts: list[str], name: str) -> None:
    """Remove one regular file or empty directory through its pinned parent."""
    try:
        _validate_parts([name])
    except ValueError as exc:
        raise ListingError(422, "Invalid file name.") from exc
    fd = open_overlay_dir(root, *parts, raise_mount_errors=True)
    if fd is None:
        raise ListingError(404, "Folder not found.")
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            os.unlink(name, dir_fd=fd)
        elif stat.S_ISDIR(info.st_mode):
            try:
                os.rmdir(name, dir_fd=fd)
            except OSError as exc:
                if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                    raise ListingError(409, "Folder is not empty. Delete its contents first.") from exc
                raise
        else:
            raise ListingError(422, "Only regular files and empty folders can be deleted.")
    except FileNotFoundError as exc:
        raise ListingError(404, "File or folder not found.") from exc
    except PermissionError as exc:
        raise ListingError(403, "You do not have permission to delete this item.") from exc
    finally:
        os.close(fd)
