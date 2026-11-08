"""The deployment's worldview, ``{root}/WORLDVIEW.md`` (#929).

An optional layer between emissaries and the persona, one for every user and
every room. Nothing is shipped: no file, no layer. ``executor.load_worldview``
is the reader; this module holds the name and the ownership rule.

**On Nextcloud storage only the bot account's own file is read.** A share
lands at the bot's root, so a file another account shares under this name
would otherwise put its text in the system half of every user's prompt.
``PERSONA.md`` is kept from that by being present (the sync writes it, so a
share under the name gets a suffix); nothing occupies this name. The owner is
asked over WebDAV once per version of the file the mount shows, and the read
counts only when the server's size matches the bytes read, so a stale mount
copy is not credited to the server's current owner. No answer, no layer.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from istota.prompts.persona import PERSONA_MAX_BYTES

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger("istota.prompts.worldview")

WORLDVIEW_FILENAME = "WORLDVIEW.md"
#: The persona's cap; the draft in #929 is about 15 KB.
WORLDVIEW_MAX_BYTES = PERSONA_MAX_BYTES

_lock = threading.Lock()
#: (st_ino, st_mtime_ns, st_size) of the mount's copy -> (owner, server size)
_owners: dict[tuple[int, int, int], tuple[str, int | None]] = {}


def worldview_path(config: "Config") -> Path | None:
    """``{root}/WORLDVIEW.md``, or None on a deployment with no file root."""
    if not config.has_workspace:
        return None
    return Path(config.workspace_path) / WORLDVIEW_FILENAME


def forget_owners() -> None:
    with _lock:
        _owners.clear()


def _dav_stat(config: "Config", path: str, **kwargs) -> dict:
    from istota.nextcloud import dav

    return dav.stat(config, path, **kwargs)


def bot_owns(config: "Config", path: Path, read_bytes: int) -> bool:
    """Whether the file just read is the bot account's own. Never raises.

    True off Nextcloud storage, where nothing can share into the root.
    """
    if not config.storage_is_nextcloud:
        return True
    bot = config.nextcloud.username
    if not bot:
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    key = (st.st_ino, st.st_mtime_ns, st.st_size)
    with _lock:
        known = _owners.get(key)
    if known is None:
        try:
            entry = _dav_stat(config, "/" + WORLDVIEW_FILENAME, timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - no answer is no layer
            logger.debug("worldview owner unknown: %s", type(exc).__name__)
            return False
        owner = entry.get("owner_id") or ""
        if not owner:
            return False
        known = (owner, entry.get("size"))
        with _lock:
            _owners.clear()
            _owners[key] = known
    owner, server_size = known
    if owner != bot:
        logger.debug("worldview refused: not the bot account's file")
        return False
    return server_size == read_bytes
