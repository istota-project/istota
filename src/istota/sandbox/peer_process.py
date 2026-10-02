"""Which process is on the other end of a Unix socket, and whose tree it is in.

The skill and network proxies' sockets are 0600, which keeps out other OS
users and nobody else: every task runs as the daemon's uid. Under bubblewrap only a task's own
socket is bound into its namespace, so that was enough there. On every shape
where the model runs without the sandbox — macOS, a container whose bwrap
probe fails, a Linux host with no bwrap — all tasks share one uid and one
``/tmp``, so any task could connect to any other live task's socket and be
served as that task's user (ISSUE-550).

The rule this module supplies is **the peer must descend from a process the
task registered**. The kernel reports the connecting pid (``SO_PEERCRED`` on
Linux, ``LOCAL_PEERPID`` on macOS), and the parent chain is walked from there.
No process chooses its parent, so a sibling cannot fake a place in the chain,
and each root is pinned to its start time so a recycled pid inherits nothing.
What the chain does not stop is a sibling getting its *code* run inside the
tree — a file the victim's task executes and the shared uid can write, or
``ptrace`` — which is why ``doctor`` still warns on an unsandboxed multi-user
host. A nonce in the environment was considered and not built, because a
same-uid process reads another's environment through ``/proc/<pid>/environ``
or ``KERN_PROCARGS2``, so it adds nothing the chain does not already decide.

Two ways a legitimate process fails the walk, both accepted: one whose ancestor
exited and left it reparented to init (a ``nohup … &`` that outlived its
shell), and one on a platform with neither peer-credential call, where every
answer is ``None`` and the caller refuses. Inside a bwrap namespace an orphan
is reparented to bwrap's own init, which is itself a descendant of the root, so
the sandboxed shape does not hit the first.

stdlib-only leaf, imports nothing from the package, and nothing raises.
"""

from __future__ import annotations

import contextlib
import os
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

__all__ = [
    "MAX_DEPTH",
    "PeerRoots",
    "descends_from",
    "parent_pid",
    "peer_pid",
    "reporting_pid",
    "start_time",
    "supported",
]

# A real chain from a task's root to a skill client is a handful of levels
# (bwrap, bwrap's init, the CLI, a shell, the client). The bound is only there
# so a cycle a stale read could produce cannot spin.
MAX_DEPTH = 128

# macOS: <sys/un.h> SOL_LOCAL and LOCAL_PEERPID, and <sys/proc_info.h>
# PROC_PIDTBSDINFO with its struct size and the offsets of pbi_ppid and
# pbi_start_tvsec (followed by pbi_start_tvusec). Python's
# socket module exposes none of them.
_SOL_LOCAL = 0
_LOCAL_PEERPID = 0x002
_PROC_PIDTBSDINFO = 3
_PROC_BSDINFO_SIZE = 136
_PBI_PPID_OFFSET = 16
_PBI_START_OFFSET = 120

_libproc = None
_libproc_lock = threading.Lock()


def peer_pid(conn: socket.socket) -> int | None:
    """The pid of the process that connected, in this process's pid namespace.

    ``None`` where the platform has no call for it or the call failed. Linux
    translates the pid into the reader's namespace, so a peer inside a bwrap
    ``--unshare-pid`` namespace is reported by its host pid.
    """
    try:
        if sys.platform.startswith("linux"):
            raw = conn.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"),
            )
            pid = struct.unpack("3i", raw)[0]
        elif sys.platform == "darwin":
            raw = conn.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, struct.calcsize("i"))
            pid = struct.unpack("i", raw)[0]
        else:
            return None
    except (OSError, struct.error, AttributeError, ValueError):
        return None
    # 0 is what Linux reports for a peer in a pid namespace it cannot map.
    return pid if pid > 0 else None


def _proc_info(pid: int, proc_root: Path) -> tuple[int, int] | None:
    """``(ppid, start)`` for ``pid``, or ``None`` if it cannot be read.

    ``start`` is an opaque token that differs between two processes that held
    the same pid: clock ticks since boot on Linux, microseconds since the epoch
    on macOS.
    """
    if sys.platform.startswith("linux"):
        try:
            stat = (proc_root / str(pid) / "stat").read_bytes()
        except (OSError, ValueError):
            return None
        # The command name is in parentheses and may itself contain spaces and
        # parentheses, so split after the *last* one. What follows starts at
        # field 3 (state), so ppid (4) is [1] and starttime (22) is [19].
        close = stat.rfind(b")")
        if close < 0:
            return None
        fields = stat[close + 1:].split()
        try:
            return int(fields[1]), int(fields[19])
        except (IndexError, ValueError):
            return None
    if sys.platform == "darwin":
        return _darwin_proc_info(pid)
    return None


