"""One lock and one revision for the user's text files in the workspace.

The web editors (CHANNEL.md, My notes, and the config files) and the daemon's
read-modify-write paths (CRON.md, TASKS.md, the USER.md curator, the memory
skill) take the same `memory_md_lock` anchor for a file, so neither side can
drop the other's write. The anchor directory is the user's deferred dir,
`{temp_dir}/{user_id}`, which the executor exports as `ISTOTA_DEFERRED_DIR`
and binds into that user's sandbox at the same path; on the stack it is on the
shared state at `/data`, so the `web` and `daemon` containers agree on it.

The revision is optimistic: the editor sends back the revision it loaded and
`save` refuses a write when the file moved since. That covers what the lock
cannot: the anchor is per user, so two members writing one shared room's
CHANNEL.md take different anchors and exclude nobody.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

# The module, not its names: a test (and anything else) that replaces
# `memory_md_lock` on the module must reach the lock taken here.
from istota.memory.curation import file_lock as _md_lock
from istota.memory.curation.file_lock import MemoryMdLocked

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger("istota.workspace.text_files")

__all__ = [
    "MemoryMdLocked",
    "SaveOutcome",
    "file_lock",
    "lock_dir",
    "revision",
    "save",
]


def revision(content: str) -> str:
    """Opaque revision tag for what a reader will see as the file's content.

    Hashes what the reader **returns**, not the bytes on disk:
    `read_channel_memory` collapses absent and whitespace-only to None, so a
    file saved as `"  \\n"` reads back as `""`. Hashing the submitted bytes
    instead would hand the client a tag the next read can never reproduce, and
    every later save would be refused as a conflict with no way out but
    discarding the user's text.
    """
    normalized = content if content.strip() else ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def lock_dir(config: "Config", user_id: str) -> Path:
    """The flock anchor directory for `user_id`'s files.

    The same expression the nightly curator and the memory skill CLI use
    (`deferred_lock_dir` of the user's deferred dir), so every writer of a
    file lands on one anchor inode.
    """
    return _md_lock.deferred_lock_dir(config.temp_dir / user_id)


@contextmanager
def file_lock(
    config: "Config", user_id: str, path: Path, *, timeout_seconds: float = 5.0,
) -> Iterator[None]:
    """Hold the exclusive lock on `path`. Raises `MemoryMdLocked` on timeout.

    `path` must be spelled the way every other writer of the file spells it,
    since the anchor is keyed on the absolute path: the resolved
    `resolve_user_config_dir(...) / name` for the bot config files.
    """
    with _md_lock.memory_md_lock(
        path, timeout_seconds=timeout_seconds, lock_dir=lock_dir(config, user_id),
    ):
        yield


@dataclass(frozen=True)
class SaveOutcome:
    status: Literal["ok", "conflict", "locked", "invalid", "too_large", "failed"]
    revision: str | None = None
    errors: tuple[str, ...] = ()


def save(
    config: "Config",
    user_id: str,
    path: Path,
    content: str,
    expected_revision: str,
    *,
    read: Callable[[], str | None],
    write: Callable[[str], bool],
    validate: Callable[[str], list[str]] | None = None,
    max_bytes: int,
) -> SaveOutcome:
    """Replace a file the user loaded at `expected_revision`. Never raises.

    `read` and `write` are the file's own hardened helpers, so containment and
    leaf checks stay where they are. `read` returning None reads as empty,
    which is how the editors already treat an absent file. Size and `validate`
    are checked before the lock: neither touches the file.
    """
    if len(content.encode("utf-8")) > max_bytes:
        return SaveOutcome("too_large")
    if validate is not None:
        errors = validate(content)
        if errors:
            return SaveOutcome("invalid", errors=tuple(errors))
    try:
        with file_lock(config, user_id, path):
            # Re-read inside the lock: the revision the client carries was
            # taken before it, so comparing outside would leave exactly the
            # window the check exists to close.
            current = read() or ""
            if revision(current) != expected_revision:
                return SaveOutcome("conflict")
            try:
                written = write(content)
            except OSError as e:
                # A full disk or a dropped mount is a reportable failure, not
                # an unstructured 500.
                logger.warning("text file save failed for %s: %s", path, e)
                return SaveOutcome("failed")
            if not written:
                logger.warning("text file save refused for %s", path)
                return SaveOutcome("failed")
    except MemoryMdLocked:
        return SaveOutcome("locked")
    return SaveOutcome("ok", revision=revision(content))
