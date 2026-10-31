"""The browser profile on an arm64 VM, and parity row 17's published-port half.

The browser image installs Google Chrome's package for the build architecture,
so on an arm64 VM it is an arm64 image and runs without Rosetta.
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

The firewall outside Chrome (#884): a raw socket from the browser container is
what a compromised renderer has, past the egress proxy, and the VM's DOCKER-USER
rules drop it. The daemon shares that network and takes its default route through
it, so it is asked too.

Control `no-browser-firewall`: the rules are removed, as on a VM that never ran
the unit.
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
    # web waits on the daemon's health, so on a session that just deployed it comes
    # up after the browser, and the in-session controls below ask it to answer.
    web = f"{_default_network_address(vm, 'web')}:8766"
    lima.wait_for(
        lambda: _connect(vm, lima.container(vm, "daemon"), [web], user="10001") == {web: "reached"},
        timeout=120, interval=3, what="web to listen",
    )
    if lima.control() == "browser-on-default-network":
        vm.run(f"docker network connect istota_default {cid}")
        try:
            yield vm
        finally:
            vm.run(f"docker network disconnect istota_default {cid}")
        return
    if lima.control() == "no-browser-firewall":
        vm.run("""
iptables -w 5 -S DOCKER-USER | { grep -F 'istota-browser:' || true; } | sed 's/^-A /-D /' | while read -r rule; do
    eval "iptables -w 5 ${rule}"
done
""")
        try:
            yield vm
        finally:
            vm.run("systemctl restart istota-devbox-egress.service")
        return
    yield vm


class TestTheBrowserRunsNatively:
    def test_the_browser_is_an_arm64_image_running_as_aarch64(self, stack):
        assert stack.out("uname -m") == "aarch64"
        cid = lima.container(stack, "browser")
        image = stack.out(f"docker inspect -f '{{{{.Image}}}}' {cid}")
        assert stack.out(f"docker image inspect -f '{{{{.Architecture}}}}' {image}") == "arm64"
        assert stack.out(f"docker exec {cid} uname -m") == "aarch64"

    def test_the_daemon_drives_the_browser(self, stack):
        istota = lima.container(stack, "daemon")
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
        istota = lima.container(stack, "daemon")
        from_istota = _connect(stack, istota, targets, user="10001")
        assert set(from_istota.values()) == {"reached"}, f"the in-session control: {from_istota}"
        from_browser = _connect(stack, lima.container(stack, "browser"), targets)
        reached = sorted(t for t, outcome in from_browser.items() if outcome == "reached")
        assert reached == [], f"row 17: the browser reached {reached}: {from_browser}"


@parity.witness(17)
class TestTheFirewallOutsideChrome:
    TARGETS = [f"{lima.METADATA_ADDR}:80", f"{lima.PRIVATE_ADDR}:80"]

    def test_the_browser_has_its_fixed_address(self, stack):
        cid = lima.container(stack, "browser")
        address = stack.out(
            "docker inspect -f '{{(index .NetworkSettings.Networks \"istota_browser\").IPAddress}}' " + cid)
        assert address == "172.29.0.2"

    def test_a_raw_socket_reaches_no_private_range_and_does_reach_the_internet(self, stack):
        for target in self.TARGETS:
            assert stack.ok(f"curl -s -m 5 -o /dev/null http://{target}/"), f"{target} does not answer the VM"
        outcome = _connect(stack, lima.container(stack, "browser"), self.TARGETS + ["deb.debian.org:80"])
        assert outcome["deb.debian.org:80"] == "reached", outcome
        reached = sorted(t for t in self.TARGETS if outcome[t] == "reached")
        assert reached == [], f"the browser reached {reached}: {outcome}"

    def test_the_daemon_on_the_same_network_keeps_its_reach(self, stack):
        outcome = _connect(stack, lima.container(stack, "daemon"), self.TARGETS, user="10001")
        assert set(outcome.values()) == {"reached"}, outcome

    def test_a_name_in_the_config_opens_its_address_and_only_that(self, stack):
        name = "wiki.vmtier.test"
        config = "/srv/app/istota/config/config.toml"
        script = lima.SCRATCH + "/browser-grant.py"
        stack.run(f"cp -p {config} {lima.SCRATCH}/config.toml.browser-grant && "
                  f"cp /etc/hosts {lima.SCRATCH}/hosts.browser-grant && "
                  f"echo '{lima.PRIVATE_ADDR} {name}' >> /etc/hosts")
        stack.run(f"""cat > {script} <<'PY'
import re, sys
path, name = sys.argv[1:]
text = open(path).read()
line = f'"{{name}}" = ["browser"]'
if re.search(r"^\\[private_hosts\\]$", text, flags=re.M):
    text = re.sub(r"^\\[private_hosts\\]$", "[private_hosts]\\n" + line, text, count=1, flags=re.M)
else:
    text += "\\n[private_hosts]\\n" + line + "\\n"
open(path, "w").write(text)
PY
python3 {script} {config} {name}""")
        try:
            assert stack.out("istota-stack browser-grant-addresses").split() == [lima.PRIVATE_ADDR]
            stack.run("systemctl start istota-browser-grants.service")
            outcome = _connect(stack, lima.container(stack, "browser"), self.TARGETS)
            assert outcome[f"{lima.PRIVATE_ADDR}:80"] == "reached", outcome
            assert outcome[f"{lima.METADATA_ADDR}:80"] != "reached", outcome
        finally:
            stack.run(f"cp -p {lima.SCRATCH}/config.toml.browser-grant {config} && "
                      f"cp {lima.SCRATCH}/hosts.browser-grant /etc/hosts && "
                      "systemctl start istota-browser-grants.service")
        outcome = _connect(stack, lima.container(stack, "browser"), self.TARGETS)
        assert outcome[f"{lima.PRIVATE_ADDR}:80"] != "reached", outcome

    def test_the_refresh_timer_runs(self, stack):
        assert stack.out("systemctl is-enabled istota-browser-grants.timer") == "enabled"
        assert stack.out("systemctl is-active istota-browser-grants.timer") == "active"
