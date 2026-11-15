"""A filesystem like rclone's FUSE mount, as far as publishing a file sees it.

No hard links, and an inode number that follows the path rather than the
file: a renamed file comes back with a new number, and a file recreated at the
same name keeps the old one (ISSUE-972). Only names opened or stat'ed relative
to a directory descriptor are renumbered, which is how the publishing code
reaches them, so the rest of the process is left alone.
"""

import errno
import os
import zlib


def _number(name: str) -> int:
    return zlib.crc32(name.encode()) + 1


def _renumbered(result: os.stat_result, name: str) -> os.stat_result:
    fields = list(result)
    fields[1] = _number(name)  # st_ino
    return os.stat_result(fields)


def install(monkeypatch) -> None:
    real_open, real_stat, real_fstat = os.open, os.stat, os.fstat
    opened: dict[int, str] = {}

    def no_links(*args, **kwargs):
        raise OSError(errno.EPERM, "no hard links here")

    def tracking_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if kwargs.get("dir_fd") is not None:
            opened[fd] = path
        else:
            opened.pop(fd, None)
        return fd

    def path_stat(path, **kwargs):
        result = real_stat(path, **kwargs)
        if kwargs.get("dir_fd") is not None:
            return _renumbered(result, path)
        return result

    def path_fstat(fd):
        result = real_fstat(fd)
        if fd in opened:
            return _renumbered(result, opened[fd])
        return result

    monkeypatch.setattr(os, "link", no_links)
    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "stat", path_stat)
    monkeypatch.setattr(os, "fstat", path_fstat)
