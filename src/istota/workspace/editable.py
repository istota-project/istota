"""Text files in My files, as the Workspace page reads and saves them.

A file is editable when its bytes are text by the viewer's own rule
(`preview.classify`): at most `MAX_BYTES`, valid UTF-8, no NUL, no media
signature. Everything else stays view-only.

The folder is walked from the home root with no link followed (`open_dir`),
and the file is read and written through that one descriptor, so a link
anywhere below the root refuses rather than redirects. Saves go through
`text_files.save`, which re-reads the file inside the lock and compares the
revision with what is on disk, so a change made in the host folder or by a
task since the load is a conflict rather than an overwrite.

The bot's config folder is refused: those files have their own editor, which
checks them with the loaders' validators (`config_files`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from istota import storage
from istota.sandbox.confined import open_dir, valid_part
from istota.skills._loader import (
    OVERLAY_IS_A_SYMLINK, OVERLAY_NOT_A_REGULAR_FILE, OVERLAY_UNREADABLY_LARGE,
    read_overlay_bytes,
)
from istota.workspace import text_files
from istota.workspace.listing import ListingError
from istota.workspace.preview import PREVIEW_MAX_BYTES, classify

if TYPE_CHECKING:
    from istota.config import Config

MAX_BYTES = PREVIEW_MAX_BYTES


class EditRefused(ListingError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(status, message)
        self.code = code


def _not_text() -> EditRefused:
    return EditRefused(415, "not_text", "Only text files can be edited here.")


def _too_large() -> EditRefused:
    return EditRefused(413, "too_large", "This file is too large to edit here.")


def _not_a_file() -> EditRefused:
    return EditRefused(422, "not_a_file", "Only regular files can be edited here.")


def _config_file() -> EditRefused:
    return EditRefused(
        403, "config_file",
        "Files in the bot's config folder are edited from Configuration or Skill instructions.",
    )


def _check_target(config: "Config", parts: list[str], name: str) -> None:
    if not valid_part(name):
        raise ListingError(404, "File not found.")
    # Case-folded, since a host folder on a Mac matches names without case.
    folded = [part.casefold() for part in parts[:2]]
    if folded == [config.bot_dir_name.casefold(), "config"]:
        raise _config_file()


def _check_folder(config: "Config", user_id: str, dir_fd: int) -> None:
    """Refuse the config folder by identity too: the daemon follows a
    `config` link that stays inside the user's tree, which the name check
    above cannot see."""
    config_dir = storage.resolve_user_config_dir(config, user_id)
    if config_dir is None:
        return
    folder = os.fstat(dir_fd)
    for directory in (config_dir, config_dir / "skills"):
        try:
            info = os.stat(directory)
        except OSError:
            continue
        if (info.st_dev, info.st_ino) == (folder.st_dev, folder.st_ino):
            raise _config_file()


def _decode(data: bytes) -> str | None:
    if classify("", len(data), "", data, truncated=False)["kind"] != "text":
        return None
    # Decoded here rather than taken from `classify`, which drops a BOM the
    # save would then remove from the file.
    return data.decode("utf-8")


def _uses_crlf(text: str) -> bool:
    return text.count("\r\n") * 2 > text.count("\n")


def _read_at(dir_fd: int, name: str) -> str:
    """The file's text with CRLF read as LF, the way a textarea hands it back,
    so the revision a save carries is the one a re-read computes."""
    return _read_raw(dir_fd, name).replace("\r\n", "\n")


def _read_raw(dir_fd: int, name: str) -> str:
    data, reason, size = read_overlay_bytes(Path(name), max_bytes=MAX_BYTES, dir_fd=dir_fd)
    if reason in (OVERLAY_IS_A_SYMLINK, OVERLAY_NOT_A_REGULAR_FILE):
        raise _not_a_file()
    if reason == OVERLAY_UNREADABLY_LARGE:
        raise _too_large()
    if reason is not None:
        raise ListingError(500, "Could not read this file.")
    if size is None:
        raise ListingError(404, "File not found.")
    text = _decode(data)
    if text is None:
        raise _not_text()
    return text


def _open_folder(root: Path, parts: list[str]) -> int:
    fd = open_dir(root, *parts, raise_mount_errors=True)
    if fd is None:
        raise ListingError(404, "Folder not found.")
    return fd


def read(config: "Config", user_id: str, root: Path, parts: list[str], name: str) -> str:
    _check_target(config, parts, name)
    dir_fd = _open_folder(root, parts)
    try:
        _check_folder(config, user_id, dir_fd)
        return _read_at(dir_fd, name)
    finally:
        os.close(dir_fd)


def save(
    config: "Config", user_id: str, root: Path, parts: list[str], name: str,
    content: str, expected_revision: str,
) -> text_files.SaveOutcome:
    """Replace an existing text file; a refused read raises `EditRefused`.

    A file that used CRLF line endings keeps them."""
    _check_target(config, parts, name)
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        raise _not_text() from None
    if "\0" in content:
        raise _not_text()
    dir_fd = _open_folder(root, parts)
    try:
        _check_folder(config, user_id, dir_fd)
        crlf = False

        def read_current() -> str:
            nonlocal crlf
            raw = _read_raw(dir_fd, name)
            crlf = _uses_crlf(raw)
            return raw.replace("\r\n", "\n")

        def write(text: str) -> bool:
            if crlf:
                text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            return storage.write_regular_file(Path(name), text, dir_fd=dir_fd, keep_mode=True)

        return text_files.save(
            config, user_id, root.joinpath(*parts, name), content, expected_revision,
            read=read_current, write=write, max_bytes=MAX_BYTES,
        )
    finally:
        os.close(dir_fd)