def _darwin_proc_info(pid: int) -> tuple[int, int] | None:
    global _libproc
    try:
        import ctypes

        with _libproc_lock:
            if _libproc is None:
                _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            lib = _libproc
        buf = ctypes.create_string_buffer(_PROC_BSDINFO_SIZE)
        got = lib.proc_pidinfo(
            int(pid), _PROC_PIDTBSDINFO, 0, buf, _PROC_BSDINFO_SIZE,
        )
        if got != _PROC_BSDINFO_SIZE:
            return None
        ppid = struct.unpack_from("I", buf, _PBI_PPID_OFFSET)[0]
        sec, usec = struct.unpack_from("QQ", buf, _PBI_START_OFFSET)
        return ppid, sec * 1_000_000 + usec
    except (OSError, AttributeError, ValueError, TypeError):
        return None


def parent_pid(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """``pid``'s parent, or ``None`` if it cannot be read (gone, or unsupported)."""
    info = _proc_info(pid, proc_root)
    return None if info is None else info[0]


def start_time(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """When ``pid`` started, as a token that tells two holders of one pid apart."""
    info = _proc_info(pid, proc_root)
    return None if info is None else info[1]


def descends_from(
    pid: int,
    roots: Mapping[int, int],
    *,
    parent_of: Callable[[int], int | None] = parent_pid,
    start_of: Callable[[int], int | None] = start_time,
    max_depth: int = MAX_DEPTH,
) -> bool:
    """Whether ``pid`` is one of ``roots`` or has one of them as an ancestor.

    ``roots`` maps each root pid to the start time recorded when it was
    registered, and a match needs both. A pid number alone would let whatever
    process later holds a recycled number inherit the root's authority, and
    on the unsandboxed shapes a sibling task can cycle the pid space.

    Refuses on anything it cannot read, so an unreadable link in the chain is a
    refusal rather than a pass. Stops at pid 1: init is everybody's ancestor,
    and a root of 1 would authorize the whole host.
    """
    current = pid
    for _ in range(max_depth):
        if current is None or current <= 1:
            return False
        if current in roots:
            started = start_of(current)
            if started is not None and started == roots[current]:
                return True
        current = parent_of(current)
    return False


def supported() -> bool:
    """Whether both calls answer here: a peer pid, and a parent for it."""
    try:
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        return (
            peer_pid(a) == os.getpid()
            and parent_pid(os.getpid()) == os.getppid()
        )
    finally:
        a.close()
        b.close()


@contextlib.contextmanager
def reporting_pid(
    on_pid: Callable[[int], None] | None,
    preexec: Callable[[], None] | None = None,
) -> Iterator[Callable[[], None] | None]:
    """A ``preexec_fn`` that reports the child's pid to ``on_pid``.

    For a spawn made with ``subprocess.run``, which never hands the pid back.
    The child writes its pid down a pipe between ``fork`` and ``exec``, and a
    reader thread in the parent calls ``on_pid`` with it — so ``on_pid`` runs
    while the child is alive, which is when a registration is any use. The
    write end is close-on-exec, and CPython runs ``preexec_fn`` before it
    closes inherited descriptors, so the child can write and the program it
    execs never holds the pipe.

    ``preexec`` runs first, so a cgroup placement composed with this happens
    before anything else in the child. A failed write is swallowed: a pid that
    was not reported costs the child its registration, never its spawn.

    With ``on_pid`` of ``None`` this yields ``preexec`` unchanged.
    """
    if on_pid is None:
        yield preexec
        return
    read_fd, write_fd = os.pipe()

    def _child() -> None:
        if preexec is not None:
            preexec()
        try:
            os.write(write_fd, b"%d\n" % os.getpid())
        except OSError:
            pass

    def _read() -> None:
        buffered = b""
        try:
            while True:
                chunk = os.read(read_fd, 64)
                if not chunk:
                    break
                buffered += chunk
                while b"\n" in buffered:
                    line, buffered = buffered.split(b"\n", 1)
                    try:
                        on_pid(int(line))
                    except Exception:
                        pass
        except OSError:
            pass
        finally:
            os.close(read_fd)

    reader = threading.Thread(target=_read, daemon=True, name="pid-report")
    reader.start()
    try:
        yield _child
    finally:
        # The child's copy closed at its exec; this is the last writer, so the
        # reader sees EOF and exits.
        os.close(write_fd)
        reader.join(timeout=5)


class PeerRoots:
    """Thread-safe task roots shared by the skill and network proxies.

    A root is pinned to its start time. Connections may wait briefly for the
    spawn callback to register a process that already reached the socket.
    """

    def __init__(self):
        self._roots: dict[int, int] = {}
        self._changed = threading.Condition()

    def authorize(self, pid: int) -> bool:
        pid = int(pid)
        started = start_time(pid)
        if started is None:
            return False
        with self._changed:
            self._roots[pid] = started
            self._changed.notify_all()
        return True

    def revoke(self, pid: int) -> None:
        with self._changed:
            self._roots.pop(int(pid), None)

    @property
    def pids(self) -> frozenset[int]:
        with self._changed:
            return frozenset(self._roots)

    def contains(self, pid: int | None, *, grace_seconds: float) -> bool:
        if pid is None:
            return False
        deadline = time.monotonic() + grace_seconds
        with self._changed:
            while True:
                if descends_from(pid, dict(self._roots)):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
