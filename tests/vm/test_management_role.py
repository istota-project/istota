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
- the secrets directory holds one file per secret, 0400 and uid 10001;
- a first converge, on a VM with no config.toml and no deployed tag, brings
  the stack up (proxied);
- turning the browser profile on starts the browser and its watchdog, and
  turning it off stops both (proxied);
- a changed rclone.conf runs provision.sh and leaves the mount, and so the
  stack, running (nextcloud);
- settings -> snapshot -> inventory -> apply (proxied): the converged VM's
  own `istota snapshot`, set as `istota_plan` in place of the plan the
  tier rendered, is no drift under --check; a timezone and a config value
  changed in it are drift a --check reports and does not write; a converge
  applies both and restarts the daemon once (ISSUE-741); and the next
  --check is clean.

Control (`scripts/test-vm-negative-control.sh`): `role-user-settings-unchecked`
runs a copy of the role whose user settings are skipped under --check, as the
ensure loops were (`lima.ROLE_CONTROLS`).
"""

from __future__ import annotations

import copy
import json
import shlex
import uuid
from dataclasses import dataclass

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


def _task_changed(run: lima.RoleRun, name: str) -> bool:
    """Whether the task named `name` (role prefix aside) reported a change."""
    blocks = [block for block in run.output.split("TASK [") if block.split("]", 1)[0].endswith(name)]
    return bool(blocks) and "\nchanged:" in blocks[0]


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
        drifted["istota_plan"]["config"]["bot_name"] = "Drifted"
        before = _config_toml(stack)
        run = lima.converge_role(stack, drifted, check=True, timeout=1800)
        assert run.returncode == 0, _describe(run)
        assert run.recap.get("changed", 0) >= 1, _describe(run)
        assert _task_changed(run, "istota-stack apply"), _describe(run)
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
        name = "istota_web_session_secret_key"
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

    def test_a_link_uid_10001_planted_is_not_written_through(self, stack):
        """`secrets/` and `config/` are 10001's. A link to a directory planted
        where a secret and the admins file go, by 10001, must be replaced, and
        nothing may land in a directory only root can write."""
        _, variables = _last(self.mode)
        name = "istota_web_session_secret_key"
        plant = f"{lima.SCRATCH}/planted"
        targets = [f"{lima.STACK}/secrets/{name}", f"{lima.STACK}/config/admins"]
        as_10001 = "setpriv --reuid=10001 --regid=10001 --clear-groups"
        stack.run(f"rm -rf {plant}; install -d -m 0755 {plant}\n" + "".join(
            f"{as_10001} sh -c {shlex.quote(f'rm -f {t} && ln -s {plant} {t}')}\n" for t in targets))
        try:
            run = lima.converge_role(stack, variables)
            assert run.returncode == 0, _describe(run)
            assert stack.out(f"ls -A {plant}") == "", "the play wrote through a planted link"
            for target in targets:
                assert stack.out(f"stat -c '%F %u' {target}") == "regular file 10001", target
            assert stack.out(f"cat {targets[0]}") == variables["istota_secrets"][name]
        finally:
            stack.run(f"rm -rf {plant}; for t in {' '.join(targets)}; do [ ! -L \"$t\" ] || rm -f \"$t\"; done")
            if not stack.ok(f"test -f {targets[0]} && test -f {targets[1]}"):
                lima.converge_role(stack, variables)

    def test_a_provision_a_dead_play_left_undone_runs_on_the_next(self, stack):
        """What a play that died between writing host.env and provision.sh
        finishing leaves: host.env already as wanted, the provision not done.
        The journald drop-in is provision.sh's, written on every run, so its
        return says provision.sh ran."""
        _, variables = _last(self.mode)
        marker = uuid.uuid4().hex
        journald = "/etc/systemd/journald.conf.d/istota.conf"
        lima.env_file_set(stack, f"{lima.STACK}/host.env", {"ISTOTA_VMTIER_MARK": marker})
        stack.run(f"rm -f {journald}")
        wanting = copy.deepcopy(variables)
        wanting["istota_host_env_extra"] = {"ISTOTA_VMTIER_MARK": marker}
        try:
            run = lima.converge_role(stack, wanting)
            assert run.returncode == 0, _describe(run)
            assert stack.ok(f"test -f {journald}"), f"provision.sh did not run\n{run.output[-4000:]}"
        finally:
            if not stack.ok(f"test -f {journald}"):
                stack.run(f"{lima.STACK}/src/host/provision.sh", timeout=1800)

    def test_the_user_secret_reached_the_database_through_stdin(self, stack):
        """The same value through `--value-stdin` and the VM's wrapper is a
        no-op: the role's settings apply stored it from the private envelope,
        and stdin crosses `docker compose exec`."""
        _, variables = _last(self.mode)
        value = variables["istota_plan_secrets"]["users"][lima.USER]["karakeep"]["base_url"]
        result = stack.run(
            f"printf '%s' {shlex.quote(value)} | istota secret ensure --user {lima.USER} "
            f"--service karakeep --key base_url --value-stdin", check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "STATE: noop" in result.stdout, result.stdout

    def test_a_claude_code_brain_gets_an_image_with_claude_code(self, stack):
        """`istota-stack update` decides from config.toml whether to build
        Claude Code in. This stack's image was built for the native brain;
        a config that switches to claude_code must leave an image with it, on
        this play, without a new release."""
        _, variables = _last(self.mode)
        wanting = copy.deepcopy(variables)
        wanting["istota_plan"]["config"].setdefault("brain", {})["kind"] = "claude_code"
        tag = variables["istota_release"]
        image_env = (f"docker image inspect -f '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' "
                     f"istota:{tag}")
        try:
            run = lima.converge_role(stack, wanting)
            assert run.returncode == 0, _describe(run)
            assert "ISTOTA_CLAUDE_CODE_INSTALLED=1" in stack.out(image_env).splitlines(), _describe(run)
            running = lima.container(stack, "daemon")
            assert stack.out(f"docker inspect -f '{{{{.Image}}}}' {running}") == stack.out(
                f"docker image inspect -f '{{{{.Id}}}}' istota:{tag}"), "the stack runs the old image"
        finally:
            restore = lima.converge_role(stack, variables)
            assert restore.returncode == 0, _describe(restore)

    def test_a_first_converge_with_no_config_toml_brings_the_stack_up(self, stack):
        """What a VM bootstrapped by cloud-init holds when the role first runs:
        a checkout and no config, no deployed tag, the stack not enabled. The
        update runs before the config exists, so nothing starts until the
        apply has written it and the play starts the unit."""
        _, variables = _last(self.mode)
        before = _config_toml(stack)
        saved = f"{lima.SCRATCH}/first-converge"
        stack.run(f"""
