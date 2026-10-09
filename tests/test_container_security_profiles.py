"""The istota container's run contract, as files: two profiles and the compose lines.

Three things are held here, and none of them needs a container.

**The seccomp profile is Docker's default with two changes.** It is derived
from the default profile Docker Engine 29.9.0 compiles in, vendored beside it,
and differs in exactly two ways: one unconditional allow rule for the calls
bubblewrap and the root phase's cgroup remount need (measured one at a time in
the Stage 1 spike), and the removal of the default's `includes.caps:
[CAP_SYS_ADMIN]` rule. Docker evaluates `includes.caps` against the
container's configured capabilities, not the calling process's, so with
`SYS_ADMIN` in `cap_add` for the root phase that rule would open `bpf`,
`perf_event_open` and the new mount API to the dropped daemon and every
sandbox. A change to either file is therefore a change this test reports.

**The AppArmor profile is docker-default with the mount rules bwrap needs.**
Rendered from moby/profiles' template at the same engine version, vendored
beside it. The diff is compared line by line, comments aside.

**Compose carries the contract, on the shipped file and on the lean test file
alike.** The smoke tier runs the lean file, so the witnesses there say
something about the shipped stack only while the two agree on every line that
decides the boundary. Stage 4 makes the lean shape the shipped file plus an
overlay; until then this is what keeps them in step.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DOCKER = REPO / "docker"
SECCOMP = DOCKER / "istota" / "seccomp-istota.json"
SECCOMP_DEFAULT = DOCKER / "istota" / "seccomp-docker-default-29.9.0.json"
APPARMOR = DOCKER / "istota" / "apparmor-istota"
APPARMOR_DEFAULT = DOCKER / "istota" / "apparmor-docker-default-29.9.0"
SHIPPED_COMPOSE = DOCKER / "docker-compose.yml"
LEAN_COMPOSE = DOCKER / "docker-compose.test.yml"

#: Measured in the Stage 1 spike by removing one name at a time. `setns`, which
#: the spec's first list named, is not needed by bwrap 0.12.
BWRAP_SYSCALLS = ["clone", "clone3", "mount", "pivot_root", "umount2", "unshare"]

#: The denylist the profile must keep. Written out, not derived: a profile that
#: lost one of these would derive a list without it.
STILL_DENIED = (
    "bpf", "keyctl", "add_key", "request_key", "perf_event_open", "userfaultfd",
    "kexec_load", "kexec_file_load", "init_module", "finit_module",
    "delete_module", "open_by_handle_at",
)

ROOT_PHASE_CAPS = {"CHOWN", "FOWNER", "SETUID", "SETGID", "SETPCAP", "SYS_ADMIN"}


def _is_sysadmin_include(rule: dict) -> bool:
    return (rule.get("includes") or {}).get("caps") == ["CAP_SYS_ADMIN"]


def derive_seccomp(default: dict) -> dict:
    profile = copy.deepcopy(default)
    profile["syscalls"] = [r for r in profile["syscalls"] if not _is_sysadmin_include(r)]
    profile["syscalls"].append(
        {
            "names": sorted(BWRAP_SYSCALLS),
            "action": "SCMP_ACT_ALLOW",
            "comment": (
                "istota: bwrap namespace setup and the root phase's cgroup "
                "remount; no capability condition"
            ),
        }
    )
    return profile


class TestTheSeccompProfile:
    @pytest.fixture(scope="class")
    def shipped(self) -> dict:
        return json.loads(SECCOMP.read_text())

    def test_it_is_dockers_default_with_exactly_the_two_changes(self, shipped):
        default = json.loads(SECCOMP_DEFAULT.read_text())
        assert shipped == derive_seccomp(default)

    def test_the_cap_sys_admin_rule_is_gone(self, shipped):
        assert not [r for r in shipped["syscalls"] if _is_sysadmin_include(r)]

    def test_the_default_action_still_refuses(self, shipped):
        assert shipped["defaultAction"] == "SCMP_ACT_ERRNO"

    @pytest.mark.parametrize("syscall", STILL_DENIED)
    def test_the_denylist_survives(self, shipped, syscall):
        # Allowed only where a capability nothing in the container holds after
        # the drop would include it, and never unconditionally.
        unconditional = [
            r for r in shipped["syscalls"]
            if r["action"] == "SCMP_ACT_ALLOW" and syscall in r["names"]
            and not (r.get("includes") or {}).get("caps")
        ]
        assert not unconditional, f"{syscall} is allowed by {unconditional}"


def _significant(text: str) -> list[str]:
    """Rule lines: comment-only and blank lines dropped, `#include` kept."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or (stripped.startswith("#") and not stripped.startswith("#include")):
            continue
        out.append(stripped)
    return out


