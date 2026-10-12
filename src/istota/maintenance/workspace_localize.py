"""Moving a mounted install's workspace onto local storage.

Three steps, each its own CLI verb, which `istota-stack localize-workspace`
runs in order with the stack's writers stopped:

- `copy_workspace` (`istota workspace localize`): copies the workspace's own
  top-level entries (`Users/`, `Channels/`, `Groups/`, `PERSONA.md`) from the
  rclone mount to the local tree. Nothing else at the root of the bot's
  Nextcloud files is the workspace's. Every level is opened `O_NOFOLLOW`
  relative to the one above it, because every user's tree is bound read-write
  into that user's sandbox and anything in it may be planted; a symlink, FIFO,
  socket or device is skipped and reported, never followed or copied. File
  data moves with `copyfileobj` on the two descriptors, with no xattr call
  (#694). mtimes are kept, modes are the workspace's 0644 and 0755.
  `write_notes` then leaves `MOVED.md` in each user's bot folder on the mount,
  the only write to the source.
- `localize_config` (`istota workspace localize-config`): the wizard's
  `config.toml` with `workspace_path` set to the local tree,
  `nextcloud_mount_path` removed and `auto_share_bot_dir` off, the previous
  file kept as `config.toml.pre-localize`. A config `istota apply` wrote is
  refused: the plan owns it and would undo the edit.
- `reindex_moved_workspace` (`istota workspace reindex --forget-root`): runs the
  memory reindex for every user on the new tree, then `forget_root` deletes the
  chunk rows still keyed under the old root. `index_file` keys a row on the
  file's resolved path and replaces only the rows under the path it is given,
  and `reindex_all` drops no row for a file it did not find (skill overlays
  apart), so without the delete every file indexed under the mount would be
  served twice by search, once from a path that no longer exists.

Exit codes for the copy: 0 copied, 1 refused before anything was written,
2 copied with entries skipped (the report names them), 3 an I/O error mid-copy
(the report says how far it got).
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import stat
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from istota.lib import toml_write

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_SKIPPED = 2
EXIT_IO = 3

TOP_LEVEL_DIRS = ("Users", "Channels", "Groups")
TOP_LEVEL_FILES = ("PERSONA.md",)
NOTE_NAME = "MOVED.md"
PRE_LOCALIZE_SUFFIX = ".pre-localize"
#: The first line `istota apply` writes into a config it owns (`apply.CONFIG_HEADER`).
APPLY_HEADER_MARK = "written by `istota apply` from a plan"

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class LocalizeRefused(Exception):
    """A refusal before anything was written."""


class _CopyError(Exception):
    def __init__(self, path: str, reason: str):
        super().__init__(f"{path}: {reason}")


@dataclass
class EntryCount:
    files: int = 0
    bytes: int = 0
    dirs: int = 0
    unchanged: int = 0


@dataclass
class Report:
    source: str
    destination: str
    dry_run: bool
    entries: dict[str, EntryCount] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    noted: list[str] = field(default_factory=list)
    note_failures: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None

    def exit_code(self) -> int:
        if self.error is not None:
            return EXIT_IO
        if self.skipped:
            return EXIT_SKIPPED
        return EXIT_OK

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "destination": self.destination,
            "dry_run": self.dry_run,
            "entries": {name: vars(count) for name, count in self.entries.items()},
            "skipped": [{"path": path, "reason": reason} for path, reason in self.skipped],
            "noted": self.noted,
            "note_failures": [{"user": user, "reason": reason} for user, reason in self.note_failures],
            "error": self.error,
            "exit_code": self.exit_code(),
        }

    def summary_lines(self) -> list[str]:
        verb = "would copy" if self.dry_run else "copied"
        lines = []
        for name, count in self.entries.items():
            line = f"{name}: {verb} {count.files} files, {count.bytes} bytes, {count.dirs} directories"
            if count.unchanged:
                line += f" ({count.unchanged} files already there, kept)"
            lines.append(line)
        for path, reason in self.skipped:
            lines.append(f"skipped {path}: {reason}")
        for user, reason in self.note_failures:
            lines.append(f"no {NOTE_NAME} for {user}: {reason}")
        if self.error is not None:
            lines.append(f"stopped: {self.error}")
        return lines


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return "not a regular file or directory"


def _is_empty_dir(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is None
    except FileNotFoundError:
        return True


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _set_times(fd: int, info: os.stat_result) -> None:
    if os.utime in os.supports_fd:
        os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))


class _Copier:
    def __init__(self, report: Report, *, dry_run: bool, resume: bool, bot_dir_name: str):
        self.report = report
        self.dry_run = dry_run
        self.resume = resume
        self.bot_dir_name = bot_dir_name

    def _is_our_note(self, rel: tuple[str, ...], name: str) -> bool:
        """`Users/<uid>/<bot_dir>/MOVED.md`, which an earlier run left on the
        source and which the local tree must not carry."""
        return (
            name == NOTE_NAME and len(rel) == 3
            and rel[0] == "Users" and rel[2] == self.bot_dir_name
        )

    def copy_file(self, src_dir: int, dst_dir: int | None, name: str, rel: str,
                  count: EntryCount) -> None:
        try:
            src = os.open(name, _READ_FLAGS, dir_fd=src_dir)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                self.report.skipped.append((rel, "symlink"))
                return
            raise
        try:
            info = os.fstat(src)
            if not stat.S_ISREG(info.st_mode):
                self.report.skipped.append((rel, _kind(info.st_mode)))
                return
            if dst_dir is None:
                count.files += 1
                count.bytes += info.st_size
                return
            if self.resume:
                try:
                    there = os.stat(name, dir_fd=dst_dir, follow_symlinks=False)
                except FileNotFoundError:
                    there = None
                if (
                    there is not None and stat.S_ISREG(there.st_mode)
                    and there.st_size == info.st_size
                    and there.st_mtime_ns == info.st_mtime_ns
                ):
                    count.unchanged += 1
                    return
            dst = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644, dir_fd=dst_dir)
            try:
                os.fchmod(dst, 0o644)
                with os.fdopen(os.dup(src), "rb", closefd=True) as reader, \
                        os.fdopen(os.dup(dst), "wb", closefd=True) as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
                _set_times(dst, info)
            finally:
                os.close(dst)
            count.files += 1
            count.bytes += info.st_size
        finally:
            os.close(src)

    def copy_dir(self, src_dir: int, dst_dir: int | None, rel: tuple[str, ...],
                 count: EntryCount) -> None:
        with os.scandir(src_dir) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
        for entry in entries:
            name = entry.name
            path = "/".join((*rel, name))
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    if self._is_our_note(rel, name):
                        continue
                    self.copy_file(src_dir, dst_dir, name, path, count)
                elif stat.S_ISDIR(info.st_mode):
                    self.copy_subdir(src_dir, dst_dir, name, (*rel, name), count)
                else:
                    self.report.skipped.append((path, _kind(info.st_mode)))
            except _CopyError:
                raise
            except OSError as exc:
                raise _CopyError(path, exc.strerror or str(exc)) from exc

    def copy_subdir(self, src_parent: int, dst_parent: int | None, name: str,
                    rel: tuple[str, ...], count: EntryCount) -> None:
        path = "/".join(rel)
        try:
            src = os.open(name, _DIR_FLAGS, dir_fd=src_parent)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                self.report.skipped.append((path, "symlink"))
                return
            raise
        dst = None
        try:
            info = os.fstat(src)
            if dst_parent is not None:
                try:
                    os.mkdir(name, 0o755, dir_fd=dst_parent)
                except FileExistsError:
                    pass
                try:
                    dst = os.open(name, _DIR_FLAGS, dir_fd=dst_parent)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        self.report.skipped.append((path, "the destination has something other than a directory here"))
                        return
                    raise
            count.dirs += 1
            self.copy_dir(src, dst, rel, count)
            if dst is not None:
                _set_times(dst, info)
        finally:
            os.close(src)
            if dst is not None:
                os.close(dst)


def copy_workspace(
    source: Path, destination: Path, *, bot_dir_name: str,
    dry_run: bool = False, resume: bool = False,
) -> Report:
    """Copy the workspace's own top-level entries from `source` to `destination`.

    Raises `LocalizeRefused` before anything is written; an `OSError` partway
    is recorded on the report rather than raised.
    """
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    if not source.is_dir():
        raise LocalizeRefused(f"{source} is not a directory")
    if _overlaps(Path(os.path.realpath(source)), Path(os.path.realpath(destination))):
        raise LocalizeRefused(f"{source} and {destination} overlap")
    if not resume and not _is_empty_dir(destination):
        raise LocalizeRefused(
            f"{destination} already holds files; pass --resume to continue an earlier copy"
        )
    report = Report(str(source), str(destination), dry_run)
    copier = _Copier(report, dry_run=dry_run, resume=resume, bot_dir_name=bot_dir_name)
    src_root = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    dst_root = None
    try:
        if not dry_run:
            from istota.workspace.writes import open_or_create_dirs

            dst_root = open_or_create_dirs(destination, [])
            os.fchmod(dst_root, 0o755)
        for name in (*TOP_LEVEL_DIRS, *TOP_LEVEL_FILES):
            try:
                info = os.stat(name, dir_fd=src_root, follow_symlinks=False)
            except FileNotFoundError:
                continue
            count = report.entries.setdefault(name, EntryCount())
            try:
                if name in TOP_LEVEL_DIRS and stat.S_ISDIR(info.st_mode):
                    copier.copy_subdir(src_root, dst_root, name, (name,), count)
                elif name in TOP_LEVEL_FILES and stat.S_ISREG(info.st_mode):
                    copier.copy_file(src_root, dst_root, name, name, count)
                else:
                    report.skipped.append((name, _kind(info.st_mode)))
            except _CopyError as exc:
                report.error = str(exc)
                break
            except OSError as exc:
                report.error = f"{name}: {exc.strerror or exc}"
                break
    finally:
        os.close(src_root)
        if dst_root is not None:
            os.close(dst_root)
    return report


def note_text(bot_name: str, when: datetime) -> str:
    return (
        f"These files now live on {bot_name}'s own server, and you reach them in "
        f"{bot_name}'s web UI. This folder is a frozen copy from "
        f"{when:%Y-%m-%d}: a change made here does not reach {bot_name}.\n"
    )


def write_notes(source: Path, report: Report, *, bot_dir_name: str, text: str) -> None:
    """`MOVED.md` in each `Users/<uid>/<bot_dir>/` on the source.

    Users are read off the source's `Users/` directory, opened without
    following a link at any level, and a user without a bot folder gets none.
    """
    users_fd = None
    try:
        root = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
        try:
            users_fd = os.open("Users", _DIR_FLAGS, dir_fd=root)
        finally:
            os.close(root)
    except OSError:
        return
    data = text.encode("utf-8")
    try:
        with os.scandir(users_fd) as listing:
            names = sorted(entry.name for entry in listing if entry.is_dir(follow_symlinks=False))
        for user in names:
            user_fd = bot_fd = None
            try:
                user_fd = os.open(user, _DIR_FLAGS, dir_fd=users_fd)
                try:
                    bot_fd = os.open(bot_dir_name, _DIR_FLAGS, dir_fd=user_fd)
                except FileNotFoundError:
                    continue
                fd = os.open(NOTE_NAME, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644, dir_fd=bot_fd)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                report.noted.append(user)
            except OSError as exc:
                report.note_failures.append((user, exc.strerror or str(exc)))
            finally:
                for fd in (bot_fd, user_fd):
                    if fd is not None:
                        os.close(fd)
    finally:
        os.close(users_fd)


def write_report(report: Report, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --- the config ---------------------------------------------------------------


def localized_config_text(text: str, workspace: str) -> str | None:
    """The config text with the workspace local, or None when it already is.

    Raises `LocalizeRefused` for a config `istota apply` owns, and for one with
    no Nextcloud to keep (a local install has nothing to move).
    """
    head = text.splitlines()[:5]
    if any(APPLY_HEADER_MARK in line for line in head):
        raise LocalizeRefused(
            "config.toml was written by `istota apply`; the plan owns it. Move by changing the plan: "
            f"workspace_path = \"{workspace}\", no nextcloud_mount_path, "
            "[nextcloud] auto_share_bot_dir = false, and STORAGE local"
        )
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LocalizeRefused(f"config.toml does not parse: {exc}") from exc
    nextcloud = document.get("nextcloud")
    if not isinstance(nextcloud, dict) or not nextcloud.get("url"):
        raise LocalizeRefused("config.toml has no [nextcloud] url: there is no mount to move from")
    if "nextcloud_mount_path" not in document:
        if document.get("workspace_path") == workspace:
            return None
        raise LocalizeRefused(
            "config.toml has no nextcloud_mount_path: the workspace is not the mount"
        )
    document.pop("nextcloud_mount_path")
    document["workspace_path"] = workspace
    nextcloud["auto_share_bot_dir"] = False
    comments = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        comments.append(line)
    header = "\n".join(comments) + "\n\n" if comments else ""
    return header + toml_write.dumps(document)


def _read_config(path: Path) -> tuple[bytes, os.stat_result]:
    fd = os.open(path, _READ_FLAGS)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise LocalizeRefused(f"{path} is not a regular file")
        return handle.read(), info


def read_config_text(path: Path) -> str:
    return _read_config(path)[0].decode("utf-8")


def localize_config(path: Path, workspace: str) -> bool:
    """Rewrite `path` for a local workspace, keeping the old one beside it.

    Returns False when the config is already local. The backup is written
    once, so a second run keeps the original. Raises `LocalizeRefused`.
    """
    raw, info = _read_config(path)
    text = raw.decode("utf-8")
    new = localized_config_text(text, workspace)
    if new is None:
        return False
    mode = stat.S_IMODE(info.st_mode)
    backup = path.with_name(path.name + PRE_LOCALIZE_SUFFIX)
    try:
        out = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    except FileExistsError:
        out = None
    if out is not None:
        with os.fdopen(out, "wb") as handle:
            handle.write(raw)
            os.fchmod(handle.fileno(), mode)
    tmp = path.with_name(f".{path.name}.localize.{os.getpid()}")
    out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(out, "wb") as handle:
            handle.write(new.encode("utf-8"))
            os.fchmod(handle.fileno(), mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return True


# --- the memory index -----------------------------------------------------------


def _root_prefix(old_root: str) -> str:
    root = str(old_root).rstrip("/")
    if not root or not os.path.isabs(root):
        raise ValueError(f"{old_root!r} is not an absolute directory below /")
    return root + "/"


def forget_root(conn: sqlite3.Connection, old_root: str, user_ids: Iterable[str]) -> int:
    """Delete `user_ids`' memory chunks whose source path is under `old_root`.

    Matched on a path boundary, so `/mnt/shared` leaves `/mnt/shared-other`
    alone. Each source goes through `_delete_source_chunks`, which clears its
    vector rows too (the FTS rows follow by trigger). Returns rows removed.
    """
    from istota.memory.search import _delete_source_chunks

    prefix = _root_prefix(old_root)
    removed = 0
    for user_id in user_ids:
        rows = conn.execute(
            "SELECT DISTINCT source_type, source_id FROM memory_chunks "
            "WHERE user_id = ? AND substr(source_id, 1, ?) = ?",
            (user_id, len(prefix), prefix),
        ).fetchall()
        for source_type, source_id in rows:
            removed += _delete_source_chunks(conn, user_id, source_type, source_id)
    return removed


def _reindex_playbooks(conn: sqlite3.Connection, config, user_id: str) -> int:
    """Index each playbook in the user's bot folder, as the sleep cycle does.

    `reindex_all` does not cover playbooks, which only the sleep cycle indexes
    when it writes one, so the delete below would otherwise take them out of
    recall until each is derived again.
    """
    from istota.memory.search import index_file
    from istota.memory.sleep_cycle import _playbook_searchable_body
    from istota.skills._loader import open_overlay_dir

    dir_fd = open_overlay_dir(
        config.workspace_path / "Users" / user_id, config.bot_dir_name, "playbooks",
    )
    if dir_fd is None:
        return 0
    playbooks_dir = config.workspace_path / "Users" / user_id / config.bot_dir_name / "playbooks"
    chunks = 0
    try:
        with os.scandir(dir_fd) as listing:
            names = sorted(e.name for e in listing if e.name.endswith(".md") and e.is_file(follow_symlinks=False))
        for name in names:
            try:
                fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
            except OSError:
                continue
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    continue
                text = handle.read().decode("utf-8", errors="replace")
            body = _playbook_searchable_body(text)
            if body.strip():
                chunks += index_file(conn, user_id, str(playbooks_dir / name), body, "playbook")
    finally:
        os.close(dir_fd)
    return chunks


def reindex_moved_workspace(conn: sqlite3.Connection, config, old_root: str) -> dict:
    """Reindex every user's files on the current workspace, then forget `old_root`.

    Conversations are keyed on the task id, not a path, so they are not
    reindexed (`lookback_days=0`). Refuses an `old_root` that is, or contains,
    the configured workspace, since that delete would take the rows just made.
    """
    from istota.memory.search import reindex_all

    prefix = _root_prefix(old_root)
    workspace = config.workspace_path
    if workspace is None:
        raise LocalizeRefused("no workspace is configured")
    if (str(workspace).rstrip("/") + "/").startswith(prefix):
        raise LocalizeRefused(f"the workspace {workspace} is under {old_root}; nothing was deleted")
    stats = {"users": 0, "chunks": 0, "playbook_chunks": 0, "removed": 0}
    for user_id in config.users:
        result = reindex_all(conn, config, user_id, lookback_days=0)
        stats["users"] += 1
        stats["chunks"] += result.get("chunks", 0)
        stats["playbook_chunks"] += _reindex_playbooks(conn, config, user_id)
    channels = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT user_id FROM memory_chunks WHERE user_id LIKE 'channel:%'"
        ).fetchall()
    ]
    stats["removed"] = forget_root(conn, old_root, [*config.users, *channels])
    return stats


def default_note(config) -> str:
    return note_text(config.bot_name, datetime.now(timezone.utc))
