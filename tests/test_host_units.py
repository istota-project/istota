"""The `host/` units for devbox egress and the browser watchdog, before a VM.

Stage 7's vm tier is what runs them on a real Debian VM (row 10 for the egress
rules). Until then these are files, and this holds the properties a reader
cannot check by eye: the egress script asks for the role's rules, at the front
of the chain, against the subnet the compose renderer gives the devbox network;
and the watchdog restarts only after its debounce. Both scripts are executed
against stub `iptables` and `docker` binaries on PATH.
"""

from __future__ import annotations

import configparser
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from istota.config import DevboxConfig

REPO = Path(__file__).resolve().parent.parent
HOST = REPO / "host"
EGRESS_SH = HOST / "istota-devbox-egress.sh"
EGRESS_UNIT = HOST / "istota-devbox-egress.service"
WATCHDOG_SH = HOST / "istota-browser-watchdog.sh"
WATCHDOG_UNIT = HOST / "istota-browser-watchdog.service"
WATCHDOG_TIMER = HOST / "istota-browser-watchdog.timer"

WANTED = {
    "169.254.0.0/16", "168.63.129.16/32", "10.0.0.0/8", "172.16.0.0/12",
    "192.168.0.0/16", "100.64.0.0/10",
}


def _unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


@pytest.mark.parametrize("script", [EGRESS_SH, WATCHDOG_SH])
def test_the_scripts_parse_and_are_executable(script):
    assert os.access(script, os.X_OK), f"{script.name} is not executable"
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "set -euo pipefail" in script.read_text()


class TestTheEgressRules:
    def _run(self, tmp_path: Path, env: dict | None = None) -> list[list[str]]:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        calls = tmp_path / "calls"
        # `-C` answers "absent", so each rule is inserted once.
        _stub(bindir, "iptables",
              f'echo "$@" >> {calls}\ncase " $* " in *" -C "*) exit 1 ;; esac\nexit 0\n')
        run_env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
        run_env.pop("ISTOTA_DEVBOX_SUBNET", None)
        run_env.update(env or {})
        result = subprocess.run(["bash", str(EGRESS_SH)], capture_output=True,
                                text=True, env=run_env, timeout=30)
        assert result.returncode == 0, result.stderr
        return [line.split() for line in calls.read_text().splitlines()]

    def test_every_destination_is_dropped_from_the_default_subnet(self, tmp_path):
        inserts = [c for c in self._run(tmp_path) if "-I" in c]
        dests = {c[c.index("-d") + 1] for c in inserts}
        sources = {c[c.index("-s") + 1] for c in inserts}
        assert dests == WANTED
        assert sources == {DevboxConfig().network_subnet}
        assert all(c[-2:] == ["-j", "DROP"] for c in inserts)

    def test_the_subnet_comes_from_the_environment(self, tmp_path):
        inserts = [c for c in self._run(tmp_path, {"ISTOTA_DEVBOX_SUBNET": "172.31.0.0/24"})
                   if "-I" in c]
        assert {c[c.index("-s") + 1] for c in inserts} == {"172.31.0.0/24"}

    def test_every_rule_goes_to_the_front_and_waits_for_the_lock(self, tmp_path):
        calls = self._run(tmp_path)
        assert calls
        for call in calls:
            assert call[:2] == ["-w", "5"], call
            assert "-A" not in call, call
            if "-I" in call:
                assert call[call.index("-I") + 1:call.index("-I") + 3] == ["DOCKER-USER", "1"]

    def test_the_subnet_default_is_the_renderers(self):
        from istota.config import Config, ContainerConfig, DeveloperConfig
        from istota.devbox import compose_file

        config = Config()
        config.developer = DeveloperConfig(
            devbox_proxy_socket_dir="/data/devbox/cred",
            container=ContainerConfig(exec_socket_dir="/data/devbox/exec"),
        )
        config.devbox = DevboxConfig(enabled=True, users=["alice"])
        doc = yaml.safe_load(compose_file.render_devbox_compose(config))
        subnet = doc["networks"]["devbox"]["ipam"]["config"][0]["subnet"]
        assert f'ISTOTA_DEVBOX_SUBNET:-{subnet}' in EGRESS_SH.read_text()

    def test_the_unit_runs_after_docker_once_per_boot(self):
        unit = _unit(EGRESS_UNIT)
        assert "docker.service" in unit["Unit"]["After"]
        assert "docker.service" in unit["Unit"]["Requires"]
        assert unit["Service"]["Type"] == "oneshot"
        assert unit["Service"]["RemainAfterExit"] == "yes"
        assert unit["Service"]["ExecStart"].endswith("istota-devbox-egress")
        assert unit["Service"]["EnvironmentFile"] == "-/srv/app/istota/host.env"
        assert unit["Install"]["WantedBy"] == "multi-user.target"


