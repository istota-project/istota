"""Read-only browser discovery for the admin UI."""
import asyncio
import math
from urllib.parse import parse_qsl, urlsplit

import httpx

from .config import BrowserConfig


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