class TestTheAppArmorProfile:
    REMOVED = {
        "deny mount,",
        "deny @{PROC}/sys/[^k]** w,  # deny /proc/sys except /proc/sys/k* (effectively /proc/sys/kernel)",
    }
    ADDED = {
        "mount,",
        "pivot_root,",
        "deny @{PROC}/sys/[^ku]** w,  # deny /proc/sys except /proc/sys/k* and /proc/sys/u*",
        "deny @{PROC}/sys/u[^s]** w,",
        "deny @{PROC}/sys/user/[^m]** w,",
        "deny @{PROC}/sys/user/max_[^u]** w,",
        "deny @{PROC}/sys/user/max_u[^s]** w,  # leaves max_user_namespaces alone writable",
    }

    def test_it_is_docker_default_with_exactly_the_mount_changes(self):
        default = _significant(APPARMOR_DEFAULT.read_text().replace("docker-default", "istota"))
        shipped = _significant(APPARMOR.read_text())

        assert set(default) - set(shipped) == self.REMOVED
        assert set(shipped) - set(default) == self.ADDED
        assert len(shipped) == len(set(shipped)), "a rule line is duplicated"

    def test_the_profile_is_named_istota(self):
        assert 'profile "istota" flags=(attach_disconnected,mediate_deleted) {' in APPARMOR.read_text()

    def test_the_proc_writes_that_matter_stay_denied(self):
        shipped = APPARMOR.read_text()
        assert "deny @{PROC}/sysrq-trigger rwklx," in shipped
        assert "deny @{PROC}/sys/kernel/{?,??,[^s][^h][^m]**} w," in shipped


def _service(compose_file: Path, name: str = "istota") -> dict:
    return yaml.safe_load(compose_file.read_text())["services"][name]


def _volume_targets(service: dict) -> list[str]:
    targets = []
    for entry in service.get("volumes") or []:
        if isinstance(entry, dict):
            targets.append(str(entry.get("target", "")))
        else:
            parts = str(entry).split(":")
            targets.append(parts[1] if len(parts) > 1 else parts[0])
    return targets


@pytest.fixture(scope="module", params=[SHIPPED_COMPOSE, LEAN_COMPOSE], ids=["shipped", "lean"])
def istota_service(request) -> tuple[Path, dict]:
    return request.param, _service(request.param)


class TestTheRunContract:
    def test_security_opt_is_the_grant_and_nothing_wider(self, istota_service):
        compose_file, service = istota_service
        opts = set(service.get("security_opt") or [])

        assert opts == {
            "seccomp=./istota/seccomp-istota.json",
            "apparmor=istota",
            "systempaths=unconfined",
            "no-new-privileges:true",
        }
        assert (compose_file.parent / "istota" / "seccomp-istota.json").resolve() == SECCOMP

    def test_capabilities_are_the_root_phase_set_only(self, istota_service):
        _, service = istota_service
        assert service.get("cap_drop") == ["ALL"]
        assert set(service.get("cap_add") or []) == ROOT_PHASE_CAPS

    def test_the_root_filesystem_is_read_only(self, istota_service):
        _, service = istota_service
        assert service.get("read_only") is True
        assert "/tmp" in (service.get("tmpfs") or [])

    def test_the_cgroup_namespace_is_private_and_unbound(self, istota_service):
        _, service = istota_service
        assert service.get("cgroup") == "private"
        assert not [t for t in _volume_targets(service) if t.startswith("/sys")]

    def test_no_user_line_the_entrypoint_drops(self, istota_service):
        # The root phase needs uid 0 for the remount and the ownership fix; the
        # drop to 10001 is the entrypoint's, through `istota-drop`.
        _, service = istota_service
        assert "user" not in service

    def test_the_delegated_root_is_declared(self, istota_service):
        _, service = istota_service
        assert (service.get("environment") or {}).get("ISTOTA_TASK_CGROUP_ROOT") == "/sys/fs/cgroup"

    def test_the_healthcheck_drops_before_it_opens_the_database(self, istota_service):
        # A healthcheck runs the way `docker exec` does: uid 0 with the cap_add
        # set. Opening the database as root is how a root-owned sidecar locks
        # the daemon out (ISSUE-458).
        _, service = istota_service
        test = service["healthcheck"]["test"]
        command = test[-1] if isinstance(test, list) else test
        assert command.lstrip().startswith("istota-drop "), command
        assert "name='tasks'" in command

    def test_no_docker_socket(self, istota_service):
        _, service = istota_service
        assert not [t for t in _volume_targets(service) if "docker" in t or t in ("/run", "/var/run")]


class TestTheLeanShapeRunsTheRootPhase:
    def test_the_lean_entrypoint_goes_through_the_root_phase(self):
        entrypoint = _service(LEAN_COMPOSE)["entrypoint"]
        assert entrypoint[0] == "/usr/local/sbin/istota-root-phase", entrypoint

    def test_the_shipped_image_entrypoint_is_the_root_phase(self):
        dockerfile = (DOCKER / "istota" / "Dockerfile").read_text()
        assert 'ENTRYPOINT ["/usr/local/sbin/istota-root-phase", "/entrypoint.sh"]' in dockerfile


class TestTheBrowserNetwork:
    """Row 17's shape: the browser shares a network with the daemon only."""

    @pytest.fixture(scope="class")
    def compose(self) -> dict:
        return yaml.safe_load(SHIPPED_COMPOSE.read_text())

    @staticmethod
    def _networks(service: dict) -> set[str]:
        nets = service.get("networks") or ["default"]
        return set(nets if isinstance(nets, list) else nets.keys())

    def test_the_browser_is_on_its_own_network_only(self, compose):
        assert self._networks(compose["services"]["browser"]) == {"browser"}

    def test_the_daemon_joins_it_and_keeps_the_default(self, compose):
        assert self._networks(compose["services"]["istota"]) == {"default", "browser"}

    def test_nothing_else_joins_it(self, compose):
        joined = sorted(
            name for name, service in compose["services"].items()
            if name not in ("browser", "istota") and "browser" in self._networks(service)
        )
        assert joined == []
