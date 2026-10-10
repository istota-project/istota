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
import os
import re
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
ROLE_EGRESS = REPO / "deploy" / "ansible" / "templates" / "istota-devbox-iptables.sh.j2"

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

    def test_it_asks_for_what_the_role_asks_for(self):
        role = set(re.findall(r'ensure_drop "([^"]+)"', ROLE_EGRESS.read_text()))
        assert role == WANTED

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
        assert unit["Service"]["EnvironmentFile"] == "-/srv/istota/host.env"
        assert unit["Install"]["WantedBy"] == "multi-user.target"


class TestTheBrowserWatchdog:
    def _run(self, tmp_path: Path, statuses: list[str]) -> str:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        calls = tmp_path / "docker-calls"
        status = tmp_path / "status"
        _stub(bindir, "docker",
              f'echo "$@" >> {calls}\n'
              f'if [ "$1" = inspect ]; then cat {status}; fi\nexit 0\n')
        _stub(bindir, "curl", "exit 0\n")
        stack = tmp_path / "stack"
        stack.mkdir()
        env = dict(
            os.environ,
            PATH=f"{bindir}:{os.environ['PATH']}",
            ISTOTA_STACK_DIR=str(stack),
            ISTOTA_STACK_BIN=str(HOST / "istota-stack"),
            ISTOTA_BROWSER_WATCHDOG_STATE_DIR=str(tmp_path / "state"),
            ISTOTA_BROWSER_WATCHDOG_LOG=str(tmp_path / "log" / "health.log"),
            # Never due inside the test, whatever the clock says.
            ISTOTA_BROWSER_DAILY_RESTART_HOUR="99",
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

    def test_it_ports_the_roles_host_values(self):
        defaults = (REPO / "deploy" / "ansible" / "defaults" / "main.yml").read_text()
        for key, value in {
            "istota_journald_max_use": "500M", "istota_journald_max_file_size": "50M",
            "istota_auditd_num_logs": "5", "istota_auditd_max_log_file": "6",
            "istota_zram_size": "ram / 2", "istota_zram_algorithm": "zstd",
            "istota_zram_priority": "100",
        }.items():
            assert re.search(rf"^{key}: \"?{re.escape(value)}\"?", defaults, re.M), key
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
        assert "--config /srv/istota/rclone.conf" in mount["Service"]["ExecStart"]


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
                     ISTOTA_STACK_DIR="/srv/istota", **env),
        )

    def test_webroot_issuance_with_the_restart_hook(self, tmp_path):
        result = self._run(tmp_path, INGRESS="direct", TLS_CERT_SOURCE="acme",
                           DOMAIN="bot.example.com", ACME_SERVER="https://pebble:14000/dir")
        assert result.returncode == 0, result.stderr
        args = (tmp_path / "args").read_text().splitlines()
        assert args[0] == "certonly" and "--webroot" in args
        assert args[args.index("-w") + 1] == "/srv/istota/acme"
        assert args[args.index("--config-dir") + 1] == "/srv/istota/letsencrypt"
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

    def test_the_lima_template_provisions_from_the_checkout(self):
        doc = yaml.safe_load((HOST / "lima" / "istota.yaml").read_text())
        assert doc["base"] == ["template:debian-13"]
        assert doc["containerd"] == {"system": False, "user": False}
        assert doc["vmOpts"]["vz"]["rosetta"]["enabled"] is True
        assert doc["mounts"][0]["writable"] is False
        assert "/mnt/istota-repo/host/provision.sh" in doc["provision"][0]["script"]
