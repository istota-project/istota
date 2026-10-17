"""Copying a local file into a Talk conversation's folder, for a share into it.

A Talk attachment is a share of a file in the bot's own Nextcloud files
(`TalkClient.share_file`). When the file is not already there (a local
workspace, or bytes the daemon made), it is uploaded first into the bot's
``Talk/<token>/<UTC day>/``, where Nextcloud keeps conversation files, under a
name no earlier upload holds: ``If-None-Match: *``, numbered on a 412. A
second post of the same name leaves the first one as it was.

This sits below the `nextcloud` skill's model-facing guards (the audience
check, the host-path scope): its two callers are that CLI, after its guards,
and the daemon's own delivery of a generated picture (a location share's map).
The share itself is the caller's, since one runs on a transient client and the
other on the daemon's persistent one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import dav as dav_mod
from ._http import OcsError

if TYPE_CHECKING:
    from istota.config import Config

#: How many numbered names an upload tries before giving up.
MAX_UPLOAD_NAMES = 100


class NamesExhausted(Exception):
    """The day's folder already holds `MAX_UPLOAD_NAMES` files of this name."""

    def __init__(self, folder: str):
        super().__init__(folder)
        self.folder = folder


def talk_folder(token: str, now: datetime | None = None) -> str:
    """The bot's folder for ``token``'s files uploaded on ``now``'s UTC day."""
    day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return f"Talk/{token}/{day}"


def upload_copy(
    config: "Config", token: str, source: Path, filename: str,
    *, supports_chunking: bool = True,
) -> str:
    """Upload ``source`` into ``token``'s Talk folder as ``filename``; the remote path.

    ``token`` must already be a validated conversation token and ``filename``
    a safe single name (`lib.filenames.safe_filename`). Raises `OcsError` on a
    DAV failure and `NamesExhausted` when every numbered name is taken.
    """
    from istota.lib.filenames import filename_parts, numbered_name

    folder = talk_folder(token)
    dav_mod.ensure_dirs(config, folder, prefixed=False)
    stem, ext = filename_parts(filename)
    for n in range(1, MAX_UPLOAD_NAMES + 1):
        remote = f"/{folder}/{numbered_name(stem, ext, n)}"
        try:
            dav_mod.upload(
                config, source, remote, supports_chunking=supports_chunking,
                prefixed=False, overwrite=False,
            )
            return remote
        except OcsError as e:
            if e.http_status != 412:
                raise
    raise NamesExhausted(folder)


__all__ = ["MAX_UPLOAD_NAMES", "NamesExhausted", "talk_folder", "upload_copy"]
