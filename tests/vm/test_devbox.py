"""Parity rows 10 and 11, and the devbox runtime Stage 5 could not run.

The local-mode stack runs two devboxes, `devbox-admin` and `devbox-bob`, from
the compose file `istota devbox compose-file` rendered.

Row 10, devbox egress: the VM's DOCKER-USER rules (`istota-devbox-egress`,
installed by provision.sh) drop a devbox's connections to link-local and
private ranges. A drop and nothing-listening look alike, so a network
namespace in the VM answers HTTP at the metadata address and at 10.0.0.1, both
routed out of the VM (forwarded, so through DOCKER-USER). The VM reaching them
is the in-session control; a devbox must not, and must still reach the internet.

Row 11, the credential socket reaches only its owner: each devbox mounts its
own `devbox-cred-<user>` volume and no other, the one socket it sees answers as
its own user, and a task sandbox sees no credential socket directory at all.

The runtime: the exec transport over the per-user socket volume, the root
phase's ownership of the volumes nested under /data, and `devbox reset`
bringing the container back under its restart policy.

Controls (`scripts/test-vm-negative-control.sh`):

- `no-egress-unit`: the egress unit is disabled and its rules removed, as on a
  VM provisioned without it;
- `every-cred-volume-everywhere`: the rendered compose file mounts every
  user's credential volume into every devbox;
- `exec-socket-symlink`: the skill's no-follow open is replaced, in the skill's
  own process, by a plain `connect(path)`, so a planted link at one devbox's
  socket is followed into another's.
"""

from __future__ import annotations

import json
import shlex
import time

import pytest

from tests.support import parity

from . import lima

pytestmark = pytest.mark.vm

USERS = (lima.USER, lima.SECOND_DEVBOX_USER)
DEVBOX_COMPOSE = f"{lima.STACK}/compose.devbox.yml"
VENV_PYTHON = "/app/.venv/bin/python"

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

PING_PROBE = r"""
import json, socket
s = socket.socket(socket.AF_UNIX)
s.settimeout(10)
s.connect("/run/istota-cred/sock")
s.sendall(json.dumps({"action": "ping"}).encode() + b"\n")
data = b""
while not data.endswith(b"\n"):
    chunk = s.recv(65536)
    if not chunk:
        break
    data += chunk
print("PING=" + data.decode().strip())
"""


def _devbox(vm: lima.Vm, user: str) -> str:
    cid = lima.container(vm, f"devbox-{user}")
    assert cid, f"devbox-{user} is not running"
    return cid


def _in_devbox(vm: lima.Vm, user: str, script: str, *args: str) -> str:
    quoted = " ".join(shlex.quote(a) for a in args)
    return vm.out(f"docker exec {_devbox(vm, user)} python3 -c {shlex.quote(script)} {quoted}")


def _connect_from_devbox(vm: lima.Vm, user: str, targets: list[str]) -> dict:
    out = _in_devbox(vm, user, CONNECT_PROBE, json.dumps(targets))
    line = next(row for row in out.splitlines() if row.startswith("CONNECT="))
    return json.loads(line[len("CONNECT="):])


@pytest.fixture(scope="module")
def stack(local_stack):
    vm = local_stack
    name = lima.control()
    if name == "no-egress-unit":
        vm.run("""
systemctl disable --now istota-devbox-egress.service >/dev/null 2>&1 || true
iptables -w 5 -S DOCKER-USER | { grep -F 'istota-devbox:' || true; } | sed 's/^-A /-D /' | while read -r rule; do
    eval "iptables -w 5 ${rule}"
done
""")
        try:
            yield vm
        finally:
            vm.run("systemctl enable --now istota-devbox-egress.service >/dev/null && "
                   "systemctl restart istota-devbox-egress.service")
        return
    if name == "every-cred-volume-everywhere":
        vm.run(f"""
cp {DEVBOX_COMPOSE} {lima.SCRATCH}/compose.devbox.yml.orig
python3 - {DEVBOX_COMPOSE} <<'PY'
import json, sys
path = sys.argv[1]
text = open(path).read()
header, body = text[:text.index("{{")], json.loads(text[text.index("{{"):])
users = [name[len("devbox-"):] for name in body["services"] if name.startswith("devbox-")]
for name, service in body["services"].items():
    if not name.startswith("devbox-"):
        continue
    owner = name[len("devbox-"):]
    for other in users:
        if other != owner:
            service["volumes"].append({{"type": "volume", "source": f"devbox-cred-{{other}}",
                                       "target": f"/run/istota-cred-{{other}}"}})
open(path, "w").write(header + json.dumps(body, indent=2) + "\\n")
PY
istota-stack compose up -d
""", timeout=900)
        try:
            yield vm
        finally:
            vm.run(f"cp {lima.SCRATCH}/compose.devbox.yml.orig {DEVBOX_COMPOSE} && istota-stack compose up -d",
                   timeout=900)
        return
    yield vm


