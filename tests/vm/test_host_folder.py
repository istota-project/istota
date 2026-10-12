"""The local VM's workspace: a folder on the Mac, shared in, and row L1.

The host-folder mode is the stack the local VM runs: local ingress, and the
workspace a Mac folder Lima shares in with virtiofs at
`/srv/mount/host-folder`, which compose binds at `/mnt/shared`. The folder is
under the tier's work directory and kept between sessions; the module shares
it in for its own run and takes it out again at the end, since row 7 asserts
the VM mounts no Mac path but the checkout.

Row L1: the daemon never writes into a workspace that is not the user's
folder. A folder renamed, deleted or on an unplugged volume leaves an empty
directory of the same type in its place (Lima recreates a missing one at
start), so the `.istota-workspace` marker is what tells the two apart. With
the marker gone the daemon refuses to start and writes nothing; with it back
it starts; doctor's `runtime.mount_liveness` fails while it is gone.

Control `no-workspace-marker`: the daemon service is started from an image
whose entrypoint has the marker refusal removed
(`docker/test/Dockerfile.no-workspace-marker`).
"""

from __future__ import annotations

import os
import shlex
import time
import uuid
from pathlib import Path

import pytest

from tests.support import parity

from . import lima

pytestmark = pytest.mark.vm


@pytest.fixture(scope="module")
def folder() -> Path:
    path = lima.host_folder()
    (path / lima.HOST_FOLDER_MARKER).write_text("")
    return path


@pytest.fixture(scope="module")
def stack(vm, release, model, folder):
    lima.share_host_folder(vm, release, folder)
    try:
        lima.ensure_mode(vm, release, lima.host_folder_mode(model.vm_url))
        yield vm
    finally:
        (folder / lima.HOST_FOLDER_MARKER).write_text("")
        lima.unshare_host_folder(vm, release)


@pytest.fixture(scope="module")
def istota_image(stack, release) -> str:
    """The tag the L1 case starts the daemon service from."""
    if lima.control() != "no-workspace-marker":
        return release.tag
    tag = f"{release.tag}-no-workspace-marker"
    stack.run(
        f"docker build -q -f {lima.STACK}/src/docker/test/Dockerfile.no-workspace-marker "
        f"--build-arg BASE=istota:{release.tag} -t istota:{tag} {lima.STACK}/src/docker/test",
        timeout=900)
    return tag


def _listing(folder: Path) -> list[str]:
    found = []
    for root, dirs, files in os.walk(folder):
        for name in dirs + files:
            found.append(os.path.relpath(os.path.join(root, name), folder))
    return sorted(found)


class TestTheWorkspaceIsTheMacFolder:
    def test_the_stack_waits_for_the_folder(self, stack):
        unit = stack.out("systemctl show istota-stack.service -p RequiresMountsFor")
        assert lima.HOST_FOLDER_MOUNT in unit, unit

    def test_the_workspace_is_the_shared_folder_in_the_container(self, stack):
        mountinfo = lima.stack_exec(stack, "grep ' /mnt/shared ' /proc/self/mountinfo").stdout
        assert " - virtiofs " in mountinfo, mountinfo
        assert lima.doctor(stack, "runtime.mount_liveness")["status"] == "ok"

    def test_a_daemon_write_is_the_mac_users_file(self, stack, folder):
        name = f"vmtier-{uuid.uuid4().hex[:10]}.txt"
        lima.stack_exec(stack, f"echo from-the-daemon > /mnt/shared/{name}")
        path = folder / name
        assert path.read_text().strip() == "from-the-daemon"
        assert path.stat().st_uid == os.getuid()

    def test_a_mac_write_is_seen_by_the_daemon(self, stack, folder):
        name = f"vmtier-{uuid.uuid4().hex[:10]}.txt"
        (folder / name).write_text("from-the-mac\n")
        lima.wait_for(
            lambda: lima.stack_exec(stack, f"cat /mnt/shared/{name}", check=False).stdout.strip() == "from-the-mac",
            timeout=2, interval=0.2, what=f"{name} to show in the container")

    def test_doctor_fails_while_the_marker_is_gone(self, stack, folder):
        marker = folder / lima.HOST_FOLDER_MARKER
        marker.unlink()
        try:
            result = lima.doctor(stack, "runtime.mount_liveness")
            assert result["status"] == "fail", result
            assert lima.HOST_FOLDER_MARKER in result.get("detail", ""), result
        finally:
            marker.write_text("")


@parity.witness("L1")
class TestAFolderThatWentAwayIsNotWritten:
    def test_without_its_marker_the_daemon_does_not_start(self, stack, folder, istota_image):
        marker = folder / lima.HOST_FOLDER_MARKER
        stack.run("systemctl stop istota-stack.service", timeout=600)
        try:
            marker.unlink()
            before = _listing(folder)
            # Past systemd, as an operator could: compose itself, the marker gone.
            stack.run(f"ISTOTA_TAG={shlex.quote(istota_image)} istota-stack compose up -d --no-deps daemon",
                      timeout=600)
            cid = stack.out(
                "docker ps -aq --filter label=com.docker.compose.project=istota "
                "--filter label=com.docker.compose.service=daemon | head -1")
            state = ""
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                state = stack.out(f"docker inspect -f '{{{{.State.Status}}}} {{{{.RestartCount}}}} "
                                  f"{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{end}}}}' {cid}")
                fields = state.split()
                if "healthy" in fields or fields[0] in ("exited", "restarting") or int(fields[1]) > 0:
                    break
                time.sleep(3)
            logs = stack.out(f"docker logs --tail 40 {cid} 2>&1")
            after = _listing(folder)
            assert "healthy" not in state.split(), f"row L1: the daemon started on a stand-in: {state}"
            assert f"REFUSE: the workspace /mnt/shared has no {lima.HOST_FOLDER_MARKER}" in logs, logs[-2000:]
            assert after == before, f"row L1: written into the stand-in: {sorted(set(after) - set(before))}"
        finally:
            stack.run("istota-stack compose rm -sf daemon >/dev/null 2>&1 || true", timeout=600)
            marker.write_text("")
            stack.run("systemctl start istota-stack.service", timeout=1800)
            lima.wait_healthy(stack)

    def test_with_the_marker_back_it_starts(self, stack):
        lima.wait_healthy(stack)
        assert lima.doctor(stack, "runtime.mount_liveness")["status"] == "ok"
