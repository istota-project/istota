"""Which mount covers a path, read from ``/proc/self/mountinfo``.

Two callers ask it: the container entrypoint's preflight (is the full
integration workspace on the VM's ``fuse.rclone`` mount, parity row 9) and
``sandbox/cgroup.py`` (is the declared task-cgroup root on a cgroup2 mount of
this container's own cgroup, parity row 4). Each had its own parse before.

A line is ``id parent maj:min root mountpoint options [optional...] - fstype
source superoptions``. The optional fields vary in number, so the filesystem
type is the field after the ``-`` separator, never a fixed column. Paths are
octal-escaped (space, tab, newline, backslash). The covering mount is the one
with the longest mount point that is the path or an ancestor of it; on a tie
the later line wins, since mountinfo lists mounts in the order they were made
and a later mount at the same point hides the earlier one.

stdlib-only leaf, imports nothing from istota, never raises.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Mount:
    point: str
    root: str
    fstype: str


def unescape(field: str) -> str:
    """Undo mountinfo's octal escapes (``\\040`` for a space, and so on)."""
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 4 <= len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def parse(text: str) -> list[Mount]:
    mounts = []
    for line in text.splitlines():
        fields = line.split()
        if "-" not in fields:
            continue
        dash = fields.index("-")
        if dash < 5 or dash + 1 >= len(fields):
            continue
        point = os.path.normpath(unescape(fields[4]))
        mounts.append(Mount(point=point, root=unescape(fields[3]), fstype=fields[dash + 1]))
    return mounts


def covering_mount(path: str | os.PathLike, text: str) -> Mount | None:
    """The mount ``path`` is on, by mountinfo ``text``; ``None`` when none covers it."""
    target = os.path.normpath(str(path))
    best: Mount | None = None
    for mount in parse(text):
        covers = target == mount.point or target.startswith(mount.point.rstrip("/") + "/")
        if covers and (best is None or len(mount.point) >= len(best.point)):
            best = mount
    return best


def read_mountinfo(proc_root: Path = Path("/proc")) -> str | None:
    try:
        return (Path(proc_root) / "self" / "mountinfo").read_text()
    except (OSError, ValueError):
        return None
