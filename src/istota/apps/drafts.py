"""App drafts: the folders under ``{bot_dir}/apps/`` in a user's workspace, read by descriptor.

Every task can write that folder, so a draft is untrusted and any component of
it can turn into a link between two system calls. Reading goes through
``sandbox.confined``: anchored at ``Config.workspace_root(user_id)``, every
directory below it opened ``O_NOFOLLOW`` relative to the one above, every file
opened ``O_NOFOLLOW | O_NONBLOCK`` and checked regular on the fd. A link
anywhere in a draft refuses the whole read rather than being skipped, so what
the user reviews is never quietly less than what the folder holds.

Not read at all: the top-level ``data/`` (the app's own files, up to
``files_max_bytes``) and ``preview/`` folders, and any entry whose name starts
with a dot (``.git``, ``.DS_Store``). Nothing reads those, so a link among them
is never followed.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import tomllib
from dataclasses import dataclass

from istota.apps.manifest import APP_ID, flatten
from istota.sandbox.confined import open_dir, open_regular_at, valid_part

logger = logging.getLogger(__name__)

#: Files and folders in one draft, counted together so a tree of empty
#: folders is bounded as well.
MAX_DRAFT_FILES = 200
SKIPPED_TOP_LEVEL = ("data", "preview")
MANIFEST = "manifest.toml"


class DraftError(Exception):
    """A draft that cannot be read, with the sentence a user is shown."""


class DraftNotFound(DraftError):
    pass


@dataclass(frozen=True)
class DraftSummary:
    app_id: str
    name: str | None
    has_manifest: bool


def _apps_dir_fd(config, user_id: str) -> int | None:
    """An fd on ``{workspace}/Users/{user_id}/{bot_dir}/apps``, or None when there is none."""
    root = config.workspace_root(user_id)
    if root is None:
        return None
    return open_dir(root, config.bot_dir_name, "apps")


def _read_fd(fd: int, budget: int, path: str) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, budget - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > budget:
            raise DraftError(f"The app folder is too large to install (at {path}).")
    return b"".join(chunks)


class _Reader:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.total = 0
        self.entries = 0
        self.files: dict[str, bytes] = {}

    def walk(self, dir_fd: int, prefix: str) -> None:
        with os.scandir(dir_fd) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            if not prefix and name in SKIPPED_TOP_LEVEL:
                continue
            path = f"{prefix}{name}"
            if not valid_part(name):
                raise DraftError(f"The app folder has a file named in a way it cannot read: {path!r}.")
            self.entries += 1
            if self.entries > MAX_DRAFT_FILES:
                raise DraftError(f"The app folder has more than {MAX_DRAFT_FILES} files and folders.")
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise DraftError(f"The app folder could not be read at {path}.") from exc
            if stat.S_ISLNK(mode):
                raise DraftError(f"The app folder contains a link at {path}; links are not allowed.")
            if stat.S_ISDIR(mode):
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
                except OSError as exc:
                    raise DraftError(f"The app folder changed while it was read, at {path}.") from exc
                try:
                    self.walk(child, f"{path}/")
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(mode):
                raise DraftError(f"The app folder has {path}, which is not a regular file.")
            fd = open_regular_at(dir_fd, name)
            if fd is None:
                raise DraftError(f"The app folder changed while it was read, at {path}.")
            try:
                data = _read_fd(fd, self.max_bytes - self.total, path)
            finally:
                os.close(fd)
            self.total += len(data)
            self.files[path] = data


def read_draft(config, user_id: str, app_id: str) -> dict[str, bytes]:
    """Every file in the draft ``app_id``, as bytes by relative posix path. Raises :class:`DraftError`."""
    if not isinstance(app_id, str) or not APP_ID.fullmatch(app_id):
        raise DraftError("That is not an app id.")
    apps_fd = _apps_dir_fd(config, user_id)
    if apps_fd is None:
        raise DraftNotFound(f"There is no app folder named {app_id}.")
    try:
        try:
            mode = os.stat(app_id, dir_fd=apps_fd, follow_symlinks=False).st_mode
        except FileNotFoundError as exc:
            raise DraftNotFound(f"There is no app folder named {app_id}.") from exc
        if stat.S_ISLNK(mode):
            raise DraftError(f"The app folder {app_id} is a link; links are not allowed.")
        if not stat.S_ISDIR(mode):
            raise DraftNotFound(f"There is no app folder named {app_id}.")
        try:
            draft_fd = os.open(app_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=apps_fd)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise DraftNotFound(f"There is no app folder named {app_id}.") from exc
            raise DraftError(f"The app folder {app_id} changed while it was read.") from exc
    finally:
        os.close(apps_fd)
    reader = _Reader(config.apps.bundle_max_bytes * 2)
    try:
        reader.walk(draft_fd, "")
    except OSError as exc:
        raise DraftError(f"The app folder {app_id} could not be read.") from exc
    finally:
        os.close(draft_fd)
    return reader.files


def _manifest_name(draft_fd: int) -> tuple[str | None, bool]:
    fd = open_regular_at(draft_fd, MANIFEST)
    if fd is None:
        return None, False
    try:
        data = _read_fd(fd, 64 * 1024, MANIFEST)
    except (DraftError, OSError):
        return None, True
    finally:
        os.close(fd)
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None, True
    name = parsed.get("name")
    if not isinstance(name, str) or not flatten(name):
        return None, True
    return flatten(name)[:40], True


def list_drafts(config, user_id: str) -> list[DraftSummary]:
    """Each folder under the user's apps folder that is named like an app id, by id.

    Links and anything else are skipped without a word: listing is not the
    review, and ``read_draft`` refuses them when it matters.
    """
    apps_fd = _apps_dir_fd(config, user_id)
    if apps_fd is None:
        return []
    drafts = []
    try:
        with os.scandir(apps_fd) as scan:
            names = sorted(entry.name for entry in scan if APP_ID.fullmatch(entry.name))
        for name in names:
            try:
                draft_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=apps_fd)
            except OSError:
                continue
            try:
                title, has_manifest = _manifest_name(draft_fd)
            finally:
                os.close(draft_fd)
            drafts.append(DraftSummary(app_id=name, name=title, has_manifest=has_manifest))
    except OSError:
        logger.warning("apps_drafts_unreadable user=%s", user_id)
    finally:
        os.close(apps_fd)
    return drafts
