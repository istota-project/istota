"""Full Nextcloud integration over the VM's rclone mount, and parity row 9.

The layout a Docker install moved off the old bundled Nextcloud runs: the
testbed's Nextcloud fixture (provisioned by `provision-nc.sh`, which creates the
bot's `Shared Files` external mount) runs in the VM as its own compose
project, the VM's `mount-nextcloud.service` mounts the bot's `Shared Files`
folder over WebDAV (the rclone remote is an alias rooted there), and the stack
runs with `dav_prefix = "Shared Files"`. Talk is left off (`--no-talk`): the
full tier covers Talk against the same fixture; this file is about the mount.

Row 9: never write into an unmounted store. `istota-stack.service` requires
and follows the mount unit (provision.sh's drop-in), so stopping the mount
stops the stack; and the entrypoint refuses a workspace that is not a
`fuse.rclone` mount, so the daemon service started by hand with the mount
down does not start, and nothing lands in `/srv/mount/nextcloud/content`.

Control `no-storage-refusal`: the daemon service is started from an image whose
entrypoint has the refusal removed (`docker/test/Dockerfile.no-storage-refusal`).
"""

from __future__ import annotations

import shlex
import time
import uuid

import pytest

from tests.support import parity

from . import lima

pytestmark = pytest.mark.vm

MOUNT = lima.MOUNT


def _webdav(vm: lima.Vm, fixture: dict, method: str, name: str, *, data: str = "") -> str:
    url = (f"http://127.0.0.1:{lima.NC_PORT}/remote.php/dav/files/{lima.NC_BOT}/"
           f"Shared%20Files/{name}")
    body = f"--data-binary {shlex.quote(data)}" if data else ""
    return vm.out(
        f"curl -s -m 30 -u {shlex.quote(lima.NC_BOT + ':' + fixture['BOT_PASSWORD'])} "
        f"-X {method} {body} -w '\\n%{{http_code}}' {shlex.quote(url)}")


@pytest.fixture(scope="module")
def stack(nextcloud_stack, vm):
    return vm


@pytest.fixture(scope="module")
def istota_image(stack, release) -> str:
    """The tag the row 9 case starts the daemon service from."""
    if lima.control() != "no-storage-refusal":
        return release.tag
    tag = f"{release.tag}-no-storage-refusal"
    stack.run(
        f"docker build -q -f {lima.STACK}/src/docker/test/Dockerfile.no-storage-refusal "
        f"--build-arg BASE=istota:{release.tag} -t istota:{tag} {lima.STACK}/src/docker/test",
        timeout=900)
    return tag


class TestTheWorkspaceIsTheMount:
    def test_the_stack_requires_and_follows_the_mount_unit(self, stack):
        unit = stack.out("systemctl show istota-stack.service -p Requires -p After")
        assert "mount-nextcloud.service" in unit.split("Requires=", 1)[1].split("\n", 1)[0], unit
        assert "mount-nextcloud.service" in unit.split("After=", 1)[1], unit

    def test_the_mount_runs_with_its_vfs_cache(self, stack):
        """`--vfs-cache-mode full` silently degrades to no cache when the cache
        directory cannot be made; writes needing it then fail."""
        since = stack.out("systemctl show mount-nextcloud.service -p ActiveEnterTimestamp --value")
        journal = stack.out(f"journalctl -u mount-nextcloud.service --since {shlex.quote(since)} --no-pager")
        assert "Failed to create vfs cache" not in journal, journal[-2000:]
        assert stack.ok("test -d /var/cache/istota-rclone/vfs"), "rclone made no VFS cache"

    def test_the_workspace_is_the_rclone_mount_in_the_container(self, stack):
        mountinfo = lima.stack_exec(stack, "grep ' /mnt/shared ' /proc/self/mountinfo").stdout
        assert " - fuse.rclone " in mountinfo, mountinfo
        assert lima.doctor(stack, "runtime.mount_liveness")["status"] == "ok"

    def test_the_bots_shared_files_are_the_workspace(self, stack):
        listing = lima.stack_exec(stack, "ls -A /mnt/shared").stdout.split()
        assert ".istota-provisioned" in listing and "Users" in listing, listing

    def test_a_daemon_write_reaches_nextcloud(self, stack, nextcloud_stack):
        name = f"vmtier-{uuid.uuid4().hex[:10]}.txt"
        lima.stack_exec(stack, f"echo from-the-daemon > /mnt/shared/{name}")
        lima.wait_for(
            lambda: _webdav(stack, nextcloud_stack, "GET", name).split() == ["from-the-daemon", "200"],
            timeout=120, what=f"{name} to reach Nextcloud")

    def test_a_nextcloud_write_shows_in_the_workspace(self, stack, nextcloud_stack):
        name = f"vmtier-{uuid.uuid4().hex[:10]}.txt"
        status = _webdav(stack, nextcloud_stack, "PUT", name, data="from-nextcloud").splitlines()[-1]
        assert status in ("201", "204"), status
        lima.wait_for(
            lambda: lima.stack_exec(stack, f"cat /mnt/shared/{name}", check=False).stdout.strip() == "from-nextcloud",
            timeout=120, what=f"{name} to show in the container")


