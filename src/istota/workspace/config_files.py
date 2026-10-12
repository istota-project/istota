"""The user's own config files, as the web UI lists, reads and saves them.

USER.md, CRON.md, TASKS.md and HEARTBEAT.md in `{bot_dir}/config/`, plus one
overlay per skill the user can use, `config/skills/<skill>.md`. A name from a
request is matched against this list and never joined into a path; the
directory comes from the same resolvers the daemon reads through, so the web
reaches nothing the daemon would not.

Saves go through `text_files.save`, keyed on the resolved path, so the web and
the daemon's own read-modify-write paths (CRON.md, TASKS.md, the USER.md
curator) take one lock per file. Each file is checked with the loader's own
rules before the write: the CRON.md and HEARTBEAT.md parsers, and the overlay
cap and denylist.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from istota import storage
from istota.workspace import text_files
from istota.workspace.writes import open_or_create_dirs

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger("istota.workspace.config_files")

CORE_FILES = ("USER.md", "CRON.md", "TASKS.md", "HEARTBEAT.md")
SKILL_PREFIX = "skills/"


@dataclass(frozen=True)
class ConfigFile:
    name: str
    kind: Literal["config", "skill"]
    leaf: str
    max_bytes: int


def overlay_skill_names(config: "Config", user_id: str) -> list[str]:
    """The skills this user can use and may write an overlay for, sorted."""
    from istota.skills._loader import (
        effective_disabled_skills, load_skill_index, overlay_denylisted,
    )

    index = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)
    disabled = effective_disabled_skills(config, user_id, index)
    is_admin = config.is_admin(user_id)
    features = frozenset(config.experimental.features)
    names = []
    for name in sorted(index):
        meta = index[name]
        if overlay_denylisted(name) or name in disabled:
            continue
        if meta.admin_only and not is_admin:
            continue
        if meta.experimental and f"skill_{name}" not in features:
            continue
        names.append(name)
    return names


def lookup(
    config: "Config", user_id: str, name: str, skills: list[str] | None = None,
) -> ConfigFile | None:
    """The file `name` stands for, or None when it is not on the list."""
    from istota.skills._loader import OVERLAY_MAX_BYTES

    if name in CORE_FILES:
        return ConfigFile(name, "config", name, storage.USER_CONFIG_READ_CAP_BYTES)
    if not name.startswith(SKILL_PREFIX):
        return None
    skill = name[len(SKILL_PREFIX):]
    if skills is None:
        skills = overlay_skill_names(config, user_id)
    if skill not in skills:
        return None
    return ConfigFile(name, "skill", f"{skill}.md", OVERLAY_MAX_BYTES)


def _directory(config: "Config", user_id: str, file: ConfigFile) -> Path | None:
    if file.kind == "config":
        return storage.resolve_user_config_dir(config, user_id)
    return storage.resolve_user_skill_overlays_dir(config, user_id)


def _read_cap(file: ConfigFile) -> int:
    # An overlay over the loading cap must still be readable, or the user
    # could not open it to shrink it.
    from istota.skills._loader import OVERLAY_READ_CAP_BYTES

    return OVERLAY_READ_CAP_BYTES if file.kind == "skill" else file.max_bytes


def list_files(config: "Config", user_id: str) -> list[dict]:
    skills = overlay_skill_names(config, user_id)
    user_root = config.workspace_root(user_id)
    names = [*CORE_FILES, *(SKILL_PREFIX + skill for skill in skills)]
    directories: dict[str, Path | None] = {}
    rows = []
    for name in names:
        file = lookup(config, user_id, name, skills)
        assert file is not None
        if file.kind not in directories:
            directories[file.kind] = _directory(config, user_id, file)
        directory = directories[file.kind]
        row = {"name": name, "kind": file.kind, "exists": False, "size": None, "editable": False, "view_path": None}
        if directory is not None:
            if user_root is not None:
                relative = (directory / file.leaf).relative_to(user_root.resolve())
                row["view_path"] = f"/Users/{user_id}/{relative.as_posix()}"
            try:
                st = os.lstat(directory / file.leaf)
            except FileNotFoundError:
                row["editable"] = True
            except OSError:
                pass
            else:
                row["exists"] = True
                if stat.S_ISREG(st.st_mode):
                    row["size"] = st.st_size
                    row["editable"] = True
        rows.append(row)
    return rows


def read(config: "Config", user_id: str, file: ConfigFile) -> str | None:
    """The file's text, `""` when it is absent, None when it was refused."""
    directory = _directory(config, user_id, file)
    if directory is None:
        logger.warning("config_file_outside_user_tree user=%s file=%s", user_id, file.name)
        return None
    return _read_path(user_id, file, directory / file.leaf)


def _read_path(user_id: str, file: ConfigFile, path: Path) -> str | None:
    text, reason = storage.read_regular_file(path, max_bytes=_read_cap(file))
    if reason is not None:
        logger.warning("config_file_read_refused user=%s file=%s reason=%s", user_id, file.name, reason)
    return text


def _validator(config: "Config", user_id: str, file: ConfigFile):
    if file.name == "CRON.md":
        from istota.cron_loader import cron_document_errors
        return lambda content: cron_document_errors(content, config, user_id)
    if file.name == "HEARTBEAT.md":
        from istota.heartbeat import heartbeat_document_errors
        return lambda content: heartbeat_document_errors(content, user_id)
    return None


def save(
    config: "Config", user_id: str, file: ConfigFile, content: str, expected_revision: str,
) -> text_files.SaveOutcome:
    directory = _directory(config, user_id, file)
    if directory is None:
        logger.warning("config_file_outside_user_tree user=%s file=%s", user_id, file.name)
        return text_files.SaveOutcome("failed")
    # The resolved path is the lock key, as for every daemon writer of the file.
    path = directory / file.leaf
    parts = [config.bot_dir_name, "config"] + (["skills"] if file.kind == "skill" else [])

    def write(text: str) -> bool:
        # The directory may not exist yet (a first overlay). Each component is
        # created and opened without following a link, as an upload's is.
        os.close(open_or_create_dirs(config.workspace_root(user_id), parts))
        return storage.write_regular_file(path, text)

    return text_files.save(
        config, user_id, path, content, expected_revision,
        read=lambda: _read_path(user_id, file, path),
        write=write,
        validate=_validator(config, user_id, file),
        max_bytes=file.max_bytes,
    )
