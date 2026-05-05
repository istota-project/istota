"""Per-file advisory flock for USER.md / CHANNEL.md parse-modify-write.

Two writers can race on USER.md: the nightly curator (long-running brain
call followed by an `os.replace`) and one or more runtime CLI calls fired
from interactive tasks. Last writer wins. Without a lock, ops can be
silently dropped.

`memory_md_lock(path, timeout=...)` returns a context manager that
acquires an exclusive flock on `<path>.lock` (sibling file, not on the
target itself, so we never trip on a target that's been replaced via
rename mid-flight). On timeout, raises `MemoryMdLocked`.

Linux + macOS: `fcntl.flock`. Windows is not a supported deployment for
istota; we don't paper over that here.
"""

from __future__ import annotations

import errno
import fcntl
import time
from contextlib import contextmanager
from pathlib import Path


class MemoryMdLocked(RuntimeError):
    """Raised when the parse-modify-write lock can't be acquired in time."""


@contextmanager
def memory_md_lock(target_path: Path, *, timeout_seconds: float = 5.0):
    """Acquire an exclusive flock on `<target_path>.lock` for the duration
    of the context. Polls every 100 ms, raises `MemoryMdLocked` after
    `timeout_seconds`."""
    lock_path = target_path.with_suffix(target_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # `O_RDWR | O_CREAT` — same FD shape regardless of whether the lock
    # file existed. We never read from it; the file is purely a flock
    # anchor. Closing the FD releases the lock.
    fd = open(lock_path, "a+")
    try:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise MemoryMdLocked(str(lock_path)) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fd.close()
