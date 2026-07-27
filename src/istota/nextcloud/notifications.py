"""Nextcloud notifications and the activity feed — read and dismiss only.

*Sending* a Nextcloud notification needs the ``admin_notifications`` app and
admin rights, and the bot already has two working push channels to its own user
(ntfy and Talk), so the send path is deferred rather than half-built.

Both feeds are bounded by default: an unbounded activity stream is a
context-flooding hazard, and every entry in either is text other people wrote.
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ._http import DEFAULT_TIMEOUT, OcsError, ocs_get, ocs_request

NOTIFICATIONS_PATH = "/apps/notifications/api/v2/notifications"
ACTIVITY_PATH = "/apps/activity/api/v2/activity"

DEFAULT_LIMIT = 25


def list_notifications(
    config: Config, limit: int = DEFAULT_LIMIT, timeout: float = DEFAULT_TIMEOUT
) -> list[dict[str, Any]]:
    data = ocs_get(config, NOTIFICATIONS_PATH, timeout=timeout)
    items = list(data or [])
    return items[:limit] if limit > 0 else items


def get_notification(
    config: Config, notification_id: int, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    data = ocs_get(config, f"{NOTIFICATIONS_PATH}/{notification_id}", timeout=timeout)
    return data if isinstance(data, dict) else {}


def dismiss(
    config: Config, notification_id: int, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    ocs_request(config, "DELETE", f"{NOTIFICATIONS_PATH}/{notification_id}", timeout=timeout)
    return {"status": "ok", "dismissed": notification_id}


def dismiss_all(
    config: Config,
    *,
    capabilities: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Clear every notification. Gated on the server advertising ``delete-all``."""
    if capabilities is not None and not supports_delete_all(capabilities):
        raise OcsError(
            "This server's notifications app does not support clearing all "
            "notifications at once; dismiss them individually by id",
            None,
            None,
            NOTIFICATIONS_PATH,
        )
    ocs_request(config, "DELETE", NOTIFICATIONS_PATH, timeout=timeout)
    return {"status": "ok", "dismissed": "all"}


def supports_delete_all(capabilities: dict[str, Any]) -> bool:
    caps = capabilities.get("capabilities") if isinstance(capabilities, dict) else None
    caps = caps if isinstance(caps, dict) else {}
    notifications = caps.get("notifications")
    if not isinstance(notifications, dict):
        return False
    return "delete-all" in (notifications.get("ocs-endpoints") or [])


def list_activity(
    config: Config,
    *,
    since: int | None = None,
    limit: int = DEFAULT_LIMIT,
    activity_filter: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    # Two distinct routes: a named stream is a path segment
    # (/activity/files), while an object lookup is the reserved "filter"
    # segment plus query params. /activity/filter/<name> is neither and 404s.
    params: dict[str, str] = {"limit": str(limit if limit > 0 else DEFAULT_LIMIT)}
    if since is not None:
        params["since"] = str(since)

    if object_type and object_id:
        path = f"{ACTIVITY_PATH}/filter"
        params["object_type"] = object_type
        params["object_id"] = str(object_id)
    elif activity_filter:
        path = f"{ACTIVITY_PATH}/{activity_filter}"
    else:
        path = ACTIVITY_PATH

    data = ocs_get(config, path, params=params, timeout=timeout)
    return list(data or [])
