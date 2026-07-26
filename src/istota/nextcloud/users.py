"""User and group lookup — non-admin-safe where the API allows it.

The bot is a regular Nextcloud user on most deployments. ``whoami`` and
``search`` use endpoints any user may call; the provisioning-API verbs
(``get_user``, ``list_groups``, ``group_members``) are admin-gated and answer
OCS 997 for a non-admin bot, so they re-raise with the cause named and
``search`` offered as the alternative.
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ._http import DEFAULT_TIMEOUT, OcsError, ocs_get

#: shareType values the autocomplete endpoint understands, mapped to the
#: ``--types`` flag's vocabulary.
SHARE_TYPES: dict[str, int] = {
    "users": 0,
    "groups": 1,
    "emails": 4,
    "federated": 6,
    "circles": 7,
    "talk": 10,
}

_ADMIN_HINT = (
    "This endpoint needs admin rights on the Nextcloud server and the bot "
    "account is a regular user here. Use `nextcloud user search QUERY` "
    "instead — it resolves users and groups through an endpoint any user may call."
)


def _reraise_with_admin_hint(err: OcsError) -> OcsError:
    """Re-shape an admin-gated denial so the cause and the alternative are visible."""
    if err.ocs_status in (401, 403, 997) or err.http_status in (401, 403):
        return OcsError(f"{err.message}. {_ADMIN_HINT}", err.http_status, err.ocs_status, err.endpoint)
    return err


def whoami(config: Config, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """The authenticated account: id, display name, email, quota, groups."""
    data = ocs_get(config, "/cloud/user", timeout=timeout)
    return data if isinstance(data, dict) else {}


def search(
    config: Config,
    query: str,
    *,
    types: list[str] | None = None,
    limit: int = 25,
    item_type: str = "file",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Autocomplete search over users/groups/emails/Talk rooms.

    Hits ``/core/autocomplete/get``, which a regular user may call — the
    workhorse lookup, and the one verb guaranteed to work as the bot.
    """
    requested = types or ["users", "groups"]
    unknown = [t for t in requested if t not in SHARE_TYPES]
    if unknown:
        raise OcsError(
            f"Unknown --types value(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(sorted(SHARE_TYPES))}",
            None,
            None,
            "/core/autocomplete/get",
        )

    params: dict[str, Any] = {
        "search": query,
        "itemType": item_type,
        "limit": str(limit),
        "shareTypes[]": [str(SHARE_TYPES[t]) for t in requested],
    }
    data = ocs_get(config, "/core/autocomplete/get", params=params, timeout=timeout)
    results = data if isinstance(data, list) else []
    return {"query": query, "types": requested, "count": len(results), "results": results}


def get_user(config: Config, uid: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Provisioning-API user record. Admin-only (or the bot's own account)."""
    try:
        data = ocs_get(config, f"/cloud/users/{uid}", timeout=timeout)
    except OcsError as e:
        raise _reraise_with_admin_hint(e) from e
    return data if isinstance(data, dict) else {}


def user_groups(config: Config, uid: str, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Groups a user belongs to. Admin-only (or the bot's own account)."""
    try:
        data = ocs_get(config, f"/cloud/users/{uid}/groups", timeout=timeout)
    except OcsError as e:
        raise _reraise_with_admin_hint(e) from e
    if isinstance(data, dict):
        return list(data.get("groups", []) or [])
    return list(data or [])


def list_groups(
    config: Config,
    search_term: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[str]:
    """All groups on the server. Admin-only."""
    params = {"search": search_term} if search_term else None
    try:
        data = ocs_get(config, "/cloud/groups", params=params, timeout=timeout)
    except OcsError as e:
        raise _reraise_with_admin_hint(e) from e
    if isinstance(data, dict):
        return list(data.get("groups", []) or [])
    return list(data or [])


def group_members(config: Config, gid: str, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Members of a group. Admin-only."""
    try:
        data = ocs_get(config, f"/cloud/groups/{gid}", timeout=timeout)
    except OcsError as e:
        raise _reraise_with_admin_hint(e) from e
    if isinstance(data, dict):
        return list(data.get("users", []) or [])
    return list(data or [])
