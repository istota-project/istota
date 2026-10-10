"""The istota container's root phase, and the drop every exec path goes through.

`docker/istota/root-phase.sh` is what runs as uid 0 before anything else in
the container: it delegates the container's own cgroup to the daemon's uid,
fixes ownership once on an upgraded volume, checks the sandbox grant, and execs
`istota-drop`, which empties every capability set and becomes uid 10001.

Its decisions are shell functions taking their inputs as arguments, so this
file sources the script (the `main` guard keeps sourcing inert) and calls them
against a fabricated `/proc/self/mountinfo`. The one decision that matters most
is the mountinfo-root refusal: a cgroup2 mount that is writable *and* rooted
above the container (`/../..`, a bind of the host's tree) would hand the daemon
the VM's whole cgroup hierarchy, so the root phase must stop before it writes
anything. The container-level witness is the smoke tier's; this is the unit.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT_PHASE = REPO / "docker" / "istota" / "root-phase.sh"
ISTOTA_DROP = REPO / "docker" / "istota" / "istota-drop"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="the root phase is bash")


def _call(function: str, *args: str) -> subprocess.CompletedProcess:
    script = f'source "{ROOT_PHASE}"; {function} "$@"'
    return subprocess.run(
        ["bash", "-c", script, "root-phase-test", *args],
        capture_output=True, text=True, timeout=30,
    )


def _mountinfo(tmp_path: Path, line: str) -> Path:
    path = tmp_path / "mountinfo"
    path.write_text(
        "30 1 0:30 / / rw,relatime - overlay overlay rw\n"
        + line
        + "50 30 0:50 / /tmp rw,nosuid,nodev - tmpfs tmpfs rw\n"
    )
    return path


OWN = "41 30 0:41 / /sys/fs/cgroup ro,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw,nsdelegate\n"
HOST_BIND = "41 30 0:41 /../.. /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw\n"
V1 = "41 30 0:41 / /sys/fs/cgroup ro,nosuid,nodev,noexec - tmpfs tmpfs ro\n"


class TestTheMountinfoRootRefusal:
    def test_reads_root_fstype_and_options(self, tmp_path):
        result = _call("cgroup_mount_fields", str(_mountinfo(tmp_path, OWN)), "/sys/fs/cgroup")

        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["/", "cgroup2", "ro,nosuid,nodev,noexec,relatime"]

    def test_the_containers_own_cgroup_is_accepted(self, tmp_path):
        result = _call("require_own_cgroup", str(_mountinfo(tmp_path, OWN)), "/sys/fs/cgroup")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_host_bind_is_refused_with_its_root_named(self, tmp_path):
        result = _call("require_own_cgroup", str(_mountinfo(tmp_path, HOST_BIND)), "/sys/fs/cgroup")

        assert result.returncode == 70
        assert "/../.." in result.stdout + result.stderr

    def test_a_cgroup_v1_tree_is_refused(self, tmp_path):
        result = _call("require_own_cgroup", str(_mountinfo(tmp_path, V1)), "/sys/fs/cgroup")

        assert result.returncode == 70

    def test_no_mount_at_all_is_refused(self, tmp_path):
        result = _call("require_own_cgroup", str(_mountinfo(tmp_path, "")), "/sys/fs/cgroup")

        assert result.returncode == 70


class TestTheScriptIsInertWhenSourced:
    def test_sourcing_runs_nothing(self):
        result = subprocess.run(
            ["bash", "-c", f'source "{ROOT_PHASE}" && echo SOURCED'],
            capture_output=True, text=True, timeout=30,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "SOURCED"


class TestTheGrantRefusal:
    def test_the_message_names_the_compose_lines_to_add(self):
        result = _call("grant_refusal")

        message = result.stdout + result.stderr
        assert result.returncode != 0
        for line in (
            "seccomp=./istota/seccomp-istota.json",
            "apparmor=istota",
            "systempaths=unconfined",
            "no-new-privileges:true",
        ):
            assert line in message


class TestTheDrop:
    """`istota-drop` is the one spelling of the drop, for the entrypoint and every exec path."""

    def test_it_empties_every_capability_set_and_becomes_10001(self):
        text = ISTOTA_DROP.read_text()
        for flag in (
            "--reuid=10001", "--regid=10001", "--init-groups",
            "--inh-caps=-all", "--bounding-set=-all",
        ):
            assert flag in text, flag
        assert 'exec setpriv' in text
        # The dropped process reads the compose secrets, then runs the command.
        assert '/usr/local/bin/istota-secrets "$@"' in text

    def test_the_root_phase_ends_by_execing_it(self):
        lines = [line.strip() for line in ROOT_PHASE.read_text().splitlines()]
        assert 'exec istota-drop "$@"' in lines
