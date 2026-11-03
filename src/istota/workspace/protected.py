"""Names Files may not create or delete in a writable place.

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
"""

from __future__ import annotations

from istota.workspace.listing import ListingError
from istota.workspace.places import Place

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
