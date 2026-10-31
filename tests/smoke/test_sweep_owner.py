"""The session sweep against real compose projects (ISSUE-900).

The default suite fakes `docker compose ls`, so only this file shows that the
sweep reads the listing Docker really returns and that `down` really removes a
project. A prefix private to the test keeps it away from every other stack,
including those of a session running beside it.
"""

from __future__ import annotations

import json
import subprocess
import uuid

import pytest

from testbed import stack as stack_support

from ..conftest import require_docker

pytestmark = pytest.mark.smoke

COMPOSE_FILE = """\
services:
  idle:
    image: busybox:1.37
    command: ["sleep", "600"]
"""


def _projects(prefix: str) -> set[str]:
    listing = subprocess.run(
        ["docker", "compose", "ls", "--all", "--format", "json"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    names = {project["Name"] for project in json.loads(listing.stdout or "[]")}
    return {name for name in names if name.startswith(prefix)}


def _up(compose_file, project: str) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "--project-name", project, "up", "--detach"],
        capture_output=True, text=True, timeout=120, check=True,
    )


def test_the_sweep_removes_only_stacks_with_no_live_owner(tmp_path):
    require_docker()
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(COMPOSE_FILE)
    prefix = f"istota-sweepcheck-{uuid.uuid4().hex[:8]}-"

    owner = subprocess.Popen(["sleep", "300"])
    finished = subprocess.Popen(["true"])
    finished.wait()
    live = f"{prefix}p{owner.pid}-abcd1234"
    dead = f"{prefix}p{finished.pid}-abcd1234"
    unowned = f"{prefix}abcd1234"
    kept = f"{prefix.rstrip('-')}{stack_support.KEEP_PROJECT_MARKER}abcd1234"
    everything = {live, dead, unowned, kept}

    try:
        for project in everything:
            _up(compose_file, project)
        assert _projects(prefix) == everything

        stack_support.sweep_projects(prefix)
        assert _projects(prefix) == {live, kept}

        # The owner going away is what makes the same stack a leftover.
        owner.terminate()
        owner.wait()
        stack_support.sweep_projects(prefix)
        assert _projects(prefix) == {kept}
    finally:
        owner.kill()
        owner.wait()
        for project in everything:
            stack_support.down(
                ["docker", "compose", "-f", str(compose_file), "--project-name", project],
                volumes=True,
            )