class TestTheBrowserWatchdog:
    def _run(self, tmp_path: Path, statuses: list[str], *, stack_active: bool = True,
             daily_hour: str = "99") -> str:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        calls = tmp_path / "docker-calls"
        status = tmp_path / "status"
        _stub(bindir, "docker",
              f'echo "$@" >> {calls}\n'
              f'if [ "$1" = inspect ]; then cat {status}; fi\nexit 0\n')
        _stub(bindir, "curl", "exit 0\n")
        _stub(bindir, "systemctl", f"exit {0 if stack_active else 3}\n")
        stack = tmp_path / "stack"
        stack.mkdir()
        env = dict(
            os.environ,
            PATH=f"{bindir}:{os.environ['PATH']}",
            ISTOTA_STACK_DIR=str(stack),
            ISTOTA_MOUNT_DIR=str(tmp_path / "mount"),
            ISTOTA_STACK_BIN=str(HOST / "istota-stack"),
            ISTOTA_BROWSER_WATCHDOG_STATE_DIR=str(tmp_path / "state"),
            ISTOTA_BROWSER_WATCHDOG_LOG=str(tmp_path / "log" / "health.log"),
            # Never due inside the test, whatever the clock says.
            ISTOTA_BROWSER_DAILY_RESTART_HOUR=daily_hour,
        )
        for value in statuses:
            status.write_text(value + "\n")
            result = subprocess.run(["bash", str(WATCHDOG_SH), "check"],
                                    capture_output=True, text=True, env=env, timeout=30)
            assert result.returncode == 0, result.stderr
        return calls.read_text() if calls.exists() else ""

    def test_a_healthy_container_is_left_alone(self, tmp_path):
        assert "restart" not in self._run(tmp_path, ["healthy", "healthy", "healthy"])

    def test_one_unhealthy_read_is_not_enough(self, tmp_path):
        assert "restart" not in self._run(tmp_path, ["unhealthy"])

    def test_two_in_a_row_restart_it_through_compose(self, tmp_path):
        calls = self._run(tmp_path, ["unhealthy", "unhealthy"])
        restart = [line for line in calls.splitlines() if line.endswith("--profile browser restart browser")]
        assert restart and restart[0].startswith("compose --project-name istota"), calls

    def test_a_healthy_read_resets_the_debounce(self, tmp_path):
        assert "restart" not in self._run(tmp_path, ["unhealthy", "healthy", "unhealthy"])

    def test_a_stack_that_is_not_running_is_left_alone(self, tmp_path):
        """Before the first start (an in-place migration provisions beside the
        old install) and after an operator stops it, there is nothing to watch:
        a restart would bring up a browser nobody asked for."""
        calls = self._run(tmp_path, ["missing", "missing", "missing"], stack_active=False)
        assert "restart" not in calls and " up " not in calls, calls

    def test_the_daily_restart_waits_for_a_running_stack(self, tmp_path):
        calls = self._run(tmp_path, ["healthy"], stack_active=False, daily_hour="0")
        assert "restart" not in calls, calls

    def test_it_watches_the_container_the_compose_file_names(self):
        compose = yaml.safe_load((REPO / "docker" / "docker-compose.yml").read_text())
        name = compose["services"]["browser"]["container_name"]
        assert f"ISTOTA_BROWSER_CONTAINER:-{name}" in WATCHDOG_SH.read_text()

    def test_the_timer_runs_the_check_every_minute(self):
        timer = _unit(WATCHDOG_TIMER)
        assert timer["Timer"]["OnCalendar"] == "*-*-* *:*:00"
        assert timer["Install"]["WantedBy"] == "timers.target"
        service = _unit(WATCHDOG_UNIT)
        assert service["Service"]["Type"] == "oneshot"
        assert service["Service"]["ExecStart"].endswith("istota-browser-watchdog check")


PROVISION = HOST / "provision.sh"
SCRIPTS = [
    PROVISION, HOST / "istota-stack", HOST / "istota-certbot",
]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_stage_6_scripts_parse_and_are_executable(script):
    assert os.access(script, os.X_OK), f"{script.name} is not executable"
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "set -euo pipefail" in script.read_text()


