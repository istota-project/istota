"""OCS sharing: CRUD, the safe-link helper, and direct-download URL synthesis.

The raising counterparts of the legacy wrappers in ``istota.nextcloud_client``,
plus the ergonomics a "give me a download link" request needs: an expiry by
default, an optional password, a URL that actually downloads, and a revocation
loop.
"""

from __future__ import annotations

import secrets
import string
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..config import Config
from ._http import (
    DEFAULT_TIMEOUT,
    OcsError,
    ocs_get,
    ocs_post,
    ocs_put,
    ocs_request,
    to_remote_path,
)

SHARES_PATH = "/apps/files_sharing/api/v1/shares"

#: OCS share types.
SHARE_TYPES: dict[str, int] = {
    "user": 0,
    "group": 1,
    "link": 3,
    "email": 4,
    "federated": 6,
    "talk": 10,
}

LINK_SHARE_TYPE = 3

_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def generate_password(length: int = 20) -> str:
    """A random link password. Returned to the caller — it has to be tellable."""
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def list_shares(
    config: Config,
    *,
    path: str | None = None,
    reshares: bool = False,
    subfiles: bool = False,
    shared_with_me: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    params: dict[str, str] = {}
    if path is not None:
        # OCS names a file by a path relative to the sharer's own root, so the
        # logical path needs the same mapping a DAV URL gets.
        params["path"] = to_remote_path(config, path)
    if reshares:
        params["reshares"] = "true"
    if subfiles:
        params["subfiles"] = "true"
    if shared_with_me:
        params["shared_with_me"] = "true"
    data = ocs_get(config, SHARES_PATH, params=params, timeout=timeout)
    return list(data or [])


def get_share(config: Config, share_id: int, timeout: float = DEFAULT_TIMEOUT) -> dict:
    data = ocs_get(config, f"{SHARES_PATH}/{share_id}", timeout=timeout)
    if isinstance(data, list):
        if not data:
            raise OcsError(
                f"No share with id {share_id}", None, 404, f"{SHARES_PATH}/{share_id}"
            )
        return data[0]
    return data if isinstance(data, dict) else {}


def create_share(
    config: Config,
    *,
    path: str,
    share_type: int,
    share_with: str | None = None,
    permissions: int | None = None,
    password: str | None = None,
    expire_date: str | None = None,
    label: str | None = None,
    note: str | None = None,
    send_mail: bool | None = None,
    attributes: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    data: dict[str, Any] = {"path": to_remote_path(config, path), "shareType": share_type}
    if share_with is not None:
        data["shareWith"] = share_with
    if permissions is not None:
        data["permissions"] = permissions
    if password is not None:
        data["password"] = password
    if expire_date is not None:
        data["expireDate"] = expire_date
    if label is not None:
        data["label"] = label
    if note is not None:
        data["note"] = note
    if send_mail is not None:
        data["sendMail"] = "true" if send_mail else "false"
    if attributes is not None:
        data["attributes"] = attributes
    result = ocs_post(config, SHARES_PATH, data=data, timeout=timeout)
    return result if isinstance(result, dict) else {}


#: Order matters: the sharing API historically accepts one field per PUT, so
#: an update is issued as a sequence rather than a single call.
_UPDATE_FIELDS = ("permissions", "password", "expireDate", "note", "label")


def update_share(
    config: Config,
    share_id: int,
    *,
    permissions: int | None = None,
    password: str | None = None,
    expire_date: str | None = None,
    note: str | None = None,
    label: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Update a share, one field per request (what the OCS API accepts)."""
    values: dict[str, Any] = {
        "permissions": permissions,
        "password": password,
        "expireDate": expire_date,
        "note": note,
        "label": label,
    }
    provided = [(k, values[k]) for k in _UPDATE_FIELDS if values[k] is not None]
    if not provided:
        raise OcsError(
            "Nothing to update — pass at least one of --permissions, --password, "
            "--expire, --note, --label",
            None,
            None,
            f"{SHARES_PATH}/{share_id}",
        )

    result: dict = {}
    for field, value in provided:
        got = ocs_put(config, f"{SHARES_PATH}/{share_id}", data={field: value}, timeout=timeout)
        if isinstance(got, dict):
            result = got
    return result or get_share(config, share_id, timeout=timeout)


def delete_share(config: Config, share_id: int, timeout: float = DEFAULT_TIMEOUT) -> None:
    ocs_request(config, "DELETE", f"{SHARES_PATH}/{share_id}", timeout=timeout)


def search_sharees(
    config: Config,
    search: str,
    item_type: str = "file",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    data = ocs_get(
        config,
        "/apps/files_sharing/api/v1/sharees",
        params={"search": search, "itemType": item_type},
        timeout=timeout,
    )
    return data if isinstance(data, dict) else {}


# --- the safe-link workflow ---


def expiry_date(days: int, *, today: date | None = None) -> str | None:
    """``YYYY-MM-DD`` for a link expiring in ``days``; None when days <= 0.

    The base is **UTC** today, not the local date. Nextcloud compares the
    expiry against its own clock, so a caller west of the server rolls over
    later than the server does: at 17:00 in California the server is already on
    tomorrow's date, and ``--days 1`` computes a date the server considers
    today — rejected outright with "Expiration date is in the past". That is a
    seven-hour window every single day for a Pacific caller against the usual
    UTC server, and it was reproduced live.

    Residual: a server running *ahead* of UTC can still see a one-day expiry as
    same-day. Eliminating that needs the date from the server itself; UTC
    covers the deployments we actually run.
    """
    if days <= 0:
        return None
    base = today or datetime.now(timezone.utc).date()
    return (base + timedelta(days=days)).isoformat()


def download_url(share: dict, *, file_name: str | None = None) -> str:
    """The URL that actually downloads.

    A link share resolves to a preview page, not a file — which is why "give me
    a download link" needs a helper rather than a flag. A file and a whole
    folder both take ``/download``; naming one entry inside a shared folder
    takes the query form.
    """
    url = (share.get("url") or "").rstrip("/")
    if not url:
        return ""
    if file_name and (share.get("item_type") == "folder"):
        from urllib.parse import quote

        return f"{url}/download?path=/&files={quote(file_name, safe='')}"
    return f"{url}/download"


def revoke_command(share: dict) -> str:
    """A literal command the model can echo so the user can kill the link."""
    return f"istota-skill nextcloud share revoke {share.get('id', '')}"


def clamp_expiry_days(days: int, server_limit: int | None) -> tuple[int, bool]:
    """Clamp a requested expiry to the server's enforced maximum.

    Returns ``(days, clamped)`` so the caller can say it happened rather than
    silently handing back a shorter-lived link than asked for.
    """
    if server_limit is None or days <= 0:
        return days, False
    if days > server_limit:
        return server_limit, True
    return days, False


def create_link(
    config: Config,
    *,
    path: str,
    days: int,
    password: str | None = None,
    permissions: int = 1,
    label: str | None = None,
    note: str | None = None,
    file_name: str | None = None,
    server_expiry_limit: int | None = None,
    today: date | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Create a public link share with opinionated defaults, and describe it fully."""
    effective_days, clamped = clamp_expiry_days(days, server_expiry_limit)
    expires = expiry_date(effective_days, today=today)

    share = create_share(
        config,
        path=path,
        share_type=LINK_SHARE_TYPE,
        permissions=permissions,
        password=password,
        expire_date=expires,
        label=label,
        note=note,
        timeout=timeout,
    )

    result: dict[str, Any] = {
        "status": "ok",
        "path": path,
        "url": share.get("url", ""),
        "download_url": download_url(share, file_name=file_name),
        "token": share.get("token", ""),
        "share_id": share.get("id"),
        "item_type": share.get("item_type", ""),
        "expires": expires,
        "has_password": bool(password),
        "permissions": share.get("permissions", permissions),
        "revoke_command": revoke_command(share),
    }
    if password:
        result["password"] = password
    if clamped:
        result["notice"] = (
            f"This server enforces a maximum public-link expiry of "
            f"{server_expiry_limit} days, so the link expires on {expires} "
            f"rather than in {days} days."
        )
    return result


def revoke(
    config: Config,
    *,
    share_id: int | None = None,
    token: str | None = None,
    path: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Revoke a share by id, by token, or every link share on a path."""
    if share_id is not None:
        delete_share(config, share_id, timeout=timeout)
        return {"status": "ok", "revoked": [{"id": share_id}]}

    if token:
        for share in list_shares(config, timeout=timeout):
            if share.get("token") == token:
                delete_share(config, int(share["id"]), timeout=timeout)
                return {
                    "status": "ok",
                    "revoked": [{"id": share.get("id"), "token": token,
                                 "path": share.get("path", "")}],
                }
        raise OcsError(f"No share found with token {token}", None, 404, SHARES_PATH)

    if path:
        shares = list_shares(config, path=path, timeout=timeout)
        links = [s for s in shares if s.get("share_type") == LINK_SHARE_TYPE]
        revoked = []
        for share in links:
            delete_share(config, int(share["id"]), timeout=timeout)
            revoked.append({
                "id": share.get("id"),
                "token": share.get("token", ""),
                "label": share.get("label", ""),
                "url": share.get("url", ""),
            })
        return {"status": "ok", "path": path, "revoked": revoked, "count": len(revoked)}

    raise OcsError(
        "Pass a share id, --token, or --path to revoke", None, None, SHARES_PATH
    )
