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
        assert "compose --profile browser restart browser" in calls

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
