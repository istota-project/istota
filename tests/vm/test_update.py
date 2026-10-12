"""`istota-stack update` and `rollback` on a real VM.

`tests/test_istota_stack.py` holds the update against a fixture repository and
a stand-in `docker`: the refusals, the order of the steps, what a failed build
leaves. What it cannot see is the stack itself moving. Here the VM, already on
the session's signed release, is updated to a second ref (`lima.next_ref`: the
same tree with one line added to `host/istota-stack`, tagged lightweight, so
only `--unsigned` installs it), and the case reads the result off the VM:

- `.env` names the new ref, marked unsigned, with the release as the previous;
- the daemon and web containers were recreated from the new tag's image and
  the daemon comes back healthy;
- the previous tag's image is kept, which is what a rollback starts;
- the host's own `istota-stack` is the new ref's, and `.provisioned` matches
  the inputs: the update ran the new checkout's `provision.sh` by `exec`, over
  the very script bash was reading the update from;
- `istota-stack rollback` returns to the signed release, through the older
  image's schema check, and the stack is healthy on it.

Controls (`scripts/test-vm-negative-control.sh`), each run against a copy of
the installed `istota-stack` with one fragment replaced (`lima.UPDATE_CONTROLS`):
`update-without-up` writes `.env` and starts nothing; `update-without-reprovision`
moves the stack and leaves the host's scripts at the old ref.

The VM is put back on the release at the end, whatever the case did, and the
release's host scripts reinstalled: `rollback` does not run `provision.sh`.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

import pytest

from . import lima

pytestmark = pytest.mark.vm

ENV = f"{lima.STACK}/.env"
CONTROLS = ("update-without-up", "update-without-reprovision")


@dataclass
class Update:
    release: str
    ref: str
    returncode: int
    output: str
    daemon_before: str


def _restore(vm: lima.Vm, release: str) -> None:
    if lima.env_value(vm, ENV, "ISTOTA_TAG") != release:
        if not vm.ok(f"{lima.STACK_COMMAND} rollback", timeout=1800) or \
                lima.env_value(vm, ENV, "ISTOTA_TAG") != release:
            vm.run(f"{lima.STACK_COMMAND} update {shlex.quote(release)}", timeout=lima.BUILD_TIMEOUT)
    vm.run(f"{lima.STACK}/src/host/provision.sh", timeout=1800)
    lima.wait_healthy(vm)


@pytest.fixture(scope="module")
def update(local_stack, release):
    vm = local_stack
    control = lima.control() if lima.control() in CONTROLS else ""
    assert lima.env_value(vm, ENV, "ISTOTA_TAG") == release.tag
    ref = lima.next_ref(release)
    command = lima.stack_command(vm, control)
    daemon_before = lima.container(vm, "daemon")
    try:
        result = vm.run(f"{command} update --unsigned {shlex.quote(ref)}", check=False, timeout=lima.BUILD_TIMEOUT)
        yield Update(release.tag, ref, result.returncode, result.stdout + result.stderr, daemon_before)
    finally:
        _restore(vm, release.tag)
        vm.run(f"rm -f {lima.SCRATCH}/istota-stack.*")


def _describe(update: Update) -> str:
    return f"exit {update.returncode}\n{update.output[-6000:]}"


class TestAnUpdateToAnotherRef:
    def test_the_update_finished(self, update):
        assert update.returncode == 0, _describe(update)

    def test_env_names_the_new_ref_and_the_release_as_previous(self, local_stack, update):
        vm = local_stack
        assert lima.env_value(vm, ENV, "ISTOTA_TAG") == update.ref
        assert lima.env_value(vm, ENV, "ISTOTA_TAG_UNSIGNED") == "1"
        assert lima.env_value(vm, ENV, "ISTOTA_PREVIOUS_TAG") == update.release
        assert lima.env_value(vm, ENV, "ISTOTA_PREVIOUS_TAG_UNSIGNED") == ""

    def test_the_stack_runs_the_new_tags_image_and_is_healthy(self, local_stack, update):
        vm = local_stack
        lima.wait_healthy(vm)
        assert lima.container(vm, "daemon") != update.daemon_before, "the daemon container was not recreated"
        for service in ("daemon", "web"):
            assert lima.running_image(vm, service) == f"{lima.IMAGE}:{update.ref}", service

    def test_the_previous_tags_image_is_kept(self, local_stack, update):
        assert local_stack.ok(f"docker image inspect {lima.IMAGE}:{update.release} >/dev/null")

    def test_the_host_runs_the_new_refs_istota_stack(self, local_stack, update):
        vm = local_stack
        installed = vm.read(lima.STACK_COMMAND)
        assert f"{lima.NEXT_REF_MARKER} {update.ref}" in installed, \
            "provision.sh did not run from the new checkout\n" + _describe(update)
        assert vm.out(f"{lima.STACK}/src/host/provision.sh --digest") == vm.read(f"{lima.STACK}/.provisioned").strip()

    def test_a_rollback_returns_to_the_release(self, local_stack, update):
        vm = local_stack
        result = vm.run(f"{lima.STACK_COMMAND} rollback", check=False, timeout=1800)
        assert result.returncode == 0, result.stdout + result.stderr
        assert lima.env_value(vm, ENV, "ISTOTA_TAG") == update.release
        assert lima.env_value(vm, ENV, "ISTOTA_PREVIOUS_TAG") == update.ref
        lima.wait_healthy(vm)
        for service in ("daemon", "web"):
            assert lima.running_image(vm, service) == f"{lima.IMAGE}:{update.release}", service