systemctl disable --now istota-stack.service
rm -rf {saved}; install -d -m 0700 {saved}
cp -a {lima.STACK}/config {saved}/config
cp -a {lima.STACK}/.env {saved}/stack.env
rm -f {lima.STACK}/config/config.toml {lima.STACK}/config/admins
""", timeout=600)
        lima.env_file_set(stack, f"{lima.STACK}/.env", {"ISTOTA_TAG": "", "ISTOTA_PREVIOUS_TAG": ""})
        try:
            run = lima.converge_role(stack, variables)
            assert run.returncode == 0, _describe(run)
            assert run.recap.get("failed") == 0, _describe(run)
            assert _config_toml(stack) == before
            assert stack.out(f"cat {lima.STACK}/config/admins") == lima.USER
            assert lima.env_value(stack, f"{lima.STACK}/.env", "ISTOTA_TAG") == variables["istota_release"]
            assert stack.out("systemctl is-enabled istota-stack.service") == "enabled"
            lima.wait_healthy(stack, timeout=300)
        finally:
            if not (stack.ok(f"test -f {lima.STACK}/config/config.toml")
                    and lima.env_value(stack, f"{lima.STACK}/.env", "ISTOTA_TAG")):
                stack.run(f"""
systemctl stop istota-stack.service || true
cp -a {saved}/config/. {lima.STACK}/config/
cp -a {saved}/stack.env {lima.STACK}/.env
systemctl enable --now istota-stack.service
""", timeout=1800)
                lima.wait_healthy(stack)
            stack.run(f"rm -rf {saved}")

    def test_a_browser_profile_change_starts_and_stops_the_browser(self, stack, release, model):
        """The estate turns the browser profile on, then off again, as the
        wizard's answers would: `--profile browser` changes both COMPOSE_PROFILES
        and the config's [browser] table."""
        _, variables = _last(self.mode)
        mode = lima.proxied_mode(model.vm_url)
        mode.setup_args = [*mode.setup_args, "--profile", "browser"]
        wanting = lima.role_vars(stack, release, mode)
        assert wanting["istota_compose_profiles"] == ["browser"], wanting["istota_compose_profiles"]
        timer = "istota-browser-watchdog.timer"
        assert not lima.container(stack, "browser"), "a browser runs before the profile is on"
        try:
            run = lima.converge_role(stack, wanting)
            assert run.returncode == 0, _describe(run)
            cid = lima.container(stack, "browser")
            assert cid, "no browser container after the profile was turned on\n" + _describe(run)
            lima.wait_for(
                lambda: stack.out(f"docker inspect -f '{{{{.State.Health.Status}}}}' {cid}", check=False) == "healthy",
                timeout=900, interval=10, what="the browser container to become healthy")
            assert stack.out(f"systemctl is-active {timer}", check=False) == "active"
            health = lima.stack_exec(
                stack, "python3 -c \"import urllib.request;"
                "print(urllib.request.urlopen('http://browser:9223/health',timeout=10).status)\"").stdout.strip()
            assert health == "200", health
        finally:
            restore = lima.converge_role(stack, variables)
        assert restore.returncode == 0, _describe(restore)
        assert not lima.container(stack, "browser"), "the browser still runs after the profile was turned off"
        assert stack.out(f"systemctl is-active {timer}", check=False) != "active"


