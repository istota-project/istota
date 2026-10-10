"""The management role (`ansible/`) against a real VM.

The proxied and nextcloud modes are applied by the role rather than by
`istota-stack setup`: `lima.role_vars` takes the answers those modes give the
wizard and renders the role's variables through the wizard's own pure
functions, so the witnesses in `test_proxied.py` and `test_nextcloud.py` run
against a stack the role converged. This file holds what is the role's own:

- the converge that applied the mode finished without a failure;
- `ansible-playbook --check` straight after it reports nothing to change,
  which is the drift answer an estate relies on;
- the same --check with one config value changed does report a change and
  writes nothing, so the no-change answer is not one the check gives always;
- a second real converge changes nothing (proxied only, for the time);
- the secrets directory holds one file per secret, 0400 and uid 10001.

The ensure loops have no dry run and are skipped under --check, so a check
cannot see a user, resource, briefing or secret row the play would change.
"""

from __future__ import annotations

import copy
import shlex

import pytest

from istota import setup_wizard

from . import lima

pytestmark = pytest.mark.vm


def _last(mode: str) -> tuple[lima.RoleRun, dict]:
    assert mode in lima.ROLE_RUNS, f"no role converge recorded for {mode}"
    return lima.ROLE_RUNS[mode]


def _describe(run: lima.RoleRun) -> str:
    return f"exit {run.returncode}, recap {run.recap}\n{run.output[-6000:]}"


def _config_toml(vm: lima.Vm) -> str:
    return vm.read(f"{lima.STACK}/config/config.toml")


class _RoleChecks:
    mode = ""

    def test_the_converge_finished_without_a_failure(self, stack):
        run, _ = _last(self.mode)
        assert run.returncode == 0, _describe(run)
        assert run.recap.get("failed") == 0 and run.recap.get("unreachable") == 0, _describe(run)

    def test_a_check_after_the_converge_changes_nothing(self, stack):
        _, variables = _last(self.mode)
        run = lima.converge_role(stack, variables, check=True, timeout=1800)
        assert run.returncode == 0, _describe(run)
        assert run.recap.get("changed") == 0, _describe(run)

    def test_a_check_reports_a_changed_config_and_writes_nothing(self, stack):
        _, variables = _last(self.mode)
        drifted = copy.deepcopy(variables)
        drifted["istota_config"]["bot_name"] = "Drifted"
        before = _config_toml(stack)
        run = lima.converge_role(stack, drifted, check=True, timeout=1800)
        assert run.returncode == 0, _describe(run)
        assert run.recap.get("changed", 0) >= 1, _describe(run)
        dry_run = [block for block in run.output.split("TASK [") if block.split("]", 1)[0].endswith("istota-stack apply --dry-run")]
        assert dry_run and "\nchanged:" in dry_run[0], _describe(run)
        assert _config_toml(stack) == before

    def test_every_secret_file_is_0400_and_owned_by_10001(self, stack):
        listing = stack.out(f"stat -c '%n %a %u %g' {lima.STACK}/secrets/*")
        seen = {}
        for line in listing.splitlines():
            path, mode, uid, gid = line.rsplit(" ", 3)
            seen[path.rsplit("/", 1)[1]] = (mode, uid, gid)
        assert sorted(seen) == sorted(setup_wizard.SECRET_NAMES), listing
        assert set(seen.values()) == {("400", "10001", "10001")}, listing

    def test_the_vault_value_is_the_file(self, stack):
        _, variables = _last(self.mode)
        name = "istota_brain_native_api_key"
        on_disk = stack.out(f"cat {shlex.quote(f'{lima.STACK}/secrets/{name}')}")
        assert on_disk == variables["istota_secrets"][name]


class TestTheRoleInProxiedMode(_RoleChecks):
    mode = "proxied"

    @pytest.fixture(scope="class")
    def stack(self, proxied_stack):
        if lima.control():
            pytest.skip("a negative control is applied; the role's own checks run without one")
        return proxied_stack

    def test_a_second_converge_changes_nothing(self, stack):
        _, variables = _last(self.mode)
        run = lima.converge_role(stack, variables)
        assert run.returncode == 0, _describe(run)
        assert run.recap.get("changed") == 0, _describe(run)

    def test_the_listener_is_the_proxied_one(self, stack):
        env = stack.read(f"{lima.STACK}/.env")
        assert f"NGINX_PUBLISH={lima.PROXIED_LISTEN}:{lima.PROXIED_PORT}:80" in env.splitlines(), env


class TestTheRoleInNextcloudMode(_RoleChecks):
    mode = "nextcloud"

    @pytest.fixture(scope="class")
    def stack(self, nextcloud_stack, vm):
        if lima.control():
            pytest.skip("a negative control is applied; the role's own checks run without one")
        return vm

    def test_the_mount_is_up_from_the_role_written_rclone_conf(self, stack):
        assert stack.out(f"stat -c '%a %u' {lima.STACK}/rclone.conf") == "600 10001"
        assert stack.out("systemctl is-active mount-nextcloud.service") == "active"
        assert stack.out(f"findmnt -n -o FSTYPE {lima.MOUNT}") == "fuse.rclone"