# The two users' exec sockets, as the istota container (where the skill runs)
# names them. devbox-<user> sees its own at /run/istota-exec/exec.sock.
def _istota_exec_socket(user: str) -> str:
    return f"/data/devbox/exec/{user}/exec.sock"


# Read at import: tests/conftest.py scrubs every ISTOTA_* variable before each
# test body runs, so lima.control() there always answers "".
_CONTROL = lima.control()


def _skill_command() -> str:
    """How the witness runs the devbox skill. Under `exec-socket-symlink` the
    no-follow open is replaced, in the skill's own process, by the plain
    `connect(path)` it replaced, before the skill imports it; the container's
    root is read-only, so this is the one place the control can act."""
    if _CONTROL != "exec-socket-symlink":
        return f"{VENV_PYTHON} -m istota.skills.devbox"
    patch = ("import runpy, istota.lib.unix_connect as u; "
             "u.connect_no_follow = lambda sock, path: sock.connect(path); "
             "runpy.run_module('istota.skills.devbox', run_name='__main__')")
    return f"{VENV_PYTHON} -c {shlex.quote(patch)}"


@parity.witness(11)
class TestAPlantedSocketSymlinkDoesNotReachAnotherDevbox:
    """devbox A's exec socket is in a volume A mounts read-write, so `dev` can
    replace it with a symlink naming B's socket as the istota container sees
    it. The skill resolves A's socket host-side and must not follow the link
    into B's devbox (where `reset` would wipe B's home). Control:
    `exec-socket-symlink` makes the skill follow it, and this goes red."""

    MARKER = "bob-home-marker-istota-vmtier"

    def test_admins_socket_linked_to_bobs_is_refused(self, stack):
        # A marker only bob's devbox can produce, so a followed link is visible.
        bob_socket = _istota_exec_socket(lima.SECOND_DEVBOX_USER)
        lima.stack_exec(
            stack,
            f"ISTOTA_USER_ID={lima.SECOND_DEVBOX_USER} {VENV_PYTHON} -m istota.skills.devbox "
            f"exec 'echo {self.MARKER} > /home/dev/{self.MARKER}'",
        )
        admin = _devbox(stack, lima.USER)
        original = stack.out(f"docker exec {admin} readlink -f /run/istota-exec/exec.sock")
        stack.run(f"docker exec {admin} sh -c "
                  f"'rm -f /run/istota-exec/exec.sock && ln -s {bob_socket} /run/istota-exec/exec.sock'")
        try:
            result = lima.stack_exec(
                stack,
                f"ISTOTA_USER_ID={lima.USER} {_skill_command()} "
                f"exec 'cat /home/dev/{self.MARKER}'",
                check=False,
            )
        finally:
            stack.run(f"docker exec {admin} sh -c "
                      f"'rm -f /run/istota-exec/exec.sock && ln -s {original} /run/istota-exec/exec.sock'",
                      check=False)
            stack.run(f"istota-stack compose restart devbox-{lima.USER}", check=False, timeout=120)
        reply = {}
        try:
            reply = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError, ValueError):
            pass
        assert reply.get("status") == "error", f"row 11: admin's call was not refused: {result.stdout}"
        assert self.MARKER not in result.stdout, "row 11: admin reached bob's devbox through the link"


@parity.witness(10)
class TestADevboxReachesNoPrivateRange:
    TARGETS = [f"{lima.METADATA_ADDR}:80", f"{lima.PRIVATE_ADDR}:80"]

    def test_the_stand_ins_answer_the_vm(self, stack):
        for target in self.TARGETS:
            assert stack.ok(f"curl -s -m 5 -o /dev/null http://{target}/"), f"{target} does not answer the VM"

    def test_a_devbox_reaches_neither_and_does_reach_the_internet(self, stack):
        outcome = _connect_from_devbox(stack, lima.USER, self.TARGETS + ["deb.debian.org:80"])
        assert outcome["deb.debian.org:80"] == "reached", outcome
        reached = sorted(t for t in self.TARGETS if outcome[t] == "reached")
        assert reached == [], f"row 10: a devbox reached {reached}: {outcome}"

    def test_doctor_reports_the_egress_probe_ok(self, stack):
        result = lima.doctor(stack, "security.devbox_netfilter")
        assert result["status"] == "ok", result


