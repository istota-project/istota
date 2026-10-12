"""Read-only browser discovery for the admin UI.

On the Docker stack the browser container shares a network with the daemon
alone, so the web process cannot reach its API (ISSUE-699). The daemon takes
the snapshot on a gate and records it; the page reads the record, and probes
for itself only where no daemon has recorded one.
"""
import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

from istota.config import BrowserConfig, Config

logger = logging.getLogger("istota.webui.admin_browsers")

# A reserved shared_kv namespace: written by the daemon, never by a task.
SNAPSHOT_NAMESPACE = "_browser"
_SNAPSHOT_KEY = "instances"
RECORD_INTERVAL_SECONDS = 15
#: Four missed records: the daemon is down or cannot reach the browser either.
STALE_AFTER_SECONDS = 60


def _console_url(value: str) -> str:
    """Require an absolute viewer URL without embedded VNC credentials."""
    try:
        parts = urlsplit(value)
        options = parse_qsl(parts.query) + parse_qsl(parts.fragment)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or any(name.lower() in {"password", "passwd", "vnc_password"} for name, _ in options)):
            return ""
    except ValueError:
        return ""
    return value


async def snapshot(config: BrowserConfig) -> dict:
    base = _console_url(config.vnc_url)
    result = {"status": "disabled", "console_configured": bool(base), "instances": []}
    if not config.enabled:
        return result
    try:
        # Bound the whole read as well as each network operation. Redirects
        # would send this management request somewhere other than the service.
        async with asyncio.timeout(6):
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                response = await client.get(
                    config.api_url.rstrip("/") + "/instances", params={"vnc_url": base},
                )
                response.raise_for_status()
                data = response.json()
        rows = data["instances"]
        if not isinstance(rows, list):
            raise ValueError("Invalid instances")
        instances = []
        for row in rows:
            user, slot, idle, url = row["user"], row["slot"], row["idle_seconds"], row["url"]
            if (not isinstance(user, str) or not user or type(slot) is not int or slot < 0
                    or type(idle) not in {int, float} or not math.isfinite(idle) or idle < 0
                    or not isinstance(url, str) or (base and not _console_url(url))):
                raise ValueError("Invalid instance metadata")
            instances.append({"user": user, "slot": slot, "idle_seconds": idle,
                              "url": url if base else ""})
        result.update(status="ok", instances=instances)
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
        # Network errors can include private addresses; keep them off the wire.
        result["status"] = "unavailable"
    return result


def record_snapshot(config: Config, result: dict, *, at: datetime | None = None) -> None:
    """Keep the daemon's snapshot for the admin page. Never raises."""
    if not Path(config.db_path).exists():
        return
    from istota import db

    stamp = (at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    value = json.dumps({**result, "as_of": stamp})
    try:
        with db.get_db(config.db_path) as conn:
            db.shared_kv_set(conn, SNAPSHOT_NAMESPACE, _SNAPSHOT_KEY, value, "daemon")
    except Exception as exc:  # noqa: BLE001 - a record for the page, not the probe
        logger.warning("browser_snapshot_unwritten err=%s", exc)


def recorded_snapshot(config: Config) -> dict | None:
    """The daemon's last snapshot, ``unavailable`` once stale, or None if none."""
    if not Path(config.db_path).exists():
        return None
    from istota import db

    with db.get_db(config.db_path) as conn:
        row = db.shared_kv_get(conn, SNAPSHOT_NAMESPACE, _SNAPSHOT_KEY)
    if row is None:
        return None
    recorded = json.loads(row["value"])
    at = datetime.strptime(recorded["as_of"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - at).total_seconds() > STALE_AFTER_SECONDS:
        recorded.update(status="unavailable", instances=[])
    return recorded


async def current(config: Config) -> dict:
    """What the admin page shows: the daemon's record where there is one."""
    if config.browser.enabled:
        recorded = await asyncio.to_thread(recorded_snapshot, config)
        if recorded is not None:
            return recorded
    return await snapshot(config.browser)
