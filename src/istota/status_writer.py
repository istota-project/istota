"""Periodic status file writer for the NC app admin panel."""

import json
import logging
import time

import httpx

from . import __version__
from .config import Config

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

    base_url = nc.url.rstrip("/")
    auth = (nc.username, nc.app_password)
    dav_base = f"{base_url}/remote.php/dav/files/{nc.username}"

    try:
        # Ensure config/ directory exists (MKCOL is a no-op if it already exists)
        httpx.request("MKCOL", f"{dav_base}/config", auth=auth, timeout=10.0)

        # PUT the status file
        resp = httpx.put(
            f"{dav_base}/config/status.json",
            content=json.dumps(status, indent=2),
            headers={"Content-Type": "application/json"},
            auth=auth,
            timeout=10.0,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error("Failed to write status.json via WebDAV: %s", e)
