"""Periodic status file writer for the NC app admin panel."""

import json
import logging
import time

from .build_info import RUNNING_VERSION
from .config import Config
from .nextcloud import dav_files_url, dav_request

logger = logging.getLogger("istota.status_writer")

# Rewrite an unchanged status at least this often, so `updated_at` still
# advances for a reader judging whether the daemon is alive.
HEARTBEAT_SECONDS = 300

# The gate's clock and ours differ by the DB queries in between, so a tick
# landing a few ms short of the heartbeat would otherwise slip it a whole
# 60 s interval. Under half a gate interval keeps it on the fifth tick.
_HEARTBEAT_SLACK = 30

# Fields that change on every call and so are left out of the change check.
_VOLATILE_FIELDS = ("uptime_seconds", "updated_at")

_daemon_start_time: float = 0.0
_last_written: dict | None = None
_last_written_at: float = 0.0


def init_status_writer() -> None:
    """Record daemon start time. Call once at scheduler startup."""
    global _daemon_start_time, _last_written, _last_written_at
    _daemon_start_time = time.time()
    _last_written = None
    _last_written_at = 0.0


def write_status(
    config: Config, active_workers: int, pending_fg: int, pending_bg: int,
    *, pending_assignment: int = 0,
) -> None:
    """Write a JSON status file to the bot user's Nextcloud storage via WebDAV.

    The NC app reads ``status.json`` from the root of the bot user's file tree
    (via ``IRootFolder::getUserFolder``), so we PUT it there directly. The root
    always exists, so there is no directory to create first.

    That is why it is one of the two callers passing ``prefixed=False``. The
    subject here is the *account* root, not the daemon's storage root, and on a
    deployment where those differ — one reaching its storage through an
    external-storage mount — the prefixed path lands on the mount, where
    ``getUserFolder`` does not look. On the rclone-mount deploy the two are the
    same directory, so the prefix is empty and the URL is the same either way —
    the file just sits in the mount root now, beside ``Users/``, ``Channels/``
    and ``Talk/``, rather than in a ``config/`` directory of its own.

    The gate calls this every minute, but it PUTs only when the payload less
    its volatile fields differs from the last one written, or when
    ``HEARTBEAT_SECONDS`` have passed since the last successful PUT. The last
    payload is recorded only on success, so a failed write is retried on the
    next call, and a restart always writes first.
    """
    global _last_written, _last_written_at
    nc = config.nextcloud
    if not nc.url or not nc.username:
        return

    now = time.time()
    status = {
        "bot_name": config.bot_name,
        "version": RUNNING_VERSION,
        "status": "online",
        "started_at": int(_daemon_start_time),
        "uptime_seconds": int(now - _daemon_start_time) if _daemon_start_time else 0,
        "worker_pool": {
            "active": active_workers,
            "max_foreground": config.scheduler.max_foreground_workers,
            "max_background": config.scheduler.max_background_workers,
        },
        "queue": {
            "pending_foreground": pending_fg,
            "pending_background": pending_bg,
            "pending_assignment": pending_assignment,
        },
        "users_configured": len(config.users),
        "updated_at": int(now),
    }

    stable = {k: v for k, v in status.items() if k not in _VOLATILE_FIELDS}
    if stable == _last_written and now - _last_written_at < HEARTBEAT_SECONDS - _HEARTBEAT_SLACK:
        return

    try:
        dav_request(
            config,
            "PUT",
            dav_files_url(config, "status.json", prefixed=False),
            content=json.dumps(status, indent=2),
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
    except Exception as e:
        logger.error("Failed to write status.json via WebDAV: %s", e)
        return
    _last_written = stable
    _last_written_at = now
