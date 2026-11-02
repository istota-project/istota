"""The testbed's networks take no address pool (ISSUE-919).

A stack takes two compose networks and a default Docker daemon's pools hold
about thirty, so a whole-tier session of a dozen profiles ran out before its
last tests. `docker compose config` is the parser here, as in
`tests/test_smoke_tier.py`; the run against a real daemon is
`tests/smoke/test_stack_subnets.py`.
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from testbed import profiles
from testbed import stack as stack_support
from tests.smoke.test_web_setup_boot import SETUP_OVERLAY

REPO = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO / "docker" / "docker-compose.yml"

#: What a Docker daemon hands out when `default-address-pools` is unset.
DOCKER_DEFAULT_POOLS = (
    *(ipaddress.ip_network(f"172.{octet}.0.0/16") for octet in range(17, 32)),
    ipaddress.ip_network("192.168.0.0/16"),
)
OVERLAP_ERROR = (
    "failed to create network p_default: Error response from daemon: "
    "invalid pool request: Pool overlaps with other one on this address space"
)


def _merged_networks(tmp_path: Path, overlay: Path) -> tuple[dict, dict[str, str]]:
    if shutil.which("docker") is None:
        pytest.skip("the docker CLI is not installed")
    environment = stack_support.lean_env(
        {}, image="istota-test/lean:unit", config_dir=tmp_path, secrets_dir=tmp_path / "secrets",
    )
    stack_support.write_secrets(tmp_path / "secrets", {})
    env_file = stack_support.write_env_file(tmp_path / "compose.env", environment)
    args = stack_support.compose_args(COMPOSE_FILE, project="p", env_file=env_file, overlays=[overlay])
    result = subprocess.run(
        args + ["config", "--format", "json"], capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(Path.home())},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["networks"], environment


def _subnet(network: dict) -> str:
    return network["ipam"]["config"][0]["subnet"]


class TestTheComposeFiles:
    def test_a_stacks_networks_are_pinned_to_its_env_file(self, tmp_path):
        networks, environment = _merged_networks(tmp_path, stack_support.TESTBED_OVERLAY)

        assert _subnet(networks["default"]) == environment["ISTOTA_TESTBED_DEFAULT_SUBNET"]
        assert _subnet(networks["browser"]) == environment["ISTOTA_TESTBED_BROWSER_SUBNET"]

    def test_the_web_setup_run_pins_the_same_networks(self, tmp_path):
        """That run and the stack after it share a project, so a network the run
        left unpinned would take a pool and differ from what the stack asks for."""
        overlay = tmp_path / "setup.yml"
        overlay.write_text(SETUP_OVERLAY)

        networks, environment = _merged_networks(tmp_path, overlay)

        assert _subnet(networks["default"]) == environment["ISTOTA_TESTBED_DEFAULT_SUBNET"]
        assert _subnet(networks["browser"]) == environment["ISTOTA_TESTBED_BROWSER_SUBNET"]


class TestTheRange:
    def test_it_is_outside_what_docker_hands_out(self):
        for pool in DOCKER_DEFAULT_POOLS:
            assert not stack_support.TESTBED_SUBNET_RANGE.overlaps(pool), pool

    def test_a_stack_gets_two_different_subnets_from_it(self):
        environment = stack_support.network_env()

        default = ipaddress.ip_network(environment["ISTOTA_TESTBED_DEFAULT_SUBNET"])
        browser = ipaddress.ip_network(environment["ISTOTA_TESTBED_BROWSER_SUBNET"])
        assert default != browser
        for subnet in (default, browser):
            assert subnet.subnet_of(stack_support.TESTBED_SUBNET_RANGE)
            assert subnet.prefixlen == stack_support.TESTBED_SUBNET_PREFIX

    def test_a_subnet_in_use_is_not_picked(self):
        slots = list(stack_support.TESTBED_SUBNET_RANGE.subnets(new_prefix=stack_support.TESTBED_SUBNET_PREFIX))
        free = slots[10:12]
        taken = [slot for slot in slots if slot not in free]

        environment = stack_support.network_env(taken)

        assert set(environment.values()) == {str(slot) for slot in free}

    def test_a_network_wider_than_a_slot_rules_out_what_it_covers(self):
        slots = list(stack_support.TESTBED_SUBNET_RANGE.subnets(new_prefix=stack_support.TESTBED_SUBNET_PREFIX))
        taken = [ipaddress.ip_network("172.16.0.0/17"), ipaddress.ip_network("172.16.128.0/18")]

        environment = stack_support.network_env(taken)

        allowed = {str(slot) for slot in slots if not any(slot.overlaps(wide) for wide in taken)}
        assert set(environment.values()) <= allowed

    def test_a_full_range_says_so(self):
        with pytest.raises(stack_support.StackError, match="172.16.0.0/16"):
            stack_support.network_env([stack_support.TESTBED_SUBNET_RANGE])


class TestTakenSubnets:
    def _docker(self, monkeypatch, *, listing: str, inspected: str, inspect_exit: int = 0) -> None:
        def fake_run(argv, **_kwargs):
            if argv[:3] == ["docker", "network", "ls"]:
                return SimpleNamespace(returncode=0, stdout=listing, stderr="")
            return SimpleNamespace(returncode=inspect_exit, stdout=inspected, stderr="")

        monkeypatch.setattr(stack_support.subprocess, "run", fake_run)

    def test_it_reads_every_ipv4_subnet_docker_reports(self, monkeypatch):
        self._docker(monkeypatch, listing="a\nb\nc\n", inspected=json.dumps([
            {"IPAM": {"Config": [{"Subnet": "172.16.1.0/26"}, {"Subnet": "fd00::/64"}]}},
            {"IPAM": {"Config": None}},
            {"IPAM": {"Config": [{"Subnet": "192.168.80.0/20"}]}},
        ]))

        assert stack_support.taken_subnets() == [
            ipaddress.ip_network("172.16.1.0/26"), ipaddress.ip_network("192.168.80.0/20"),
        ]

    def test_a_network_gone_between_the_two_calls_costs_only_itself(self, monkeypatch):
        """`inspect` exits 1 for an id it no longer finds and still prints the
        rest, which is routine with another session tearing stacks down."""
        self._docker(monkeypatch, listing="a\nb\n", inspect_exit=1, inspected=json.dumps([
            {"IPAM": {"Config": [{"Subnet": "172.16.1.0/26"}]}},
        ]))

        assert stack_support.taken_subnets() == [ipaddress.ip_network("172.16.1.0/26")]

    def test_a_daemon_that_does_not_answer_takes_nothing(self, monkeypatch):
        def refused(*_args, **_kwargs):
            raise OSError("no docker")

        monkeypatch.setattr(stack_support.subprocess, "run", refused)

        assert stack_support.taken_subnets() == []


class TestTheBoot:
    def _pool(self, tmp_path: Path) -> stack_support.StackPool:
        return stack_support.StackPool(
            workdir=tmp_path,
            shape=stack_support.Shape(compose_file=COMPOSE_FILE, image="istota-test/lean:unit"),
        )

    def _subnets_in(self, args: list[str]) -> set[ipaddress.IPv4Network]:
        env_file = Path(args[args.index("--env-file") + 1])
        return {
            ipaddress.ip_network(line.split("=", 1)[1])
            for line in env_file.read_text().splitlines()
            if line.startswith(("ISTOTA_TESTBED_DEFAULT_SUBNET=", "ISTOTA_TESTBED_BROWSER_SUBNET="))
        }

    def _boot_answering(self, monkeypatch, *failures: str) -> tuple[list[set], list[list[str]]]:
        """Boot `base`, with `up` raising each failure in turn and then stopping.

        Docker reports whatever an earlier attempt chose as taken, as it would
        once the session that won the race holds those subnets.
        """
        seen: list[set] = []
        downs: list[list[str]] = []
        remaining = list(failures)

        def fake_up(args, **_kwargs):
            seen.append(self._subnets_in(args))
            if remaining:
                raise stack_support.ComposeError(remaining.pop(0))
            raise stack_support.StackError("stop here")

        monkeypatch.setattr(stack_support, "up", fake_up)
        monkeypatch.setattr(stack_support, "logs", lambda *a, **k: "")
        monkeypatch.setattr(stack_support, "taken_subnets", lambda: [subnet for chosen in seen for subnet in chosen])
        monkeypatch.setattr(stack_support, "down", lambda args, **k: downs.append(args))
        return seen, downs

    def test_a_lost_race_for_a_subnet_boots_on_another(self, tmp_path, monkeypatch):
        seen, downs = self._boot_answering(monkeypatch, OVERLAP_ERROR)

        with pytest.raises(stack_support.StackError, match="stop here"):
            self._pool(tmp_path).get(profiles.BASE)

        assert len(seen) == 2
        assert len(seen[0]) == len(seen[1]) == 2
        assert not seen[0] & seen[1]
        # One for what the lost race created, one for the stopped boot.
        assert len(downs) == 2

    def test_any_other_compose_failure_is_not_retried(self, tmp_path, monkeypatch):
        seen, _ = self._boot_answering(monkeypatch, "failed to bind host port")

        with pytest.raises(stack_support.ComposeError, match="failed to bind host port"):
            self._pool(tmp_path).get(profiles.BASE)

        assert len(seen) == 1

    def test_it_gives_up_on_a_race_it_keeps_losing(self, tmp_path, monkeypatch):
        seen, _ = self._boot_answering(monkeypatch, *[OVERLAP_ERROR] * stack_support.SUBNET_ATTEMPTS)

        with pytest.raises(stack_support.ComposeError, match="Pool overlaps"):
            self._pool(tmp_path).get(profiles.BASE)

        assert len(seen) == stack_support.SUBNET_ATTEMPTS
