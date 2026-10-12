"""The local VM's workspace, the Mac's loopback and the Mac's commands: rows L1 to L4.

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

Row L3: only the workspace folder, and in developer mode the checkout
read-only, crosses from the Mac. The VM mounts exactly the template's two
(this mode shares the folder in with the mounts Lima resolves from
`host/lima/istota.yaml`); nothing in the VM, the daemon or a task reads a Mac
file under the Mac home outside the folder, by its Mac path or through a link
the Mac made in the folder; and the checkout refuses a write.

Row L4: the Mac's `istota-stack` and `istota` run as uid 10001 with no
capabilities. They are run here, on this Mac, against the tier's VM
(`ISTOTA_LIMA_INSTANCE`), as an operator runs them: the Mac CLI's `exec`
reports uid 10001 and every capability set empty, and the `istota` shim
beside it reaches the CLI through that same `exec` (the default suite holds
the argv it forwards, `tests/test_mac_istota_stack.py`).

Controls (`scripts/test-vm-negative-control.sh`):

- `no-workspace-marker`: the daemon service is started from an image whose
  entrypoint has the marker refusal removed
  (`docker/test/Dockerfile.no-workspace-marker`);
- `host-loopback-unblocked`: HOST_LOOPBACK_BLOCK=0 in host.env and the egress
  unit re-run, as on a VM set up without the block;
- `default-mounts-in-template`: the VM gets the mounts Lima resolves from the
  template with `template:_default/mounts` added to its `base:`, which is what
  `template:debian-13` would bring in: the Mac home, read-only;
- `mac-shim-without-drop`: a copy of the Mac CLI whose `exec` goes straight to
  `compose exec daemon`, past the VM's `istota-stack exec` and `istota-drop`.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
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


L3_MARKER = "istota-vmtier-l3"


@pytest.fixture(scope="module")
def crossing(stack, release, folder):
    """The host-folder stack with the Mac-side files row L3 reads: a file in
    the folder, a file outside it, and links to that file from the folder
    root and from the task's own subtree, absolute and relative."""
    outside = lima._workdir() / "l3-outside"
    outside.mkdir(parents=True, exist_ok=True)
    secret = outside / "secret.txt"
    secret.write_text(f"{L3_MARKER}-outside\n")
    task_dir = folder / "Users" / lima.USER
    task_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for directory in (folder, task_dir):
        inside = directory / "l3-inside.txt"
        inside.write_text(f"{L3_MARKER}-inside\n")
        made.append(inside)
        for name, target in (("l3-link-abs", str(secret)),
                             ("l3-link-rel", os.path.relpath(secret, directory))):
            link = directory / name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(target)
            made.append(link)
    applied = lima.control() == "default-mounts-in-template"
    if applied:
        stack.set_mounts(lima.template_mounts(release, folder, extra_base="template:_default/mounts"))
        lima.wait_healthy(stack)
    try:
        yield {"outside": str(secret), "task_dir": f"/mnt/shared/Users/{lima.USER}"}
    finally:
        for path in made:
            path.unlink(missing_ok=True)
        if applied:
            stack.set_mounts(lima.template_mounts(release, folder))
            lima.wait_healthy(stack)


def _l3_probe(paths: dict[str, str]) -> str:
    """`name=<state>` per path: `read:<text>`, `unreadable` (there but not
    readable, a dangling link included) or `absent`."""
    lines = ["echo PROBE_BEGIN"]
    for name, path in paths.items():
        quoted = shlex.quote(path)
        lines.append(
            f'if c="$(cat {quoted} 2>/dev/null)"; then echo "{name}=read:$c"; '
            f"elif test -e {quoted} || test -L {quoted}; then echo {name}=unreadable; "
            f"else echo {name}=absent; fi")
    lines.append("echo PROBE_END")
    return "\n".join(lines)


def _l3_paths(root: str, crossing: dict) -> dict[str, str]:
    # Not the home directory's own path: Lima leaves the directories it made
    # for an earlier session's mount points on the guest's disk, empty, so the
    # witness is a Mac file under the home, by its Mac path and through links.
    return {
        "inside": f"{root}/l3-inside.txt",
        "link_abs": f"{root}/l3-link-abs",
        "link_rel": f"{root}/l3-link-rel",
        "outside": crossing["outside"],
    }


def _assert_nothing_crossed(seen: dict[str, str], where: str) -> None:
    # The in-session control: the same probe reads a file in the folder.
    assert seen.get("inside") == f"read:{L3_MARKER}-inside", f"{where}: {seen}"
    crossed = {name: state for name, state in seen.items()
               if name != "inside" and state.startswith("read:")}
    assert crossed == {}, f"row L3: {where} reads a Mac file outside the folder: {crossed}"
    assert set(seen) == {"inside", "link_abs", "link_rel", "outside"}, seen


