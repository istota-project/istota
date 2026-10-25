"""The scratchpad: snapshots around each wake, outside changes, and repair.

`SCRATCHPAD.md` sits in the assignment directory, where a wake edits it with
ordinary file tools. The history lives in `assignment_snapshots`, not beside
the file, because a wake can write anything in its own directory and the
history is the audit trail for injection.

- `begin`, at pickup: the file is compared with the last `after` snapshot (or
  the seed, before the first wake). A difference is recorded as an untrusted
  `scratchpad_changed` inbox event carrying a unified diff: any task of the
  user's (an email turn, a cron job, a shared-room turn) can write the file,
  so the host cannot say who made it. The user steers with `assignment note`.
  A file that is missing, empty, not UTF-8, over 1 MiB or not a regular file
  is restored from the last whole snapshot. Then the `before` snapshot is
  stored.
- `end`, when the wake finishes: a file the wake broke goes back to its
  `before` text, and the `after` snapshot is stored.

Every file access goes through a descriptor on the assignment directory
(`charter.open_assignment_dir`), with no link followed at the leaf. Nothing
here commits; the caller owns the transaction.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from istota import storage
from istota.assignments import charter, store

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger(__name__)

FILE_MAX_BYTES = 1024 * 1024
SNAPSHOT_MAX_BYTES = 512 * 1024
DIFF_MAX_CHARS = 8000
DIFF_CUT_MARKER = "\n[diff cut: longer than 8,000 characters]\n"

#: Why a file was not usable as it stood.
MISSING = "missing"
EMPTY = "empty"
NOT_UTF8 = "not_utf8"
TOO_LARGE = "too_large"
NOT_REGULAR = "not_regular"
#: The directory could not be opened (the mount is down): nothing is restored.
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FileState:
    text: str | None
    problem: str | None


@dataclass(frozen=True)
class BeginResult:
    """What the wake starts from: the text in its `before` snapshot."""

    text: str
    change_event_id: int | None
    restored: str | None


@dataclass(frozen=True)
class EndResult:
    text: str
    restored: str | None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def read_file(config: "Config", assignment: store.Assignment) -> FileState:
    """The scratchpad's text, or why it cannot be used."""
    dir_fd = charter.open_assignment_dir(config, assignment)
    if dir_fd is None:
        return FileState(None, UNAVAILABLE)
    try:
        try:
            st = os.stat(charter.SCRATCHPAD_FILE, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return FileState(None, MISSING)
        except OSError:
            return FileState(None, UNAVAILABLE)
        if not stat.S_ISREG(st.st_mode):
            return FileState(None, NOT_REGULAR)
        if st.st_size > FILE_MAX_BYTES:
            return FileState(None, TOO_LARGE)
        text, reason = storage.read_regular_file(
            Path(charter.SCRATCHPAD_FILE), max_bytes=FILE_MAX_BYTES, dir_fd=dir_fd,
        )
    finally:
        os.close(dir_fd)
    if text is None:
        from istota.skills._loader import OVERLAY_NOT_UTF8

        return FileState(None, NOT_UTF8 if reason == OVERLAY_NOT_UTF8 else NOT_REGULAR)
    if not text.strip():
        return FileState(None, EMPTY)
    return FileState(text, None)


def write_file(config: "Config", assignment: store.Assignment, text: str) -> bool:
    """Replace the scratchpad. A link, FIFO or device at the name is removed
    first, since `write_regular_file` refuses to write through one. False when
    the write did not land."""
    dir_fd = charter.open_assignment_dir(config, assignment)
    if dir_fd is None:
        return False
    try:
        try:
            st = os.stat(charter.SCRATCHPAD_FILE, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        except OSError:
            return False
        if st is not None and not stat.S_ISREG(st.st_mode):
            try:
                os.unlink(charter.SCRATCHPAD_FILE, dir_fd=dir_fd)
            except OSError:
                logger.warning("scratchpad not replaced assignment_id=%s: not a regular file", assignment.id)
                return False
        return storage.write_regular_file(Path(charter.SCRATCHPAD_FILE), text, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def store_snapshot(
    conn: sqlite3.Connection, assignment_id: int, *, phase: str, text: str, wake_id: int | None = None,
) -> int:
    """Store `text` whole, or its first 512 KiB with the truncated flag. The hash
    is always of the whole text, so a comparison against it stays exact."""
    data = text.encode("utf-8")
    truncated = len(data) > SNAPSHOT_MAX_BYTES
    content = data[:SNAPSHOT_MAX_BYTES].decode("utf-8", errors="ignore") if truncated else text
    return store.insert_snapshot(
        conn, assignment_id, phase=phase, content=content, sha256=sha256(text),
        wake_id=wake_id, truncated=truncated,
    )


def baseline(conn: sqlite3.Connection, assignment_id: int) -> store.Snapshot:
    """What the file should hold between wakes: the last `after`, else the seed.

    The seed snapshot is written here the first time it is needed, from
    `charter.SCRATCHPAD_SEED`, which is what `charter.create_directory` wrote.
    """
    snapshot = store.latest_snapshot(conn, assignment_id, phases=("after", "seed"))
    if snapshot is not None:
        return snapshot
    store_snapshot(conn, assignment_id, phase="seed", text=charter.SCRATCHPAD_SEED)
    return store.latest_snapshot(conn, assignment_id, phases=("seed",))


def restore_text(conn: sqlite3.Connection, assignment_id: int) -> str:
    """The last good text: the newest whole `after` or seed snapshot, else the seed."""
    snapshot = store.latest_snapshot(
        conn, assignment_id, phases=("after", "seed"), whole_only=True,
    )
    return snapshot.content if snapshot is not None else charter.SCRATCHPAD_SEED


# ---------------------------------------------------------------------------
# Diffs
# ---------------------------------------------------------------------------


def unified_diff(before: str, after: str, *, before_label: str, after_label: str) -> tuple[str, bool]:
    """`difflib.unified_diff` as one string, cut at `DIFF_MAX_CHARS`. Returns
    `(diff, cut)`."""
    text = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=before_label, tofile=after_label,
        )
    )
    if len(text) <= DIFF_MAX_CHARS:
        return text, False
    return text[:DIFF_MAX_CHARS] + DIFF_CUT_MARKER, True


def added_lines(before: str, after: str) -> list[str]:
    """The non-blank lines `after` adds to `before`, in order."""
    added: list[str] = []
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            if content.strip():
                added.append(content)
    return added


# ---------------------------------------------------------------------------
# Around a wake
# ---------------------------------------------------------------------------


def begin(
    config: "Config", conn: sqlite3.Connection, assignment: store.Assignment, wake: store.Wake,
) -> BeginResult:
    """Record an outside change or repair a broken file, then store `before`."""
    base = baseline(conn, assignment.id)
    state = read_file(config, assignment)
    change_id: int | None = None
    restored: str | None = None

    if state.problem == UNAVAILABLE:
        # Nothing to compare and nowhere to write; the wake starts from the
        # last snapshot and the file is left as it is.
        text = base.content
        store.insert_event(
            conn, assignment.id, kind="scratchpad_unavailable", actor="system", wake_id=wake.id,
        )
        logger.warning("scratchpad unavailable assignment_id=%s wake_id=%s", assignment.id, wake.id)
    elif state.problem is not None:
        if state.problem == EMPTY:
            store.insert_event(
                conn, assignment.id, kind="scratchpad_changed", actor="system", wake_id=wake.id,
                payload={"emptied": True},
            )
        text = restore_text(conn, assignment.id)
        written = write_file(config, assignment, text)
        restored = state.problem
        store.insert_event(
            conn, assignment.id, kind="scratchpad_restored", actor="system", wake_id=wake.id,
            payload={"reason": state.problem, "phase": "begin", "written": written},
        )
    else:
        text = state.text
        if sha256(text) != base.sha256:
            label = f"after wake {_seq_of(conn, base)}" if base.phase == "after" else "seed"
            diff, cut = unified_diff(
                base.content, text,
                before_label=f"SCRATCHPAD.md ({label})", after_label="SCRATCHPAD.md (now)",
            )
            change_id = store.insert_inbox_event(
                conn, assignment.id, kind="scratchpad_changed", trusted=False,
                payload={"diff": diff, "cut": cut},
            )
            store.insert_event(
                conn, assignment.id, kind="scratchpad_changed", actor="system", wake_id=wake.id,
                payload={
                    "inbox_id": change_id,
                    "lines_added": len(added_lines(base.content, text)),
                    "sha256": sha256(text),
                },
            )

    store_snapshot(conn, assignment.id, phase="before", text=text, wake_id=wake.id)
    return BeginResult(text=text, change_event_id=change_id, restored=restored)


def _seq_of(conn: sqlite3.Connection, snapshot: store.Snapshot) -> int | str:
    wake = store.get_wake(conn, snapshot.wake_id) if snapshot.wake_id is not None else None
    return wake.seq if wake is not None else "?"


def end(
    config: "Config", conn: sqlite3.Connection, assignment: store.Assignment, wake: store.Wake,
) -> EndResult:
    """Repair a file the wake broke, then store `after`."""
    before = store.wake_snapshot(conn, wake.id, "before")
    before_text = before.content if before is not None else restore_text(conn, assignment.id)
    state = read_file(config, assignment)
    restored: str | None = None

    if state.problem == UNAVAILABLE:
        text = before_text
        store.insert_event(
            conn, assignment.id, kind="scratchpad_unavailable", actor="system", wake_id=wake.id,
            payload={"phase": "end"},
        )
        store_snapshot(conn, assignment.id, phase="after", text=text, wake_id=wake.id)
        return EndResult(text=text, restored=None)
    if state.problem is not None:
        # The wake broke the file; it goes back to what the wake started from.
        text = before_text if before is not None and not before.truncated else restore_text(conn, assignment.id)
        restored = state.problem
        written = write_file(config, assignment, text)
        store.insert_event(
            conn, assignment.id, kind="scratchpad_restored", actor="system", wake_id=wake.id,
            payload={"reason": state.problem, "phase": "end", "written": written},
        )
    else:
        text = state.text

    store_snapshot(conn, assignment.id, phase="after", text=text, wake_id=wake.id)
    return EndResult(text=text, restored=restored)
