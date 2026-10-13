"""`istota-stack apply` with a settings plan and a credential sidecar, from the Mac.

The default suite holds both wrappers against stand-ins: what the VM's
`istota-stack apply` binds and passes, and how it reads the result document
(`tests/test_istota_stack.py::TestApplyResult`, `TestApplySidecar`), and what the
Mac's `istota-stack apply` copies and stages (`tests/test_mac_istota_stack.py`).
Neither is evidence that a sidecar staged on the VM is readable in a real
one-shot container, or that a real result decides the restart. Here the Mac
CLI is run on this Mac against the tier's VM (`ISTOTA_LIMA_INSTANCE`), as an
operator runs it, in the local mode:

- a plan declaring one provisionable credential slot of the setup's user, with
  a fresh value in a 0600 sidecar on the Mac: the apply changes it (exit 2),
  stdout is the result document alone and says no restart is needed, the
  daemon is not restarted, the value appears in no output, and nothing is left
  in the VM's staging directory. A dry run with the same sidecar then exits 0,
  which only a value the container read and stored can give;
- a plan changing the user's timezone, a profile change the result marks as
  needing a restart: exit 2, the daemon restarted once, and the same plan
  again exits 0 and restarts nothing.

The timezone is read from `istota snapshot` through the exec wrapper first and
put back at the end.

Controls (`scripts/test-vm-negative-control.sh`), each a copy of the installed
`istota-stack` with one fragment replaced (`lima.UPDATE_CONTROLS`), installed
in its place for the module and put back after:
`apply-restart-on-any-change` restarts on the content-only change;
`apply-sidecar-not-passed` stages and binds the sidecar but never hands its path
to `istota apply`.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from . import lima

pytestmark = pytest.mark.vm

CONTROLS = ("apply-restart-on-any-change", "apply-sidecar-not-passed")
STAGING = f"{lima.STACK}/staging"
SAVED = f"{lima.STACK_COMMAND}.vmtier-orig"
SERVICE, KEY = "karakeep", "api_key"
TIMEZONES = ("Pacific/Auckland", "Europe/Lisbon")


@dataclass
class Run:
    returncode: int
    stdout: str
    stderr: str

    @property
    def document(self) -> dict:
        return json.loads(self.stdout)

    def describe(self) -> str:
        return f"exit {self.returncode}\n--- stdout ---\n{self.stdout[-4000:]}\n--- stderr ---\n{self.stderr[-4000:]}"


@dataclass
class Applied:
    content: Run
    content_again: Run
    content_value: str
    content_started: tuple[str, str]
    profile: Run
    profile_again: Run
    profile_started: tuple[str, str, str]
    staging_before: str
    staging_after: str


def _apply(vm: lima.Vm, *argv: str) -> Run:
    result = subprocess.run(
        [str(lima.MAC_CLI), "apply", *argv], capture_output=True, text=True, stdin=subprocess.DEVNULL,
        timeout=1800, env={**os.environ, "ISTOTA_LIMA_INSTANCE": vm.name},
    )
    return Run(result.returncode, result.stdout, result.stderr)


def _started(vm: lima.Vm) -> str:
    """The daemon's container and its start time: a restart or a recreate changes it."""
    cid = lima.container(vm, "daemon")
    return f"{cid} {vm.out(f'docker inspect -f {{{{.State.StartedAt}}}} {cid}')}" if cid else ""


def _staging(vm: lima.Vm) -> str:
    return vm.out(f"find {STAGING} -mindepth 1 2>/dev/null | sort || true")


def _timezone(vm: lima.Vm) -> str:
    result = lima.stack_exec(vm, f"istota snapshot --user {shlex.quote(lima.USER)} --format json")
    return json.loads(result.stdout)["users"][lima.USER]["profile"].get("timezone", "")


