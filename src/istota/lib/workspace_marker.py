"""The marker a host folder's workspace carries at its root, and whether one is required.

On the local VM the workspace is a folder on the Mac shared in with virtiofs.
A folder renamed, deleted or on an unplugged volume does not take the mount
with it: Lima recreates a missing folder empty at the next start, and the
guest sees an empty directory of the same filesystem type, so neither
``ismount`` nor the type tells the two apart. ``.istota-workspace``, written
into the folder when it is created, does.

``ISTOTA_WORKSPACE_MARKER_REQUIRED=1`` says this install has one. `istota-stack`
sets it from host.env's ``STORAGE=host-folder`` and compose passes it to the
daemon and web; an rclone mount has no marker and nothing asks it for one.
Three readers: the entrypoint's preflight, doctor's ``runtime.mount_liveness``
and the backup's mount guard.

stdlib-only leaf, imports nothing from istota, never raises.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping

MARKER_NAME = ".istota-workspace"
REQUIRED_ENV = "ISTOTA_WORKSPACE_MARKER_REQUIRED"


def marker_required(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the host says the workspace must carry the marker."""
    env = os.environ if environ is None else environ
    return env.get(REQUIRED_ENV, "").strip() == "1"


def marker_present(root: str | os.PathLike) -> bool:
    """Whether ``root`` holds the marker as a regular file.

    Not followed through a link: a link in the folder is resolved by whichever
    side reads it, and one the Mac made names a path the VM cannot see.
    """
    try:
        st = os.lstat(os.path.join(os.fspath(root), MARKER_NAME))
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(st.st_mode)
