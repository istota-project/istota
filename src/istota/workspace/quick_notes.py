"""Quick notes: a markdown file written into the user's chosen folder in My files.

A note is created once, named from its first line (`derive_name`), and never
renamed. `create` claims the client's `client_note_id` in `quick_note_keys`
before it writes, so a retry after a lost response replays the first answer
instead of writing a second file, and a concurrent retry is told the first one
is still in progress. A write that fails frees the key, so the claim cannot
strand a note.

`save` replaces a note the web editor created, and only inside the current
quick notes folder. It never creates a folder or a file: a note moved or
deleted since it was created is refused, not resurrected.

Every path is walked from `Config.workspace_root(user_id)` with no link
followed (`sandbox.confined`), and the folder preference is validated the
same way before it is stored (`normalize_folder`).
"""

from __future__ import annotations

import errno
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from istota import db, user_profiles
from istota.lib.filenames import safe_filename
from istota.sandbox.confined import open_dir, open_regular_at, valid_part
from istota.storage import write_regular_file
from istota.workspace import text_files
from istota.workspace.writes import open_or_create_dirs, write_new_file

if TYPE_CHECKING:
    from istota.config import Config

QUICK_NOTE_MAX_BYTES = 256 * 1024
NAME_MAX_CHARS = 60
# How far back from the cut a space may be and still be the cut.
_WORD_BOUNDARY_WINDOW = 15
FOLDER_MAX_PARTS = 8
FOLDER_MAX_CHARS = 512
KEY_RETENTION = "-30 days"

_CLIENT_NOTE_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
# Block markers at the start of a line, peeled one at a time so `> - [ ] x`
# loses all three. Each needs whitespace or the end after it, so `-5` and
# `#hashtag` keep their first character.
_LEADING_MARKERS = (
    re.compile(r"#+(?:\s+|$)"),
    re.compile(r">\s*"),
    re.compile(r"[-*+](?:\s+|$)"),
    re.compile(r"\d+[.)](?:\s+|$)"),
    re.compile(r"\[[ xX]\](?:\s+|$)"),
)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_INLINE_MARKERS = re.compile(r"~~|[*_`]")
_WHITESPACE = re.compile(r"\s+")
# `safe_filename` keeps only the basename after a separator, which would turn
# "Meeting 10/20" into "20". In a title a slash is text, so it becomes the `_`
# the sanitiser gives every other character it refuses.
_SEPARATORS = re.compile(r"[/\\]")


