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
  image's schema check, and the stack is healthy on it;
- the rollback ran the release checkout's `provision.sh` too, so the host's own
  `istota-stack` is the release's again and `.provisioned` matches (ISSUE-710).

Controls (`scripts/test-vm-negative-control.sh`), each run against a copy of
the installed `istota-stack` with one fragment replaced (`lima.UPDATE_CONTROLS`):
`update-without-up` writes `.env` and starts nothing; `update-without-reprovision`
moves the stack and leaves the host's scripts at the old ref;
`rollback-without-reprovision` returns the stack and leaves them at the new one.

The VM is put back on the release at the end, whatever the case did. Both verbs
reprovision, so a host script is run by hand only when a case failed partway and
left `.provisioned` behind the checkout.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

import pytest

from . import lima

pytestmark = pytest.mark.vm

ENV = f"{lima.STACK}/.env"
CONTROLS = ("update-without-up", "update-without-reprovision")
ROLLBACK_CONTROLS = ("rollback-without-reprovision",)


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
    if not _provisioned(vm):
        vm.run(f"{lima.STACK}/src/host/provision.sh", timeout=1800)
    lima.wait_healthy(vm)


def _provisioned(vm: lima.Vm) -> bool:
    """Whether the host's scripts and units are the checkout's, by provision.sh's own record."""
    return vm.out(f"{lima.STACK}/src/host/provision.sh --digest") == vm.read(f"{lima.STACK}/.provisioned").strip()


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

    def test_the_stack_logs_to_the_journal_under_its_container_names(self, local_stack, update):
        """#712: on the VM container output reaches the journal, with the
        container's name as CONTAINER_NAME and as its identifier."""
        vm = local_stack
        for service in ("daemon", "web", "nginx"):
            cid = lima.container(vm, service)
            assert vm.out(f"docker inspect -f '{{{{.HostConfig.LogConfig.Type}}}}' {cid}") == "journald", service
            name = f"istota-{service}-1"
            # -a: an AppArmor host adds _SELINUX_CONTEXT as a binary field, and
            # grep would otherwise print "binary file matches" instead of the line.
            line = vm.out(f"journalctl CONTAINER_NAME={name} -n1 -o export | grep -a '^SYSLOG_IDENTIFIER=' || true")
            assert line == f"SYSLOG_IDENTIFIER={name}", service

    def test_the_previous_tags_image_is_kept(self, local_stack, update):
        assert local_stack.ok(f"docker image inspect {lima.IMAGE}:{update.release} >/dev/null")

    def test_the_host_runs_the_new_refs_istota_stack(self, local_stack, update):
        vm = local_stack
        installed = vm.read(lima.STACK_COMMAND)
        assert f"{lima.NEXT_REF_MARKER} {update.ref}" in installed, \
            "provision.sh did not run from the new checkout\n" + _describe(update)
        assert _provisioned(vm)


@pytest.fixture(scope="module")
def rollback(local_stack, update):
    """The rollback, once the update's own cases have read the VM on the new ref."""
    vm = local_stack
    control = lima.control() if lima.control() in ROLLBACK_CONTROLS else ""
    command = lima.stack_command(vm, control)
    result = vm.run(f"{command} rollback", check=False, timeout=1800)
    return result.returncode, result.stdout + result.stderr


class TestARollbackToTheRelease:
    def test_a_rollback_returns_to_the_release(self, local_stack, update, rollback):
        vm = local_stack
        returncode, output = rollback
        assert returncode == 0, output
        assert lima.env_value(vm, ENV, "ISTOTA_TAG") == update.release
        assert lima.env_value(vm, ENV, "ISTOTA_PREVIOUS_TAG") == update.ref
        lima.wait_healthy(vm)
        for service in ("daemon", "web"):
            assert lima.running_image(vm, service) == f"{lima.IMAGE}:{update.release}", service

    def test_a_rollback_returns_the_hosts_istota_stack(self, local_stack, update, rollback):
        """ISSUE-710: the rollback runs the release checkout's provision.sh, so
        the host's scripts and units are not left at the newer ref."""
        vm = local_stack
        returncode, output = rollback
        assert returncode == 0, output
        assert lima.NEXT_REF_MARKER not in vm.read(lima.STACK_COMMAND), \
            "the host still runs the newer ref's istota-stack after the rollback\n" + output[-6000:]
        assert _provisioned(vm)
