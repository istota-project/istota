"""Parity row 18: the proxied listener answers the upstream proxy alone.

In `proxied` ingress the web app takes the client address from
X-Forwarded-For with `trusted_proxy_hops = 2`, which is right only if nothing
but the upstream can reach the listener. Three things hold that, and each has a
control: nginx's `allow`/`deny` from UPSTREAM_PROXY, the VM's DOCKER-USER rule
on the published port (provision.sh, through the egress unit), and
`userland-proxy: false`, without which a connection can arrive from the bridge
gateway instead of from the upstream.

The world outside the VM is two network namespaces on a bridge in it: the
upstream (UPSTREAM_PROXY) and an outsider, both reaching LISTEN_ADDR on that
bridge the way a host on the private network would. A forged X-Forwarded-For is
sent by the upstream exactly as a real proxy relays one, the client's own value
followed by the address it appended; what the app took as the client is read
back from the login throttle's own table, `web_auth_attempts`.

Controls (`scripts/test-vm-negative-control.sh`):

- `no-allow-list`: the deployed `listen-proxied.conf` without its allow and
  deny lines;
- `no-docker-user-rule`: the proxied listener's DOCKER-USER rules removed;
- `userland-proxy-on`: dockerd with `userland-proxy: true`.
"""

from __future__ import annotations

import json
import random
import re
import shlex
import time
import uuid

import pytest

from tests.support import parity

from . import lima

pytestmark = pytest.mark.vm

LISTENER = f"{lima.PROXIED_LISTEN}:{lima.PROXIED_PORT}"

CONNECT = r"""
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()
    print("CONNECT=reached")
except OSError as exc:
    print("CONNECT=" + type(exc).__name__)
"""


def _connect_from(vm: lima.Vm, namespace: str) -> str:
    out = vm.out(
        f"ip netns exec istota-{namespace} python3 -c {shlex.quote(CONNECT)} "
        f"{lima.PROXIED_LISTEN} {lima.PROXIED_PORT}")
    return out.rsplit("CONNECT=", 1)[-1]


def _curl_from_upstream(vm: lima.Vm, args: str) -> str:
    return vm.out(f"ip netns exec istota-upstream curl -s -m 15 -D - {args}")


def _set_userland_proxy(vm: lima.Vm, enabled: bool) -> None:
    vm.run(f"""
python3 - <<'PY'
import json
path = "/etc/docker/daemon.json"
conf = json.load(open(path))
conf["userland-proxy"] = {json.dumps(enabled)}
json.dump(conf, open(path, "w"), indent=2)
PY
systemctl restart docker.service
systemctl restart istota-devbox-egress.service
""", timeout=600)
    lima.wait_healthy(vm)
    lima.wait_for(lambda: bool(lima.container(vm, "nginx")), timeout=300, what="nginx to come back")


@pytest.fixture(scope="module")
def stack(proxied_stack):
    vm = proxied_stack
    name = lima.control()
    if name == "no-allow-list":
        vm.run(f"sed -i -e '/UPSTREAM_ALLOW/d' -e '/deny all;/d' {lima.STACK}/src/docker/nginx/listen-proxied.conf"
               " && istota-stack compose restart nginx")
        try:
            yield vm
        finally:
            vm.run(f"git -C {lima.STACK}/src checkout -- docker/nginx/listen-proxied.conf"
                   " && istota-stack compose restart nginx")
        return
    if name == "no-docker-user-rule":
        vm.run("""
iptables -w 5 -S DOCKER-USER | { grep -F 'istota-proxied:' || true; } | sed 's/^-A /-D /' | while read -r rule; do
    eval "iptables -w 5 ${rule}"
done
""")
        try:
            yield vm
        finally:
            vm.run("systemctl restart istota-devbox-egress.service")
        return
    if name == "userland-proxy-on":
        _set_userland_proxy(vm, True)
        try:
            yield vm
        finally:
            _set_userland_proxy(vm, False)
        return
    yield vm


@parity.witness(18)
class TestTheProxiedListenerAnswersOnlyTheUpstream:
    def test_an_outsider_cannot_connect_and_the_upstream_can(self, stack):
        assert _connect_from(stack, "upstream") == "reached", "the in-session control: the upstream connects"
        outcome = _connect_from(stack, "outsider")
        assert outcome != "reached", "row 18: a client outside UPSTREAM_PROXY opened a TCP connection"

    def test_nginx_refuses_a_peer_outside_upstream_proxy(self, stack):
        """A peer that reaches nginx without crossing the published port (the
        istota container, on the stack's own network) meets the allow list."""
        status = lima.stack_exec(stack, (
            "python3 -c \"import urllib.request, urllib.error\n"
            "try:\n"
            "    print(urllib.request.urlopen('http://nginx:80/istota/login', timeout=10).status)\n"
            "except urllib.error.HTTPError as e:\n"
            "    print(e.code)\""
        )).stdout.strip()
        assert status == "403", f"row 18: nginx answered {status} to a peer outside UPSTREAM_PROXY"

    def test_a_forged_forwarded_for_reaches_the_app_as_the_upstreams_client(self, stack):
        forged = f"198.51.100.{random.randint(1, 254)}"
        client = f"203.0.113.{random.randint(1, 254)}"
        headers = (
            f"-H 'Host: {lima.DOMAIN}' -H 'X-Forwarded-For: {forged}, {client}' "
            f"-H 'X-Forwarded-Proto: https' -H 'Origin: https://{lima.DOMAIN}'"
        )
        page = _curl_from_upstream(stack, f"{headers} http://{LISTENER}/istota/login")
        cookie = re.search(r"(?im)^set-cookie:\s*([^=;\s]+=[^;\r\n]+)", page)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page)
        assert cookie and token, f"no session cookie or login form:\n{page[:2000]}"
        form = f"email=nobody-{uuid.uuid4().hex[:8]}@example.test&password=wrong&csrf_token={token.group(1)}"
        _curl_from_upstream(stack, (
            f"{headers} -H {shlex.quote('Cookie: ' + cookie.group(1))} "
            f"-H 'Content-Type: application/x-www-form-urlencoded' --data {shlex.quote(form)} "
            f"http://{LISTENER}/istota/login/email"))
        query = (
            "import sqlite3,json;c=sqlite3.connect('file:/data/db/istota.db?mode=ro',uri=True);"
            "print(json.dumps([r[0] for r in c.execute(\\\"select key from web_auth_attempts where kind='ip'\\\")]))"
        )
        keys = json.loads(lima.stack_exec(stack, f'python3 -c "{query}"').stdout.strip())
        assert forged not in keys, f"row 18: the app took the forged address {forged} as the client"
        assert client in keys, f"the app did not record the upstream's client {client}: {keys}"

    def test_nginx_sees_the_upstreams_own_address(self, stack):
        nonce = uuid.uuid4().hex[:12]
        since = int(time.time()) - 1
        _curl_from_upstream(stack, f"-H 'Host: {lima.DOMAIN}' -o /dev/null http://{LISTENER}/istota/login?vmtier={nonce}")
        cid = lima.container(stack, "nginx")
        lines = [line for line in stack.out(f"docker logs --since {since} {cid} 2>&1").splitlines() if nonce in line]
        assert lines, "nginx logged no request carrying the marker"
        peers = sorted({line.split()[0] for line in lines})
        assert peers == [lima.UPSTREAM], f"row 18: nginx saw the request from {peers}, not the upstream"
