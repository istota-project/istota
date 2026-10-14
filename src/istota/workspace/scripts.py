"""Descriptions, references and limits for the user's scripts inventory."""

import logging
import os
import re
import sqlite3
import stat
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from pathlib import Path, PurePosixPath

from istota import cron_loader, db, heartbeat, storage
from istota.skills._loader import open_overlay_dir, read_overlay_bytes
from istota.workspace.listing import ListingError, list_dir

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger(__name__)

DESCRIPTION_HEAD_BYTES = 8192
DESCRIPTION_MAX_CHARS = 200
SCRIPTS_MAX_DEPTH = 4
SCRIPTS_MAX_FILES = 1000

_PREAMBLE = re.compile(
    r"#\s*(?:(?:-\*-\s*)?coding[:=]\s*[-\w.]+|shellcheck\b|noqa\b|type:|pyright:|vim:)"
    r"|set\s+(?:-[a-zA-Z]+(?:\s+pipefail)?)(?:\s*;)?\s*$"
)
_DOCSTRING = re.compile(r"[rRuU]?(\"\"\"|''')")
_REFERENCE_START = r"(?<![^\s'\"`;|&<>()=])"
_REFERENCE_TAIL = r"([^\s'\"`;|&<>()]+)"


def _matched_script_paths(text: str, prefix_pattern: str) -> list[str]:
    paths = set()
    pattern = _REFERENCE_START + prefix_pattern + _REFERENCE_TAIL
    for match in re.finditer(pattern, text):
        raw = match.group(1)
        # Reject traversal before stripping punctuation: a final '..' is
        # a path component too, not a sentence-ending period.
        if re.search(r"(?:^|/)\.\.(?:/|[.,:]*$)", raw):
            continue
        path = PurePosixPath(raw.rstrip(".,:").lstrip("/"))
        if path.parts:
            paths.add(path.as_posix())
    return sorted(paths)


def find_script_paths(
    text: str, *, user_id: str, bot_dir: str, workspace_path: Path,
) -> list[str]:
    """Extract relative script paths from the user's absolute path forms.

    Like maintenance.workspace_paths._PATH_END, shell quotes and punctuation
    delimit paths. Here slashes stay inside the extracted relative path;
    trailing prose punctuation is stripped after extraction. Roots must start
    at a shell boundary so a longer, unrelated root cannot match by suffix.
    """
    roots = {
        str(workspace_path).rstrip("/"),
        str(workspace_path.resolve()).rstrip("/"),
        "$ISTOTA_WORKSPACE_PATH", "${ISTOTA_WORKSPACE_PATH}",
        "$NEXTCLOUD_MOUNT_PATH", "${NEXTCLOUD_MOUNT_PATH}",
    }
    root_pattern = "|".join(re.escape(root) for root in sorted(roots))
    prefix = re.escape(f"/Users/{user_id}/{bot_dir}/scripts/")
    return _matched_script_paths(text, f"(?:{root_pattern})?{prefix}")


def find_bare_mentions(text: str, existing: set[str]) -> list[str]:
    """Find bare scripts/ paths in prompts, limited to known files."""
    return [path for path in _matched_script_paths(text, "scripts/") if path in existing]


def _summary(text: str) -> str | None:
    text = " ".join(text.split())
    if len(text) > DESCRIPTION_MAX_CHARS:
        return text[:DESCRIPTION_MAX_CHARS] + "…"
    return text or None


def describe(name: str, head: bytes, *, kind: str) -> str | None:
    """Read only the leading description, never comments below code.

    The filename does not select a language: every script may use a comment
    or a Python-style docstring. Prompt files use their first non-empty line.
    """
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\0" in text:
        return None
    lines = iter(text.splitlines())
    if kind == "prompt":
        for line in lines:
            if line.strip():
                return _summary(line.strip().lstrip("#"))
        return None

    comment_started = False
    for index, line in enumerate(lines):
        if index == 0 and line.startswith("#!"):
            continue
        line = line.strip()
        if not comment_started and (not line or _PREAMBLE.match(line)):
            continue
        for marker in ("#", "//", "--"):
            if line.startswith(marker):
                content = line[len(marker):].strip()
                if marker == "#" and not content.strip("#"):
                    content = ""
                if content:
                    return _summary(content)
                comment_started = True
                break
        else:
            if comment_started:
                return None
            opening = _DOCSTRING.match(line)
            if opening is None:
                return None
            quotes = opening.group(1)
            content = line[opening.end():]
            while True:
                description, closing, _ = content.partition(quotes)
                if description.strip():
                    return _summary(description)
                if closing:
                    return None
                content = next(lines, None)
                if content is None:
                    return None
    return None