@parity.witness("L3")
class TestOnlyTheFolderCrossesFromTheMac:
    def test_the_vm_mounts_the_read_only_checkout_and_the_folder_alone(self, crossing, stack):
        mounts = {fields[0]: fields[1] for fields in (line.split() for line in stack.out(
            "findmnt -rn -t virtiofs,9p,fuse.sshfs -o TARGET,OPTIONS || true").splitlines())}
        # Apple's Rosetta runtime, shared for a forced amd64 image (row 7).
        mounts.pop("/mnt/lima-rosetta", None)
        assert sorted(mounts) == sorted([lima.REPO_MOUNT, lima.HOST_FOLDER_MOUNT]), (
            f"row L3: Mac paths mounted in the VM: {mounts}")
        assert "ro" in mounts[lima.REPO_MOUNT].split(","), mounts
        assert "rw" in mounts[lima.HOST_FOLDER_MOUNT].split(","), mounts

    def test_the_vm_reads_nothing_of_the_mac_outside_the_folder(self, crossing, stack):
        seen = lima.marked_block(stack.out(_l3_probe(_l3_paths(lima.HOST_FOLDER_MOUNT, crossing))))
        _assert_nothing_crossed(seen, "the VM")
        # The link is there with the Mac's target text; the guest resolves it
        # in its own namespace, where that path is not the Mac's.
        target = stack.out(f"readlink {lima.HOST_FOLDER_MOUNT}/l3-link-abs")
        assert target == crossing["outside"], target

    def test_the_daemon_reads_nothing_of_the_mac_outside_the_folder(self, crossing, stack):
        out = lima.stack_exec(stack, _l3_probe(_l3_paths("/mnt/shared", crossing))).stdout
        _assert_nothing_crossed(lima.marked_block(out), "the daemon")

    def test_a_task_reads_nothing_of_the_mac_outside_the_folder(self, crossing, stack, model):
        out = lima.run_probe_task(stack, model, _l3_probe(_l3_paths(crossing["task_dir"], crossing)))
        _assert_nothing_crossed(lima.marked_block(out), "a task")

    def test_the_checkout_refuses_a_write(self, crossing, stack):
        result = stack.run(f"touch {lima.REPO_MOUNT}/.istota-vmtier-l3-write", check=False)
        assert result.returncode != 0, "row L3: root wrote into the checkout mount"
        assert "Read-only file system" in result.stderr, result.stderr


STATUS = "grep -E '^(Uid|Gid|CapInh|CapPrm|CapEff|CapBnd|CapAmb):' /proc/self/status"
EMPTY_CAPS = "0000000000000000"


def _on_the_mac(script: Path, vm: lima.Vm, *argv: str) -> subprocess.CompletedProcess:
    """A Mac command against the tier's VM, run as the operator runs it."""
    return subprocess.run(
        [str(script), *argv], capture_output=True, text=True, stdin=subprocess.DEVNULL,
        timeout=300, env={**os.environ, "ISTOTA_LIMA_INSTANCE": vm.name},
    )


def _status_fields(text: str) -> dict[str, str]:
    fields = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if value.split():
            fields[key.strip()] = value.split()[0]
    return fields


@pytest.fixture(scope="module")
def mac_commands(stack) -> tuple[Path, Path]:
    """The Mac CLI and the shim beside it; under row L4's control, a copy of
    the CLI with the drop skipped, and the shim beside that copy."""
    applied = lima.control() == "mac-shim-without-drop"
    cli = lima.mac_cli("mac-shim-without-drop" if applied else "")
    return cli, cli.parent / "istota"


@parity.witness("L4")
class TestTheMacCliExecsAsTheDaemon:
    def test_the_mac_cli_execs_as_10001_with_no_capabilities(self, stack, mac_commands):
        cli, _ = mac_commands
        result = _on_the_mac(cli, stack, "exec", "sh", "-c", STATUS)
        assert result.returncode == 0, result.stdout + result.stderr
        fields = _status_fields(result.stdout)
        assert fields.get("Uid") == "10001" and fields.get("Gid") == "10001", (
            f"row L4: the Mac CLI's exec ran as {fields}")
        for cap in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            assert fields.get(cap) == EMPTY_CAPS, f"row L4: {cap} is {fields.get(cap)} through the Mac CLI"

    def test_the_istota_shim_reaches_the_cli(self, stack, mac_commands):
        """Without this a shim that ran nothing would leave the case above as
        the only evidence; the shim's route is that same `exec`."""
        _, shim = mac_commands
        result = _on_the_mac(shim, stack, "user", "list")
        assert result.returncode == 0, result.stdout + result.stderr
        assert lima.USER in result.stdout, result.stdout
