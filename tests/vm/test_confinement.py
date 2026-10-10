"""Parity rows 7, 20 and the owner half of 12, on the local-mode stack.

Row 7: the daemon and its tasks see the container, never the VM: not root's or
a user's home, not the stack's .env or host.env, not the checkout the images
were built from, and on Lima no Mac path but the read-only checkout mount, which
the containers do not see either.

Row 20: the istota container runs under the shipped AppArmor profile, so a
root `docker exec` (which does not inherit the daemon's drop, and holds the
root phase's capabilities in a container with a writable /proc/sys) still
cannot write a VM sysctl or the sysrq trigger, while bwrap still starts.

Row 12, the owner half: read from inside the container, every secret file is
0400 and owned by uid 10001. Docker Desktop shows every bind as uid 0, so the
image tier could witness only the mode.

Controls (`scripts/test-vm-negative-control.sh`):

- `mac-home-mounted`: a Mac directory standing in for the home is added to the
  VM's Lima mounts (the Lima default template mounts the whole home);
- `apparmor-unconfined`: the deployed compose file's `apparmor=istota` becomes
  `apparmor=unconfined` and the istota service is recreated;
- `secret-owned-by-root`: one secret file on the VM is chowned to root.
"""

from __future__ import annotations

import json
import shlex

import pytest

from tests.support import parity

from . import lima

pytestmark = pytest.mark.vm

MARKER = "istota-vmtier-row7-marker"


def _lima_user_home(vm: lima.Vm) -> str:
    return vm.out("getent passwd | awk -F: '$3 >= 1000 && $3 < 10000 && $6 ~ /^\\/home\\// { print $6; exit }'")


@pytest.fixture(scope="module")
def stack(local_stack, release):
    vm = local_stack
    home = _lima_user_home(vm)
    vm.run(f"echo {MARKER} > /root/{MARKER}; echo {MARKER} > {home}/{MARKER}")
    name = lima.control()
    if name == "mac-home-mounted":
        stand_in = lima._workdir() / "mac-home"
        stand_in.mkdir(parents=True, exist_ok=True)
        (stand_in / MARKER).write_text(MARKER)
        original = [{"location": str(release.repo), "mountPoint": lima.REPO_MOUNT, "writable": False}]
        vm.set_mounts(original + [{"location": str(stand_in), "writable": False}])
        lima.wait_healthy(vm)
        try:
            yield vm
        finally:
            vm.set_mounts(original)
            lima.wait_healthy(vm)
        return
    if name == "apparmor-unconfined":
        vm.run(f"sed -i 's/apparmor=istota/apparmor=unconfined/' {lima.STACK}/src/docker/docker-compose.yml"
               " && istota-stack compose up -d istota", timeout=600)
        lima.wait_healthy(vm)
        try:
            yield vm
        finally:
            vm.run(f"git -C {lima.STACK}/src checkout -- docker/docker-compose.yml"
                   " && istota-stack compose up -d istota", timeout=600)
            lima.wait_healthy(vm)
        return
    if name == "secret-owned-by-root":
        vm.run(f"chown 0:0 {lima.STACK}/secrets/anthropic_api_key")
        try:
            yield vm
        finally:
            vm.run(f"chown 10001:10001 {lima.STACK}/secrets/anthropic_api_key")
        return
    yield vm


def _vm_paths(vm: lima.Vm) -> list[str]:
    home = _lima_user_home(vm)
    return [
        f"/root/{MARKER}",
        f"{home}/{MARKER}",
        f"{lima.STACK}/.env",
        f"{lima.STACK}/host.env",
        f"{lima.STACK}/allowed_signers",
        f"{lima.STACK}/src/host/provision.sh",
        f"{lima.REPO_MOUNT}/host/provision.sh",
    ]


def _visibility_probe(paths: list[str]) -> str:
    lines = ["echo PROBE_BEGIN"]
    for path in paths:
        quoted = shlex.quote(path)
        lines.append(f"if test -e {quoted}; then echo {quoted}=visible; else echo {quoted}=absent; fi")
    lines.append("echo PROBE_END")
    return "\n".join(lines)


