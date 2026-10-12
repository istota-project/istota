"""The istota container's root phase, and the drop every exec path goes through.

`docker/istota/root-phase.sh` is what runs as uid 0 before anything else in
the container: it delegates the container's own cgroup to the daemon's uid,
fixes ownership once on an upgraded volume, checks the sandbox grant, and execs
`istota-drop`, which empties every capability set and becomes uid 10001.

Its decisions are shell functions taking their inputs as arguments, so this
file sources the script (the `main` guard keeps sourcing inert) and calls them
against a fabricated `/proc/self/mountinfo`. The one decision that matters most
is the mountinfo-root refusal: a cgroup2 mount that is writable *and* rooted
above the container (`/../..`, a bind of the host's tree) would hand the daemon
the VM's whole cgroup hierarchy, so the root phase must stop before it writes
anything. The container-level witness is the smoke tier's; this is the unit.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT_PHASE = REPO / "docker" / "istota" / "root-phase.sh"
ISTOTA_DROP = REPO / "docker" / "istota" / "istota-drop"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="the root phase is bash")


def _call(function: str, *args: str) -> subprocess.CompletedProcess:
    script = f'source "{ROOT_PHASE}"; {function} "$@"'
    return subprocess.run(
        ["bash", "-c", script, "root-phase-test", *args],
        capture_output=True, text=True, timeout=30,
    )


def _mountinfo(tmp_path: Path, line: str) -> Path:
    path = tmp_path / "mountinfo"
    path.write_text(
        "30 1 0:30 / / rw,relatime - overlay overlay rw\n"
        + line
        + "50 30 0:50 / /tmp rw,nosuid,nodev - tmpfs tmpfs rw\n"
    )
    return path


OWN = "41 30 0:41 / /sys/fs/cgroup ro,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw,nsdelegate\n"
HOST_BIND = "41 30 0:41 /../.. /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw\n"
V1 = "41 30 0:41 / /sys/fs/cgroup ro,nosuid,nodev,noexec - tmpfs tmpfs ro\n"


class TestTheMountinfoRootRefusal:
    def test_reads_root_fstype_and_options(self, tmp_path):
        result = _call("cgroup_mount_fields", str(_mountinfo(tmp_path, OWN)), "/sys/fs/cgroup")

        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["/", "cgroup2", "ro,nosuid,nodev,noexec,relatime"]

    def test_the_containers_own_cgroup_is_accepted(self, tmp_path):
        cgroup = tmp_path / "cgroup"
        cgroup.write_text("0::/\n")
        result = _call("require_own_cgroup", str(_mountinfo(tmp_path, OWN)), "/sys/fs/cgroup", str(cgroup))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_host_bind_is_refused_with_its_root_named(self, tmp_path):
        result = _call("require_own_cgroup", str(_mountinfo(tmp_path, HOST_BIND)), "/sys/fs/cgroup")

        assert result.returncode == 70
        assert "/../.." in result.stdout + result.stderr

    def test_the_hosts_cgroup_namespace_is_refused(self, tmp_path):
        # `cgroup: host` mounts the VM's tree rooted at `/`, so the mount root
        # passes; the root phase's own cgroup is then the VM's path for the
        # container, where a private namespace reads `0::/`.
        cgroup = tmp_path / "cgroup"
        cgroup.write_text("0::/system.slice/docker-0123abcd.scope\n")
        result = _call("require_own_cgroup", str(_mountinfo(tmp_path, OWN)), "/sys/fs/cgroup", str(cgroup))

        assert result.returncode == 70
        assert "docker-0123abcd.scope" in result.stdout + result.stderr

    def test_a_cgroup_v1_tree_is_refused(self, tmp_path):
        result = _call("require_own_cgroup", str(_mountinfo(tmp_path, V1)), "/sys/fs/cgroup")

        assert result.returncode == 70

    def test_no_mount_at_all_is_refused(self, tmp_path):
        result = _call("require_own_cgroup", str(_mountinfo(tmp_path, "")), "/sys/fs/cgroup")

        assert result.returncode == 70


class TestTheScriptIsInertWhenSourced:
    def test_sourcing_runs_nothing(self):
        result = subprocess.run(
            ["bash", "-c", f'source "{ROOT_PHASE}" && echo SOURCED'],
            capture_output=True, text=True, timeout=30,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "SOURCED"


class TestTheGrantRefusal:
    def test_the_message_names_the_compose_lines_to_add(self):
        result = _call("grant_refusal")

        message = result.stdout + result.stderr
        assert result.returncode != 0
        for line in (
            "seccomp=./istota/seccomp-istota.json",
            "apparmor=istota",
            "systempaths=unconfined",
            "no-new-privileges:true",
        ):
            assert line in message


class TestTheDropIsCheckedBeforeTheGrant:
    """`istota-drop` reads every credential file as 10001 and exits 78 on one
    it cannot read. The bwrap probe drops the same way, so without a check of
    its own that refusal read as a missing sandbox grant, naming compose lines
    that were all present."""

    def _with_drop(self, tmp_path, body: str, function: str) -> subprocess.CompletedProcess:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        drop = bindir / "istota-drop"
        drop.write_text("#!/bin/sh\n" + body)
        drop.chmod(0o755)
        script = f'export PATH="{bindir}:$PATH"; source "{ROOT_PHASE}"; {function}'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)

    def test_an_unreadable_secret_is_named_as_that(self, tmp_path):
        result = self._with_drop(
            tmp_path,
            'echo "[istota-secrets] REFUSE: /run/secrets/x is not readable by uid 10001." >&2; exit 78\n',
            "check_drop",
        )

        output = result.stdout + result.stderr
        assert result.returncode == 78
        assert "/run/secrets" in output and "chmod 0400" in output
        assert "bubblewrap" not in output

    def test_a_working_drop_passes(self, tmp_path):
        assert self._with_drop(tmp_path, "exec \"$@\"\n", "check_drop").returncode == 0

    def test_main_checks_the_drop_before_the_ownership_walk_and_the_probe(self):
        main = ROOT_PHASE.read_text().split("main() {", 1)[1]
        assert main.index("check_drop") < main.index("fix_ownership") < main.index("probe_grant")


#: The root phase's cap_add as the kernel prints CapBnd: CHOWN, FOWNER, SETGID,
#: SETUID, SETPCAP and SYS_ADMIN (bit 21), then the same without SYS_ADMIN.
CAPBND_WITH_SYS_ADMIN = "00000000002001c9"
CAPBND_WITHOUT_SYS_ADMIN = "00000000000001c9"


def _status(path: Path, capbnd: str | None) -> Path:
    lines = ["Name:\tistota-root-ph", "Uid:\t0\t0\t0\t0", "CapInh:\t0000000000000000"]
    if capbnd is not None:
        lines.append(f"CapBnd:\t{capbnd}")
    path.write_text("\n".join(lines) + "\n")
    return path


class TestTheSysAdminCheck:
    def test_a_bounding_set_without_sys_admin_passes(self, tmp_path):
        status = _status(tmp_path / "status", CAPBND_WITHOUT_SYS_ADMIN)

        result = _call("require_no_sys_admin", str(status))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_bounding_set_with_sys_admin_is_refused(self, tmp_path):
        status = _status(tmp_path / "status", CAPBND_WITH_SYS_ADMIN)

        result = _call("require_no_sys_admin", str(status))

        assert result.returncode == 77
        assert CAPBND_WITH_SYS_ADMIN in result.stdout

    def test_a_status_with_no_bounding_set_is_refused(self, tmp_path):
        result = _call("require_no_sys_admin", str(_status(tmp_path / "status", None)))

        assert result.returncode == 77


class TestTheSetpcapCheck:
    def test_a_bounding_set_with_setpcap_passes(self, tmp_path):
        result = _call("require_setpcap", str(_status(tmp_path / "status", CAPBND_WITH_SYS_ADMIN)))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_bounding_set_without_setpcap_is_refused_by_name(self, tmp_path):
        without_setpcap = f"{int(CAPBND_WITH_SYS_ADMIN, 16) & ~(1 << 8):016x}"

        result = _call("require_setpcap", str(_status(tmp_path / "status", without_setpcap)))

        assert result.returncode == 71
        assert "REFUSE" in result.stdout and "SETPCAP" in result.stdout


class TestMainDropsSysAdminBeforeTheWalk:
    """`main` end to end, with every step that needs a container replaced.

    The steps are shell functions, redefined after sourcing to append their
    name to a trace; `id`, `setpriv` and `istota-drop` are stubs on PATH. The
    harness is itself the script the re-exec names (`"$0"`), so the re-exec'd
    process runs the same stubs. The `setpriv` stub records its arguments and
    rewrites the fake `/proc/self/status` to what the kernel would show after
    the drop, unless the case is the one where the drop did not happen.
    """

    def _run(self, tmp_path, *args: str, drop_works: bool = True) -> tuple[subprocess.CompletedProcess, list[str]]:
        trace = tmp_path / "trace"
        status = _status(tmp_path / "status", CAPBND_WITH_SYS_ADMIN)
        after = _status(tmp_path / "status.after", CAPBND_WITHOUT_SYS_ADMIN if drop_works else CAPBND_WITH_SYS_ADMIN)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        stubs = {
            "id": 'echo 0\n',
            "setpriv": (
                'echo "setpriv $*" >> "$TRACE"\n'
                'while [ "$1" != "--" ]; do shift; done; shift\n'
                f'cp "{after}" "{status}"\n'
                'exec "$@"\n'
            ),
            "istota-drop": 'echo "istota-drop $*" >> "$TRACE"\n',
        }
        for name, body in stubs.items():
            stub = bindir / name
            stub.write_text("#!/bin/sh\n" + body)
            stub.chmod(0o755)
        harness = tmp_path / "harness.sh"
        steps = ("require_own_cgroup", "delegate_cgroup", "check_drop", "fix_ownership", "probe_grant")
        harness.write_text(
            "#!/bin/bash\n"
            f'source "{ROOT_PHASE}"\n'
            f'SELF_STATUS="{status}"\n'
            + "".join(f'{step}() {{ echo {step} >> "$TRACE"; }}\n' for step in steps)
            + 'main "$@"\n'
        )
        harness.chmod(0o755)
        result = subprocess.run(
            [str(harness), *args],
            capture_output=True, text=True, timeout=30,
            env={"PATH": f"{bindir}:/usr/bin:/bin", "TRACE": str(trace)},
        )
        steps_run = trace.read_text().splitlines() if trace.exists() else []
        return result, steps_run

    def test_the_remount_then_the_re_exec_then_the_walk(self, tmp_path):
        result, steps = self._run(tmp_path, "/entrypoint.sh")

        assert result.returncode == 0, result.stdout + result.stderr
        assert steps == [
            "require_own_cgroup",
            "delegate_cgroup",
            f"setpriv --bounding-set=-sys_admin --inh-caps=-sys_admin -- {tmp_path / 'harness.sh'} --after-remount /entrypoint.sh",
            "check_drop",
            "fix_ownership",
            "probe_grant",
            "istota-drop /entrypoint.sh",
        ]

    def test_the_bounding_set_is_logged_before_the_walk(self, tmp_path):
        result, _ = self._run(tmp_path, "/entrypoint.sh")

        assert f"[istota-root] CapBnd {CAPBND_WITHOUT_SYS_ADMIN} before the ownership walk" in result.stdout

    def test_the_flag_stops_a_second_re_exec(self, tmp_path):
        # Started with the flag, as the re-exec'd process is: no remount and no
        # setpriv, whatever the bounding set holds.
        result, steps = self._run(tmp_path, "--after-remount", "/entrypoint.sh")

        assert not [step for step in steps if step.startswith("setpriv")], steps
        assert "delegate_cgroup" not in steps

    def test_sys_admin_still_held_after_the_re_exec_stops_before_the_walk(self, tmp_path):
        result, steps = self._run(tmp_path, "/entrypoint.sh", drop_works=False)

        assert result.returncode == 77, result.stdout + result.stderr
        assert "CAP_SYS_ADMIN is still in the bounding set" in result.stdout
        assert [step for step in steps if step.startswith("setpriv")], steps
        assert "fix_ownership" not in steps
        assert not [step for step in steps if step.startswith("istota-drop")], steps


class TestTheDrop:
    """`istota-drop` is the one spelling of the drop, for the entrypoint and every exec path."""

    def test_it_empties_every_capability_set_and_becomes_10001(self):
        text = ISTOTA_DROP.read_text()
        for flag in (
            "--reuid=10001", "--regid=10001", "--init-groups",
            "--inh-caps=-all", "--bounding-set=-all",
        ):
            assert flag in text, flag
        assert 'exec setpriv' in text
        # The dropped process reads the compose secrets, then runs the command.
        assert '/usr/local/bin/istota-secrets "$@"' in text

    def test_the_root_phase_ends_by_execing_it(self):
        lines = [line.strip() for line in ROOT_PHASE.read_text().splitlines()]
        assert 'exec istota-drop "$@"' in lines
