"""A stack's networks on a real daemon (ISSUE-919).

`tests/test_testbed_networks.py` holds the compose model and the retry against
a faked `up`. Only a daemon can say that a booted stack sits where the model
put it, and that a lost race reads the way the retry expects.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
import uuid

import pytest

from testbed import stack as stack_support

from ..conftest import require_docker

pytestmark = pytest.mark.smoke


def _subnet_of(network: str) -> ipaddress.IPv4Network:
    inspected = subprocess.run(
        ["docker", "network", "inspect", network],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return ipaddress.ip_network(json.loads(inspected.stdout)[0]["IPAM"]["Config"][0]["Subnet"])


def test_a_booted_stack_holds_two_small_subnets_from_the_testbed_range(stack):
    project = stack_support._project_of(stack.args)

    default = _subnet_of(f"{project}_default")
    browser = _subnet_of(f"{project}_browser")

    assert str(default) == stack.env["ISTOTA_TESTBED_DEFAULT_SUBNET"]
    assert str(browser) == stack.env["ISTOTA_TESTBED_BROWSER_SUBNET"]
    for subnet in (default, browser):
        assert subnet.subnet_of(stack_support.TESTBED_SUBNET_RANGE)
        assert subnet.prefixlen == stack_support.TESTBED_SUBNET_PREFIX


def test_a_subnet_already_held_fails_with_the_error_the_boot_retries_on(tmp_path):
    require_docker()
    subnet = stack_support.network_env(stack_support.taken_subnets())["ISTOTA_TESTBED_DEFAULT_SUBNET"]
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        "services:\n"
        "  idle:\n"
        "    image: busybox:1.37\n"
        '    command: ["sleep", "600"]\n'
        "networks:\n"
        "  default:\n"
        "    ipam:\n"
        "      config:\n"
        f"        - subnet: {subnet}\n"
    )
    suffix = uuid.uuid4().hex[:8]
    holder = stack_support.compose_args(compose_file, project=f"istota-subnetcheck-{suffix}-a")
    loser = stack_support.compose_args(compose_file, project=f"istota-subnetcheck-{suffix}-b")

    try:
        stack_support.up(holder, build=False)
        with pytest.raises(stack_support.ComposeError, match=stack_support.SUBNET_OVERLAP_ERROR):
            stack_support.up(loser, build=False)
    finally:
        stack_support.down(holder, volumes=True)
        stack_support.down(loser, volumes=True)