@parity.witness(7)
class TestTheDaemonCannotSeeTheMachine:
    def test_the_vm_mounts_no_mac_path_but_the_read_only_checkout(self, stack):
        mounts = {fields[0]: fields[1] for fields in (line.split() for line in stack.out(
            "findmnt -rn -t virtiofs,9p,fuse.sshfs -o TARGET,OPTIONS || true").splitlines())}
        # Apple's Rosetta runtime, which Lima shares for the amd64 browser image;
        # it holds the translator, not anything of the Mac user's.
        mounts.pop("/mnt/lima-rosetta", None)
        assert sorted(mounts) == [lima.REPO_MOUNT], f"row 7: Mac paths mounted in the VM: {mounts}"
        assert "ro" in mounts[lima.REPO_MOUNT].split(","), mounts

    def test_the_istota_container_binds_only_the_stacks_own_paths(self, stack):
        cid = lima.container(stack, "istota")
        mounts = json.loads(stack.out(f"docker inspect -f '{{{{json .Mounts}}}}' {cid}"))
        binds = sorted(m["Source"] for m in mounts if m["Type"] == "bind")
        allowed = (f"{lima.STACK}/config", f"{lima.STACK}/mount", f"{lima.STACK}/secrets/")
        strays = [b for b in binds if not (b in allowed[:2] or b.startswith(allowed[2]))]
        assert strays == [], f"row 7: the istota container binds {strays}"
        assert f"{lima.STACK}/config" in binds, binds

    def test_the_daemon_sees_none_of_the_vm(self, stack):
        paths = _vm_paths(stack)
        on_vm = lima.marked_block(stack.out(_visibility_probe(paths)))
        assert set(on_vm.values()) == {"visible"}, f"the in-session control: {on_vm}"
        in_container = lima.marked_block(lima.stack_exec(stack, _visibility_probe(paths)).stdout)
        seen = sorted(path for path, state in in_container.items() if state != "absent")
        assert seen == [], f"row 7: the daemon's container sees {seen}"

    def test_a_task_sees_none_of_the_vm(self, stack, model):
        paths = _vm_paths(stack)
        in_task = lima.marked_block(lima.run_probe_task(stack, model, _visibility_probe(paths)))
        assert set(in_task) == set(paths), in_task
        seen = sorted(path for path, state in in_task.items() if state != "absent")
        assert seen == [], f"row 7: a task sees {seen}"


@parity.witness(12)
class TestTheSecretFilesAreTheDaemons:
    def test_every_secret_file_is_0400_and_owned_by_10001_inside_the_container(self, stack):
        listing = lima.stack_exec(stack, "stat -c '%u:%g %a %n' /run/secrets/*").stdout.splitlines()
        assert len(listing) >= 10, listing
        wrong = [line for line in listing if not line.startswith("10001:10001 400 ")]
        assert wrong == [], f"row 12: secret files not 0400 and uid 10001 inside the container: {wrong}"


_WRITE_SYSCTL = (
    'v="$(cat /proc/sys/kernel/core_pattern)"; '
    'printf "%s\\n" "$v" > /proc/sys/kernel/core_pattern'
)


@parity.witness(20)
class TestTheContainerIsConfinedByAppArmor:
    def test_the_istota_profile_is_in_force(self, stack):
        cid = lima.container(stack, "istota")
        assert stack.out(f"docker inspect -f '{{{{.AppArmorProfile}}}}' {cid}") == "istota"
        current = stack.out(f"docker exec -u 0 {cid} cat /proc/1/attr/current")
        assert current == "istota (enforce)", current

    def test_a_root_exec_cannot_write_core_pattern(self, stack):
        cid = lima.container(stack, "istota")
        result = stack.run(f"docker exec -u 0 {cid} sh -c {shlex.quote(_WRITE_SYSCTL)}", check=False)
        assert result.returncode != 0, "row 20: a root exec wrote kernel/core_pattern"
        assert "Permission denied" in result.stderr, result.stderr

    def test_a_root_exec_cannot_write_the_sysrq_trigger(self, stack):
        cid = lima.container(stack, "istota")
        # `h` only prints the sysrq help to the kernel log, should the write land.
        result = stack.run(f"docker exec -u 0 {cid} sh -c 'echo h > /proc/sysrq-trigger'", check=False)
        assert result.returncode != 0, "row 20: a root exec wrote /proc/sysrq-trigger"
        assert "Permission denied" in result.stderr, result.stderr

    def test_bwrap_still_starts(self, stack):
        for check in ("runtime.bwrap", "security.sandbox_effective"):
            result = lima.doctor(stack, check)
            assert result["status"] == "ok", result
