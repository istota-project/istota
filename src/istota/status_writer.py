"""Periodic status file writer for the NC app admin panel."""

import json
import logging
import time

from . import __version__
from .config import Config
from .nextcloud import dav_files_url, dav_request

logger = logging.getLogger("istota.status_writer")

_daemon_start_time: float = 0.0


def init_status_writer() -> None:
    """Record daemon start time. Call once at scheduler startup."""
    global _daemon_start_time
    _daemon_start_time = time.time()


def write_status(config: Config, active_workers: int, pending_fg: int, pending_bg: int) -> None:
    """Write a JSON status file to the bot user's Nextcloud storage via WebDAV.

    The NC app reads ``config/status.json`` from the bot user's file tree
    (via ``IRootFolder::getUserFolder``), so we PUT it there directly.
    """
    nc = config.nextcloud
    if not nc.url or not nc.username:
        return

    now = time.time()
    status = {
        "bot_name": config.bot_name,
        "version": __version__,
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
        },
        "users_configured": len(config.users),
        "updated_at": int(now),
    }

    try:
        # Ensure config/ exists. 405 means it already does, which is the
        # steady state — dav_request accepts it rather than raising.
        dav_request(
            config,
            "MKCOL",
            dav_files_url(config, "config"),
            timeout=10.0,
            ok_statuses=(201, 405),
        )
        dav_request(
            config,
            "PUT",
            dav_files_url(config, "config/status.json"),
            content=json.dumps(status, indent=2),
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
    except Exception as e:
        logger.error("Failed to write status.json via WebDAV: %s", e)
