"""The local VM's workspace and the Mac's loopback: rows L1 and L2.

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

Row L2: no container reaches a service on the Mac's loopback unless its port
is allowed. Lima forwards its host gateway to the Mac's 127.0.0.1, and the
egress unit's HOST_LOOPBACK_BLOCK rules drop container traffic to it but for
DNS (Lima serves it at the gateway) and HOST_LOOPBACK_ALLOW, which here holds
the scripted model's port. A listener on the Mac's loopback the allow list does
not name is unreachable from the istota and the browser containers, and
reachable from the VM itself, whose own traffic is not filtered.

Controls (`scripts/test-vm-negative-control.sh`):

- `no-workspace-marker`: the daemon service is started from an image whose
  entrypoint has the marker refusal removed
  (`docker/test/Dockerfile.no-workspace-marker`);
- `host-loopback-unblocked`: HOST_LOOPBACK_BLOCK=0 in host.env and the egress
  unit re-run, as on a VM set up without the block.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import threading
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
        lima.ensure_mode(vm, release, lima.host_folder_mode(
            model.vm_url, gateway=lima.mac_address(vm), allow_ports=[model.port]))
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


CONNECT_PROBE = r"""
import json, socket, sys
out = {}
for target in json.loads(sys.argv[1]):
    host, port = target.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=5).close()
        out[target] = "reached"
    except OSError as exc:
        out[target] = type(exc).__name__
print("CONNECT=" + json.dumps(out))
"""

RESOLVE_PROBE = "import socket; print('RESOLVED=' + socket.gethostbyname('debian.org'))"


def _connect(vm: lima.Vm, targets: list[str], *, cid: str = "", user: str = "") -> dict:
    """Connect outcomes from a container, or from the VM itself without `cid`."""
    argv = f"python3 -c {shlex.quote(CONNECT_PROBE)} {shlex.quote(json.dumps(targets))}"
    if cid:
        argv = f"docker exec {f'-u {user} ' if user else ''}{cid} {argv}"
    line = next(row for row in vm.out(argv).splitlines() if row.startswith("CONNECT="))
    return json.loads(line[len("CONNECT="):])


@pytest.fixture(scope="module")
def mac_listener():
    """A listener on the Mac's loopback whose port no allow list names."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    server.settimeout(0.5)
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        stop.set()
        thread.join(timeout=2)
        server.close()


@pytest.fixture(scope="module")
def loopback(stack):
    """The host-folder stack, with the block switched off under its control."""
    if lima.control() != "host-loopback-unblocked":
        yield stack
        return
    lima.env_file_set(stack, f"{lima.STACK}/host.env", {"HOST_LOOPBACK_BLOCK": "0"})
    stack.run("systemctl restart istota-devbox-egress.service")
    try:
        yield stack
    finally:
        lima.env_file_set(stack, f"{lima.STACK}/host.env", {"HOST_LOOPBACK_BLOCK": "1"})
        stack.run("systemctl restart istota-devbox-egress.service")


def _reaches_the_allowed_port_only(vm: lima.Vm, service: str, user: str, model, mac_listener: int) -> None:
    gateway = lima.mac_address(vm)
    unlisted = f"{gateway}:{mac_listener}"
    allowed = f"{gateway}:{model.port}"
    from_vm = _connect(vm, [unlisted])
    assert from_vm == {unlisted: "reached"}, f"the in-session control: the VM reaches the Mac listener: {from_vm}"
    cid = lima.container(vm, service)
    assert cid, f"{service} is not running"
    outcome = _connect(vm, [unlisted, allowed], cid=cid, user=user)
    assert outcome[allowed] == "reached", f"{service}: the allowed model port: {outcome}"
    assert outcome[unlisted] != "reached", f"row L2: the {service} container reached an unlisted Mac port: {outcome}"


@parity.witness("L2")
class TestTheMacLoopbackIsClosedToContainers:
    def test_the_daemon_reaches_the_allowed_port_only(self, loopback, model, mac_listener):
        _reaches_the_allowed_port_only(loopback, "daemon", "10001", model, mac_listener)

    def test_the_browser_reaches_the_allowed_port_only(self, loopback, model, mac_listener):
        _reaches_the_allowed_port_only(loopback, "browser", "", model, mac_listener)


class TestTheBlockLeavesDnsAlone:
    """Lima's DNS is at the gateway the block closes (Stage 1 of the local-VM
    spec), so port 53 there stays open."""

    def test_the_unit_wrote_the_block_with_its_dns_exception(self, loopback):
        if lima.control() == "host-loopback-unblocked":
            pytest.skip("the control removes the block")
        rules = loopback.out("iptables -w 5 -S DOCKER-USER | grep -F 'istota-host-loopback:' || true")
        assert "-j DROP" in rules, rules
        assert "-p udp" in rules and "--dport 53" in rules, rules

    def test_a_compose_service_resolves_a_name(self, loopback):
        out = lima.stack_exec(loopback, f'python3 -c "{RESOLVE_PROBE}"').stdout
        assert "RESOLVED=" in out, out

    def test_a_default_bridge_container_resolves_a_name(self, loopback, release):
        out = loopback.out(
            f"docker run --rm --network bridge --entrypoint python3 istota:{shlex.quote(release.tag)} "
            f"-c {shlex.quote(RESOLVE_PROBE)}", timeout=300)
        assert "RESOLVED=" in out, out
