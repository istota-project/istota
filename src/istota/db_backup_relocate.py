"""Move legacy dated DB snapshots into the consolidated backup tree.

The old scheduler destination was ``{mount}/istota-db-backups``. The current
default is ``{mount}/Backups/db/snapshots`` beside the host backup script's
``daily`` and ``weekly`` tiers. This one-shot migrator renames each dated
directory across on the same mount, then writes a marker in the destination.

Run it with the scheduler stopped::

    python -m istota.db_backup_relocate
    python -m istota.db_backup_relocate --dry-run
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .db_backup import backup_destination
from .db_restore import _DAEMON_LOCK_PATH
from .user_scope import is_within

LEGACY_DIR_NAME = "istota-db-backups"
DESTINATION_PARTS = ("Backups", "db", "snapshots")
MARKER_NAME = ".istota-snapshot-layout"
LAYOUT_VERSION = "1"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_PARTIAL = 2

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class RelocationReport:
    status: str
    source: Path | None
    destination: Path | None
    moved: tuple[str, ...] = ()
    left: tuple[str, ...] = ()
    detail: str = ""
    marker_written: bool = False


def _paths(config) -> tuple[Path | None, Path | None, Path | None]:
    mount_value = getattr(config, "nextcloud_mount_path", None)
    if not mount_value:
        return None, None, None
    mount = Path(mount_value)
    return mount, mount / LEGACY_DIR_NAME, mount.joinpath(*DESTINATION_PARTS)


def _resolve(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _entries(root: Path) -> tuple[list[Path], tuple[str, ...]]:
    dated: list[Path] = []
    left: list[str] = []
    with os.scandir(root) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            if _DATE_RE.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                dated.append(Path(entry.path))
            else:
                left.append(entry.name)
    return dated, tuple(left)


def _read_marker(marker: Path) -> tuple[str, str]:
    """Return ``(state, detail)`` without following or dumping a marker link."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        marker_fd = os.open(marker, flags)
    except FileNotFoundError:
        return "missing", ""
    except OSError as exc:
        return "unreadable", str(exc)
    try:
        with os.fdopen(marker_fd, "r") as marker_file:
            version = marker_file.read(32)
    except OSError as exc:
        return "unreadable", str(exc)
    if version == f"{LAYOUT_VERSION}\n":
        return "current", ""
    return "mismatch", "layout marker does not contain the current version"


@contextmanager
def _hold_daemon_lock(lock_path: Path = _DAEMON_LOCK_PATH):
    """Hold the scheduler singleton flock across the whole mutation."""
    try:
        lock_file = open(lock_path, "a")
    except OSError:
        yield False
        return
    held = False
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except (BlockingIOError, OSError):
            pass
        yield held
    finally:
        if held:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


@contextmanager
def _injected_daemon_lock(daemon_running: Callable[[], bool]):
    yield not daemon_running()


