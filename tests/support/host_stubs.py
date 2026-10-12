"""Stand-ins for host binaries the `host/` scripts need and a test machine may lack.

`flock` is util-linux's, always on the Debian VM and absent on macOS. The stub
takes the same arguments `istota-stack` passes (`-n`, `-w SECONDS`, a
descriptor number) and locks the descriptor it inherited with `flock(2)`, so a
lock held by the test process, or by a second `istota-stack`, really conflicts.
A flock lock belongs to the open file description, so it outlives the stub.
"""

from __future__ import annotations

import fcntl
from pathlib import Path

FLOCK_STUB = """#!/usr/bin/env python3
import fcntl, sys, time
args = sys.argv[1:]
nonblock, wait = False, None
while args and args[0].startswith("-"):
    flag = args.pop(0)
    if flag == "-n":
        nonblock = True
    elif flag == "-w":
        wait = float(args.pop(0))
    else:
        sys.exit(f"flock stub: unsupported option {flag}")
fd = int(args[0])
deadline = None if wait is None else time.monotonic() + wait
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except BlockingIOError:
        if nonblock or (deadline is not None and time.monotonic() >= deadline):
            sys.exit(1)
        time.sleep(0.05)
"""


def install_flock(bindir: Path) -> None:
    path = bindir / "flock"
    path.write_text(FLOCK_STUB)
    path.chmod(0o755)


class HeldLock:
    """The stack lock, held by the test process as another `istota-stack` would."""

    def __init__(self, path: Path, verb: str):
        path.write_text(f"{verb}\n")
        self._file = open(path, "a")
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)

    def release(self) -> None:
        if not self._file.closed:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
