"""Resumable mount work for room_relocate; service units must be stopped.

The mapping is permanent. Completion is observed from the actual trees, never
recorded as a flag that could hide a late VFS flush into an old directory.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import sys
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import tomli
import tomli_w

from . import cron_loader, storage
from .nextcloud import dav
from .nextcloud._http import OcsError, dav_files_url, dav_request
from .room_relocate import EXIT_OK, EXIT_PARTIAL, EXIT_REFUSED, _descriptor, _preflight, _refusal
from .toml_fence import BACKTICK_RUN_RE, FENCE_OPEN_RE, find_toml_block
from .user_scope import is_scopable_user_id


def _local_kind(path: Path) -> str | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    raise ValueError(f"unsafe entry: {path.name}")


def _check_tree(path: Path) -> None:
    if _local_kind(path) == "directory":
        for child in path.iterdir():
            _check_tree(child)


def _merge_local(source: Path, target: Path, *, dry_run: bool) -> None:
    source_kind, target_kind = _local_kind(source), _local_kind(target)
    if source_kind is None:
        return
    if target_kind is None:
        if not dry_run:
            if source_kind == "directory":
                os.rename(source, target)
            else:
                # link refuses an existing destination atomically. A plain
                # rename could overwrite a file created since the stat above.
                os.link(source, target, follow_symlinks=False)
                source.unlink()
        return
    if source_kind == target_kind == "file" and os.path.samefile(source, target):
        # A prior run stopped between link and unlink.
        if not dry_run:
            source.unlink()
        return
    if source_kind != "directory" or target_kind != "directory":
        raise ValueError(f"collision: {source.name}")
    for child in sorted(source.iterdir()):
        _merge_local(child, target / child.name, dry_run=dry_run)
    if not dry_run:
        source.rmdir()  # Empty only: never recursively delete a late write.


def _dav_stat(config, path: str):
    try:
        return dav.stat(config, path)
    except OcsError as exc:
        if exc.http_status == 404:
            return None
        raise


def _move_dav(config, source: str, target: str) -> None:
    try:
        dav_request(config, "MOVE", dav_files_url(config, source), headers={
            "Destination": dav_files_url(config, target), "Overwrite": "F",
        })
    except OcsError:
        # A timeout can be an applied MOVE whose answer was lost. Never use
        # the FUSE view here: its directory cache may lag the server by 30s.
        source_after = _dav_stat(config, source)
        target_after = _dav_stat(config, target)
        if source_after is None and target_after is not None:
            return
        raise


def _merge_dav(config, source: str, target: str, *, dry_run: bool) -> None:
    source_info, target_info = _dav_stat(config, source), _dav_stat(config, target)
    if source_info is None:
        return
    if target_info is None:
        if not dry_run:
            _move_dav(config, source, target)
        return
    if not source_info["is_dir"] or not target_info["is_dir"]:
        raise ValueError(f"collision: {source}")
    for entry in dav.list_dir(config, source):
        name = entry["name"]
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("unsafe DAV entry")
        if entry["path"].rstrip("/") != source + "/" + name:
            raise ValueError("DAV entry outside source directory")
        _merge_dav(config, source + "/" + name, target + "/" + name, dry_run=dry_run)
    # DAV DELETE on a collection is recursive. Leave the empty shell rather
    # than risk deleting a write that arrived after this listing. Every pass
    # lists it again; an empty shell carries no outstanding history.


def _block(content: str):
    span = find_toml_block(content)
    if span is None:
        if BACKTICK_RUN_RE.search(content):
            raise ValueError("unresolved TOML fence")
        return None, {}
    # Multiple TOML blocks are ambiguous configuration. Keep all prose and
    # non-TOML examples outside the one recognized block byte-for-byte.
    if len(list(FENCE_OPEN_RE.finditer(content))) != 1:
        raise ValueError("multiple TOML fences")
    return span, tomli.loads(content[span[0]:span[1]])


def _cron_plan(config, user: str, content: str, mapping: dict[str, str]):
    span, raw = _block(content)
    if span is None:
        return None
    doc = cron_loader.parse_cron_document(content, config, user)
    if doc is None or doc.skipped_entries or set(raw) - {"jobs"}:
        raise ValueError("CRON.md cannot preserve every entry")
    names = set()
    for entry, job in zip(raw.get("jobs", []), doc.jobs, strict=True):
        if job.name in names:
            raise ValueError("duplicate job name")
        names.add(job.name)
        # The runtime parser intentionally coerces bad fields and ignores
        # unknown ones. A migration must refuse that loss before rendering.
        for key, value in entry.items():
            if not hasattr(job, key) or type(value) is not type(getattr(job, key)) or value != getattr(job, key):
                raise ValueError(f"CRON.md field would change: {key}")
    jobs = []
    for job in doc.jobs:
        target = job.target
        for old, new in mapping.items():
            target = _descriptor(target, old, new)
        jobs.append(replace(job, room=mapping.get(job.room, job.room), target=target))
    if jobs == doc.jobs:
        return None
    block = cron_loader.render_jobs_block(jobs)
    rendered = cron_loader.parse_cron_document(content[:span[0]] + block + content[span[1]:], config, user)
    if rendered is None or rendered.jobs != jobs or rendered.skipped_entries:
        raise ValueError("CRON.md jobs do not round-trip")
    return (content[:span[0]] + block + content[span[1]:], doc, jobs)


def _briefing_plan(content: str, mapping: dict[str, str]):
    from .user_briefings import parse_briefings_md, _row_from_entry
    span, raw = _block(content)
    if span is None:
        return None
    entries = parse_briefings_md(content)
    if entries is None or not isinstance(raw.get("briefings", []), list) or len(entries) != len(raw.get("briefings", [])):
        raise ValueError("BRIEFINGS.md cannot preserve every entry")
    names = set()
    changed = False
    for entry in entries:
        if _row_from_entry(entry, "migration") is None:
            raise ValueError("malformed briefing entry")
        if entry["name"] in names:
            raise ValueError("duplicate briefing name")
        names.add(entry["name"])
        for key in ("conversation_token", "output"):
            if key not in entry:
                continue
            value = entry[key]
            if not isinstance(value, str):
                raise ValueError(f"BRIEFINGS.md non-string {key}")
            updated = mapping.get(value, value) if key == "conversation_token" else value
            if key == "output":
                for old, new in mapping.items():
                    updated = _descriptor(updated, old, new)
            changed |= updated != value
            entry[key] = updated
    if not changed:
        return None
    raw["briefings"] = entries
    block = tomli_w.dumps(raw).replace("`", "\\u0060")
    if tomli.loads(block) != raw:
        raise ValueError("BRIEFINGS.md does not round-trip")
    return content[:span[0]] + block + content[span[1]:]


def _backup(config, user: str, filename: str, content: str) -> None:
    root = config.workspace_path
    path = root / "Backups" / "room-token-migration" / datetime.now(timezone.utc).strftime("%Y-%m-%d") / user
    # Backups are host-side writes too; reject every link in their ancestry.
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        kind = _local_kind(current)
        if kind is None:
            current.mkdir(mode=0o700)
        elif kind != "directory":
            raise ValueError("backup path is not a directory")
    target = path / filename
    if _local_kind(target) is not None:
        original, reason = storage.read_regular_file(target)
        if reason or original != content:
            raise ValueError("backup collision")
    elif not storage.create_file_if_absent(target, content):
        raise OSError("backup write failed")


def _rewrite_user(config, user: str, mapping: dict[str, str], *, dry_run: bool) -> None:
    if not is_scopable_user_id(user):
        raise ValueError("unsafe user id")
    directory = storage.resolve_user_config_dir(config, user)
    if directory is None or not directory.is_dir():
        raise ValueError("user workspace unavailable")
    plans = []
    # Parse both before touching either; malformed input refuses the user.
    for filename in ("CRON.md", "BRIEFINGS.md"):
        path = directory / filename
        if _local_kind(path) is None:
            continue
        content, reason = storage.read_regular_file(path)
        if reason:
            raise ValueError(f"{filename}: {reason}")
        plan = _cron_plan(config, user, content, mapping) if filename == "CRON.md" else _briefing_plan(content, mapping)
        if plan is not None:
            plans.append((filename, content, plan))
    if dry_run:
        return
    for filename, content, plan in plans:
        _backup(config, user, filename, content)
    for filename, content, plan in plans:
        current, reason = storage.read_regular_file(directory / filename)
        if reason or current != content:
            raise ValueError(f"{filename} changed during migration")
        if filename == "CRON.md":
            ok = cron_loader._write_cron_md(config, user, plan[2], doc=plan[1], externalize_prompts=False)
        else:
            ok = storage.write_regular_file(directory / filename, plan)
        if not ok:
            raise OSError(f"{filename} write failed")


def reconcile(config, *, dry_run: bool = False) -> int:
    try:
        with closing(sqlite3.connect(config.db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            _preflight(conn)
            # A deleted room leaves an alias tombstone. Never resurrect its
            # files and never make every future sweep report it incomplete.
            mapping = dict(conn.execute(
                "SELECT m.old_token,m.new_token FROM room_token_migration m "
                "JOIN rooms r ON r.token=m.new_token ORDER BY m.old_token",
            ))
            users = set(config.users) | {r[0] for r in conn.execute("SELECT user_id FROM user_profiles")}
        for old, new in mapping.items():
            storage.validate_conversation_token(old)
            storage.validate_conversation_token(new)
    except Exception as exc:
        _refusal(str(exc))
        return EXIT_REFUSED
    try:
        root = config.workspace_path
        if root is None or not root.is_dir():
            raise ValueError("workspace unavailable")
        if config.nextcloud_mount_path is not None and not os.path.ismount(config.nextcloud_mount_path):
            raise ValueError("Nextcloud mount unavailable")
        if config.nextcloud.url:
            if _dav_stat(config, "/") is None:
                raise ValueError("DAV workspace unavailable")
        elif _local_kind(root / "Channels") not in {None, "directory"}:
            raise ValueError("Channels is not a directory")
    except Exception as exc:
        print(f"partial: {exc}", file=sys.stderr)
        return EXIT_PARTIAL
    failures = 0
    for old, new in mapping.items():
        try:
            if config.nextcloud.url:
                _merge_dav(config, f"/Channels/{old}", f"/Channels/{new}", dry_run=dry_run)
            else:
                source, target = root / "Channels" / old, root / "Channels" / new
                _check_tree(source)
                _check_tree(target)
                _merge_local(source, target, dry_run=dry_run)
            print(f"mount: {old} -> {new}")
        except Exception as exc:
            failures += 1
            print(f"failed: {old}: {exc}", file=sys.stderr)
    for user in sorted(users):
        try:
            _rewrite_user(config, user, mapping, dry_run=dry_run)
        except Exception as exc:
            failures += 1
            print(f"refusal: user {user}: {exc}", file=sys.stderr)
    return EXIT_PARTIAL if failures else EXIT_OK
