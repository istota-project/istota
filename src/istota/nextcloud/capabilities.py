"""Nextcloud capabilities probe — "what does this server actually support".

One call answers the deployment fit-check (does this instance have Talk,
sharing, notifications, versioning?) that every other group wants as a
precondition. ``feature_map`` flattens the payload into dotted booleans so
``capabilities --check talk,sharing.public`` works as a shell gate.
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ._http import DEFAULT_TIMEOUT, OcsError, ocs_get


def fetch_capabilities(config: Config, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Raw ``/cloud/capabilities`` payload (``{version, capabilities}``)."""
    data = ocs_get(config, "/cloud/capabilities", timeout=timeout)
    return data if isinstance(data, dict) else {}


def fetch_account(config: Config, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Raw ``/cloud/user`` payload for the authenticated (bot) account."""
    data = ocs_get(config, "/cloud/user", timeout=timeout)
    return data if isinstance(data, dict) else {}


def _dig(payload: Any, *keys: str, default: Any = None) -> Any:
    node = payload
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return default if node is None else node


def feature_map(capabilities: dict[str, Any]) -> dict[str, bool]:
    """Flatten a capabilities payload into dotted feature → bool.

    These are the names ``--check`` accepts. Kept deliberately small and
    stable: each one answers a question some part of the skill needs before
    it can promise anything.
    """
    caps = capabilities.get("capabilities") if isinstance(capabilities, dict) else None
    caps = caps if isinstance(caps, dict) else {}

    sharing = caps.get("files_sharing") if isinstance(caps.get("files_sharing"), dict) else {}
    files = caps.get("files") if isinstance(caps.get("files"), dict) else {}
    dav = caps.get("dav") if isinstance(caps.get("dav"), dict) else {}

    return {
        "sharing": bool(sharing),
        "sharing.api": bool(_dig(sharing, "api_enabled", default=False)),
        "sharing.public": bool(_dig(sharing, "public", "enabled", default=False)),
        "sharing.public.password_enforced": bool(
            _dig(sharing, "public", "password", "enforced", default=False)
        ),
        "sharing.public.expire_date": bool(
            _dig(sharing, "public", "expire_date", "enabled", default=False)
        ),
        "sharing.public.expire_date_enforced": bool(
            _dig(sharing, "public", "expire_date", "enforce", default=False)
            or _dig(sharing, "public", "expire_date", "enforced", default=False)
        ),
        "sharing.resharing": bool(sharing.get("resharing", False)),
        "sharing.federation": bool(
            _dig(sharing, "federation", "outgoing", default=False)
            or _dig(sharing, "federation", "incoming", default=False)
        ),
        "sharing.email": bool(_dig(sharing, "sharebymail", "enabled", default=False)),
        "talk": "spreed" in caps,
        "notifications": "notifications" in caps,
        "activity": "activity" in caps,
        "files.versioning": bool(files.get("versioning", False)),
        "files.undelete": bool(files.get("undelete", False)),
        "dav.chunking": bool(dav.get("chunking")),
    }


def public_link_expiry_limit(capabilities: dict[str, Any]) -> int | None:
    """Server-enforced maximum public-link expiry in days, if it enforces one."""
    caps = capabilities.get("capabilities") if isinstance(capabilities, dict) else None
    caps = caps if isinstance(caps, dict) else {}
    sharing = caps.get("files_sharing") if isinstance(caps.get("files_sharing"), dict) else {}
    expire = _dig(sharing, "public", "expire_date", default={})
    if not isinstance(expire, dict):
        return None
    enforced = bool(expire.get("enforce") or expire.get("enforced"))
    if not enforced:
        return None
    days = expire.get("days")
    try:
        days = int(days)
    except (TypeError, ValueError):
        return None
    return days if days > 0 else None


def summarize(capabilities: dict[str, Any], account: dict[str, Any] | None = None) -> dict[str, Any]:
    """The curated summary an operator (or the model) reads."""
    caps = capabilities.get("capabilities") if isinstance(capabilities, dict) else None
    caps = caps if isinstance(caps, dict) else {}
    version = capabilities.get("version") if isinstance(capabilities, dict) else None
    version = version if isinstance(version, dict) else {}

    sharing = caps.get("files_sharing") if isinstance(caps.get("files_sharing"), dict) else {}
    files = caps.get("files") if isinstance(caps.get("files"), dict) else {}
    dav = caps.get("dav") if isinstance(caps.get("dav"), dict) else {}
    spreed = caps.get("spreed") if isinstance(caps.get("spreed"), dict) else {}
    notifications = (
        caps.get("notifications") if isinstance(caps.get("notifications"), dict) else {}
    )
    activity = caps.get("activity") if isinstance(caps.get("activity"), dict) else {}
    core = caps.get("core") if isinstance(caps.get("core"), dict) else {}

    account = account or {}

    summary: dict[str, Any] = {
        "server": {
            "version": version.get("string") or version.get("versionstring") or "",
            "edition": version.get("edition", ""),
            "extended_support": bool(version.get("extendedSupport", False)),
        },
        "webdav_root": core.get("webdav-root", "remote.php/webdav"),
        "sharing": {
            "api_enabled": bool(sharing.get("api_enabled", False)),
            "public_enabled": bool(_dig(sharing, "public", "enabled", default=False)),
            "public_password_enforced": bool(
                _dig(sharing, "public", "password", "enforced", default=False)
            ),
            "public_expire_date_enabled": bool(
                _dig(sharing, "public", "expire_date", "enabled", default=False)
            ),
            "public_expire_date_days": _dig(sharing, "public", "expire_date", "days"),
            "public_expire_date_enforced": bool(
                _dig(sharing, "public", "expire_date", "enforce", default=False)
                or _dig(sharing, "public", "expire_date", "enforced", default=False)
            ),
            "resharing": bool(sharing.get("resharing", False)),
            "federation": {
                "outgoing": bool(_dig(sharing, "federation", "outgoing", default=False)),
                "incoming": bool(_dig(sharing, "federation", "incoming", default=False)),
            },
            "email_shares": bool(_dig(sharing, "sharebymail", "enabled", default=False)),
        },
        "talk": {
            "available": "spreed" in caps,
            "features": list(spreed.get("features", []) or []),
        },
        "notifications": {
            "available": "notifications" in caps,
            "endpoints": list(notifications.get("ocs-endpoints", []) or []),
        },
        "activity": {
            "available": "activity" in caps,
            "apiv2": list(activity.get("apiv2", []) or []),
        },
        "files": {
            "versioning": bool(files.get("versioning", False)),
            "undelete": bool(files.get("undelete", False)),
            "chunking": dav.get("chunking", ""),
        },
        "account": {
            "id": account.get("id", ""),
            "display_name": account.get("display-name") or account.get("displayname", ""),
            "email": account.get("email", ""),
            "groups": list(account.get("groups", []) or []),
            "quota": account.get("quota", {}),
        },
        "features": feature_map(capabilities),
    }
    return summary


def evaluate_checks(capabilities: dict[str, Any], names: list[str]) -> dict[str, bool]:
    """Resolve each requested dotted feature name against the payload.

    An unknown name resolves False rather than raising — a deployment gate
    asking for something this server has never heard of has failed, and the
    caller sees which name it was.
    """
    features = feature_map(capabilities)
    return {name: bool(features.get(name, False)) for name in names}


def known_feature_names() -> list[str]:
    return sorted(feature_map({}).keys())


def require(config: Config, feature: str, *, capabilities: dict[str, Any] | None = None) -> None:
    """Raise a legible OcsError when a server lacks the feature a verb needs."""
    caps = capabilities if capabilities is not None else fetch_capabilities(config)
    if feature_map(caps).get(feature, False):
        return
    raise OcsError(
        f"This Nextcloud server does not have '{feature}' available "
        "(run `nextcloud capabilities` to see what it supports)",
        None,
        None,
        "/cloud/capabilities",
    )
