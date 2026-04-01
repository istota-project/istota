"""Periodic status file writer for the NC app admin panel."""

import json
import logging
import time
from pathlib import Path

from . import __version__
from .config import Config

logger = logging.getLogger("istota.status_writer")

_daemon_start_time: float = 0.0


def init_status_writer() -> None:
    """Record daemon start time. Call once at scheduler startup."""
    global _daemon_start_time
    _daemon_start_time = time.time()


def write_status(config: Config, active_workers: int, pending_fg: int, pending_bg: int) -> None:
    """Write a JSON status file readable by the NC app.

    The file is written to ``{users_dir}/../status.json`` (i.e. the config
    base directory, sibling to ``users/``).
    """
    if config.users_dir is None:
        return

    status_path = config.users_dir.parent / "status.json"
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
        tmp_path = status_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(status, indent=2))
        tmp_path.rename(status_path)
    except Exception as e:
        logger.error("Failed to write status file %s: %s", status_path, e)