@dataclass(frozen=True)
class ScriptRef:
    source: str
    name: str
    field: str
    kind: str
    state: str


@dataclass(frozen=True)
class ScriptFile:
    path: str
    kind: str
    size: int
    modified: str
    description: str | None


@dataclass(frozen=True)
class ScriptsIndex:
    scripts_view_path: str
    files: list[ScriptFile]
    referenced_by: dict[str, list[ScriptRef]]
    mentioned_in: dict[str, list[ScriptRef]]
    broken: dict[str, list[ScriptRef]]
    truncated: bool
    sources: dict[str, str]


def scan_scripts(root: Path) -> tuple[list[ScriptFile], bool]:
    """Walk a user's bot/scripts tree, checking every component below the user."""
    user_root = root.parent.parent
    base = [root.parent.name, root.name]
    try:
        fd = open_overlay_dir(user_root, *base, raise_errors=True)
    except FileNotFoundError:
        return [], False
    if fd is None:
        raise ListingError(404, "Scripts folder refused.")
    os.close(fd)
    pending = deque([[]])
    files = []
    truncated = False
    while pending:
        parts = pending.popleft()
        try:
            listing = list_dir(user_root, base + parts)
            directory_fd = open_overlay_dir(user_root, *base, *parts, raise_errors=True)
            if directory_fd is None:
                raise ListingError(404, "Scripts folder refused.")
        except (ListingError, OSError) as exc:
            if not parts:
                raise
            logger.debug("scripts_subdir_skipped error=%s", type(exc).__name__)
            continue
        truncated |= listing.truncated
        try:
            for entry in listing.entries:
                if entry.hidden or entry.name in {"__pycache__", "node_modules", "venv"}:
                    continue
                if entry.kind == "dir":
                    if len(parts) < SCRIPTS_MAX_DEPTH:
                        pending.append([*parts, entry.name])
                    else:
                        truncated = True
                    continue
                if entry.kind != "file":
                    continue
                if len(files) >= SCRIPTS_MAX_FILES:
                    return sorted(files, key=lambda file: file.path), True
                path = PurePosixPath(*parts, entry.name)
                kind = "prompt" if path.parts[0] == "prompts" and path.suffix in {".txt", ".md"} else "script"
                head, _, _ = read_overlay_bytes(
                    root / path, dir_fd=directory_fd,
                    max_bytes=DESCRIPTION_HEAD_BYTES, head_only=True,
                )
                files.append(ScriptFile(
                    path=path.as_posix(), kind=kind, size=entry.size,
                    modified=datetime.fromtimestamp(entry.modified, timezone.utc).isoformat().replace("+00:00", "Z"),
                    description=describe(entry.name, head, kind=kind) if head is not None else None,
                ))
                if len(files) >= SCRIPTS_MAX_FILES:
                    return sorted(files, key=lambda file: file.path), True
        finally:
            os.close(directory_fd)
    return sorted(files, key=lambda file: file.path), truncated


def _target_kind(root: Path, path: str) -> str | None:
    """Check an unlisted reference without following any directory or leaf link."""
    parts = PurePosixPath(path).parts
    fd = open_overlay_dir(root.parent.parent, root.parent.name, root.name, *parts[:-1])
    if fd is None:
        return None
    try:
        info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            return "file"
        if stat.S_ISDIR(info.st_mode):
            return "dir"
    except (OSError, ValueError):
        pass
    finally:
        os.close(fd)
    return None


