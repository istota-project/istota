"""Small helpers every verb module shares: paging arguments, a total header,
and the lookup command an ambiguous write reports.

A module of its own so `media` (which `content`'s post writes upload through)
does not have to import `content` back.
"""

from __future__ import annotations

import os
import shlex
import stat

from .client import WordPressError

MAX_LIMIT = 100


def int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def limit_arg(value: int | None, default: int) -> int:
    if value is None:
        return default
    if value < 1 or value > MAX_LIMIT:
        raise WordPressError(f"--limit must be between 1 and {MAX_LIMIT}.", "validation_error")
    return value


def total_header(headers, name: str) -> int | None:
    return int_or_none(headers.get(name)) if headers is not None else None


def lookup(ctx, *argv: str) -> str:
    """The command that finds out whether an ambiguous write applied.

    It names ``--site`` and ``--blog`` explicitly: run bare, it would search
    the default site and find nothing, which reads as "send it again".
    """
    scope = ["--site", ctx.record.name]
    if ctx.blog:
        scope += ["--blog", ctx.blog]
    return " ".join(shlex.quote(part) for part in (*argv, *scope))


def read_text_file(path: str, label: str, cap: int) -> str:
    """A UTF-8 file the `EGRESS` stamp already resolved, read without following
    a symlink swapped in since, and bounded by its own descriptor's size."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise WordPressError(f"Could not open {label}: {exc.strerror}.",
                             "validation_error") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WordPressError(f"{label} is not a regular file.", "validation_error")
        if info.st_size > cap:
            raise WordPressError(f"{label} is over {cap // 1024} KiB.", "validation_error")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(cap + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > cap:
        raise WordPressError(f"{label} is over {cap // 1024} KiB.", "validation_error")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise WordPressError(f"{label} is not UTF-8 text.", "validation_error") from None
