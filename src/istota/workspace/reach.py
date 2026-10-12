"""Project workspace reach from the task's user-data mounts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit

from istota import db, doctor
from istota.config import Config
from istota.rooms.scopes import all_scopes
from istota.sandbox.plan import Mount, user_data_mounts
from istota.skills._loader import advertised_cli_skills, effective_disabled_skills, load_skill_index
from istota.workspace.places import Place, user_resources_for


@dataclass(frozen=True)
class Reach:
    private: str
    shared_member: str
    restricted: str
    note: str | None = None


def _mount_reach(place: Place, mounts: list[Mount]) -> str:
    if place.root is None:
        return "none"
    root = place.root.resolve()
    mode = "none"
    for mount in mounts:
        if mount.source is not None and root.is_relative_to(mount.source.resolve()):
            mode = mount.mode
    return mode


def place_reach(
    config: Config, conn: sqlite3.Connection, user_id: str, places: list[Place],
) -> dict[str, Reach]:
    resources = user_resources_for(config, conn, user_id)
    groups = db.list_user_groups(conn, user_id)
    index = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)
    advertised = advertised_cli_skills(
        index, is_admin=config.is_admin(user_id),
        disabled_skills=effective_disabled_skills(config, user_id, index),
    )
    contexts = ((frozenset(), groups), (frozenset(), groups), (all_scopes(index), []))
    # Cache by room token: a channel bind exists only on that room's tasks.
    plans: dict[str | None, list[list[Mount]]] = {}
    result = {}
    for place in places:
        if place.kind in {"drive", "nextcloud"}:
            skill = "google_workspace" if place.kind == "drive" else "nextcloud"
            mode = "skill" if skill in advertised else "none"
            result[place.id] = Reach(mode, mode, "none")
            continue
        token = place.conversation_token if place.kind == "room" else None
        if token not in plans:
            plans[token] = [user_data_mounts(
                config, user_id=user_id, conversation_token=token,
                user_resources=resources, withheld_scopes=scopes, group_ids=group_ids,
            ) for scopes, group_ids in contexts]
        modes = [_mount_reach(place, mounts) for mounts in plans[token]]
        note = None
        if place.kind == "room":
            note = "A room folder is reachable only from that room's own tasks."
        elif place.kind == "home":
            note = "Personal memory is not loaded into the prompt in shared rooms."
        result[place.id] = Reach(*modes, note=note)
    return result


def deployment_notes(config: Config) -> list[str]:
    """Use the daemon's verdict; never probe bwrap in the web process."""
    from istota.executor import effective_sandboxing_if_known

    effective = doctor.daemon_sandbox_verdict(config)
    if effective is None:
        effective = effective_sandboxing_if_known(config)
    notes = []
    if effective is False:
        notes.append(
            "This is a development checkout without a sandbox. A task can read any file "
            "the Istota process can, so the reach shown below is what Istota is told, not a limit."
        )
    elif effective is None:
        notes.append("Sandboxing has not been checked yet in this process.")
    if config.storage_is_nextcloud:
        host = urlsplit(config.nextcloud.url).hostname or "Nextcloud"
        notes.append(f"Files are stored in your Nextcloud at {host}.")
    else:
        notes.append("Files are stored in a local folder.")
    return notes
