"""Connecting to a Unix socket whose leaf somebody else can write.

A devbox's exec socket lives in a volume that devbox mounts read-write, so the
user inside it can replace ``exec.sock`` with a symlink naming another user's
socket as the istota container sees it. ``connect(2)`` on a path follows a
symlink, so the daemon would then speak to that other user's devbox.

On Linux the leaf is opened ``O_PATH | O_NOFOLLOW``, checked to be a socket
with ``fstat``, and connected through ``/proc/self/fd/<n>``, which names the
inode that was checked rather than whatever the path names a moment later. A
platform without ``O_PATH`` (macOS, where no deployment runs) gets the same
refusals from ``lstat`` and a connect by path, which leaves a swap window.

Only the leaf is held this way: a caller's directories above it must be ones
the writer of the leaf cannot replace. stdlib-only, imports nothing from istota.
"""

from __future__ import annotations

import errno
import os
import socket
import stat


def connect_no_follow(sock: socket.socket, path: str) -> None:
    """Connect ``sock`` to the socket at ``path``, refusing a symlink or a non-socket.

    Raises ``OSError`` like ``sock.connect`` does: ``ELOOP`` for a symlink,
    ``ENOTSOCK`` for anything that is not a socket.
    """
    o_path = getattr(os, "O_PATH", None)
    if o_path is None:
        info = os.lstat(path)
        _require_socket(info, path)
        sock.connect(path)
        return
    try:
        fd = os.open(path, o_path | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OSError(errno.ELOOP, f"{path} is a symlink, which is never followed") from exc
        raise
    try:
        _require_socket(os.fstat(fd), path)
        sock.connect(f"/proc/self/fd/{fd}")
    finally:
        os.close(fd)


def _require_socket(info: os.stat_result, path: str) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise OSError(errno.ELOOP, f"{path} is a symlink, which is never followed")
    if not stat.S_ISSOCK(info.st_mode):
        raise OSError(errno.ENOTSOCK, f"{path} is not a socket")
