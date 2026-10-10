"""The browser profile on an arm64 VM, and parity row 17's published-port half.

The browser image installs Google Chrome's amd64 package, so on an arm64 VM it
runs under Rosetta (`host/lima/istota.yaml` turns it on for the `vz` backend).
This builds it there, starts it from the shipped compose file and asks the
daemon to reach it.

Row 17: the smoke tier witnesses the browser reaching no unpublished service on
the default network. Docker Desktop routes one bridge network to another
container's published port by address, which Docker Engine does not, so a
published service could only be probed here: from the browser container,
neither nginx (published) nor web answers by address, while both answer the
istota container, which joins both networks. Signaling, the other published
service, is not in this stack: the wizard refuses its profile without Talk.

Control `browser-on-default-network`: the browser container is connected to
the default network.
"""

from __future__ import annotations

import json
import shlex

import pytest

from tests.support import parity

from . import lima

pytestmark = pytest.mark.vm

CONNECT_PROBE = r"""
import json, socket, sys
out = {}
for target in json.loads(sys.argv[1]):
    host, port = target.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=5).close()
        out[target] = "reached"
    except OSError as exc:
        out[target] = type(exc).__name__
print("CONNECT=" + json.dumps(out))
"""


def _connect(vm: lima.Vm, cid: str, targets: list[str], *, user: str = "") -> dict:
    as_user = f"-u {user} " if user else ""
    out = vm.out(
        f"docker exec {as_user}{cid} python3 -c {shlex.quote(CONNECT_PROBE)} {shlex.quote(json.dumps(targets))}")
    line = next(row for row in out.splitlines() if row.startswith("CONNECT="))
    return json.loads(line[len("CONNECT="):])


def _default_network_address(vm: lima.Vm, service: str) -> str:
    cid = lima.container(vm, service)
    assert cid, f"{service} is not running"
    return vm.out(
        "docker inspect -f '{{(index .NetworkSettings.Networks \"istota_default\").IPAddress}}' " + cid)


@pytest.fixture(scope="module")
def stack(local_stack):
    vm = local_stack
    cid = lima.container(vm, "browser")
    assert cid, "the browser profile is not running"
    lima.wait_for(
        lambda: vm.out(f"docker inspect -f '{{{{.State.Health.Status}}}}' {cid}", check=False) == "healthy",
        timeout=900, interval=10, what="the browser container to become healthy",
    )
    if lima.control() == "browser-on-default-network":
        vm.run(f"docker network connect istota_default {cid}")
        try:
            yield vm
        finally:
            vm.run(f"docker network disconnect istota_default {cid}")
        return
    yield vm


class TestTheBrowserRunsUnderRosetta:
    def test_the_vm_is_arm64_with_rosetta_registered(self, stack):
        assert stack.out("uname -m") == "aarch64"
        handler = stack.read("/proc/sys/fs/binfmt_misc/rosetta")
        assert handler.startswith("enabled"), handler

    def test_the_browser_is_an_amd64_image_running_as_x86_64(self, stack):
        cid = lima.container(stack, "browser")
        image = stack.out(f"docker inspect -f '{{{{.Image}}}}' {cid}")
        assert stack.out(f"docker image inspect -f '{{{{.Architecture}}}}' {image}") == "amd64"
        assert stack.out(f"docker exec {cid} uname -m") == "x86_64"

    def test_the_daemon_drives_the_browser(self, stack):
        istota = lima.container(stack, "istota")
        assert _connect(stack, istota, ["browser:9223"], user="10001") == {"browser:9223": "reached"}
        health = lima.stack_exec(
            stack, "python3 -c \"import urllib.request;"
            "print(urllib.request.urlopen('http://browser:9223/health',timeout=10).status)\"").stdout.strip()
        assert health == "200", health


@parity.witness(17)
class TestTheBrowserReachesNoPublishedPort:
    def test_the_browser_reaches_nothing_on_the_default_network_by_address(self, stack):
        targets = [
            f"{_default_network_address(stack, 'nginx')}:80",
            f"{_default_network_address(stack, 'web')}:8766",
        ]
        istota = lima.container(stack, "istota")
        from_istota = _connect(stack, istota, targets, user="10001")
        assert set(from_istota.values()) == {"reached"}, f"the in-session control: {from_istota}"
        from_browser = _connect(stack, lima.container(stack, "browser"), targets)
        reached = sorted(t for t, outcome in from_browser.items() if outcome == "reached")
        assert reached == [], f"row 17: the browser reached {reached}: {from_browser}"