@parity.witness(11)
class TestACredentialSocketReachesOnlyItsOwner:
    def test_each_devbox_mounts_only_its_own_credential_volume(self, stack):
        for user in USERS:
            mounts = json.loads(stack.out(f"docker inspect -f '{{{{json .Mounts}}}}' {_devbox(stack, user)}"))
            credential = sorted(m.get("Name", "") for m in mounts if "devbox-cred-" in m.get("Name", ""))
            assert credential == [f"istota_devbox-cred-{user}"], f"row 11: devbox-{user} mounts {credential}"

    def test_the_one_socket_a_devbox_sees_answers_as_its_owner(self, stack):
        for user in USERS:
            sockets = stack.out(
                f"docker exec {_devbox(stack, user)} sh -c "
                "'find / -xdev -type s -path \"*istota-cred*\" 2>/dev/null; "
                "find /run -type s -path \"*cred*\" 2>/dev/null' | sort -u"
            ).split()
            assert sockets == ["/run/istota-cred/sock"], f"row 11: devbox-{user} sees {sockets}"
            line = next(row for row in _in_devbox(stack, user, PING_PROBE).splitlines() if row.startswith("PING="))
            reply = json.loads(line[len("PING="):])
            assert reply.get("ok") is True and reply.get("user_id") == user, reply

    def test_a_task_sandbox_sees_no_credential_socket_directory(self, stack, model):
        cred_root = "/data/devbox/cred"
        daemon = lima.stack_exec(stack, f"ls {cred_root}/{lima.USER}/sock").stdout.strip()
        assert daemon == f"{cred_root}/{lima.USER}/sock", "the in-session control: the daemon has the socket"
        probe = (
            "echo PROBE_BEGIN\n"
            f"for p in {cred_root} {cred_root}/{lima.USER} {cred_root}/{lima.SECOND_DEVBOX_USER} /run/istota-cred; do\n"
            '  if test -e "$p"; then echo "$p=visible"; else echo "$p=absent"; fi\n'
            "done\n"
            "echo sockets=$(find / -xdev -type s 2>/dev/null | grep -c cred || true)\n"
            "echo PROBE_END\n"
        )
        seen = lima.marked_block(lima.run_probe_task(stack, model, probe))
        assert seen.pop("sockets") == "0", seen
        visible = sorted(path for path, state in seen.items() if state != "absent")
        assert visible == [], f"row 11: a task sees {visible}"


class TestTheDevboxRuntime:
    def test_the_exec_transport_crosses_the_socket_volume(self, stack):
        result = lima.stack_exec(
            stack, f"ISTOTA_USER_ID={lima.USER} {VENV_PYTHON} -m istota.skills.devbox exec 'id -u; pwd'")
        reply = json.loads(result.stdout.strip().splitlines()[-1])
        assert reply.get("status") == "ok", reply
        assert reply.get("stdout", "").split()[0] == "10001", reply

    def test_the_nested_socket_volumes_belong_to_the_daemon(self, stack):
        dirs = [f"/data/devbox/{kind}/{user}" for kind in ("exec", "cred") for user in USERS]
        owners = lima.stack_exec(stack, "stat -c '%u %n' " + " ".join(dirs)).stdout.splitlines()
        assert all(line.startswith("10001 ") for line in owners), owners
        for user in USERS:
            inside = stack.out(f"docker exec {_devbox(stack, user)} stat -c '%u %n' /run/istota-exec /run/istota-cred")
            assert all(line.startswith("10001 ") for line in inside.splitlines()), inside

    def test_reset_restarts_the_container(self, stack):
        user = lima.SECOND_DEVBOX_USER
        cid = _devbox(stack, user)
        before = stack.out(f"docker inspect -f '{{{{.State.StartedAt}}}}' {cid}")
        result = lima.stack_exec(stack, f"ISTOTA_USER_ID={user} {VENV_PYTHON} -m istota.skills.devbox reset --yes")
        reply = json.loads(result.stdout.strip().splitlines()[-1])
        assert reply.get("status") == "ok" and reply.get("reset") is True, reply

        def restarted() -> bool:
            state = stack.out(f"docker inspect -f '{{{{.State.StartedAt}}}} {{{{.State.Running}}}}' {cid}", check=False)
            return state.split()[0] != before and state.endswith("true")

        lima.wait_for(restarted, timeout=120, what=f"devbox-{user} to restart")
        deadline = time.monotonic() + 120
        reply = {}
        while time.monotonic() < deadline:
            again = lima.stack_exec(
                stack, f"ISTOTA_USER_ID={user} {VENV_PYTHON} -m istota.skills.devbox exec 'id -u'", check=False)
            try:
                reply = json.loads(again.stdout.strip().splitlines()[-1])
            except (IndexError, json.JSONDecodeError):
                reply = {}
            if reply.get("status") == "ok":
                break
            time.sleep(3)
        assert reply.get("status") == "ok", reply
