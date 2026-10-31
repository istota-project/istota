"""`egress-refused.jsonl`: what a task tried and could not do, on its way to the DB.

A file because the seams that decide run where no database is: WebFetch in the
`istota_web` server, a skill CLI under the proxy, the CONNECT proxy's threads. It
sits in the task's control directory, which no task can write, and only daemon-side
writers hold its path. `rooms.egress.entries_from_log` decides what a line may mean.

A refused name can itself carry data, so this module never logs one. Logging never
decides: a failure here is a WARNING and the refusal stands.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

REFUSED_LOG_NAME = "egress-refused.jsonl"
#: In a skill CLI's environment only, from the proxy's snapshot. Never the model's.
EGRESS_LOG_VAR = "ISTOTA_EGRESS_LOG"
#: `temp_dir` is tmpfs on the stack and a loop can refuse without end. Only 20 rows
#: per subject survive anyway.
MAX_LOG_BYTES = 64 * 1024
_MAX_ENTRY_CHARS = 253


def append_refusal(path: "str | Path | None", channel: str, entry: str) -> None:
    """One line, in one `write`, so concurrent writers do not interleave."""
    if not path or len(entry) > _MAX_ENTRY_CHARS:
        return
    line = json.dumps({"channel": channel, "entry": entry}) + "\n"
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
        )
        try:
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_size < MAX_LOG_BYTES:
                os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        logger.warning("egress_log_write_failed channel=%s: %s", channel, type(exc).__name__)


def consume_refusals(path: "str | Path") -> list[tuple[str, str]]:
    """The file's `(channel, entry)` pairs, and the file is gone, so a retry on the
    same control directory does not count them twice. Skips a line it cannot read:
    a writer's last line can be torn."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("egress_log_read_failed: %s", type(exc).__name__)
        return []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            logger.warning("egress_log_read_failed: not a regular file")
            return []
        # The cap is checked before a write, so one line can pass it.
        data = os.read(fd, MAX_LOG_BYTES + 4096)
        os.unlink(path)
    except OSError as exc:
        logger.warning("egress_log_read_failed: %s", type(exc).__name__)
        return []
    finally:
        os.close(fd)
    pairs = []
    for line in data.split(b"\n"):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        channel, entry = item.get("channel"), item.get("entry")
        if isinstance(channel, str) and isinstance(entry, str):
            pairs.append((channel, entry))
    return pairs
