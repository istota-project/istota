"""The file roots and connections a user can see in Workspace."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from istota import db
from istota.config import Config
from istota.credentials.google_scopes import levels_from_scopes
from istota.sandbox.user_scope import scoped_user_dir


@dataclass(frozen=True)
class Place:
    id: str
    kind: str
    label: str
    root: Path | None
    browsable: bool
    view_root: str | None
    conversation_token: str | None = None


def user_resources_for(
    config: Config, conn: sqlite3.Connection | None, user_id: str,
    *, resources: list[db.UserResource] | None = None,
) -> list[db.UserResource]:
    """Append config resources to DB rows, or to a caller's task snapshot."""
    if resources is None:
        resources = db.get_user_resources(conn, user_id) if conn is not None else []
    result = list(resources)
    user = config.get_user(user_id)
    if user:
        for resource in user.resources:
            result.append(db.UserResource(
                id=0, user_id=user_id, resource_type=resource.type,
                resource_path=resource.path, display_name=resource.name or None,
                permissions=resource.permissions,
            ))
    return result


def list_places(config: Config, conn: sqlite3.Connection, user_id: str) -> list[Place]:
    """Derive roots from current resources and membership on every request."""
    places: list[Place] = []
    home = config.workspace_root(user_id)
    if config.has_workspace:
        workspace = config.workspace_path.resolve()
        if home is not None:
            places.append(Place("home", "home", "My files", home, True, f"/Users/{user_id}"))
        for resource in user_resources_for(config, conn, user_id):
            if resource.resource_type not in {"folder", "shared_file"} or not resource.resource_path:
                continue
            root = (workspace / resource.resource_path.lstrip("/")).resolve()
            if not root.exists() or (home is not None and root.is_relative_to(home.resolve())):
                continue
            # Viewer paths name workspace files, never an arbitrary host path.
            if not root.is_relative_to(workspace):
                continue
            place_id = f"linked:{resource.id}"
            if resource.id == 0:
                digest = hashlib.sha256(resource.resource_path.encode()).hexdigest()[:12]
                place_id = f"linked:cfg:{digest}"
            places.append(Place(place_id, "linked", (resource.display_name or root.name)[:120],
                                root, True, "/" + root.relative_to(workspace).as_posix()))
        for room in db.list_member_rooms(conn, user_id, include_dismissed=True):
            root = scoped_user_dir(workspace / "Channels", room.token)
            if root is not None and root.is_dir():
                places.append(Place(f"room:{room.token}", "room", (room.name or "Room")[:120],
                                    root, True, f"/Channels/{room.token}", room.token))
        for group_id in db.list_user_groups(conn, user_id):
            root = scoped_user_dir(workspace / "Groups", group_id)
            if root is not None and root.is_dir():
                group = db.get_group(conn, group_id)
                label = group["display_name"] or group_id
                places.append(Place(f"group:{group_id}", "group", label[:120],
                                    root, True, f"/Groups/{group_id}"))
        places.append(Place("talk", "talk", "Talk attachments", workspace / "Talk", False, None))
    if config.google_workspace.enabled:
        scopes = db.get_google_scopes(conn, user_id)
        if scopes is not None and levels_from_scopes(scopes).get("drive") in {"readonly", "full"}:
            places.append(Place("drive", "drive", "Google Drive", None, True, None))
    if config.storage_is_nextcloud:
        places.append(Place("nextcloud", "nextcloud", "Nextcloud", None, False, None))
    return places
