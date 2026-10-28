"""The file name of a user's browser console socket.

The browser container's pool binds each live instance's x11vnc to a Unix socket
under a directory it shares with the web container, and the web process's
console relay connects to it. Both sides derive the name from the user id with
this one function, so the two cannot disagree about which display a name leads
to.

A hash rather than the id: a user id may hold characters a file name cannot,
and AF_UNIX paths are short. A hash of the id rather than the pool's slot
number: a slot is reused across users, so a stale slot reference could open
somebody else's display.

stdlib-only, imports nothing from istota. Vendored byte for byte into
``docker/browser/lib/istota_browser_console.py`` by
``scripts/sync-devbox-lib.sh``; edit this copy and run the script.
"""

from __future__ import annotations

import hashlib

_HEX_CHARS = 32


def console_socket_name(user_id: str) -> str:
    """``<first 32 hex characters of sha256(user_id)>.sock``."""
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return digest[:_HEX_CHARS] + ".sock"