class TestTheMountsRemoteControl:
    """#939. The mount keeps listings for ten minutes and has an rc on a
    socket, with a password, reached by the daemon alone."""

    def test_the_mount_runs_with_the_long_cache_and_the_rc(self, stack):
        pid = stack.out("systemctl show -p MainPID --value mount-nextcloud.service")
        argv = stack.out(f"tr '\\0' ' ' < /proc/{pid}/cmdline").split()
        assert argv[argv.index("--dir-cache-time") + 1] == "10m", argv
        assert "--poll-interval" not in argv, argv
        assert argv[argv.index("--rc-addr") + 1] == f"unix://{lima.STACK}/rclone-rc/rc.sock", argv

    def test_the_socket_and_the_password_are_kept_from_everyone_else(self, stack):
        assert stack.out(f"stat -c '%a %u' {lima.STACK}/rclone-rc") == "700 10001"
        assert stack.out(f"stat -c '%a %U %g' {lima.STACK}/rclone-rc.env") == "640 root 10001"
        assert stack.ok(f"test -S {lima.STACK}/rclone-rc/rc.sock")

    def test_the_daemon_reaches_it_and_it_wants_the_password(self, stack):
        result = lima.doctor(stack, "nextcloud.rclone_rc")
        assert result["status"] == "ok", result

    def test_the_web_container_has_no_rc(self, stack):
        web = lima.container(stack, "web")
        assert web, "no web container"
        assert not stack.ok(f"docker exec {web} test -e /run/istota-rclone")

    def test_a_forget_reaches_the_mount(self, stack):
        """The daemon's own client, in the daemon container, on the real rc."""
        out = lima.stack_exec(
            stack, "python -c 'from istota.nextcloud import rclone_rc; "
                   "r = rclone_rc.forget_dirs([\"Users\"]); print(r.ok, r.reason)'",
        ).stdout.split()
        assert out == ["True", ""] or out == ["True"], out


@parity.witness(9)
class TestNothingIsWrittenIntoAnUnmountedStore:
    def test_with_the_mount_stopped_the_daemon_does_not_start(self, stack, istota_image):
        stack.run("systemctl stop mount-nextcloud.service", timeout=300)
        try:
            lima.wait_for(lambda: stack.out("systemctl is-active istota-stack.service || true") != "active",
                          timeout=120, what="istota-stack.service to stop with the mount")
            assert not stack.ok(f"mountpoint -q {MOUNT}"), "the mount is still there"
            before = stack.out(f"find {MOUNT} -mindepth 1 | sort")
            # Past systemd, as an operator could: compose itself, the mount down.
            stack.run(f"ISTOTA_TAG={shlex.quote(istota_image)} istota-stack compose up -d --no-deps daemon",
                      timeout=600)
            cid = stack.out(
                "docker ps -aq --filter label=com.docker.compose.project=istota "
                "--filter label=com.docker.compose.service=daemon "
                "--filter label=com.docker.compose.oneoff=False | head -1")
            # Until it has either refused (exited, and restarted by its policy)
            # or come up healthy; a daemon that starts is healthy within this.
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
            after = stack.out(f"find {MOUNT} -mindepth 1 | sort")
            assert "healthy" not in state.split(), f"row 9: the daemon started on an unmounted store: {state}"
            assert "REFUSE: the workspace /mnt/shared" in logs, logs[-2000:]
            assert after == before, f"row 9: written into the unmounted store: {after}"
        finally:
            # The hand-started container goes first: left restarting, `up` reads it as a failed
            # dependency. Then whatever it wrote (the no-storage-refusal control writes), since
            # rclone will not mount over a non-empty directory; only ever while unmounted.
            stack.run("istota-stack compose rm -sf daemon >/dev/null 2>&1 || true\n"
                      f"if ! mountpoint -q {MOUNT}; then find {MOUNT} -mindepth 1 -delete; fi\n"
                      "systemctl start istota-stack.service", timeout=1800)
            lima.wait_healthy(stack)