def _private(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path


def _install_control(vm: lima.Vm, name: str) -> None:
    copy = lima.stack_command(vm, name)
    vm.run(f"cp -p {lima.STACK_COMMAND} {SAVED} && install -m 0755 {copy} {lima.STACK_COMMAND}")


def _restore_command(vm: lima.Vm) -> None:
    vm.run(f"if [ -f {SAVED} ]; then mv -f {SAVED} {lima.STACK_COMMAND}; fi; rm -f {lima.SCRATCH}/istota-stack.*")


@pytest.fixture(scope="module")
def applied(local_stack, tmp_path_factory):
    vm = local_stack
    # A session that died under a control left the copy installed.
    _restore_command(vm)
    lima.wait_healthy(vm)
    control = lima.control() if lima.control() in CONTROLS else ""
    work = tmp_path_factory.mktemp("settings-apply")
    original = _timezone(vm)
    target = TIMEZONES[0] if original != TIMEZONES[0] else TIMEZONES[1]
    if control:
        _install_control(vm, control)
    try:
        staging_before = _staging(vm)
        value = f"vmtier-{uuid.uuid4().hex}"
        plan = _private(work / "secrets-plan.json", {
            "users": {lima.USER: {"secrets": [{"service": SERVICE, "keys": [KEY]}]}}})
        # The same name as the plan: each keeps its own directory in the VM.
        (work / "creds").mkdir()
        sidecar = _private(work / "creds" / "secrets-plan.json", {
            "version": 1, "users": {lima.USER: {SERVICE: {KEY: value}}}})
        before = _started(vm)
        content = _apply(vm, str(plan), "--secrets-file", str(sidecar))
        lima.wait_healthy(vm)
        after = _started(vm)
        content_again = _apply(vm, str(plan), "--secrets-file", str(sidecar), "--dry-run")

        profile_plan = _private(work / "profile.json", {"users": {lima.USER: {"profile": {"timezone": target}}}})
        profile_before = _started(vm)
        profile = _apply(vm, str(profile_plan))
        lima.wait_healthy(vm)
        profile_after = _started(vm)
        profile_again = _apply(vm, str(profile_plan))
        lima.wait_healthy(vm)
        profile_settled = _started(vm)
        yield Applied(
            content, content_again, value, (before, after),
            profile, profile_again, (profile_before, profile_after, profile_settled),
            staging_before, _staging(vm),
        )
    finally:
        if control:
            _restore_command(vm)
        if original and _timezone(vm) != original:
            restore = _private(work / "restore.json", {"users": {lima.USER: {"profile": {"timezone": original}}}})
            _apply(vm, str(restore))
        lima.wait_healthy(vm)


class TestASidecarReachesTheContainer:
    def test_the_credential_changed_and_the_result_says_no_restart(self, applied):
        run = applied.content
        assert run.returncode == 2, run.describe()
        document = run.document
        assert document["changed"] is True and document["partial"] is False, document
        assert document["restart_required"] is False, document
        assert [(op["section"], op["identity"], op["status"]) for op in document["operations"]] == [
            ("secrets", f"{SERVICE}.{KEY}", "applied")], document

    def test_the_stored_value_is_the_sidecars(self, applied):
        """A dry run with the same sidecar is unchanged only if the container
        read the sidecar and stored that value."""
        run = applied.content_again
        assert run.returncode == 0, run.describe()
        assert run.document["changed"] is False

    def test_a_content_only_change_restarts_nothing(self, applied):
        before, after = applied.content_started
        assert before and after == before, f"the daemon was restarted: {before} -> {after}\n" + \
            applied.content.describe()
        assert "restarting the stack" not in applied.content.stderr

    def test_no_output_carries_the_value(self, applied):
        for run in (applied.content, applied.content_again):
            assert applied.content_value not in run.stdout + run.stderr

    def test_the_vms_staging_is_left_as_it_was(self, applied):
        assert applied.staging_after == applied.staging_before


class TestAProfileChangeRestartsOnce:
    def test_the_change_is_applied_and_needs_a_restart(self, applied):
        run = applied.profile
        assert run.returncode == 2, run.describe()
        assert run.document["restart_required"] is True, run.document

    def test_the_daemon_restarted_once(self, applied):
        before, after, _ = applied.profile_started
        assert after != before, "the daemon was not restarted\n" + applied.profile.describe()
        assert applied.profile.stderr.count("restarting the stack") == 1, applied.profile.describe()

    def test_the_same_plan_again_changes_and_restarts_nothing(self, applied):
        run = applied.profile_again
        assert run.returncode == 0, run.describe()
        _, after, settled = applied.profile_started
        assert settled == after
        assert "restarting the stack" not in run.stderr
