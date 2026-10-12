"""`security.devbox_netfilter` — can a devbox reach what the VM's egress rules drop?

The rules live in the VM's DOCKER-USER chain (`host/istota-devbox-egress.sh`,
held by `tests/test_host_units.py`), which the daemon's container cannot read.
So the check asks each devbox in `[devbox] users` to connect out through the
exec transport, and a connection that opens is a FAIL. The vm tier's row 10
witness runs it against a target that answers.

The bare-metal arm that read `iptables -S DOCKER-USER` against the old role's
boot script went with the role: no install has that script any more.
"""

from __future__ import annotations

import json

import pytest

from istota import doctor
from istota.config import Config, ContainerConfig, DeveloperConfig, DevboxConfig
from istota.doctor import CHECKS, CHECK_SCOPES, DEPLOYMENT, FAIL, OK, SKIP, WARN

CHECK_NAME = "security.devbox_netfilter"


@pytest.fixture
def config(tmp_path):
    config = Config()
    config.developer = DeveloperConfig(
        enabled=True, repos_dir=str(tmp_path / "repos"),
        container=ContainerConfig(exec_socket_dir=str(tmp_path / "exec")),
    )
    config.devbox = DevboxConfig(enabled=True, users=["alice", "bob"])
    return config


def _answer(monkeypatch, exit_codes: dict[str, int] | None = None, error: str = ""):
    asked: list[tuple[str, list[str]]] = []

    def fake(socket_path, payload, timeout):
        request = json.loads(payload)
        asked.append((str(socket_path), request["argv"]))
        if error:
            return [], error
        target = request["argv"][-1].rsplit("/", 2)[-2]
        return [{"type": "exit", "exit_code": (exit_codes or {}).get(target, 124)}], ""

    monkeypatch.setattr(doctor, "_exec_transport_request", fake)
    monkeypatch.setattr(doctor, "_run", lambda *a, **k: pytest.fail("a host command was run"))
    return asked


class TestTheGate:
    def test_devbox_disabled_skips_and_asks_nobody(self, config, monkeypatch):
        asked = _answer(monkeypatch)
        config.devbox = DevboxConfig(enabled=False, users=["alice"])
        result = doctor.check_devbox_netfilter(config, probe=True)
        assert result.status == SKIP
        assert not asked

    def test_no_devbox_users_skips_and_asks_nobody(self, config, monkeypatch):
        asked = _answer(monkeypatch)
        config.devbox = DevboxConfig(enabled=True, users=[])
        result = doctor.check_devbox_netfilter(config, probe=True)
        assert result.status == SKIP
        assert "[devbox] users" in result.detail
        assert not asked

    def test_builds_cannot_route_into_a_devbox_this_gate_calls_disabled(self, make_config, tmp_path):
        """The check's gate is `[devbox] enabled` alone. That is only safe while
        the container backend is derived from the same switch, so there is no
        configuration where the check skips and builds still land in a devbox."""
        from istota.config import devbox_container_backend

        repos = tmp_path / "repos"
        repos.mkdir()

        def _config(devbox: bool):
            return make_config(
                developer=DeveloperConfig(enabled=True, repos_dir=str(repos), container=ContainerConfig()),
                devbox=DevboxConfig(enabled=devbox),
            )

        assert devbox_container_backend(_config(False)) is False
        assert devbox_container_backend(_config(True)) is True


class TestTheProbe:
    def test_nothing_reachable_is_ok(self, config, monkeypatch):
        asked = _answer(monkeypatch)
        result = doctor.check_devbox_netfilter(config, probe=True)
        assert result.status == OK, result.detail
        users = {path.split("/")[-2] for path, _ in asked}
        assert users == {"alice", "bob"}
        targets = {argv[-1] for _, argv in asked}
        assert targets == {f"exec 3<>/dev/tcp/{h}/{p}" for h, p in doctor.DEVBOX_EGRESS_TARGETS}

    def test_a_connection_that_opens_is_a_fail(self, config, monkeypatch):
        _answer(monkeypatch, {"169.254.169.254": 0})
        result = doctor.check_devbox_netfilter(config, probe=True)
        assert result.status == FAIL
        assert "169.254.169.254:80" in result.detail
        assert "istota-devbox-egress" in result.remedy

    def test_an_unreachable_devbox_is_a_warn_not_an_ok(self, config, monkeypatch):
        _answer(monkeypatch, error="could not connect")
        assert doctor.check_devbox_netfilter(config, probe=True).status == WARN

    def test_no_probe_no_spawn(self, config, monkeypatch):
        monkeypatch.setattr(doctor, "_exec_transport_request", lambda *a: pytest.fail("probed"))
        assert doctor.check_devbox_netfilter(config, probe=False).status == SKIP


class TestItIsWiredIn:
    def test_it_is_registered(self):
        assert CHECK_NAME in dict(CHECKS)

    def test_it_is_deployment_scoped(self):
        """It asks a running devbox — nothing an image can answer."""
        assert CHECK_SCOPES[CHECK_NAME] == DEPLOYMENT

    def test_it_is_not_a_deep_check(self):
        """Deep means "spawns a namespace". This asks the devboxes over a
        socket, so it belongs in the default run."""
        assert CHECK_NAME not in doctor.DEEP_CHECKS