def relocate_snapshots(
    config,
    *,
    dry_run: bool = False,
    daemon_running: Callable[[], bool] | None = None,
    daemon_lock_path: Path = _DAEMON_LOCK_PATH,
) -> RelocationReport:
    """Relocate every legacy dated directory without merging two histories."""
    mount, source, destination = _paths(config)
    if mount is None or source is None or destination is None:
        return RelocationReport("no_mount", source, destination)

    effective_destination = backup_destination(config)
    resolved_effective = _resolve(effective_destination) if effective_destination else None
    resolved_expected = _resolve(destination)
    if resolved_effective != resolved_expected:
        return RelocationReport(
            "non_default_destination", source, destination,
            detail="configured db_backup_dir does not resolve to the new default",
        )

    if source.is_symlink():
        return RelocationReport(
            "source_unreadable", source, destination,
            detail="legacy snapshot root is not a plain directory",
        )

    source_exists = source.exists()
    if source_exists and not source.is_dir():
        return RelocationReport(
            "source_unreadable", source, destination,
            detail="legacy snapshot root is not a plain directory",
        )

    resolved_mount = _resolve(mount)
    resolved_source = _resolve(source)
    resolved_destination = resolved_expected
    if resolved_mount is None or resolved_source is None or resolved_destination is None:
        return RelocationReport(
            "path_unresolvable", source, destination,
            detail="mount, source, or destination could not be resolved",
        )
    if not is_within(resolved_source, resolved_mount):
        return RelocationReport(
            "source_outside_mount", source, destination,
            detail="legacy snapshot root resolves outside the configured mount",
        )
    if not is_within(resolved_destination, resolved_mount):
        return RelocationReport(
            "destination_outside_mount", source, destination,
            detail="snapshot destination resolves outside the configured mount",
        )

    dated: list[Path] = []
    left: tuple[str, ...] = ()
    if source_exists:
        try:
            dated, left = _entries(source)
        except OSError as exc:
            return RelocationReport(
                "source_unreadable", source, destination, detail=str(exc)
            )

    marker = destination / MARKER_NAME
    marker_state, marker_detail = _read_marker(marker)
    if marker_state == "unreadable":
        return RelocationReport(
            "marker_unreadable", source, destination, moved=(), left=left,
            detail=marker_detail,
        )
    if marker_state == "mismatch":
        return RelocationReport(
            "marker_mismatch", source, destination, moved=(), left=left,
            detail=marker_detail,
        )
    if marker_state == "current":
        if dated:
            return RelocationReport(
                "legacy_after_marker", source, destination, left=left,
                detail="legacy dated directories appeared after migration completed",
            )
        return RelocationReport("already_migrated", source, destination, left=left)
    if not dated:
        return RelocationReport("no_snapshots", source, destination, left=left)

    try:
        destination_dated, _ = _entries(destination)
    except FileNotFoundError:
        destination_dated = []
    except OSError as exc:
        return RelocationReport(
            "destination_unreadable", source, destination, left=left,
            detail=str(exc),
        )
    if destination_dated:
        names = ", ".join(path.name for path in destination_dated)
        return RelocationReport(
            "destination_has_snapshots", source, destination, left=left,
            detail=f"destination already has dated directories: {names}",
        )

    names = tuple(path.name for path in dated)
    if dry_run:
        return RelocationReport(
            "would_migrate", source, destination, moved=names, left=left
        )

    if getattr(config, "storage_is_nextcloud", False):
        if not os.path.ismount(str(resolved_mount)):
            return RelocationReport(
                "mount_unavailable", source, destination, left=left,
                detail=f"configured Nextcloud mount is not mounted: {resolved_mount}",
            )
    lock = (
        _injected_daemon_lock(daemon_running)
        if daemon_running is not None
        else _hold_daemon_lock(daemon_lock_path)
    )
    with lock as lock_held:
        if not lock_held:
            return RelocationReport(
                "daemon_running", source, destination, left=left,
                detail="scheduler daemon lock is held or cannot be acquired",
            )

        try:
            destination.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink():
                return RelocationReport(
                    "destination_unreadable", source, destination, left=left,
                    detail="snapshot destination is a symlink",
                )
            destination.chmod(0o700)
        except OSError as exc:
            return RelocationReport(
                "destination_unreadable", source, destination, left=left,
                detail=str(exc),
            )

        moved: list[str] = []
        for old_dir in dated:
            new_dir = destination / old_dir.name
            if new_dir.exists():
                return RelocationReport(
                    "partial", source, destination, moved=tuple(moved), left=left,
                    detail=f"destination appeared during migration: {new_dir}",
                )
            try:
                old_dir.rename(new_dir)
            except OSError as exc:
                return RelocationReport(
                    "partial", source, destination, moved=tuple(moved), left=left,
                    detail=f"could not move {old_dir.name}: {exc}",
                )
            moved.append(old_dir.name)

        marker_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            marker_flags |= os.O_NOFOLLOW
        try:
            marker_fd = os.open(marker, marker_flags, 0o600)
            with os.fdopen(marker_fd, "w") as marker_file:
                marker_file.write(f"{LAYOUT_VERSION}\n")
        except OSError as exc:
            return RelocationReport(
                "partial", source, destination, moved=tuple(moved), left=left,
                detail=f"snapshots moved but marker could not be written: {exc}",
            )

        if not left:
            try:
                source.rmdir()
            except OSError:
                pass
        return RelocationReport(
            "migrated", source, destination, moved=tuple(moved), left=left,
            marker_written=True,
        )


def _print_report(report: RelocationReport) -> None:
    for name in report.moved:
        print(f"moved: {name}")
    for name in report.left:
        print(f"left: {name}")
    if report.marker_written and report.destination is not None:
        print(f"marker written: {report.destination / MARKER_NAME}", file=sys.stderr)
    if report.detail:
        print(f"detail: {report.detail}", file=sys.stderr)
    print(f"done: {report.status}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    from .config import load_config

    parser = argparse.ArgumentParser(prog="istota.db_backup_relocate")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report the dated directories that would move without writing.",
    )
    args = parser.parse_args(argv)

    report = relocate_snapshots(load_config(), dry_run=args.dry_run)
    _print_report(report)
    if report.status == "partial":
        return EXIT_PARTIAL
    if report.status in {
        "already_migrated", "migrated", "no_snapshots", "would_migrate",
    }:
        return EXIT_OK
    print(f"refusal: {report.status}", file=sys.stderr)
    return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
