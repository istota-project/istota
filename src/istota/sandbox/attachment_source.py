"""Where the daemon may read an inbound attachment from, without following a link.

The daemon chooses an attachment's path, but usually not the file at it. Web
uploads, WhatsApp and email copies sit in the user's workspace or per-user temp
dir, both bound read-write into that user's sandbox, so any component below
those roots can be a symlink a task planted. The daemon then reads the
attachment outside every sandbox (audio staging, image normalization, OCR, the
native brain's image encoding), and following such a link is a daemon read of
any file it can open, delivered back to the task (ISSUE-610).

Two rules decide where a source may be:

- **Under one of the caller's roots.** Every component below the root is
  opened with ``O_NOFOLLOW`` (``skills._loader.open_overlay_dir``) and the leaf
  must be a regular file. A link is refused wherever it points, including back
  inside the roots: a resolve-then-compare check is a race the model can win.
- **Under no root, only where the daemon itself writes an attachment**:
  ``{NC_DATA_ROOT}/<user>/files/Talk/<name>`` (``download_talk_attachments``,
  Nextcloud kept a shared file in the sender's data dir), ``{temp_dir}/<name>``
  (its rclone branch), ``{temp_dir}/whatsapp-media/<name>`` and
  ``{temp_dir}/attachments_<id>/<name>`` (the WhatsApp and email copies kept
  when the inbox upload fails), and any directory the caller names in
  ``extra`` (a restricted task's own ``room-attachments`` copy). None of these
  is bound into any sandbox, they are matched by spelling only, and the same
  walk applies below them. The bare relative ``Talk/<name>`` is refused:
  ``download_talk_attachments`` returns it only when it found the file
  nowhere.

The root itself is opened following links, as ``open_overlay_dir`` does: it is
composed by the daemon out of its own config, and a mount reached through a
symlink is ordinary. Never raises.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath

# Where `download_talk_attachments` looks for a file Nextcloud stored in the
# sender's data dir. One name for both, so the producer and this rule agree.
NC_DATA_ROOT = Path("/mnt/nc-data")

_COPY_CHUNK = 1 << 20


@dataclass(frozen=True)
class AttachmentSource:
    """A source path split into the directory it is trusted from and the rest."""

    anchor: Path
    parts: tuple[str, ...]
    in_roots: bool


def _spellings(anchor: Path) -> list[PurePath]:
    spellings = [PurePath(os.path.normpath(anchor))]
    try:
        resolved = PurePath(anchor.resolve())
    except (OSError, RuntimeError):
        return spellings
    if resolved != spellings[0]:
        spellings.append(resolved)
    return spellings


def _parts_under(path: PurePath, anchor: Path) -> tuple[str, ...] | None:
    for spelling in _spellings(anchor):
        try:
            rel = path.relative_to(spelling)
        except ValueError:
            continue
        if rel.parts:
            return rel.parts
    return None


def _parts_under_resolved(path: PurePath, anchor: Path) -> tuple[str, ...] | None:
    """`_parts_under` for a path spelled through a link above the root.

    The deepest ancestor whose real path is the root wins. Safe only because
    the read then walks from the root itself: whatever an ancestor's links
    did, nothing outside the root is opened. Never used for the Talk fallback
    anchors, where a link aimed at `temp_dir` would reach another user's
    download.
    """
    spellings = _spellings(anchor)
    parts = path.parts
    for cut in range(len(parts) - 1, 0, -1):
        try:
            real = PurePath(os.path.realpath(PurePath(*parts[:cut])))
        except (OSError, ValueError):
            continue
        if real in spellings:
            return parts[cut:]
    return None


def locate(
    path: str | os.PathLike,
    *,
    roots: Iterable[str | os.PathLike],
    temp_dir: str | os.PathLike | None = None,
    extra: Iterable[str | os.PathLike] = (),
) -> AttachmentSource | None:
    """Which trusted directory `path` lies under, and the components below it.

    By spelling first: a link anywhere below the anchor is left for
    `open_source` to refuse rather than followed here. Only when no root
    matches that way is an ancestor's real path compared with a root, for a
    path spelled through a link above it. The innermost matching root wins.
    """
    text = os.fspath(path)
    if not text or "\0" in text:
        return None
    pure = PurePath(text)
    if not pure.is_absolute() or ".." in pure.parts:
        return None
    pure = PurePath(os.path.normpath(text))

    roots = list(roots)
    best: AttachmentSource | None = None
    for root in roots:
        if not root:
            continue
        anchor = Path(root)
        parts = _parts_under(pure, anchor)
        if parts is not None and (best is None or len(parts) < len(best.parts)):
            best = AttachmentSource(anchor, parts, True)
    if best is None:
        for root in roots:
            if not root:
                continue
            anchor = Path(root)
            parts = _parts_under_resolved(pure, anchor)
            if parts is not None and (best is None or len(parts) < len(best.parts)):
                best = AttachmentSource(anchor, parts, True)
    if best is not None:
        return best

    parts = _parts_under(pure, NC_DATA_ROOT)
    if parts is not None and len(parts) == 4 and parts[1:3] == ("files", "Talk"):
        return AttachmentSource(NC_DATA_ROOT, parts, False)
    if temp_dir:
        anchor = Path(temp_dir)
        parts = _parts_under(pure, anchor)
        if parts is not None and (
            len(parts) == 1
            or (len(parts) == 2 and (
                parts[0] == "whatsapp-media" or parts[0].startswith("attachments_")
            ))
        ):
            return AttachmentSource(anchor, parts, False)
    for directory in extra:
        if not directory:
            continue
        anchor = Path(directory)
        parts = _parts_under(pure, anchor)
        if parts is not None:
            return AttachmentSource(anchor, parts, False)
    return None


def open_source(source: AttachmentSource) -> int | None:
    """A read-only fd on the regular file `source` names, or None.

    No link is followed below the anchor. The caller closes the fd.
    """
    from istota.skills._loader import open_overlay_dir  # noqa: PLC0415 - import cost

    *dirs, leaf = source.parts
    dir_fd = open_overlay_dir(source.anchor, *dirs)
    if dir_fd is None:
        return None
    try:
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except (OSError, ValueError):
        return None
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        os.set_blocking(fd, True)
    except OSError:
        os.close(fd)
        return None
    return fd


def open_attachment(
    path: str | os.PathLike,
    *,
    roots: Iterable[str | os.PathLike],
    temp_dir: str | os.PathLike | None = None,
    extra: Iterable[str | os.PathLike] = (),
) -> tuple[int, AttachmentSource] | None:
    """`locate` then `open_source`: an fd and where it came from, or None."""
    source = locate(path, roots=roots, temp_dir=temp_dir, extra=extra)
    if source is None:
        return None
    fd = open_source(source)
    if fd is None:
        return None
    return fd, source


def copy_fd(src_fd: int, dst_fd: int) -> None:
    """Copy all of `src_fd` into `dst_fd` by offset, leaving `src_fd`'s position alone."""
    offset = 0
    while True:
        chunk = os.pread(src_fd, _COPY_CHUNK, offset)
        if not chunk:
            return
        view = memoryview(chunk)
        while view:
            written = os.write(dst_fd, view)
            view = view[written:]
        offset += len(chunk)


def write_copy(src_fd: int, parent: Path, subdir: str, name: str) -> Path | None:
    """Copy `src_fd` to `{parent}/{subdir}/{name}`, following no link below `parent`.

    For a landing directory inside a tree the model writes: `subdir` is
    created or opened ``O_NOFOLLOW``, and the file is created ``O_EXCL |
    O_NOFOLLOW`` after removing any entry already at `name` (``unlink`` does
    not follow a link at the leaf). `parent` is daemon-composed and opened
    following links. None on any refusal or failure.
    """
    for part in (subdir, name):
        if not part or "/" in part or "\0" in part or part in (".", ".."):
            return None
    try:
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        try:
            os.mkdir(subdir, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        dir_fd = os.open(subdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError:
        os.close(parent_fd)
        return None
    os.close(parent_fd)
    try:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        except OSError as e:
            if e.errno not in (errno.EISDIR, errno.EPERM):
                raise
            return None
        out_fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
            dir_fd=dir_fd,
        )
        try:
            copy_fd(src_fd, out_fd)
        except OSError:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(out_fd)
    except OSError:
        return None
    finally:
        os.close(dir_fd)
    return Path(parent) / subdir / name