class QuickNoteError(Exception):
    """A refusal with the HTTP status and stable code the routes answer with."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CreateResult:
    path: str
    name: str
    revision: str | None
    created: bool


def user_zone(config: "Config", user_id: str, *, conn: sqlite3.Connection | None = None) -> ZoneInfo:
    name = config.resolve_user_timezone(user_id, conn=conn)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _strip_line(line: str) -> str:
    text = line.strip()
    changed = True
    while changed:
        changed = False
        for marker in _LEADING_MARKERS:
            match = marker.match(text)
            if match and match.end() > 0:
                text = text[match.end():].lstrip()
                changed = True
    text = _LINK.sub(r"\1", text)
    text = _INLINE_MARKERS.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def _cut(name: str) -> str:
    if len(name) <= NAME_MAX_CHARS:
        return name
    if name[NAME_MAX_CHARS] == " ":
        return name[:NAME_MAX_CHARS].rstrip(" .")
    window = name[:NAME_MAX_CHARS]
    space = window.rfind(" ")
    if space >= NAME_MAX_CHARS - _WORD_BOUNDARY_WINDOW:
        window = window[:space]
    return window.rstrip(" .")


def derive_name(content: str, tz: ZoneInfo, now: datetime) -> str:
    """The file stem a note is created under, without `.md`."""
    first = next((line for line in content.splitlines() if line.strip()), "")
    text = _SEPARATORS.sub("_", _strip_line(first))
    name = _cut(safe_filename(text, fallback="").strip(" ."))
    if not any(char.isalnum() for char in name):
        # A dot, not a colon: `13:45` is not a portable file name.
        return now.astimezone(tz).strftime("%Y-%m-%d %H.%M")
    return name


def folder_parts(value: object) -> tuple[str, ...]:
    """The components of a folder preference, or `QuickNoteError` 422.

    Empty parts are dropped, so leading, trailing and doubled slashes are
    spelling, not structure. Validates the text only; `normalize_folder` also
    checks what is on disk.
    """
    if not isinstance(value, str):
        raise QuickNoteError(422, "invalid_folder", "The quick notes folder must be a path.")
    parts = tuple(part for part in value.split("/") if part)
    if not parts or not all(valid_part(part) for part in parts):
        raise QuickNoteError(422, "invalid_folder", "That is not a folder in My files.")
    if len(parts) > FOLDER_MAX_PARTS or len("/".join(parts)) > FOLDER_MAX_CHARS:
        raise QuickNoteError(422, "invalid_folder", "That folder path is too long.")
    return parts


def normalize_folder(config: "Config", user_id: str, value: object) -> str | None:
    """The value to store for a folder preference: None clears it.

    A folder that exists must be a directory reached with no link followed. A
    folder that does not exist yet is accepted, and the first note creates it;
    nothing is created here.
    """
    if value is None or value == "":
        return None
    parts = folder_parts(value)
    root = config.workspace_root(user_id)
    if root is None:
        raise QuickNoteError(422, "invalid_folder", "No workspace is configured.")
    try:
        fd = open_dir(root, *parts, raise_errors=True)
    except FileNotFoundError:
        # The first missing component ends the walk; nothing below it exists.
        return "/".join(parts)
    except OSError:
        raise QuickNoteError(422, "invalid_folder", "That path is not a folder in My files.") from None
    if fd is not None:
        os.close(fd)
    return "/".join(parts)


def configured_folder(conn: sqlite3.Connection, user_id: str) -> tuple[str, ...] | None:
    """The user's quick notes folder as parts, or None when unset. Also what a `notes` grant checks."""
    profile = user_profiles.get_profile(Path(), user_id, conn=conn)
    if profile is None or not profile.quick_notes_folder:
        return None
    try:
        return folder_parts(profile.quick_notes_folder)
    except QuickNoteError:
        # Stored by a writer that skipped validation; treated as unset rather
        # than walked.
        return None


def _workspace_root(config: "Config", user_id: str) -> Path:
    root = config.workspace_root(user_id)
    if root is None:
        raise QuickNoteError(503, "no_workspace", "No workspace is configured.")
    return root


def _read_note(dir_fd: int, leaf: str) -> dict:
    """`text_files.save`'s read result for one note, opened by descriptor."""
    fd = open_regular_at(dir_fd, leaf)
    if fd is None:
        try:
            os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {"status": "refused", "reason": "missing"}
        except OSError:
            pass
        return {"status": "refused", "reason": "not_a_file"}
    try:
        data = b""
        while len(data) <= QUICK_NOTE_MAX_BYTES:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > QUICK_NOTE_MAX_BYTES:
        return {"status": "refused", "reason": "too_large"}
    try:
        return {"status": "readable", "content": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"status": "refused", "reason": "not_utf8"}


def _current_revision(root: Path, parts: list[str]) -> str | None:
    """The revision of the note at `parts` now, or None if it cannot be read."""
    dir_fd = open_dir(root, *parts[:-1])
    if dir_fd is None:
        return None
    try:
        result = _read_note(dir_fd, parts[-1])
    finally:
        os.close(dir_fd)
    if result["status"] != "readable":
        return None
    return text_files.revision(result["content"])


def _claim(conn: sqlite3.Connection, user_id: str, client_note_id: str | None) -> str | None:
    """Claim the key, or return the path the request that claimed it wrote.

    None means this request holds the claim (or has no key) and must write the
    note. Committed at once, so a concurrent retry on another connection sees
    the claim; a key still without a path is that retry's `in_progress`.
    """
    conn.execute(
        "DELETE FROM quick_note_keys WHERE created_at < datetime('now', ?)", (KEY_RETENTION,),
    )
    claimed = True
    if client_note_id is not None:
        claimed = conn.execute(
            "INSERT OR IGNORE INTO quick_note_keys (user_id, client_note_id) VALUES (?, ?)",
            (user_id, client_note_id),
        ).rowcount > 0
    conn.commit()
    if claimed:
        return None
    row = conn.execute(
        "SELECT path FROM quick_note_keys WHERE user_id = ? AND client_note_id = ?",
        (user_id, client_note_id),
    ).fetchone()
    if row is None or row["path"] is None:
        raise QuickNoteError(409, "in_progress", "This note is being saved. Try again.")
    return row["path"]


