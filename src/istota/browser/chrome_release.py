"""Google's stable Chrome releases for Linux, and how far a Chrome trails them (#930).

The browser image is the only way its Chrome gets updated, so the daemon asks
Google's version history API which stable releases there have been and when
each started serving. `browser.chrome_version` in doctor reads it. The build
half, which keys the image's Chrome layers on the current stable version, is
`resolve_chrome_stable` in `host/istota-stack`, reading the same listing by the
same rule: a release counts once it serves every user (`fraction` 1), since
Google also lists a staged rollout's arms there.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from istota.lib import fixed_get

RELEASES_URL = (
    "https://versionhistory.googleapis.com/v1/chrome/platforms/linux/channels/stable"
    "/versions/all/releases?pageSize=50"
)

_VERSION = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\.(\d+)\b")


class HistoryUnavailable(Exception):
    """The release history could not be read; the message is a short reason."""


@dataclass(frozen=True)
class Release:
    version: tuple[int, ...]
    text: str
    started: datetime


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """`157.0.8103.38`, alone or inside `Google Chrome 157.0.8103.38`, as numbers."""
    if not text:
        return None
    match = _VERSION.search(text)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def parse_releases(body: bytes) -> list[Release]:
    """The API's `releases` listing, full rollouts only; an entry missing a
    version or a start is dropped, and a listing with none left is unavailable."""
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise HistoryUnavailable("not JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("releases"), list):
        raise HistoryUnavailable("no releases listed")
    releases = []
    for entry in data["releases"]:
        if not isinstance(entry, dict):
            continue
        text = entry.get("version")
        version = parse_version(text) if isinstance(text, str) else None
        serving = entry.get("serving")
        start = serving.get("startTime") if isinstance(serving, dict) else None
        if version is None or not isinstance(start, str) or _VERSION.fullmatch(text) is None:
            continue
        fraction = entry.get("fraction", 1)
        if isinstance(fraction, (int, float)) and fraction < 1:
            continue
        try:
            started = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            continue
        releases.append(Release(version, text, started))
    if not releases:
        raise HistoryUnavailable("no release could be read")
    return releases


def fetch_stable_releases(timeout: float = 10.0) -> list[Release]:
    """One unauthenticated GET to Google, no redirect followed and no proxy consulted."""
    try:
        status, body, _ = fixed_get.get(RELEASES_URL, {}, timeout)
    except Exception as exc:  # noqa: BLE001 - a transport failure of any kind is one answer
        raise HistoryUnavailable(fixed_get.describe_failure(exc)) from exc
    if status != 200:
        raise HistoryUnavailable(f"HTTP {status}")
    return parse_releases(body)


def trailing(running: tuple[int, ...], releases: list[Release]) -> tuple[Release | None, datetime | None]:
    """The newest stable release, and since when `running` has been behind.

    Behind since the start of the oldest release newer than `running`, which is
    when an update would first have changed it; None when nothing is newer.
    """
    newest = max(releases, key=lambda r: r.version, default=None)
    newer = [r for r in releases if r.version > running]
    since = min((r.started for r in newer), default=None)
    return newest, since
