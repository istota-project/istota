"""Opening a file in a tree a task can write, by descriptor, never by name.

Every user workspace, room folder and per-user temp dir is bound read-write
into some task's sandbox, so any component below the root can become a
symlink between one system call and the next. A check on a resolved path
followed by a second open of the same name (``realpath`` + ``is_relative_to``
then ``open``, or ``FileResponse(path)``) loses to a swap in between
(ISSUE-341, ISSUE-775, ISSUE-776, ISSUE-782). The rule here is containment by
construction:

- **Anchor at a root the daemon composed from its own config**:
  ``Config.workspace_root(user_id)``, the mount, a temp dir. The anchor is
  opened following links, since a mount reached through a symlink is
  ordinary. Never anchor at a path derived from user data or at a resolved
  descendant: a no-follow walk follows a link at its anchor (#775's review).
- **Walk every component below it with ``O_NOFOLLOW``**, relative to the
  descriptor above (``open_dir``), and open the leaf ``O_NOFOLLOW |
  O_NONBLOCK`` with ``S_ISREG`` on the fd (``open_regular_at``). A link is
  refused wherever it points, including back inside the root.
- **Keep the descriptor and use it**: read, ``fstat``, ``scandir``,
  ``rename`` and ``unlink`` with ``dir_fd``. Serving over HTTP is
  ``webui.fd_response.fd_file_response``, which streams an open fd and lives
  in the web package because it needs Starlette.

``tests/test_confined_file_access.py`` fails on ``FileResponse`` anywhere in
``src/`` and on a new resolve-then-compare function outside its allowlist.

stdlib-only and imports nothing from ``istota``, so the daemon, the skill
CLIs and the web process can all take it without the skills loader's import
cost.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterable
from pathlib import Path, PurePath

_MOUNT_ERRNOS = frozenset({errno.ENOTCONN, errno.EIO})


def valid_part(part: object) -> bool:
    """Whether ``part`` is one ordinary path component a walk may descend into.

    A ``/`` would have its interior walked by a single ``os.open``, where
    ``O_NOFOLLOW`` covers only the last component. ``.`` and ``..`` climb, and
    a NUL makes ``os.open`` raise ``ValueError`` rather than ``OSError``.
    """
    return (
        isinstance(part, str) and bool(part) and "/" not in part
        and "\0" not in part and part not in (".", "..")
    )


def valid_parts(parts: Iterable[object]) -> bool:
    return all(valid_part(part) for part in parts)


def open_dir(
    root: Path, *parts: str, raise_mount_errors: bool = False,
    raise_errors: bool = False,
) -> int | None:
    """An fd on ``{root}/{parts...}``, or None if any component below the root
    is not a plain directory reached directly from the one above it.

    The fd is pinned to an inode, so whatever is renamed over the path
    afterwards, ``scandir`` and ``openat`` through it still see the directory
    that passed. This is deliberately stricter than a resolve-then-compare
    check (``skills._loader.contained_overlay_dir``), which accepts a link
    landing back inside the root and is true only until something moves.

    The caller closes the fd. By default every ``OSError`` is a None.
    ``raise_mount_errors`` preserves ENOTCONN/EIO for callers reporting a
    disconnected mount; ``raise_errors`` preserves every directory-open error
    for callers that must tell an unavailable root from an unsafe one.
    """
    if not valid_parts(parts):
        return None
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        if raise_errors or (raise_mount_errors and exc.errno in _MOUNT_ERRNOS):
            raise
        return None
    try:
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError as exc:
        os.close(fd)
        if raise_errors or (raise_mount_errors and exc.errno in _MOUNT_ERRNOS):
            raise
        return None
    return fd


def open_regular_at(
    dir_fd: int, name: str, *, raise_mount_errors: bool = False,
) -> int | None:
    """A read-only fd on the regular file ``name`` in ``dir_fd``, or None.

    ``O_NONBLOCK`` so a FIFO planted at the name cannot block the open, then
    ``S_ISREG`` on the fd, then blocking mode back on. ``raise_mount_errors``
    as for ``open_dir``. The caller closes the fd.
    """
    if not valid_part(name):
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        if raise_mount_errors and exc.errno in _MOUNT_ERRNOS:
            raise
        return None
    except ValueError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        os.set_blocking(fd, True)
    except OSError:
        os.close(fd)
        return None
    return fd


def open_regular(
    root: Path, parts: Iterable[str], *, raise_mount_errors: bool = False,
) -> int | None:
    """``open_dir`` over all but the last of ``parts``, then ``open_regular_at``.

    None for an empty ``parts``, a refused walk, or a leaf that is not a
    regular file. The caller closes the fd.
    """
    parts = tuple(parts)
    if not parts:
        return None
    *dirs, leaf = parts
    dir_fd = open_dir(root, *dirs, raise_mount_errors=raise_mount_errors)
    if dir_fd is None:
        return None
    try:
        return open_regular_at(dir_fd, leaf, raise_mount_errors=raise_mount_errors)
    finally:
        os.close(dir_fd)


def open_or_create_dirs(root: Path, parts: list[str] | tuple[str, ...]) -> int:
    """``open_dir`` that creates missing components, mode 0755.

    ``root`` is created with its parents if missing, following links, as the
    anchor always is. The caller owns the returned descriptor. ``ValueError``
    for an invalid component; ordinary filesystem refusals raise ``OSError``.
    """
    if not valid_parts(parts):
        raise ValueError("invalid path component")
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def parts_below(path: str | os.PathLike, root: Path) -> tuple[str, ...] | None:
    """``path``'s components below ``root`` by spelling, for a walk from ``root``.

    ``path`` is normalized, never resolved: a link below the root is left for
    the walk to refuse. ``root`` is matched as written and as resolved, since
    the daemon composes it and a mount reached through a link is ordinary.
    ``()`` for the root itself; None when ``path`` is not below it or a
    component is not one ``valid_part`` accepts.
    """
    text = os.fspath(path)
    if not text or "\0" in text:
        return None
    target = PurePath(os.path.normpath(text))
    spellings = [PurePath(os.path.normpath(root))]
    try:
        resolved = PurePath(Path(root).resolve())
    except (OSError, RuntimeError):
        resolved = None
    if resolved is not None and resolved != spellings[0]:
        spellings.append(resolved)
    for spelling in spellings:
        try:
            parts = target.relative_to(spelling).parts
        except ValueError:
            continue
        return parts if valid_parts(parts) else None
    return None