def create(
    config: "Config", conn: sqlite3.Connection, user_id: str, content: str,
    client_note_id: str | None,
) -> CreateResult:
    if not content.strip():
        raise QuickNoteError(422, "empty", "The note is empty.")
    data = content.encode("utf-8")
    if len(data) > QUICK_NOTE_MAX_BYTES:
        raise QuickNoteError(413, "too_large", "The note is too large.")
    parts = configured_folder(conn, user_id)
    if parts is None:
        raise QuickNoteError(409, "notes_folder_unset", "Choose a quick notes folder first.")
    root = _workspace_root(config, user_id)
    if client_note_id is not None and (
        not isinstance(client_note_id, str) or not _CLIENT_NOTE_ID.fullmatch(client_note_id)
    ):
        raise QuickNoteError(400, "invalid_client_note_id", "Invalid client_note_id.")
    delivered = _claim(conn, user_id, client_note_id)
    if delivered is not None:
        # Delivered before. A note deleted or moved since keeps its path and
        # loses its revision; writing it again would resurrect it.
        stored = delivered.split("/")
        return CreateResult(delivered, stored[-1], _current_revision(root, stored), False)

    stem = derive_name(content, user_zone(config, user_id, conn=conn), datetime.now(timezone.utc))
    try:
        try:
            os.close(open_or_create_dirs(root, list(parts)))
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EEXIST}:
                raise QuickNoteError(
                    422, "invalid_folder", "The quick notes folder is not a folder in My files.",
                ) from None
            raise
        name = write_new_file(
            root, list(parts), stem + ".md", [data],
            max_bytes=QUICK_NOTE_MAX_BYTES, numbered=True,
        )
        path = "/".join([*parts, name])
        if client_note_id is not None:
            conn.execute(
                "UPDATE quick_note_keys SET path = ? WHERE user_id = ? AND client_note_id = ?",
                (path, user_id, client_note_id),
            )
            conn.commit()
    except BaseException:
        if client_note_id is not None:
            conn.execute(
                "DELETE FROM quick_note_keys WHERE user_id = ? AND client_note_id = ? AND path IS NULL",
                (user_id, client_note_id),
            )
            conn.commit()
        raise
    return CreateResult(path, name, text_files.revision(content), True)


def _note_parts(path: object, folder: tuple[str, ...]) -> list[str]:
    """`path` as parts if it names a `.md` file directly in `folder`, else 403."""
    refused = QuickNoteError(403, "outside_folder", "This note is not in your quick notes folder.")
    if not isinstance(path, str) or path.startswith("/"):
        raise refused
    parts = path.split("/")
    if len(parts) < 2 or tuple(parts[:-1]) != folder:
        raise refused
    leaf = parts[-1]
    if not valid_part(leaf) or not leaf.endswith(".md") or leaf == ".md":
        raise refused
    return parts


def save(
    config: "Config", user_id: str, path: str, content: str, expected_revision: str,
) -> text_files.SaveOutcome:
    """Replace the note at `path`, which must still be in the current folder."""
    with db.get_db(config.db_path) as conn:
        folder = configured_folder(conn, user_id)
    if folder is None:
        raise QuickNoteError(403, "outside_folder", "This note is not in your quick notes folder.")
    parts = _note_parts(path, folder)
    root = _workspace_root(config, user_id)
    leaf = parts[-1]

    def read() -> dict:
        dir_fd = open_dir(root, *folder)
        if dir_fd is None:
            return {"status": "refused", "reason": "invalid_folder"}
        try:
            return _read_note(dir_fd, leaf)
        finally:
            os.close(dir_fd)

    def write(text: str) -> bool:
        dir_fd = open_dir(root, *folder)
        if dir_fd is None:
            return False
        try:
            # `read` saw a regular file inside the lock; a name gone since then
            # is not re-created.
            try:
                info = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(info.st_mode):
                return False
            return write_regular_file(Path(leaf), text, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)

    return text_files.save(
        config, user_id, root.joinpath(*parts), content, expected_revision,
        read=read, write=write, max_bytes=QUICK_NOTE_MAX_BYTES,
    )
