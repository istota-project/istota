"""Back-compat shim over :mod:`istota.nextcloud` — the ``None``-returning layer.

The client itself now lives in ``src/istota/nextcloud/``, where every failure
raises :class:`~istota.nextcloud.OcsError` carrying the HTTP status, the OCS
status code and the server's message. That is what the skill CLI uses.

Four daemon paths predate the structured errors and are *deliberately*
best-effort — a Nextcloud hiccup must not fail daemon startup or wedge a
``!search``:

* ``nextcloud_api.hydrate_user_configs`` (startup user metadata)
* ``ocs_share_folder`` (the idempotent pre-check behind ``storage.share_folder_with_user``)
* ``webdav_get_owner`` (``shared_file_organizer.get_file_owner``)
* ``commands._search_talk_api``

They keep the historical ``None`` / ``False`` contract through this module. The
wrappers below call this module's own ``ocs_*`` names on purpose, so a caller
(or a test) patching ``istota.nextcloud_client.ocs_get`` still intercepts them.
"""

import logging
import xml.etree.ElementTree as ET
from typing import Any

# Not called directly any more — the request bodies live in istota.nextcloud._http
# — but kept as the patch anchor for `istota.nextcloud_client.httpx.*`, which
# several callers and tests still target.
import httpx  # noqa: F401

from .config import Config
from .nextcloud import _http
from .nextcloud._http import (
    OcsError,
    dav_files_url,
    dav_request,
    nc_auth as _nc_auth,
    nc_base_url as _nc_base_url,
    nc_configured as _nc_configured,
    ocs_headers as _ocs_headers,
    to_remote_path as _to_remote_path,
)
from .nextcloud.shares import relabel as _relabel_share

logger = logging.getLogger("istota.nextcloud_client")

_SHARES_PATH = "/apps/files_sharing/api/v1/shares"

__all__ = [
    "OcsError",
    "ocs_get",
    "ocs_post",
    "ocs_delete",
    "webdav_get_owner",
    "ocs_list_shares",
    "ocs_create_share",
    "ocs_delete_share",
    "ocs_search_sharees",
    "ocs_create_public_link",
    "ocs_share_folder",
    "_nc_auth",
    "_nc_base_url",
    "_nc_configured",
    "_ocs_headers",
]


# --- OCS operations (legacy None-returning) ---


