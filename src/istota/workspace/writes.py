"""Descriptor-relative writes below a trusted workspace or temp root."""

import os
from pathlib import Path


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


def write_new_file(root: Path, parts: tuple[str, ...], data: bytes) -> Path:
    """Create a new file without following links or overwriting an existing file."""
    _validate_parts(parts)
    if not parts:
        raise ValueError("missing filename")
    dir_fd = open_or_create_dirs(root, parts[:-1])
    try:
        fd = os.open(
            parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=dir_fd,
        )
        with os.fdopen(fd, "wb") as file:
            file.write(data)
    finally:
        os.close(dir_fd)
    return root.joinpath(*parts)