TIMEZONES = ("Pacific/Auckland", "Europe/Lisbon")


@dataclass
class RoundTrip:
    timezone_before: str
    timezone_wanted: str
    unchanged_check: lima.RoleRun
    drift_check: lima.RoleRun
    timezone_after_check: str
    daemon_after_check: str
    converge: lima.RoleRun
    timezone_after_converge: str
    daemon_after_converge: str
    clean_check: lima.RoleRun
    daemon_before: str
    daemon_starts_in_converge: int


def _snapshot(vm: lima.Vm) -> dict:
    return json.loads(lima.stack_exec(vm, "istota snapshot --format json").stdout)


def _daemon_started(vm: lima.Vm) -> str:
    cid = lima.container(vm, "daemon")
    return f"{cid} {vm.out(f'docker inspect -f {{{{.State.StartedAt}}}} {cid}')}" if cid else ""


def _daemon_starts(vm: lima.Vm, since: str, until: str) -> int:
    """How many times a daemon container started between two VM clock readings."""
    listing = vm.out(
        f"docker events --since {since} --until {until} --filter event=start "
        f"--filter label=com.docker.compose.project=istota "
        f"--filter label=com.docker.compose.service=daemon --format '{{{{.ID}}}}'")
    return len(listing.split())


class TestASnapshotIsTheRolesPlan:
    """settings -> snapshot -> inventory -> apply, through the role, on the
    VM the role converged in proxied mode. The inventory swaps the plan the
    tier rendered for the VM's own snapshot as `istota_plan`, as an estate
    would after `istota snapshot --format json`; every deployment variable and
    the vault's `istota_plan_secrets` stay as they were."""

    @pytest.fixture(scope="class")
    def round_trip(self, proxied_stack):
        if lima.control() not in ("", "role-user-settings-unchecked"):
            pytest.skip("another negative control is applied")
        stack = proxied_stack
        _, variables = _last("proxied")
        snapshot = _snapshot(stack)
        canonical = copy.deepcopy(variables)
        canonical["istota_plan"] = snapshot
        before = snapshot["users"][lima.USER]["profile"].get("timezone", "")
        wanted = TIMEZONES[0] if before != TIMEZONES[0] else TIMEZONES[1]
        drifted = copy.deepcopy(canonical)
        drifted["istota_plan"]["users"][lima.USER]["profile"]["timezone"] = wanted
        # A config change as well, so the converge changes both halves of the plan.
        facts = snapshot["config"].get("max_knowledge_facts")
        drifted["istota_plan"]["config"]["max_knowledge_facts"] = 48 if facts == 49 else 49
        try:
            unchanged = lima.converge_role(stack, canonical, check=True, timeout=1800)
            daemon_before = _daemon_started(stack)
            drift = lima.converge_role(stack, drifted, check=True, timeout=1800)
            after_check = _snapshot(stack)["users"][lima.USER]["profile"].get("timezone", "")
            daemon_after_check = _daemon_started(stack)
            since = stack.out("date +%s")
            converge = lima.converge_role(stack, drifted)
            lima.wait_healthy(stack)
            starts = _daemon_starts(stack, since, stack.out("date +%s"))
            after_converge = _snapshot(stack)["users"][lima.USER]["profile"].get("timezone", "")
            daemon_after_converge = _daemon_started(stack)
            clean = lima.converge_role(stack, drifted, check=True, timeout=1800)
        finally:
            # The snapshot names the timezone it was taken with, so applying it puts that back.
            restore = lima.converge_role(stack, canonical)
            assert restore.returncode == 0, _describe(restore)
            lima.wait_healthy(stack)
        return RoundTrip(before, wanted, unchanged, drift, after_check, daemon_after_check, converge,
                         after_converge, daemon_after_converge, clean, daemon_before, starts)

    def test_the_snapshot_as_istota_plan_is_no_drift(self, round_trip):
        run = round_trip.unchanged_check
        assert run.returncode == 0 and run.recap.get("failed") == 0, _describe(run)
        assert run.recap.get("changed") == 0, _describe(run)

    def test_a_check_sees_the_changed_timezone_and_writes_nothing(self, round_trip):
        run = round_trip.drift_check
        assert run.returncode == 0 and run.recap.get("failed") == 0, _describe(run)
        assert _task_changed(run, "istota-stack apply (user settings)"), _describe(run)
        assert round_trip.timezone_after_check == round_trip.timezone_before
        assert round_trip.daemon_after_check == round_trip.daemon_before, "a --check restarted the daemon"

    def test_a_converge_applies_it_and_restarts_the_daemon(self, round_trip):
        run = round_trip.converge
        assert run.returncode == 0 and run.recap.get("failed") == 0, _describe(run)
        assert _task_changed(run, "istota-stack apply (user settings)"), _describe(run)
        assert round_trip.timezone_after_converge == round_trip.timezone_wanted
        assert round_trip.daemon_after_converge != round_trip.daemon_before, "a profile change did not restart"

    def test_a_converge_changing_both_halves_restarts_once(self, round_trip):
        run = round_trip.converge
        assert _task_changed(run, "istota-stack apply"), _describe(run)
        assert round_trip.daemon_starts_in_converge == 1, _describe(run)

    def test_the_next_check_is_clean(self, round_trip):
        run = round_trip.clean_check
        assert run.returncode == 0 and run.recap.get("changed") == 0, _describe(run)


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

    def test_an_rclone_conf_change_provisions_and_leaves_the_mount_running(self, stack):
        """rclone.conf is one of provision.sh's inputs, so a change to it runs
        provision.sh. provision.sh restarts the mount only for a changed unit or
        a replaced rclone, since the stack Requires= it and would go down too."""
        _, variables = _last(self.mode)
        changed = copy.deepcopy(variables)
        changed["istota_rclone_conf"] = variables["istota_rclone_conf"] + (
            "\n[vmtier-spare]\ntype = alias\nremote = ncroot:\n")
        mount_state = ("systemctl show -p MainPID -p ActiveEnterTimestampMonotonic --value "
                       "mount-nextcloud.service")
        mount_before = stack.out(mount_state)
        daemon = lima.container(stack, "daemon")
        started = f"docker inspect -f '{{{{.State.StartedAt}}}}' {daemon}"
        daemon_before = stack.out(started)
        provisioned_before = stack.read(f"{lima.STACK}/.provisioned").strip()
        try:
            run = lima.converge_role(stack, changed)
            assert run.returncode == 0, _describe(run)
            provisioned = stack.read(f"{lima.STACK}/.provisioned").strip()
            assert provisioned != provisioned_before, "provision.sh did not run\n" + _describe(run)
            assert provisioned == stack.out(f"{lima.STACK}/src/host/provision.sh --digest")
            assert "[vmtier-spare]" in stack.read(f"{lima.STACK}/rclone.conf")
            assert stack.out(mount_state) == mount_before, "the mount was restarted"
            assert stack.out(f"findmnt -n -o FSTYPE {lima.MOUNT}") == "fuse.rclone"
            assert lima.container(stack, "daemon") == daemon and stack.out(started) == daemon_before, \
                "the stack was restarted"
        finally:
            restore = lima.converge_role(stack, variables)
            assert restore.returncode == 0, _describe(restore)
