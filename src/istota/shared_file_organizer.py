"""Auto-organize files shared with the istota Nextcloud user."""

import logging
from dataclasses import dataclass

from . import db
from .config import Config
from istota.nextcloud.compat import webdav_get_owner
from istota.prompts.persona import PERSONA_FILENAME, SHIPPED_SUFFIX
from istota.prompts.worldview import WORLDVIEW_FILENAME
from .skills.files import list_files, path_exists, move_file
from .storage import get_user_shared_path, ensure_user_directories_v2

logger = logging.getLogger("istota.shared_file_organizer")

_WATCH_KEY = "shared-files"

# Bot-managed directories at the root, skipped whoever owns them.
_ROOT_SKIP = frozenset({"users"})

# The operator files' names. Skipped only when the bot account owns the
# item: a user who shares a file under one of these names into the root is
# sharing their own file, and left there it would become every user's persona.
# The worldview's reader also checks the owner, since a share whose owner is
# not a configured user is never moved.
_BOT_OWNED_SKIP = frozenset({
    PERSONA_FILENAME.lower(),
    (PERSONA_FILENAME + SHIPPED_SUFFIX).lower(),
    WORLDVIEW_FILENAME.lower(),
})


@dataclass
class OrganizedFile:
    """Result of organizing a shared file."""
    original_path: str
    new_path: str
    owner_id: str
    is_dir: bool
    resource_type: str  # 'folder' or 'shared_file'


def get_file_owner(config: Config, file_path: str) -> str | None:
    """Get the owner of a file via WebDAV PROPFIND.

    Delegates to nextcloud_client.webdav_get_owner.
    """
    return webdav_get_owner(config, file_path)


def discover_and_organize_shared_files(config: Config) -> list[OrganizedFile]:
    """
    Discover files shared with the bot, move to user's shared/ folder,
    and create resource entries.

    Scans root level for files/folders, determines owner via WebDAV,
    moves to /Users/{owner}/shared/, and creates user_resources entries.

    Returns list of organized files with their new locations.

    With the tree watcher answering (#939), a pass runs only when the root's
    entries changed, and takes the root's entries and their owners from the
    watcher's listing, which already asked for ``oc:owner-id``. Its hourly
    pass, and every pass while the watcher cannot answer, lists the mount and
    asks each entry's owner, as before.
    """
    from .nextcloud import tree_watch

    run, snap = tree_watch.should_run(_WATCH_KEY, [tree_watch.ROOT])
    if not run:
        return []
    listing = None if tree_watch.heal_due(_WATCH_KEY) else tree_watch.root_listing()
    failed = False

    organized = []

    if listing is not None:
        root_items = [
            {"name": e["name"], "is_dir": e["is_dir"], "owner": e.get("owner_id") or None}
            for e in listing
        ]
    else:
        try:
            root_items = list_files(config, "")
        except Exception as e:
            # Not recorded as a run, so the next pass tries again.
            logger.error("Error listing root directory: %s", e)
            return []

    for item in root_items:
        item_name = item["name"]
        is_dir = item["is_dir"]

        if item_name.lower() in _ROOT_SKIP:
            continue

        # The owner is WebDAV's to say; the filesystem cannot.
        owner = item["owner"] if "owner" in item else get_file_owner(config, item_name)
        if not owner:
            # Could not determine owner, skip
            continue

        if item_name.lower() in _BOT_OWNED_SKIP and owner == config.nextcloud.username:
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
            failed = True
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

    if not failed:
        tree_watch.record_run(_WATCH_KEY, snap)
    return organized
