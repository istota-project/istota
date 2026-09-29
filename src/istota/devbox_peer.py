"""Authenticate devbox credential clients using host-visible Docker cgroups.

Only the configured container is a client. Host tasks obtain their credentials
through SkillProxy. Container exec processes need not descend from the init
process, so ancestry alone cannot authenticate them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from istota import peer_process


_INSPECT_FORMAT = (
    '{"id":{{json .Id}},"pid":{{json .State.Pid}},'
    '"running":{{json .State.Running}},'
    '"user":{{json (index .Config.Labels "com.istota.user_id")}}}'
)


def _cgroup(pid: int, proc_root: Path) -> str | None:
    """The unified cgroup, or the pids controller on a v1 host."""
    try:
        lines = (proc_root / str(pid) / "cgroup").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) == 3 and (parts[0] == "0" or "pids" in parts[1].split(",")):
            return parts[2]
    return None


def peer_in_devbox(
    pid: int | None,
    started: int | None,
    *,
    user_id: str,
    container_name: str,
    docker_cli: str,
    proc_root: Path = Path("/proc"),
) -> bool:
    """Fail closed unless the peer belongs to this user's running container.

    Inspect on each connection so a container restart or recreation invalidates
    the old identity. No token or caller-supplied user selects the container.
    The daemon must see Docker's host PIDs and cgroups; remote Docker and Docker
    Desktop across the VM boundary cannot authenticate a peer this way.
    """
    if not sys.platform.startswith("linux") or pid is None or started is None:
        return False
    try:
        result = subprocess.run(
            [docker_cli, "container", "inspect", "--format", _INSPECT_FORMAT,
             "--", container_name],
            capture_output=True, timeout=5, check=False,
        )
        if result.returncode != 0:
            return False
        info = json.loads(result.stdout)
        container_id = info["id"]
        root_pid = info["pid"]
        if (info["running"] is not True or info["user"] != user_id
                or not isinstance(root_pid, int) or root_pid <= 1
                or not isinstance(container_id, str) or len(container_id) != 64
                or any(c not in "0123456789abcdef" for c in container_id)):
            return False
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return False

    root = _cgroup(root_pid, proc_root)
    peer = _cgroup(pid, proc_root)
    # An unisolated container must never authorize the host cgroup. Require
    # Docker's full ID in the init's cgroup, then compare the complete path;
    # a lookalike nested under another user's cgroup cannot match it.
    if not root or not peer or root.rsplit("/", 1)[-1] not in (
        container_id, f"docker-{container_id}.scope",
    ):
        return False
    if peer != root and not peer.startswith(root + "/"):
        return False
    # The peer may have exited while Docker answered. Recycled PIDs inherit
    # nothing, even when the replacement lives inside the allowed container.
    return peer_process.start_time(pid) == started
