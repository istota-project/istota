"""Auto-organize files shared with the istota Nextcloud user."""

import logging
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from . import db
from .config import Config
from .skills.files import list_files, path_exists, move_file, rclone_list
from .storage import get_user_shared_path, ensure_user_directories_v2

logger = logging.getLogger("istota.shared_file_organizer")


@dataclass
class OrganizedFile:
    """Result of organizing a shared file."""
    original_path: str
    new_path: str
    owner_id: str
    is_dir: bool
    resource_type: str  # 'folder' or 'shared_file'


def get_file_owner(config: Config, file_path: str) -> str | None:
    """
    Get the owner of a file via WebDAV PROPFIND.

    Args:
        config: Application config (for Nextcloud credentials)
        file_path: Path to the file (relative to Nextcloud root)

    Returns:
        Owner's Nextcloud username, or None if not found
    """
    if not config.nextcloud.url or not config.nextcloud.username:
        return None

    webdav_url = f"{config.nextcloud.url.rstrip('/')}/remote.php/dav/files/{config.nextcloud.username}/{file_path.lstrip('/')}"

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
            auth=(config.nextcloud.username, config.nextcloud.app_password),
            timeout=10.0,
        )
        response.raise_for_status()

        # Parse XML response
        root = ET.fromstring(response.text)
        # Find oc:owner-id element (namespace handling)
        for elem in root.iter():
            if elem.tag.endswith('}owner-id') or elem.tag == 'owner-id':
                return elem.text
        return None
    except Exception as e:
        logger.debug("Error getting file owner for %s: %s", file_path, e)
        return None


def discover_and_organize_shared_files(config: Config) -> list[OrganizedFile]:
    """
    Discover files shared with the bot, move to user's shared/ folder,
    and create resource entries.

    Scans root level for files/folders, determines owner via WebDAV,
    moves to /Users/{owner}/shared/, and creates user_resources entries.

    Returns list of organized files with their new locations.
    """
    organized = []

    # List all items at root level (use mount-aware function)
    try:
        root_items = list_files(config, "")
    except Exception as e:
        logger.error("Error listing root directory: %s", e)
        return []

    for item in root_items:
        item_name = item["name"]
        is_dir = item["is_dir"]

        # Skip items already in /Users/ path (bot-managed files)
        if item_name.lower() == "users":
            continue

        # Get owner for this item (always via WebDAV - can't get owner from filesystem)
        owner = get_file_owner(config, item_name)
        if not owner:
            # Could not determine owner, skip
            continue

        # Check if owner is a configured user
        if owner not in config.users:
            # Owner not configured, skip silently
            continue

        # Destination path in user's shared folder
        shared_path = get_user_shared_path(owner)
        dest_path = f"{shared_path}/{item_name}"

        # Check if already exists at destination (skip if so)
        if path_exists(config, dest_path):
            # Already organized, skip
            continue

        # Ensure user directories exist (including shared/)
        ensure_user_directories_v2(config, owner)

        # Move the item to user's shared folder
        if not move_file(config, item_name, dest_path):
            logger.error("Failed to move %s to %s", item_name, dest_path)
            continue

        # Determine resource type
        resource_type = "folder" if is_dir else "shared_file"

        # Create user_resources entry
        with db.get_db(config.db_path) as conn:
            db.add_user_resource(
                conn,
                user_id=owner,
                resource_type=resource_type,
                resource_path=dest_path,
                display_name=item_name,
                permissions="read",  # Default to read-only
            )

        organized.append(OrganizedFile(
            original_path=item_name,
            new_path=dest_path,
            owner_id=owner,
            is_dir=is_dir,
            resource_type=resource_type,
        ))

        logger.info("Organized: %s -> %s (owner: %s)", item_name, dest_path, owner)

    return organized
