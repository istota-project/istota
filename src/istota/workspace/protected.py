"""Names Files may not create, edit or delete in a writable place.

A group folder's root holds its layout: `GROUP.md`, the notebook, which has
its own editor with a membership re-check and an audit trail, and `memories/`,
which `storage.ensure_group_directories` expects to be a directory. Files
refuses to create either name there, present or absent (an upload named
`GROUP.md` into a group whose notebook is gone would otherwise become the
notebook), and refuses to delete them.

`parts` is the path below the place, without its walk. It has been through
`valid_part` and is walked with no link followed from an anchor that is itself
walked the same way, so `parts == []` is the root and no other spelling is.
Names are compared case-folded, since a host folder on a Mac matches names
without case. The create routes pass the name the write would use, after
`safe_filename`.

In My files, the bot's config folder is refused to the text editor: those files
have their own editor, which checks them with the loaders' validators
(`config_files`). Deleting there is allowed, as it always was.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from istota import storage
from istota.workspace.listing import ListingError
from istota.workspace.places import Place

if TYPE_CHECKING:
    from istota.config import Config

LAYOUT_NAMES = {"group.md": "group_memory", "memories": "group_folder"}

_MESSAGES = {
    "group_memory": "The group notebook is edited on the Groups page.",
    "group_folder": "This name is part of the group's layout.",
}


def _layout_refusal(place: Place, parts: list[str], name: str) -> ListingError | None:
    if place.kind != "group" or parts:
        return None
    code = LAYOUT_NAMES.get(name.casefold())
    if code is None:
        return None
    return ListingError(403, _MESSAGES[code], code=code)


def check_create(place: Place, parts: list[str], name: str) -> None:
    """Refuse an upload or a new folder named for the group's layout."""
    refusal = _layout_refusal(place, parts, name)
    if refusal is not None:
        raise refusal


def check_delete(place: Place, parts: list[str], name: str) -> None:
    """Refuse deleting the group's notebook or its memories folder."""
    refusal = _layout_refusal(place, parts, name)
    if refusal is not None:
        raise refusal


def _config_file() -> ListingError:
    # Imported here: `editable` calls into this module for its checks.
    from istota.workspace.editable import EditRefused
    return EditRefused(
        403, "config_file",
        "Files in the bot's config folder are edited from Configuration or Skill instructions.",
    )


def _is_config_folder(config: "Config", user_id: str, dir_fd: int) -> bool:
    """The config folder by identity too: the daemon follows a `config` link
    that stays inside the user's tree, which a name check cannot see."""
    config_dir = storage.resolve_user_config_dir(config, user_id)
    if config_dir is None:
        return False
    folder = os.fstat(dir_fd)
    for directory in (config_dir, config_dir / "skills"):
        try:
            info = os.stat(directory)
        except OSError:
            continue
        if (info.st_dev, info.st_ino) == (folder.st_dev, folder.st_ino):
            return True
    return False


def check_edit(config: "Config", user_id: str, place: Place, parts: list[str], name: str, dir_fd: int) -> None:
    """Refuse a text edit of the bot's config files or the group notebook.

    `dir_fd` is the open folder holding `name`."""
    if place.kind == "home":
        folded = [part.casefold() for part in parts[:2]]
        if folded == [config.bot_dir_name.casefold(), "config"] or _is_config_folder(config, user_id, dir_fd):
            raise _config_file()
        return
    if place.kind == "group" and not parts and name.casefold() == "group.md":
        raise ListingError(403, _MESSAGES["group_memory"], code="group_memory")