def ocs_get(
    config: Config,
    path: str,
    params: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> Any | None:
    """OCS GET. Returns parsed ``ocs.data``, or None on any error."""
    try:
        return _http.ocs_get(config, path, params=params, timeout=timeout)
    except OcsError as e:
        logger.debug("OCS GET %s failed: %s", path, e)
        return None


def ocs_post(
    config: Config,
    path: str,
    data: dict[str, Any],
    timeout: float = 10.0,
) -> Any | None:
    """OCS POST. Returns parsed ``ocs.data``, or None on any error."""
    try:
        return _http.ocs_post(config, path, data=data, timeout=timeout)
    except OcsError as e:
        logger.debug("OCS POST %s failed: %s", path, e)
        return None


def ocs_delete(
    config: Config,
    path: str,
    timeout: float = 10.0,
) -> bool:
    """OCS DELETE. Returns True on success, False on any error."""
    try:
        _http.ocs_delete(config, path, timeout=timeout)
        return True
    except OcsError as e:
        logger.debug("OCS DELETE %s failed: %s", path, e)
        return False


# --- WebDAV operations ---


def webdav_get_owner(config: Config, file_path: str) -> str | None:
    """Owner of a file via WebDAV PROPFIND, or None if unknown/unreachable."""
    propfind_body = '''<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <oc:owner-id/>
  </d:prop>
</d:propfind>'''

    try:
        response = dav_request(
            config,
            "PROPFIND",
            dav_files_url(config, file_path),
            content=propfind_body,
            headers={"Content-Type": "application/xml", "Depth": "0"},
            timeout=10.0,
        )
        root = ET.fromstring(response.text)
        for elem in root.iter():
            if elem.tag.endswith("}owner-id") or elem.tag == "owner-id":
                return elem.text
        return None
    except Exception as e:
        logger.debug("WebDAV PROPFIND %s failed: %s", file_path, e)
        return None


# --- OCS sharing (legacy None-returning) ---


def ocs_list_shares(
    config: Config,
    path: str | None = None,
    reshares: bool = False,
    timeout: float = 10.0,
) -> list[dict] | None:
    """List shares, optionally filtered by path. None on error."""
    params: dict[str, str] = {}
    if path is not None:
        # Same mapping the raising counterpart applies: OCS names a file by a
        # path relative to the sharer's own root. `ocs_share_folder` below is
        # the one call the Docker shape makes on every boot.
        params["path"] = _to_remote_path(config, path)
    if reshares:
        params["reshares"] = "true"
    rows = ocs_get(config, _SHARES_PATH, params=params, timeout=timeout)
    if rows is None:
        return None
    # Inverted on the way back for the same reason `shares.list_shares` does
    # it: the skill reads these rows and speaks logical paths.
    return [_relabel_share(config, row) for row in rows]


def ocs_create_share(
    config: Config,
    path: str,
    share_type: int,
    share_with: str | None = None,
    permissions: int | None = None,
    password: str | None = None,
    expire_date: str | None = None,
    label: str | None = None,
    timeout: float = 10.0,
) -> dict | None:
    """Create a share via the OCS Sharing API. None on error.

    share_type: 0=user, 1=group, 3=public link, 4=email, 6=federated, 10=Talk.
    permissions: bitmask (1=read, 2=update, 4=create, 8=delete, 16=share, 31=all).
    """
    data: dict[str, Any] = {"path": _to_remote_path(config, path), "shareType": share_type}
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
    result = ocs_post(config, _SHARES_PATH, data=data, timeout=timeout)
    return _relabel_share(config, result) if isinstance(result, dict) else result


def ocs_delete_share(config: Config, share_id: int, timeout: float = 10.0) -> bool:
    """Delete a share by ID. True on success."""
    return ocs_delete(config, f"{_SHARES_PATH}/{share_id}", timeout=timeout)


def ocs_search_sharees(
    config: Config,
    search: str,
    item_type: str = "file",
    timeout: float = 10.0,
) -> dict | None:
    """Search for sharees. Returns the full data dict, or None on error."""
    return ocs_get(
        config,
        "/apps/files_sharing/api/v1/sharees",
        params={"search": search, "itemType": item_type},
        timeout=timeout,
    )


def ocs_create_public_link(
    config: Config,
    path: str,
    permissions: int = 1,
    password: str | None = None,
    expire_date: str | None = None,
    label: str | None = None,
    timeout: float = 10.0,
) -> dict | None:
    """Convenience wrapper: create a public link share (shareType=3)."""
    return ocs_create_share(
        config,
        path=path,
        share_type=3,
        permissions=permissions,
        password=password,
        expire_date=expire_date,
        label=label,
        timeout=timeout,
    )


def ocs_share_folder(config: Config, folder_path: str, user_id: str) -> bool:
    """Share a folder with a Nextcloud user (shareType=0, full permissions).

    Idempotent: checks existing shares first. True on success or already shared.
    """
    if not _nc_configured(config):
        logger.warning("Cannot share folder: Nextcloud not configured")
        return False

    existing = ocs_list_shares(config, path=folder_path, reshares=True)
    if existing is not None:
        for share in existing:
            if share.get("share_with") == user_id and share.get("share_type") == 0:
                logger.debug("Folder %s already shared with %s", folder_path, user_id)
                return True

    result = ocs_create_share(
        config,
        path=folder_path,
        share_type=0,
        share_with=user_id,
        permissions=31,
    )
    if result is not None:
        logger.info("Shared folder %s with user %s", folder_path, user_id)
        return True

    logger.warning("Failed to share folder %s with %s", folder_path, user_id)
    return False
