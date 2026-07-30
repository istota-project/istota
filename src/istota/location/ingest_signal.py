"""Cross-process signal that the location ingest token map is stale.

The webhook receiver holds ``token -> user_id`` in memory, rebuilt only by
``reload_config()`` — which runs at startup and on ``SIGHUP``. The web app
is where a token is actually provisioned, and it is a *different process*
(separate systemd unit on the server, separate container under Docker), so
a freshly generated token 403s until someone restarts the receiver. That is
a bad first impression of provisioning and an opaque one: the token is
right, the URL is right, and the server says no.

A stamped file is the mechanism because it needs neither a pid nor a
signal permission nor a shared network namespace, which rules out SIGHUP
in the Docker shape. The receiver pays one ``os.stat`` per ingest request
to read it — cheap next to the batch of inserts that follows, and ingest
requests are minutes apart.

Every function here is best-effort by design. A failed signal degrades to
the pre-existing behaviour (the token lands on the next restart or SIGHUP),
which is a delay; raising into a successful secret write would turn a
delay into a lost token.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path


logger = logging.getLogger(__name__)


SENTINEL_NAME = ".location_ingest_reload"


def sentinel_path(db_path: Path | str) -> Path:
    """Where the sentinel lives, given the framework DB path.

    Beside ``istota.db`` rather than in the workspace: both processes
    already agree on that directory, it is local disk (the receiver reads
    it per request, and the rclone mount's latency is unbounded), and it is
    not user-visible, so nothing syncs it to a phone.
    """
    return Path(db_path).parent / SENTINEL_NAME


def signal_reload(db_path: Path | str) -> None:
    """Stamp the sentinel so the receiver rebuilds its token map."""
    path = sentinel_path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # touch() alone does not move mtime on an existing file, and the
        # receiver compares mtimes — so the utime is the actual signal.
        path.touch(exist_ok=True)
        os.utime(path, None)
    except OSError as exc:
        logger.warning(
            "could not stamp the location ingest reload sentinel at %s: %s "
            "(a new token will apply on the next receiver restart)",
            path, exc,
        )


def reload_stamp(db_path: Path | str) -> float:
    """The sentinel's mtime, or ``0.0`` when there is none to read.

    ``0.0`` is "no signal", which is why it is also the answer for an
    unreadable sentinel: a receiver that cannot stat the file should keep
    serving the token map it has rather than reload on every request.
    """
    try:
        return sentinel_path(db_path).stat().st_mtime
    except OSError:
        return 0.0
