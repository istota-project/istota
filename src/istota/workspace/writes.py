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
from istota.sandbox.confined import open_dir, open_or_create_dirs, valid_part
from istota.workspace.listing import ListingError


def open_or_create_dir_below(root: Path, path: Path) -> int:
    """``open_or_create_dirs`` for a ``path`` the caller composed under ``root``.

    The components are ``path``'s own, taken lexically, so nothing below
    ``root`` is resolved: a link at any of them fails the open. ``ValueError``
    when ``path`` is not under ``root`` as written.
    """
    return open_or_create_dirs(root, Path(path).relative_to(root).parts)


# What `link` answers on a filesystem that has no hard links.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})


def _write_chunks(fd: int, chunks: Iterable[bytes], max_bytes: int, *, sync: bool) -> os.stat_result:
    """Write and close ``fd``, returning its ``fstat`` once every byte is in."""
    with os.fdopen(fd, "wb") as file:
        size = 0
        for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                raise ListingError(413, f"file exceeds {max_bytes / (1024 * 1024):g} MB")
            file.write(chunk)
        file.flush()
        if sync:
            os.fsync(file.fileno())
        return os.fstat(file.fileno())


def publish_new(dir_fd: int, staging: str, name: str, written: os.stat_result) -> None:
    """Give the complete staging file its final name, never replacing an entry.

    Every task in the room can write this directory, so the staging name may
    have been swapped since it was created: nothing here follows a link, and
    the published name must be the inode that was written, or it is refused.
    ``written`` is the staging file's ``fstat`` taken after the last byte, so
    it carries the final size. A refusal leaves nothing at ``name``.
    """
    try:
        os.link(staging, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINKS:
            raise
        _rename_new(dir_fd, staging, name, written)
        return
    published = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not _same_file(published, written):
        # The link this call made: a name holding somebody else's bytes would
        # block every later publish of it (ISSUE-972).
        os.unlink(name, dir_fd=dir_fd)
        raise OSError(errno.EEXIST, "staging file was replaced before publishing", name)


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _rename_new(dir_fd: int, staging: str, name: str, written: os.stat_result) -> None:
    """``publish_new`` where there are no hard links: check, then rename.

    There is no atomic no-replace here, and losing either race replaces one
    complete copy with another, never a part. The checks are on the staging
    name before the rename, because rclone's FUSE mount, which has no hard
    links, numbers a file by its path: the renamed file has a new inode, and a
    check after the rename refused every publish (ISSUE-972). Numbering by
    path also means a file planted at the staging name keeps the number, so
    the type and size are compared too; a planted regular file of the same
    size still passes, as losing the check-then-rename race would.
    """
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(errno.EEXIST, "File exists", name)
    staged = os.stat(staging, dir_fd=dir_fd, follow_symlinks=False)
    if not (
        _same_file(staged, written)
        and stat.S_ISREG(staged.st_mode)
        and staged.st_size == written.st_size
    ):
        raise OSError(errno.EEXIST, "staging file was replaced before publishing", name)
    os.rename(staging, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


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
    dir_fd = open_dir(root, *parts, raise_mount_errors=True)
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
                written = _write_chunks(fd, chunks, max_bytes, sync=True)
                publish_new(dir_fd, staging, name, written)
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
    fd = open_dir(root, *parts, raise_mount_errors=True)
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
    if not valid_part(name):
        raise ListingError(422, "Invalid file name.")
    fd = open_dir(root, *parts, raise_mount_errors=True)
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