def test_the_wrapper_is_posix_sh():
    wrapper = HOST / "istota"
    assert os.access(wrapper, os.X_OK)
    result = subprocess.run(["sh", "-n", str(wrapper)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "exec istota" in wrapper.read_text()


class TestTheProxiedListenerRule:
    """INGRESS=proxied: the published port is reachable from UPSTREAM_PROXY only."""

    def _run(self, tmp_path: Path, env: dict) -> list[list[str]]:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        calls = tmp_path / "calls"
        _stub(bindir, "iptables",
              f'echo "$@" >> {calls}\ncase " $* " in *" -C "*) exit 1 ;; esac\nexit 0\n')
        run_env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", **env)
        result = subprocess.run(["bash", str(EGRESS_SH)], capture_output=True,
                                text=True, env=run_env, timeout=30)
        assert result.returncode == 0, result.stderr
        return [line.split() for line in calls.read_text().splitlines()]

    def _proxied(self, calls):
        return [c for c in calls if "-I" in c and "--ctorigdstport" in c]

    def test_upstreams_return_before_the_drop(self, tmp_path):
        calls = self._run(tmp_path, {
            "INGRESS": "proxied", "LISTEN_ADDR": "10.0.0.5", "LISTEN_PORT": "8080",
            "UPSTREAM_PROXY": "10.0.0.2,10.0.0.3",
        })
        rules = self._proxied(calls)
        # Each is inserted at position 1, so the last inserted is evaluated first:
        # the drop goes in first and both upstreams' RETURNs land ahead of it.
        assert rules[0][-2:] == ["-j", "DROP"] and "-s" not in rules[0]
        returns = rules[1:]
        assert {c[c.index("-s") + 1] for c in returns} == {"10.0.0.2", "10.0.0.3"}
        assert all(c[-2:] == ["-j", "RETURN"] for c in returns)
        for rule in rules:
            assert rule[rule.index("--ctorigdst") + 1] == "10.0.0.5"
            assert rule[rule.index("--ctorigdstport") + 1] == "8080"

    def test_no_rule_outside_proxied_mode(self, tmp_path):
        assert self._proxied(self._run(tmp_path, {"INGRESS": "direct"})) == []

    def test_proxied_without_an_upstream_fails(self, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        _stub(bindir, "iptables", "exit 1\n")
        result = subprocess.run(
            ["bash", str(EGRESS_SH)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                     INGRESS="proxied", LISTEN_ADDR="10.0.0.5", UPSTREAM_PROXY=""),
        )
        assert result.returncode != 0


class TestProvision:
    """What provision.sh installs, read off the script: running it is the VM's."""

    TEXT = PROVISION.read_text() if PROVISION.exists() else ""

    def test_it_carries_the_host_bounds(self):
        """The values the bare-metal role set, ported when the role went."""
        for line in ("SystemMaxUse=500M", "SystemMaxFileSize=50M", "num_logs = 5",
                     "max_log_file = 6", "max_log_file_action = ROTATE",
                     "zram-size = ram / 2", "compression-algorithm = zstd",
                     "swap-priority = 100", "kernel.unprivileged_userns_clone = 1"):
            assert line in self.TEXT, line

    def test_docker_keeps_the_client_address(self):
        assert '"userland-proxy": False' in self.TEXT

    def test_it_installs_every_host_file(self):
        for name in ("istota-stack.service", "istota-devbox-egress.service",
                     "istota-browser-watchdog.service", "istota-browser-watchdog.timer",
                     "istota-certbot.service", "istota-certbot.timer", "mount-nextcloud.service"):
            assert (HOST / name).is_file(), name
            assert name in self.TEXT, name
        for script in ("istota-stack", "istota-devbox-egress.sh", "istota-browser-watchdog.sh",
                       "istota-certbot", '"${HERE}/istota"'):
            assert script in self.TEXT, script

    def test_the_apparmor_profile_is_loaded_before_the_stack(self):
        assert "apparmor_parser -r /etc/apparmor.d/istota" in self.TEXT
        stack = _unit(HOST / "istota-stack.service")
        assert "apparmor.service" in stack["Unit"]["After"]
        assert "istota-devbox-egress.service" in stack["Unit"]["After"]

    def test_the_stack_requires_the_mount_in_nextcloud_mode(self):
        assert "Requires=mount-nextcloud.service\nAfter=mount-nextcloud.service" in self.TEXT
        mount = _unit(HOST / "mount-nextcloud.service")
        assert mount["Service"]["Type"] == "notify"
        assert mount["Service"]["User"] == "10001"
        assert "--config /srv/app/istota/rclone.conf" in mount["Service"]["ExecStart"]

    def test_the_vfs_cache_is_somewhere_the_mount_user_can_write(self):
        """The user's home is /srv/app/istota, root's; rclone's default cache there is
        refused and the cache silently disabled (the vm tier found it)."""
        mount = _unit(HOST / "mount-nextcloud.service")
        assert mount["Service"]["CacheDirectory"] == "istota-rclone"
        assert "--cache-dir /var/cache/istota-rclone" in mount["Service"]["ExecStart"]



class TestUpstreamRclone:
    """The mount runs rclone.org's own build, pinned, never Debian's package (#694).

    Debian links rclone against its own 2016 bazil.org/fuse snapshot, whose
    error path turns any plain errno into EIO, so every xattr call on a file
    in the workspace fails with an I/O error and `shutil.copy2` out of it
    breaks. rclone.org's builds use the 2023 fuse rclone pins, which passes
    the errno through and lets the kernel mark the mount xattr-less."""

    TEXT = PROVISION.read_text() if PROVISION.exists() else ""

    def _run(self, tmp_path, *, installed="rclone v1.60.1-DEV", arch="amd64",
             payload=b"an upstream rclone .deb", sha=None, on_path=None):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        calls = tmp_path / "calls"
        calls.touch()
        source = tmp_path / "payload"
        source.write_bytes(payload)
        # The package's binary lives apart from PATH, as /usr/bin/rclone does
        # behind /usr/local/bin.
        pkgdir = tmp_path / "pkg"
        pkgdir.mkdir()
        package_bin = pkgdir / "rclone"
        if installed is None:
            _stub(pkgdir, "rclone", "exit 127\n")
        else:
            _stub(pkgdir, "rclone", f'[ "$1" = version ] && printf "%s\\n" "{installed}" "- os/version: debian"\n')
        if on_path is not None:
            _stub(bindir, "rclone", f'[ "$1" = version ] && printf "%s\\n" "{on_path}"\n')
        _stub(bindir, "curl", (
            f'echo "curl $*" >> {calls}\n'
            'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
            f'cp {source} "$out"\n'
        ))
        _stub(bindir, "dpkg", (
            f'[ "$1" = --print-architecture ] && {{ echo {arch}; exit 0; }}\n'
            f'echo "dpkg $*" >> {calls}\n'
        ))
        _stub(bindir, "apt-mark", f'echo "apt-mark $*" >> {calls}\n')
        function = re.search(r"^install_rclone\(\) \{.*?^\}", self.TEXT, re.M | re.S)
        assert function, "provision.sh defines install_rclone"
        pinned = re.search(r"^RCLONE_VERSION=(\S+)$", self.TEXT, re.M)
        assert pinned, "provision.sh pins RCLONE_VERSION"
        if sha is None:
            sha = hashlib.sha256(payload).hexdigest()
        script = (
            'set -euo pipefail\n'
            'log() { echo "$*"; }\ndie() { echo "$*" >&2; exit 1; }\n'
            f'RCLONE_VERSION={pinned.group(1)}\n'
            f'RCLONE_SHA256_AMD64={sha}\nRCLONE_SHA256_ARM64={sha}\n'
            f'RCLONE_BIN={package_bin}\n'
            f'{function.group(0)}\n'
            'install_rclone\necho done\n'
        )
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=30,
            env={"PATH": f"{bindir}:{os.environ['PATH']}", "TMPDIR": str(tmp_path)},
        )
        return result, calls.read_text().splitlines(), pinned.group(1)

    def test_debian_rclone_is_never_installed(self):
        assert not re.search(r"apt_install[^\n]*\brclone\b", self.TEXT)
        assert re.search(r"^\s*apt_install fuse3$", self.TEXT, re.M)
        assert "install_rclone" in self.TEXT.split('if [ "$(host_env STORAGE)" = "nextcloud" ]; then', 1)[1]

    def test_the_pin_carries_a_digest_per_architecture(self):
        for arch in ("AMD64", "ARM64"):
            assert re.search(rf"^RCLONE_SHA256_{arch}=[0-9a-f]{{64}}$", self.TEXT, re.M), arch
        assert re.search(r"^RCLONE_VERSION=\d+\.\d+\.\d+$", self.TEXT, re.M)

    def test_debians_build_is_replaced_and_held(self, tmp_path):
        result, calls, version = self._run(tmp_path)
        assert result.returncode == 0, result.stderr
        # Where mktemp puts the file is the platform's business: macOS's ignores TMPDIR.
        assert (calls[0].startswith("curl -fsSL -o /") and "/rclone.deb " in calls[0]
                and calls[0].endswith(f"https://downloads.rclone.org/v{version}/"
                                      f"rclone-v{version}-linux-amd64.deb"))
        assert calls[1].startswith("dpkg -i ") and calls[1].endswith("rclone.deb")
        assert calls[2] == "apt-mark hold rclone"

    def test_a_missing_rclone_is_installed(self, tmp_path):
        result, calls, _ = self._run(tmp_path, installed=None, arch="arm64")
        assert result.returncode == 0, result.stderr
        assert calls[0].endswith("-linux-arm64.deb")
        assert any(c.startswith("dpkg -i ") for c in calls)

    def test_a_wrong_digest_installs_nothing(self, tmp_path):
        result, calls, _ = self._run(tmp_path, sha="0" * 64)
        assert result.returncode != 0
        assert not any(c.startswith(("dpkg", "apt-mark")) for c in calls)
        assert "done" not in result.stdout

    def test_the_pinned_build_is_left_alone_but_held(self, tmp_path):
        """A run killed between `dpkg -i` and the hold is finished by the next."""
        pinned = re.search(r"^RCLONE_VERSION=(\S+)$", self.TEXT, re.M).group(1)
        result, calls, _ = self._run(tmp_path, installed=f"rclone v{pinned}")
        assert result.returncode == 0, result.stderr
        assert calls == ["apt-mark hold rclone"]

    def test_the_package_binary_is_asked_not_the_one_on_path(self, tmp_path):
        """A host moved from the Ansible role keeps its rclone in /usr/local/bin,
        ahead on PATH. Asking that one reinstalled the package on every run."""
        pinned = re.search(r"^RCLONE_VERSION=(\S+)$", self.TEXT, re.M).group(1)
        result, calls, _ = self._run(tmp_path, installed=f"rclone v{pinned}", on_path="rclone v1.73.0")
        assert result.returncode == 0, result.stderr
        assert calls == ["apt-mark hold rclone"]

    def test_an_unpinned_architecture_is_refused(self, tmp_path):
        result, calls, _ = self._run(tmp_path, arch="riscv64")
        assert result.returncode != 0
        assert calls == []

    def test_a_replaced_binary_restarts_the_mount(self):
        """A running mount keeps the binary it started with, so the fix would
        wait for a reboot. Decided from the running process, not from this
        run, so a re-run after one that died before the restart still does it."""
        assert re.search(
            r'if \[ -n "\$mount_unit_changed" \] \|\| mount_runs_stale_rclone; then\n'
            r'.*\n\s*systemctl restart mount-nextcloud.service', self.TEXT)

    def _stale(self, tmp_path, pid, exe):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        _stub(bindir, "systemctl", f'echo {pid}\n')
        _stub(bindir, "readlink", f'[ "$1" = /proc/{pid}/exe ] || exit 1\n'
              + (f'echo "{exe}"\n' if exe is not None else 'exit 1\n'))
        function = re.search(r"^mount_runs_stale_rclone\(\) \{.*?^\}", self.TEXT, re.M | re.S)
        assert function, "provision.sh defines mount_runs_stale_rclone"
        result = subprocess.run(
            ["bash", "-c", f"set -euo pipefail\nRCLONE_BIN=/usr/bin/rclone\n{function.group(0)}\nmount_runs_stale_rclone"],
            env={"PATH": f"{bindir}:{os.environ['PATH']}"}, capture_output=True, text=True,
            timeout=30,
        )
        assert result.returncode in (0, 1), result.stderr
        return result.returncode == 0

    def test_a_mount_on_the_replaced_binary_is_stale(self, tmp_path):
        assert self._stale(tmp_path, 4242, "/usr/bin/rclone (deleted)")

    def test_a_mount_on_the_current_binary_is_not(self, tmp_path):
        assert not self._stale(tmp_path, 4242, "/usr/bin/rclone")

    def test_a_stopped_mount_is_not(self, tmp_path):
        assert not self._stale(tmp_path, 0, None)

    def test_a_vanished_process_is_not(self, tmp_path):
        assert not self._stale(tmp_path, 4242, None)


ROLE_MOUNT_UNIT = """[Unit]
Description=Nextcloud rclone FUSE mount

[Service]
User=assistant
ExecStart=/usr/bin/rclone mount nextcloud: /srv/mount/nextcloud/content
"""


class TestABareMetalInstallOnTheSameHost:
    """An in-place migration provisions the host the old role converged, while
    that install still runs. Its mount-nextcloud.service shares our unit's name
    and path, so provision.sh leaves it alone until the export has disabled it."""

    TEXT = PROVISION.read_text() if PROVISION.exists() else ""

    def _live(self, tmp_path, unit_text, enabled, active):
        units = tmp_path / "units"
        units.mkdir()
        if unit_text is not None:
            (units / "mount-nextcloud.service").write_text(unit_text)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        _stub(bindir, "systemctl", (
            f'case "$1" in is-enabled) exit {0 if enabled else 1} ;;'
            f' is-active) exit {0 if active else 1} ;; esac\nexit 1\n'
        ))
        function = re.search(r"^bare_metal_mount_live\(\) \{.*?^\}", self.TEXT, re.M | re.S)
        assert function, "provision.sh defines bare_metal_mount_live"
        result = subprocess.run(
            ["bash", "-c", f'{function.group(0)}\nbare_metal_mount_live'],
            env={"PATH": f"{bindir}:{os.environ['PATH']}", "UNITS": str(units),
                 "ISTOTA_UID": "10001"},
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode in (0, 1), result.stderr
        return result.returncode == 0

    def test_an_enabled_role_mount_is_live(self, tmp_path):
        assert self._live(tmp_path, ROLE_MOUNT_UNIT, enabled=True, active=False)

    def test_a_running_role_mount_is_live(self, tmp_path):
        assert self._live(tmp_path, ROLE_MOUNT_UNIT, enabled=False, active=True)

    def test_the_role_mount_the_export_disabled_is_taken_over(self, tmp_path):
        assert not self._live(tmp_path, ROLE_MOUNT_UNIT, enabled=False, active=False)

    def test_our_own_mount_is_never_the_old_install(self, tmp_path):
        ours = (HOST / "mount-nextcloud.service").read_text()
        assert not self._live(tmp_path, ours, enabled=True, active=True)

    def test_no_mount_unit_is_nothing_to_leave_alone(self, tmp_path):
        assert not self._live(tmp_path, None, enabled=True, active=True)

    def test_every_change_to_the_mount_waits_for_the_export(self):
        """Installing the unit, creating or chowning the mountpoint, and enabling,
        restarting or disabling the unit each pull the workspace from under the
        old daemon. Each sits behind the one flag."""
        assert 'bare_metal_mount_live && bare_metal_mount=1' in self.TEXT
        assert ('if [ "$unit" = mount-nextcloud.service ] && [ -n "$bare_metal_mount" ]; then'
                in self.TEXT)
        assert '[ -n "$bare_metal_mount" ] || install -d -m 0755 "$MOUNT_DIR"' in self.TEXT
        nextcloud = self.TEXT.split('if [ "$(host_env STORAGE)" = "nextcloud" ]; then', 1)[1]
        nextcloud = nextcloud.split("# --- Firewall", 1)[0]
        guarded = nextcloud.split('if [ -n "$bare_metal_mount" ]; then', 1)
        assert len(guarded) == 2, "the nextcloud branch has a bare-metal arm"
        before, after = guarded
        for change in ('chown "$ISTOTA_UID:$ISTOTA_UID" "$MOUNT_DIR"',
                       "systemctl enable --now mount-nextcloud.service",
                       "systemctl restart mount-nextcloud.service",
                       "systemctl disable --now mount-nextcloud.service"):
            assert change not in before, change
        assert ('[ -n "$bare_metal_mount" ] || systemctl disable --now mount-nextcloud.service'
                in after)

    def test_the_mount_user_can_reach_the_mountpoint(self):
        """The role made /srv/mount/nextcloud 0770 root:nextcloud-mount; uid 10001 is
        in no such group, and fusermount refuses a mountpoint its user cannot reach."""
        assert 'chmod o+x "$(dirname "$MOUNT_DIR")"' in self.TEXT


class TestTheSelfSignedCertificate:
    """Proxied mode with TLS_CERT_SOURCE=self-signed: a certificate the reverse
    proxy pins, made once on the VM and never replaced, since a new one breaks
    the pin."""

    TEXT = PROVISION.read_text() if PROVISION.exists() else ""

    def _run(self, certs, domain="istota.example.com", addr="10.0.0.5"):
        function = re.search(r"^ensure_self_signed_cert\(\) \{.*?^\}", self.TEXT, re.M | re.S)
        assert function, "provision.sh defines ensure_self_signed_cert"
        return subprocess.run(
            ["bash", "-c", f'log() {{ echo "$*"; }}\n{function.group(0)}\n'
                           f'ensure_self_signed_cert "{certs}" "{domain}" "{addr}"'],
            capture_output=True, text=True, timeout=60,
        )

    def _x509(self, cert, *args):
        return subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", *args],
                              capture_output=True, text=True, check=True).stdout

    @pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
    def test_it_names_the_host_and_the_listen_address_and_keeps_the_key_private(self, tmp_path):
        result = self._run(tmp_path)
        assert result.returncode == 0, result.stderr
        cert, key = tmp_path / "fullchain.pem", tmp_path / "privkey.pem"
        names = self._x509(cert, "-ext", "subjectAltName")
        assert "DNS:istota.example.com" in names and "IP Address:10.0.0.5" in names
        assert key.stat().st_mode & 0o777 == 0o600
        fingerprint = self._x509(cert, "-fingerprint", "-sha256").split("=", 1)[1].strip()
        assert fingerprint in result.stdout

    @pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
    def test_an_existing_certificate_is_never_replaced(self, tmp_path):
        assert self._run(tmp_path).returncode == 0
        before = (tmp_path / "fullchain.pem").read_bytes()
        assert self._run(tmp_path, domain="other.example.com").returncode == 0
        assert (tmp_path / "fullchain.pem").read_bytes() == before

    def test_provision_makes_it_only_for_proxied_self_signed(self):
        assert 'if [ "$(host_env INGRESS)" = "proxied" ] && [ "$(host_env TLS_CERT_SOURCE)" = "self-signed" ]; then' in self.TEXT


STACK_DIR = "/srv/app/istota"
MOUNT_DIR = "/srv/mount/nextcloud/content"


class TestTheHostLayout:
    """The estate's layout: the stack under /srv/app/istota, and the Nextcloud
    mount at the path the bare-metal installs already use, outside the stack."""

    def test_provision_defaults_to_the_estate_paths(self):
        text = PROVISION.read_text()
        assert f'STACK="${{ISTOTA_STACK_DIR:-{STACK_DIR}}}"' in text
        assert f'MOUNT_DIR="${{ISTOTA_MOUNT_DIR:-{MOUNT_DIR}}}"' in text
        assert '"${STACK}/mount"' not in text

    def test_istota_stack_defaults_to_the_estate_paths(self):
        result = subprocess.run(
            ["bash", "-c", f'source "{HOST / "istota-stack"}"; echo "$STACK"; echo "$MOUNT_DIR"'],
            capture_output=True, text=True, timeout=30,
            env={"PATH": os.environ["PATH"]},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [STACK_DIR, MOUNT_DIR]

    def test_the_mount_unit_mounts_the_estate_path(self):
        service = _unit(HOST / "mount-nextcloud.service")["Service"]
        assert service["ExecStartPre"] == f"-/bin/fusermount3 -uz {MOUNT_DIR}"
        assert service["ExecStart"].split()[-1] == MOUNT_DIR
        assert service["ExecStop"] == f"/bin/fusermount3 -u {MOUNT_DIR}"
        assert f"--config {STACK_DIR}/rclone.conf" in service["ExecStart"]

    @pytest.mark.parametrize("unit", sorted(p.name for p in HOST.glob("*.service")))
    def test_every_unit_reads_the_stacks_host_env(self, unit):
        service = _unit(HOST / unit)["Service"]
        if "EnvironmentFile" in service:
            assert service["EnvironmentFile"] == f"-{STACK_DIR}/host.env"


def _functions_setting(tree, name: str):
    import ast

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                names.add(inner.value)
            elif isinstance(inner, ast.keyword) and inner.arg:
                names.add(inner.arg)
        if name in names:
            yield node, names


def test_no_test_runs_a_host_script_against_the_real_mount_path():
    """provision.sh and `istota-stack setup` create the mount directory, which
    is outside the stack directory. A test that points the stack at a temporary
    directory must point the mount there too, or it creates /srv/mount/... on
    the machine running the suite (or fails to, as a user). The vm tier runs on
    its own VM and wants the real paths."""
    import ast

    offenders = []
    for path in sorted((REPO / "tests").rglob("*.py")):
        if "vm" in path.relative_to(REPO / "tests").parts[:1]:
            continue
        tree = ast.parse(path.read_text())
        for func, names in _functions_setting(tree, "ISTOTA_STACK_DIR"):
            if "ISTOTA_MOUNT_DIR" not in names:
                offenders.append(f"{path.relative_to(REPO)}:{func.lineno} {func.name}")
    assert offenders == [], f"sets ISTOTA_STACK_DIR without ISTOTA_MOUNT_DIR: {offenders}"


class TestTheStackUnit:
    def test_it_runs_istota_stack(self):
        unit = _unit(HOST / "istota-stack.service")
        assert unit["Service"]["ExecStart"] == "/usr/local/sbin/istota-stack up"
        assert unit["Service"]["ExecStop"] == "/usr/local/sbin/istota-stack down"
        assert unit["Service"]["Type"] == "oneshot"
        assert "docker.service" in unit["Unit"]["Requires"]


class TestCertbot:
    def _run(self, tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        _stub(bindir, "certbot", f'printf "%s\\n" "$@" > {tmp_path / "args"}\n')
        return subprocess.run(
            ["bash", str(HOST / "istota-certbot")], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                     ISTOTA_STACK_DIR="/srv/app/istota",
                     ISTOTA_MOUNT_DIR=str(tmp_path / "mount"), **env),
        )

    def test_webroot_issuance_with_the_restart_hook(self, tmp_path):
        result = self._run(tmp_path, INGRESS="direct", TLS_CERT_SOURCE="acme",
                           DOMAIN="bot.example.com", ACME_SERVER="https://pebble:14000/dir")
        assert result.returncode == 0, result.stderr
        args = (tmp_path / "args").read_text().splitlines()
        assert args[0] == "certonly" and "--webroot" in args
        assert args[args.index("-w") + 1] == "/srv/app/istota/acme"
        assert args[args.index("--config-dir") + 1] == "/srv/app/istota/letsencrypt"
        assert args[args.index("--deploy-hook") + 1].endswith("compose restart nginx")
        assert args[args.index("--server") + 1] == "https://pebble:14000/dir"
        assert "--register-unsafely-without-email" in args

    def test_nothing_happens_outside_direct_acme(self, tmp_path):
        assert self._run(tmp_path, INGRESS="proxied", TLS_CERT_SOURCE="files").returncode == 0
        assert not (tmp_path / "args").exists()

    def test_the_timer_runs_twice_a_day(self):
        timer = _unit(HOST / "istota-certbot.timer")
        assert timer["Timer"]["OnCalendar"] == "*-*-* 00,12:00:00"


class TestBootstrapFiles:
    def test_cloud_init_verifies_the_tag_before_running_anything(self):
        doc = yaml.safe_load((HOST / "cloud-init.yaml").read_text())
        script = next(f["content"] for f in doc["write_files"] if f["path"] == "/root/istota-bootstrap.sh")
        verify = script.index("verify-tag")
        assert script.index("host/provision.sh") > verify
        assert "GIT_CONFIG_GLOBAL=/dev/null" in script
        assert doc["runcmd"] == [["bash", "/root/istota-bootstrap.sh"]]

    def test_cloud_init_verifies_the_tag_with_istota_stacks_own_code(self):
        """The bootstrap runs before any verified code exists, so it cannot
        source istota-stack from the checkout it is about to verify; it carries
        a copy instead. istota-stack's is authoritative (row 13's refusals are
        held against it in tests/test_istota_stack.py), and this holds the copy
        to it byte for byte: the hand-written one had lost the tag-name check
        (a moved ref downgrades the first provision) and the X.509 override."""
        stack = (HOST / "istota-stack").read_text()
        start = stack.index("# --- release tags ---")
        end = stack.index("# --- builds and images ---")
        section = stack[start:end].rstrip() + "\n"
        doc = yaml.safe_load((HOST / "cloud-init.yaml").read_text())
        script = next(f["content"] for f in doc["write_files"] if f["path"] == "/root/istota-bootstrap.sh")

        assert section in script
        assert 'object="$(verify_release_tag "$TAG")"' in script
        assert 'checkout --quiet --detach "${object}^{commit}"' in script
        assert script.index("verify_release_tag \"$TAG\"") < script.index("host/provision.sh")

    def test_the_root_installer_verifies_the_tag_with_istota_stacks_own_code(self):
        """`install.sh --server` is the same bootstrap run by hand, so it
        carries the same copy, held the same way, and runs nothing from the
        checkout before the tag is verified."""
        stack = (HOST / "istota-stack").read_text()
        start = stack.index("# --- release tags ---")
        end = stack.index("# --- builds and images ---")
        section = stack[start:end].rstrip() + "\n"
        script = (REPO / "install.sh").read_text()
        run_server = script[script.index("run_server() {"):]

        assert section in script
        verify = run_server.index('object="$(verify_release_tag "$RELEASE_TAG")"')
        checkout = run_server.index('checkout --quiet --detach "${object}^{commit}"')
        assert verify < checkout < run_server.index("host/provision.sh")
        assert run_server.index("host/provision.sh") < run_server.index("istota-stack update")
        # The first build ran before config.toml existed: a config that needs
        # Claude Code gets its rebuild after the wizard, before the unit starts.
        setup = run_server.index("    istota-stack setup\n")
        check = run_server.index("istota-stack check-image || image_rc=$?")
        assert setup < check < run_server.index("systemctl enable --now istota-stack\n", check)
        assert run_server.index('istota-stack update "$RELEASE_TAG"', check) < run_server.index(
            "systemctl enable --now istota-stack\n", check)

    def test_the_root_installer_has_no_bare_metal_or_docker_arm(self):
        """The old --bare arm ran the deleted deploy/install.sh; --docker printed
        hand steps the stack replaced. Both are refused by name now."""
        for flag in ("--bare", "--docker"):
            result = subprocess.run(
                ["bash", str(REPO / "install.sh"), flag],
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            )
            assert result.returncode == 1, (flag, result.stdout, result.stderr)
            assert "--server" in result.stderr
        assert "deploy/install.sh" not in (REPO / "install.sh").read_text()

    def test_the_lima_template_provisions_from_the_checkout(self):
        doc = yaml.safe_load((HOST / "lima" / "istota.yaml").read_text())
        # The image alone: `template:debian-13` adds a mount of the whole Mac home
        # (parity row 7; the vm tier found it).
        assert doc["base"] == ["template:_images/debian-13"]
        assert [m["location"] for m in doc["mounts"]] == ["{{.Param.ISTOTA_REPO}}"]
        assert doc["containerd"] == {"system": False, "user": False}
        assert doc["vmOpts"]["vz"]["rosetta"]["enabled"] is True
        assert doc["mounts"][0]["writable"] is False
        assert "/mnt/istota-repo/host/provision.sh" in doc["provision"][0]["script"]


class TestTheProvisionDigest:
    """`provision.sh --digest` names the inputs a run acts on, and a successful
    run records it, so the management role can tell a provision a dead play
    left undone from one already done. Anything that changes what provision.sh
    installs or enables changes the digest; the deployed tag in .env alone
    does not (the checkout's HEAD is what carries a release)."""

    @pytest.fixture
    def stack(self, tmp_path):
        stack = tmp_path / "stack"
        (stack / "src" / ".git").mkdir(parents=True)
        (stack / "src" / ".git" / "HEAD").write_text("1" * 40 + "\n")
        (stack / "host.env").write_text("STORAGE=local\nINGRESS=local\n")
        (stack / ".env").write_text("COMPOSE_PROFILES=\nISTOTA_TAG=v1\n")
        return stack

    def _digest(self, stack: Path) -> str:
        result = subprocess.run(
            ["bash", str(PROVISION), "--digest"], capture_output=True, text=True, timeout=30,
            env={"PATH": os.environ["PATH"], "ISTOTA_STACK_DIR": str(stack),
                 "ISTOTA_MOUNT_DIR": str(stack / "mount")},
        )
        assert result.returncode == 0, result.stderr
        digest = result.stdout.strip()
        assert re.fullmatch(r"[0-9a-f]{64}", digest), result.stdout
        return digest

    def test_it_is_stable(self, stack):
        assert self._digest(stack) == self._digest(stack)

    @pytest.mark.parametrize("change", [
        lambda s: (s / "host.env").write_text("STORAGE=nextcloud\nINGRESS=local\n"),
        lambda s: (s / "rclone.conf").write_text("[nextcloud]\ntype = webdav\n"),
        lambda s: (s / ".env").write_text("COMPOSE_PROFILES=browser\nISTOTA_TAG=v1\n"),
        lambda s: (s / "src" / ".git" / "HEAD").write_text("2" * 40 + "\n"),
    ], ids=["host.env", "rclone.conf", "compose-profiles", "release"])
    def test_an_input_change_changes_it(self, stack, change):
        before = self._digest(stack)
        change(stack)
        assert self._digest(stack) != before

    def test_the_tag_line_alone_does_not(self, stack):
        before = self._digest(stack)
        (stack / ".env").write_text("COMPOSE_PROFILES=\nISTOTA_TAG=v2\nISTOTA_PREVIOUS_TAG=v1\n")
        assert self._digest(stack) == before

    def test_a_successful_run_records_it_last(self):
        """Read before anything runs and recorded at the very end, so a run
        that fails partway leaves no record and the next play provisions
        again."""
        text = PROVISION.read_text()
        assert 'digest="$(inputs_digest)"' in text
        assert text.index('digest="$(inputs_digest)"') < text.index("apt-get update")
        assert text.rindex('"${STACK}/.provisioned"') > text.index("# --- The stack ---")

    def test_a_run_removes_the_old_record_before_it_acts(self):
        """ISSUE-710: a run that died after installing the new checkout's host
        files left the previous release's digest, so a rollback to that release
        read it as provisioned and kept the newer scripts and units."""
        text = PROVISION.read_text()
        removal = text.index('rm -f "${STACK}/.provisioned"')
        assert text.index('digest="$(inputs_digest)"') < removal < text.index("apt-get update")
