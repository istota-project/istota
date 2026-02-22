"""Shared Nextcloud HTTP plumbing (OCS + WebDAV)."""

import logging
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from .config import Config

logger = logging.getLogger("istota.nextcloud_client")

_SHARES_PATH = "/apps/files_sharing/api/v1/shares"


# --- Private helpers ---


def _nc_auth(config: Config) -> tuple[str, str]:
    return (config.nextcloud.username, config.nextcloud.app_password)


def _nc_base_url(config: Config) -> str:
    return config.nextcloud.url.rstrip("/")


def _ocs_headers() -> dict[str, str]:
    return {"OCS-APIRequest": "true", "Accept": "application/json"}


def _nc_configured(config: Config) -> bool:
    return bool(config.nextcloud.url and config.nextcloud.username)


# --- OCS operations ---


def ocs_get(
    config: Config,
    path: str,
    params: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> Any | None:
    """
    OCS GET request. Returns parsed ocs.data or None on error.

    path: OCS path after /ocs/v2.php, e.g. "/cloud/users/alice"
    """
    if not _nc_configured(config):
        return None

    url = f"{_nc_base_url(config)}/ocs/v2.php{path}"

    try:
        resp = httpx.get(
            url,
            auth=_nc_auth(config),
            headers=_ocs_headers(),
            params=params,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("ocs", {}).get("data")
    except Exception as e:
        logger.debug("OCS GET %s failed: %s", path, e)
        return None


def ocs_post(
    config: Config,
    path: str,
    data: dict[str, Any],
    timeout: float = 10.0,
) -> Any | None:
    """
    OCS POST request. Returns parsed ocs.data or None on error.

    path: OCS path after /ocs/v2.php, e.g. "/apps/files_sharing/api/v1/shares"
    """
    if not _nc_configured(config):
        return None

    url = f"{_nc_base_url(config)}/ocs/v2.php{path}"

    try:
        resp = httpx.post(
            url,
            auth=_nc_auth(config),
            headers=_ocs_headers(),
            data=data,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("ocs", {}).get("data")
    except Exception as e:
        logger.debug("OCS POST %s failed: %s", path, e)
        return None


def ocs_delete(
    config: Config,
    path: str,
    timeout: float = 10.0,
) -> bool:
    """
    OCS DELETE request. Returns True on success, False on error.

    path: OCS path after /ocs/v2.php, e.g. "/apps/files_sharing/api/v1/shares/42"
    """
    if not _nc_configured(config):
        return False

    url = f"{_nc_base_url(config)}/ocs/v2.php{path}"

    try:
        resp = httpx.delete(
            url,
            auth=_nc_auth(config),
            headers=_ocs_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.debug("OCS DELETE %s failed: %s", path, e)
        return False


# --- WebDAV operations ---


def webdav_get_owner(config: Config, file_path: str) -> str | None:
    """
    Get the owner of a file via WebDAV PROPFIND.

    Args:
        config: Application config (for Nextcloud credentials)
        file_path: Path to the file (relative to Nextcloud root)

    Returns:
        Owner's Nextcloud username, or None if not found
    """
    if not _nc_configured(config):
        return None

    webdav_url = (
        f"{_nc_base_url(config)}/remote.php/dav/files"
        f"/{config.nextcloud.username}/{file_path.lstrip('/')}"
    )

    propfind_body = '''<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <oc:owner-id/>
  </d:prop>
</d:propfind>'''

    try:
        response = httpx.request(
            "PROPFIND",
            webdav_url,
            content=propfind_body,
            headers={
                "Content-Type": "application/xml",
                "Depth": "0",
            },
            auth=_nc_auth(config),
            timeout=10.0,
        )
        response.raise_for_status()

        root = ET.fromstring(response.text)
        for elem in root.iter():
            if elem.tag.endswith('}owner-id') or elem.tag == 'owner-id':
                return elem.text
        return None
    except Exception as e:
        logger.debug("WebDAV PROPFIND %s failed: %s", file_path, e)
        return None


# --- OCS sharing ---


def ocs_list_shares(
    config: Config,
    path: str | None = None,
    reshares: bool = False,
    timeout: float = 10.0,
) -> list[dict] | None:
    """
    List shares, optionally filtered by path.

    Returns list of share dicts, or None on error.
    """
    params: dict[str, str] = {}
    if path is not None:
        params["path"] = path
    if reshares:
        params["reshares"] = "true"
    return ocs_get(config, _SHARES_PATH, params=params, timeout=timeout)


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
    """
    Create a share via OCS Sharing API.

    Args:
        path: Nextcloud file/folder path
        share_type: 0=user, 3=public link, 4=email
        share_with: Username (type 0) or email (type 4)
        permissions: Bitmask (1=read, 2=update, 4=create, 8=delete, 16=share, 31=all)
        password: Password protection (public links)
        expire_date: Expiry in YYYY-MM-DD format
        label: Label for public links

    Returns share dict with id, url, etc. or None on error.
    """
    data: dict[str, Any] = {"path": path, "shareType": share_type}
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
    return ocs_post(config, _SHARES_PATH, data=data, timeout=timeout)


def ocs_delete_share(config: Config, share_id: int, timeout: float = 10.0) -> bool:
    """Delete a share by ID. Returns True on success."""
    return ocs_delete(config, f"{_SHARES_PATH}/{share_id}", timeout=timeout)


def ocs_search_sharees(
    config: Config,
    search: str,
    item_type: str = "file",
    timeout: float = 10.0,
) -> dict | None:
    """
    Search for sharees (users/groups to share with).

    Returns the full data dict with 'exact' and partial matches, or None on error.
    """
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
    """
    Convenience wrapper: create a public link share (shareType=3).

    Returns share dict (includes 'url' field) or None on error.
    """
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
    """
    Share a folder with a Nextcloud user via OCS Sharing API.

    Creates a user share (shareType=0) with full permissions.
    Idempotent: checks existing shares first.

    Returns True on success or already shared, False on error.
    """
    if not _nc_configured(config):
        logger.warning("Cannot share folder: Nextcloud not configured")
        return False

    # Check existing shares
    existing = ocs_list_shares(config, path=folder_path, reshares=True)
    if existing is not None:
        for share in existing:
            if share.get("share_with") == user_id and share.get("share_type") == 0:
                logger.debug("Folder %s already shared with %s", folder_path, user_id)
                return True

    # Create share
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
