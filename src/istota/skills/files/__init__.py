"""Workspace file operations for the files skill."""

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger("istota.skills.files")


def _workspace_path(config: "Config", path: str) -> Path:
    """`path` under the workspace root; raises where there is no workspace."""
    if not config.has_workspace:
        raise RuntimeError("No workspace is configured")
    return config.workspace_path / path.lstrip("/")


def list_files(config: "Config", path: str) -> list[dict]:
    """
    List files at a path.
    Returns list of dicts with 'name', 'size', 'mod_time', 'is_dir'.
    """
    mount_path = _workspace_path(config, path)
    if not mount_path.exists():
        raise RuntimeError(f"Path not found: {mount_path}")
    return [
        {
            "name": p.name,
            "size": p.stat().st_size if p.is_file() else 0,
            "mod_time": "",
            "is_dir": p.is_dir(),
        }
        for p in mount_path.iterdir()
    ]


def read_text(config: "Config", path: str) -> str:
    """Read a text file."""
    mount_path = _workspace_path(config, path)
    if not mount_path.exists():
        raise RuntimeError(f"File not found: {mount_path}")
    return mount_path.read_text()


def write_text(config: "Config", path: str, content: str) -> None:
    """Write text content to a file."""
    mount_path = _workspace_path(config, path)
    mount_path.parent.mkdir(parents=True, exist_ok=True)
    mount_path.write_text(content)


def mkdir(config: "Config", path: str) -> bool:
    """Create a directory. Returns True on success."""
    if not config.has_workspace:
        return False
    _workspace_path(config, path).mkdir(parents=True, exist_ok=True)
    return True


def path_exists(config: "Config", path: str) -> bool:
    """Check if a path exists."""
    if not config.has_workspace:
        return False
    return _workspace_path(config, path).exists()


def move_file(config: "Config", src_path: str, dst_path: str) -> bool:
    """Move a file or directory. Returns True on success."""
    if not config.has_workspace:
        return False
    src = _workspace_path(config, src_path)
    dst = _workspace_path(config, dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return True


def copy_to_local(config: "Config", remote_path: str, local_path: Path) -> None:
    """Copy a workspace file to the local filesystem."""
    src = _workspace_path(config, remote_path)
    if not src.exists():
        raise RuntimeError(f"Source file not found: {src}")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    # Not copy2: its xattr step raises EIO on some FUSE workspace mounts (#694).
    shutil.copyfile(src, local_path)
    shutil.copymode(src, local_path)


def copy_to_remote(config: "Config", local_path: Path, remote_path: str) -> None:
    """Copy a local file into the workspace."""
    dst = _workspace_path(config, remote_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Not copy2: a local file's user.* xattrs would be set on the mount (#694).
    shutil.copyfile(str(local_path), str(dst))
    shutil.copymode(str(local_path), str(dst))


def get_local_path(config: "Config", remote_path: str) -> Path | None:
    """The workspace path for a file, or None where there is no workspace."""
    if config.has_workspace:
        return config.workspace_path / remote_path.lstrip("/")
    return None
