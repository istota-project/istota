"""Descriptor-relative writes below a trusted workspace or temp root."""

import os
import errno
import secrets
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


# What `link` answers on a filesystem that has no hard links.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})


def _write_chunks(fd: int, chunks: Iterable[bytes], max_bytes: int, *, sync: bool) -> None:
    with os.fdopen(fd, "wb") as file:
        size = 0
        for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                raise ListingError(413, f"file exceeds {max_bytes / (1024 * 1024):g} MB")
            file.write(chunk)
        if sync:
            file.flush()
            os.fsync(file.fileno())


def _publish_new(dir_fd: int, staging: str, name: str, written: os.stat_result) -> None:
    """Give the complete staging file its final name, never replacing an entry.

    Every task in the room can write this directory, so the staging name may
    have been swapped since it was created: nothing here follows a link, and
    the published name must be the inode that was written, or it is refused.
    """
    try:
        os.link(staging, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINKS:
            raise
        # Without hard links there is no atomic no-replace, so check, then rename.
        # Losing that race replaces one complete copy with another, never a part.
        try:
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(staging, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            raise FileExistsError(errno.EEXIST, "File exists", name) from None
    published = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if (published.st_dev, published.st_ino) != (written.st_dev, written.st_ino):
        raise OSError(errno.EEXIST, "staging file was replaced before publishing", name)


def write_new_file(
    root: Path, parts: list[str], filename: str, chunks: Iterable[bytes],
    *, max_bytes: int, numbered: bool = True, max_stem: int = DEFAULT_MAX_STEM,
) -> str:
    """Write chunks through a pinned parent, removing incomplete uploads.

    ``numbered=False`` writes the name as given or not at all: an existing
    entry raises ``FileExistsError``, for a caller that owns the destination
    and treats a concurrent writer's copy as the answer. Because that caller
    reuses whatever stands at the name, the bytes go to a dot-prefixed staging
    name first and the final name only ever holds a complete, synced file
    (ISSUE-724). A writer killed mid-write leaves the staging file behind, and
    nothing reads it. ``max_stem`` is for a caller whose name is already
    sanitised with a wider stem (the inbox).
    """
    dir_fd = open_overlay_dir(root, *parts, raise_mount_errors=True)
    if dir_fd is None:
        raise ListingError(404, "Folder not found.")
    try:
        stem, ext = filename_parts(
            safe_filename(filename, fallback="upload", max_stem=max_stem),
            max_stem=max_stem,
        )
        if not numbered:
            name = numbered_name(stem, ext, 1)
            staging = f".{secrets.token_hex(8)}.staging"
            fd = os.open(
                staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644, dir_fd=dir_fd,
            )
            try:
                try:
                    written = os.fstat(fd)
                except BaseException:
                    os.close(fd)
                    raise
                _write_chunks(fd, chunks, max_bytes, sync=True)
                _publish_new(dir_fd, staging, name, written)
            finally:
                try:
                    os.unlink(staging, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
            return name
        for n in range(1, 101):
            name = numbered_name(stem, ext, n)
            try:
                fd = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644, dir_fd=dir_fd,
                )
                break
            except FileExistsError:
                continue
        else:
            raise ListingError(409, "Too many files with this name.")
        try:
            _write_chunks(fd, chunks, max_bytes, sync=False)
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
