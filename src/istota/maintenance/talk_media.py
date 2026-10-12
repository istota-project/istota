"""Retention for Talk attachments fetched into the room folders.

On a local workspace `scheduler._fetch_talk_attachment` writes each Talk
attachment to `Channels/<token>/media/<message id>/<name>`. A Talk room is
never deleted, so nothing else would ever remove them. This sweep removes a
message directory once its newest file is older than
`[talk] attachment_retention_days`.

Every task in a room can write its folder, so the walk follows no link: each
directory is opened relative to its parent with `O_NOFOLLOW`, and only regular
files are removed. Anything else in a message directory (a link, a FIFO, a
subdirectory) is left in place with a WARNING, and the directory with it.
"""

from __future__ import annotations

import logging
import os
import stat
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_dir(name: str, dir_fd: int) -> int | None:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None


def _sweep_message_dir(media_fd: int, name: str, cutoff: float, where: str) -> bool:
    """Remove one message directory if it is past the cutoff. True if removed."""
    try:
        msg_fd = _open_dir(name, media_fd)
    except OSError as e:
        logger.warning("Talk media sweep skipped %s/%s: %s", where, name, e)
        return False
    if msg_fd is None:
        return False
    try:
        files: list[str] = []
        others: list[str] = []
        newest: float | None = None
        with os.scandir(msg_fd) as entries:
            for entry in entries:
                st = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(st.st_mode):
                    files.append(entry.name)
                    newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
                else:
                    others.append(entry.name)
        if newest is None:
            # An empty directory (an interrupted fetch) ages by its own mtime.
            newest = os.fstat(msg_fd).st_mtime
        if newest >= cutoff:
            return False
        for leaf in files:
            os.unlink(leaf, dir_fd=msg_fd)
    finally:
        os.close(msg_fd)
    if others:
        logger.warning(
            "Talk media sweep left %s/%s in place: it holds %d entries that "
            "are not regular files", where, name, len(others),
        )
        return False
    os.rmdir(name, dir_fd=media_fd)
    return True


def sweep_talk_media(
    workspace: Path, retention_days: int, *, now: float | None = None,
) -> int:
    """Remove expired `Channels/*/media/<message id>/` directories.

    Returns the number of message directories removed. `retention_days <= 0`
    keeps everything. Never raises: every failure is a WARNING and a skip.
    """
    if retention_days <= 0:
        return 0
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    channels = Path(workspace) / "Channels"
    try:
        channels_fd = os.open(channels, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return 0
    except OSError as e:
        logger.warning("Talk media sweep could not open %s: %s", channels, e)
        return 0

    removed = 0
    try:
        with os.scandir(channels_fd) as rooms:
            tokens = [entry.name for entry in rooms]
        for token in tokens:
            try:
                room_fd = _open_dir(token, channels_fd)
            except OSError as e:
                logger.warning("Talk media sweep skipped Channels/%s: %s", token, e)
                continue
            if room_fd is None:
                continue
            try:
                try:
                    media_fd = _open_dir("media", room_fd)
                except OSError as e:
                    logger.warning(
                        "Talk media sweep skipped Channels/%s/media: %s", token, e,
                    )
                    continue
                if media_fd is None:
                    continue
                try:
                    with os.scandir(media_fd) as messages:
                        names = [entry.name for entry in messages]
                    for name in names:
                        # Only the `<message id>` directories the fetch writes;
                        # anything else under `media/` is someone else's.
                        if not (name.isascii() and name.isdigit()):
                            continue
                        try:
                            if _sweep_message_dir(
                                media_fd, name, cutoff, f"Channels/{token}/media",
                            ):
                                removed += 1
                        except OSError as e:
                            logger.warning(
                                "Talk media sweep skipped Channels/%s/media/%s: %s",
                                token, name, e,
                            )
                finally:
                    os.close(media_fd)
            finally:
                os.close(room_fd)
    except OSError as e:
        logger.warning("Talk media sweep stopped early: %s", e)
    finally:
        os.close(channels_fd)
    return removed
