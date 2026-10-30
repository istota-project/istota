"""Opens a task's linked resources by descriptor, then execs bwrap holding them.

``python -I -m istota.sandbox.bind_launcher --binds JSON -- BWRAP ARGS...``, which
``render_bwrap_argv`` puts in front of bwrap when a plan has a linked resource
(ISSUE-881). A resource sits in a tree another user's task may write, so a bind
that names its path binds whatever is at that name when bwrap walks it, and a
validated path says nothing about what the mount gets.

Each ``{"root", "parts"}`` in ``JSON`` pairs in order with one ``--ro-bind-fd`` or
``--bind-fd`` in the argv whose fd is :data:`FD_PLACEHOLDER`. The walk from the
daemon-composed root follows no link, the descriptor pins the object it found,
and bwrap binds that. bwrap goes from the descriptor to a name and back, so it
compares the mount's device and inode against the descriptor and dies before
running anything if a swap got in between ("Race detected binding fd").

A process rather than descriptors the daemon passes down, because the ``claude``
CLI spawns the command on that brain, and it hands a child no descriptor of ours.

A resource that fails the walk loses its bind and the task runs without it, as a
missing source always has: failing the namespace would let a task in one home
stop another user's tasks by swapping a name they share. ``ARGV[0]`` must be
absolute, because the environment here is the task's and its ``PATH`` starts in
directories the model can write. Any other refusal exits 2 before running anything.

Imports only ``sandbox.confined``, which is stdlib-only: this runs under
``python -I`` before any sandbox exists. ``tests/test_sandbox_bind_launcher.py``,
``tests/linux/test_resource_bind_identity.py``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from .confined import open_dir, valid_part

FD_PLACEHOLDER = "<fd>"

_FD_FLAGS = frozenset({"--ro-bind-fd", "--bind-fd"})
_EXIT_REFUSED = 2
# macOS has no O_PATH. bwrap never runs there; this lets the default suite walk.
_O_PATH = getattr(os, "O_PATH", os.O_RDONLY | os.O_NONBLOCK)


class Refused(Exception):
    pass


def open_source(root: str, parts: list[str]) -> int | None:
    """An inheritable ``O_PATH`` fd on ``{root}/{parts...}``, or None.

    None when a component below the root is a link or missing, or the leaf is not
    a directory or a regular file, the rule ``linked_resource_parts`` applied at
    planning time.
    """
    if not parts or not valid_part(parts[-1]):
        return None
    dir_fd = open_dir(Path(root), *parts[:-1])
    if dir_fd is None:
        return None
    try:
        fd = os.open(parts[-1], _O_PATH | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return None
    finally:
        os.close(dir_fd)
    mode = os.fstat(fd).st_mode
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        os.close(fd)
        return None
    os.set_inheritable(fd, True)
    return fd


def apply_binds(binds: list[dict], command: list[str]) -> list[str]:
    """``command`` with each placeholder an open fd, or its bind removed."""
    end = command.index("--") if "--" in command else len(command)
    slots = [
        i for i in range(end - 2)
        if command[i] in _FD_FLAGS and command[i + 1] == FD_PLACEHOLDER
    ]
    if len(slots) != len(binds):
        raise Refused("the binds do not match the command's fd placeholders")
    result = list(command)
    # Last first, so a removal leaves the earlier slots where they are.
    for slot, bind in zip(reversed(slots), reversed(binds)):
        fd = open_source(bind["root"], bind["parts"])
        if fd is None:
            print(
                f"sandbox: not binding {result[slot + 2]}: it failed the no-link walk",
                file=sys.stderr,
            )
            del result[slot:slot + 3]
        else:
            result[slot + 1] = str(fd)
    return result


def _parse_argv(argv: list[str]) -> tuple[list[dict], list[str]]:
    if len(argv) < 4 or argv[0] != "--binds" or argv[2] != "--":
        raise Refused("usage: --binds JSON -- ARGV...")
    try:
        binds = json.loads(argv[1])
    except ValueError:
        raise Refused("the binds are not JSON") from None
    command = argv[3:]
    if not os.path.isabs(command[0]):
        raise Refused("the command must be an absolute path")
    return binds, command


def main(argv: list[str] | None = None) -> int:
    try:
        binds, command = _parse_argv(sys.argv[1:] if argv is None else argv)
        command = apply_binds(binds, command)
        os.execv(command[0], command)
    except Refused as exc:
        print(f"bind_launcher: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"bind_launcher: cannot exec: {exc.strerror}", file=sys.stderr)
    return _EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