def _load_source(config: "Config", user_id: str, source: str, loader) -> tuple[object, str]:
    directory = storage.resolve_user_config_dir(config, user_id)
    if directory is None:
        return None, "error"
    try:
        (directory / f"{source.upper()}.md").lstat()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "error"
    try:
        loaded = loader(config, user_id)
    except Exception as exc:
        logger.warning("scripts_source_failed user=%s source=%s error=%s", user_id, source, type(exc).__name__)
        return None, "error"
    return loaded, "ok" if loaded is not None else "error"


def _cron_state(job: cron_loader.CronJob, row: db.ScheduledJob | None, is_admin: bool) -> str:
    if cron_loader.fj_is_disallowed_command(job, is_admin):
        return "admin_only"
    if not job.enabled or (row is not None and row.disabled_at):
        return "disabled"
    if row is None:
        return "not_synced"
    if row.auto_disabled_at:
        return "suspended"
    return "enabled"


def scripts_index(config: "Config", conn: sqlite3.Connection, user_id: str) -> ScriptsIndex | None:
    if not config.has_workspace or config.workspace_root(user_id) is None:
        return None
    root = storage.resolve_user_scripts_dir(config, user_id)
    files, truncated = [], False
    sources = {"scripts": "ok"}
    if root is None:
        sources["scripts"] = "refused"
    else:
        try:
            files, truncated = scan_scripts(root)
        except (ListingError, OSError) as exc:
            sources["scripts"] = "refused"
            logger.warning("scripts_dir_refused user=%s reason=%s", user_id, type(exc).__name__)
    existing = {file.path for file in files}
    referenced_by: dict[str, list[ScriptRef]] = {}
    mentioned_in: dict[str, list[ScriptRef]] = {}
    broken: dict[str, list[ScriptRef]] = {}

    def paths(text: str) -> list[str]:
        return find_script_paths(text, user_id=user_id, bot_dir=config.bot_dir_name, workspace_path=config.workspace_path)

    def add(paths: list[str], ref: ScriptRef) -> None:
        for path in paths:
            if ref.kind == "mentions":
                if path in existing:
                    refs = mentioned_in.setdefault(path, [])
                else:
                    continue
            else:
                kind = None
                if path in existing:
                    kind = "file"
                elif root is not None:
                    kind = _target_kind(root, path)
                if kind == "dir":
                    continue
                refs = (referenced_by if kind == "file" else broken).setdefault(path, [])
            if ref not in refs:
                refs.append(ref)

    document, sources["cron"] = _load_source(config, user_id, "cron", cron_loader.load_cron_document)
    rows = {row.name: row for row in db.get_user_scheduled_jobs(conn, user_id)}
    is_admin = config.is_admin(user_id)
    if document is not None:
        for job in document.jobs:
            state = _cron_state(job, rows.get(job.name), is_admin)
            if job.command:
                add(paths(job.command), ScriptRef("cron", job.name, "command", "runs", state))
            if job.prompt_file:
                add(paths(job.prompt_file), ScriptRef("cron", job.name, "prompt_file", "prompt_file", state))
            elif job.prompt:
                mentions = sorted(set(paths(job.prompt)) | set(find_bare_mentions(job.prompt, existing)))
                add(mentions, ScriptRef("cron", job.name, "prompt", "mentions", state))
    loaded, sources["heartbeat"] = _load_source(config, user_id, "heartbeat", heartbeat.load_heartbeat_config)
    if loaded is not None:
        for check in loaded[1]:
            command = check.config.get("command")
            if check.type == "shell-command" and isinstance(command, str):
                state = "enabled" if is_admin else "admin_only"
                add(paths(command), ScriptRef("heartbeat", check.name, "command", "runs", state))
    for mapping in (referenced_by, mentioned_in, broken):
        for refs in mapping.values():
            refs.sort(key=lambda ref: (ref.source, ref.name))
    return ScriptsIndex(
        scripts_view_path=storage.get_user_scripts_path(user_id, config.bot_dir_name),
        files=files, referenced_by=referenced_by, mentioned_in=mentioned_in,
        broken=broken, truncated=truncated, sources=sources,
    )
