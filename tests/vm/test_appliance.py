"""The appliance VM: rows L3 and M11 for a VM made from a release directory.

The Mac app's VM boots a prebuilt disk and installs its release from the
directory of unpacked releases, which the template mounts read-only at
`/mnt/istota-release` (the Mac app spec's L3', an extension of row L3). What
crosses from the Mac is then the workspace folder, writable, and that
directory, read-only, and nothing else: not the Mac home. And the first launch
needs no network (row M11): the disk already has every package provision.sh
wants, and the images come from the release's archive.

The VM is made the way the app makes it, by the Mac CLI's `create --appliance
DIR --no-setup`, in a LIMA_HOME, state directory and instance of its own, then
set up in the terminal with the scripted answers the other modes use, started
and waited on until the daemon is healthy. That is the first-launch window M11
measures. The module deletes the VM at its end. DIR is an unpacked release,
`<releases>/<tag>/`, with its disk, bundle, images archive and signed manifest:

| Variable | Effect |
|---|---|
| `ISTOTA_VM_APPLIANCE_DIR` | a release directory to use; without it the module builds one |
| `ISTOTA_VM_APPLIANCE_KEY` | the public key that directory's tag and manifest are signed with (the project's otherwise) |

Without a directory, `scripts/build-appliance.sh` builds one from the tier's
signed snapshot of this checkout (`lima.snapshot_release`) into
`<ISTOTA_VM_WORKDIR>/appliance-releases/<tag>/`, in its own LIMA_HOME
(`~/.istota-vmb`), and an unchanged tree reuses it. That build takes about
fifteen minutes and needs the network; the appliance VM itself does not.

**What M11 counts.** The template is given one extra provision step, ahead of
its own, that runs at the first boot before anything else of istota's: every
new outbound packet from the VM's own processes except loopback, link-local
multicast, DNS to Lima's resolver and NTP is logged (`istota-m11:`) and rejected,
and `apt-get` is wrapped to record its calls. Container traffic crosses the
FORWARD chain, not OUTPUT, so the counter is the host's: apt, Docker pulls,
curl, git. NTP (UDP 123) is let through and counted apart: the Debian cloud
image's own clock sync queries the public pool from the first boot (measured,
about a hundred packets in the window), which no istota step asks for and
whose failure offline costs nothing, since Lima's guest agent also steps the
clock from the Mac. The window closes when the daemon is healthy. Debian's daily apt
timers are not stopped; generalize removed their stamps so they do not catch up
at the first boot, and one that fired inside the window would count.

Controls (`scripts/test-vm-negative-control.sh`):

- `appliance-default-mounts` (L3): the template with `template:_default/mounts`
  added to its `base:`, which mounts the Mac home read-only at its own path.
- `appliance-without-offline-rule` (M11): the same extra step also takes the
  offline rule out of the disk's provision.sh, so every run makes the
  `apt-get update` it made before Stage 2 (with `|| true`, as an offline
  `apt-get update` only warns). A checkout at the same commit keeps the edit.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from tests.support import parity

from . import lima

pytestmark = pytest.mark.vm

INSTANCE = "istota-vm-appliance"
RELEASE_MOUNT = "/mnt/istota-release"
WORKSPACE_MOUNT = "/srv/mount/host-folder"
BASE = "base:\n  - template:_images/debian-13\n"
PROVISION = "provision:\n"
MOUNTS_CONTROL = "appliance-default-mounts"
OFFLINE_CONTROL = "appliance-without-offline-rule"
BUILD = lima.REPO / "scripts" / "build-appliance.sh"
# Larger than the 40 GiB the disk is built with: the guest's filesystem must follow.
DISK_GIB = 44

#: Runs at the first boot ahead of the template's own provision. `|| true` on
#: each rule: a second boot finds them in place.
WINDOW = r"""#!/bin/bash
set -eu
state=/var/lib/istota-m11
[ ! -e "${state}/armed" ] || exit 0
mkdir -p "$state"
for ipt in iptables ip6tables; do
    "$ipt" -N ISTOTA_M11
    "$ipt" -A ISTOTA_M11 -j LOG --log-prefix "istota-m11: " --log-level 4
    "$ipt" -A ISTOTA_M11 -j REJECT
    "$ipt" -A OUTPUT -o lo -j ACCEPT
    "$ipt" -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
done
# Lima's resolver is on its user network; a lookup is not a request beyond the VM.
for proto in udp tcp; do
    iptables -A OUTPUT -d 192.168.5.0/24 -p "$proto" --dport 53 -j ACCEPT
done
iptables -A OUTPUT -d 224.0.0.0/4 -j ACCEPT
ip6tables -A OUTPUT -d ff00::/8 -j ACCEPT
# The Debian image's own clock sync, counted apart and let through (see the docstring).
for ipt in iptables ip6tables; do
    "$ipt" -N ISTOTA_M11_TIME
    "$ipt" -A ISTOTA_M11_TIME -j ACCEPT
    "$ipt" -A OUTPUT -p udp --dport 123 -j ISTOTA_M11_TIME
done
iptables -A OUTPUT -j ISTOTA_M11
ip6tables -A OUTPUT -j ISTOTA_M11
cat > /usr/local/sbin/apt-get <<'EOF'
#!/bin/bash
echo "apt-get $*" >> /var/lib/istota-m11/calls
exec /usr/bin/apt-get "$@"
EOF
chmod 0755 /usr/local/sbin/apt-get
touch "${state}/calls" "${state}/armed"
"""

#: The M11 control's addition: the offline rule taken out of the disk's provision.sh.
WITHOUT_OFFLINE_RULE = r"""
provision=/srv/app/istota/src/host/provision.sh
grep -q '^    \[ "\${#missing\[@\]}" -gt 0 \] || return 0$' "$provision"
sed -i -e '/^    \[ "\${#missing\[@\]}" -gt 0 \] || return 0$/d' \
    -e 's/^        apt-get update -qq$/        apt-get update -qq || true/' "$provision"
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _indent(script: str, spaces: int) -> str:
    return "".join((" " * spaces + line) if line.strip() else line for line in script.splitlines(True))


def instrumented_template(work: Path, control: str) -> Path:
    """The template with the M11 window as its first provision step, and the
    control's change when one is asked for."""
    text = lima.TEMPLATE.read_text()
    assert text.count(BASE) == 1, "the template's base moved; the controls no longer apply"
    assert text.count(PROVISION) == 1, "the template's provision list moved"
    script = WINDOW + (WITHOUT_OFFLINE_RULE if control == OFFLINE_CONTROL else "")
    step = f"  - mode: system\n    script: |\n{_indent(script, 6)}"
    text = text.replace(PROVISION, PROVISION + step)
    if control == MOUNTS_CONTROL:
        text = text.replace(BASE, f"{BASE}  - template:_default/mounts\n")
    copy = work / "istota-appliance.yaml"
    copy.write_text(text)
    return copy


def release_directory() -> tuple[str, str]:
    """A release directory and its public key: the one named, or one built here."""
    named = os.environ.get("ISTOTA_VM_APPLIANCE_DIR", "").strip()
    if named:
        key = os.environ.get("ISTOTA_VM_APPLIANCE_KEY", "").strip() or (
            lima.REPO / "host" / "release-signing-key.pub").read_text().strip()
        return named, key
    release = lima.snapshot_release()
    releases = lima._workdir() / "appliance-releases"
    directory = releases / release.tag
    if not directory.is_dir():
        subprocess.run(
            ["bash", str(BUILD), "--tag", release.tag, "--key", str(lima._workdir() / "signing" / "release"),
             "--repo", str(release.repo), "--out", str(releases)],
            env={**os.environ, "ISTOTA_BUILD_LIMA_HOME": str(Path.home() / ".istota-vmb")},
            stdin=subprocess.DEVNULL, check=True, timeout=lima.BUILD_TIMEOUT,
        )
    return str(directory), release.signing_key


class ApplianceVm:
    def __init__(self, env: dict[str, str], folder: Path, outside: Path):
        self.env = env
        self.folder = folder
        self.outside = outside
        self.window: dict[str, str] = {}

    def limactl(self, *args: str, timeout: int = 300, check: bool = True,
                stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["limactl", *args], env=self.env, capture_output=True, text=True,
                              input=stdin, timeout=timeout, check=check)

    def sh(self, command: str, check: bool = False, timeout: int = 120) -> subprocess.CompletedProcess:
        result = self.limactl("shell", "--workdir", "/", INSTANCE, "sudo", "bash", "-s",
                              stdin="set -uo pipefail\n" + command + "\n", check=False, timeout=timeout)
        if check and result.returncode != 0:
            raise lima.VmError(f"in {INSTANCE}, exit {result.returncode}:\n{command[:1500]}\n"
                               f"--- stdout ---\n{result.stdout[-6000:]}\n--- stderr ---\n{result.stderr[-6000:]}")
        return result


def _wait_healthy(vm: ApplianceVm, timeout: int = 900) -> str:
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        state = vm.sh(
            "cid=$(docker ps -q --filter label=com.docker.compose.project=istota "
            "--filter label=com.docker.compose.service=daemon | head -1); "
            "[ -n \"$cid\" ] && docker inspect -f '{{.State.Health.Status}}' \"$cid\"").stdout.strip()
        if state == "healthy":
            return state
        time.sleep(5)
    return state


def _close_window(vm: ApplianceVm) -> None:
    """Read what the window saw, while the VM still holds it."""
    for ipt in ("iptables", "ip6tables"):
        rule = vm.sh(f"{ipt} -L ISTOTA_M11 -v -x -n | awk '$3 == \"LOG\" {{ print $1 }}'").stdout.strip()
        vm.window[f"{ipt}_packets"] = rule or "missing"
        vm.window[f"{ipt}_time_sync_packets"] = vm.sh(
            f"{ipt} -L ISTOTA_M11_TIME -v -x -n | awk '$3 == \"ACCEPT\" {{ print $1 }}'").stdout.strip()
    vm.window["dropped"] = vm.sh(
        "journalctl -k --no-pager | grep 'istota-m11: ' "
        "| sed -E 's/.*(DST=[^ ]+).*(PROTO=[^ ]+)( SPT=[^ ]+)?( DPT=[^ ]+)?.*/\\1 \\2\\4/' "
        "| sort | uniq -c | sort -rn | head -20").stdout.strip()
    vm.window["calls"] = vm.sh("cat /var/lib/istota-m11/calls 2>/dev/null || echo missing").stdout.strip()
    vm.window["apt_daily"] = vm.sh(
        "systemctl show -p ExecMainStartTimestamp apt-daily.service apt-daily-upgrade.service").stdout.strip()


@pytest.fixture(scope="module")
def appliance(tmp_path_factory) -> ApplianceVm:
    if not lima.limactl_available() or shutil.which("zstd") is None:
        pytest.skip("the appliance VM needs limactl and zstd on this Mac")
    release, key = release_directory()

    # Lima's sockets cap a path near 104 characters: a short LIMA_HOME.
    work = lima._workdir() / "appliance"
    lima_home = Path.home() / ".istota-vma"
    folder = work / "ws"
    outside = work / "outside"
    for path in (work, folder, outside, lima_home):
        path.mkdir(parents=True, exist_ok=True)
    (folder / "l3-inside.txt").write_text("l3-inside\n")
    (outside / "secret.txt").write_text("l3-outside\n")
    env = {
        **os.environ,
        "LIMA_HOME": str(lima_home),
        "XDG_CONFIG_HOME": str(work / "config"),
        "ISTOTA_MAC_BIN_DIR": str(work / "bin"),
        "ISTOTA_LIMA_INSTANCE": INSTANCE,
        "ISTOTA_LIMA_TEMPLATE": str(instrumented_template(work, lima.control())),
    }
    shutil.rmtree(work / "config", ignore_errors=True)
    vm = ApplianceVm(env, folder, outside)
    vm.limactl("delete", "--force", INSTANCE, check=False)
    port = _free_port()
    try:
        started = time.monotonic()
        subprocess.run(
            ["/bin/bash", str(lima.MAC_CLI), "create", "--appliance", release, "--signing-key", key,
             "--workspace", str(folder), "--port", str(port),
             "--cpus", "4", "--memory", "4", "--disk", str(DISK_GIB), "--no-setup",
             # The folder already holds the L3 probe file.
             "--adopt"],
            env=env, stdin=subprocess.DEVNULL, check=True, timeout=lima.CREATE_TIMEOUT,
        )
        vm.window["create_seconds"] = f"{time.monotonic() - started:.0f}"
        setup = [*lima.common_args("http://127.0.0.1:9/v1"),
                 "--ingress", "local", "--hostname", f"localhost:{port}", "--storage", "host-folder"]
        vm.sh(f"export ISTOTA_BRAIN_NATIVE_API_KEY={shlex.quote(lima.NATIVE_KEY)}\n"
              f"istota-stack setup {shlex.join(setup)} </dev/null && "
              "/srv/app/istota/src/host/provision.sh && systemctl enable --now istota-stack",
              check=True, timeout=lima.BUILD_TIMEOUT)
        vm.window["daemon"] = _wait_healthy(vm)
        vm.window["launch_seconds"] = f"{time.monotonic() - started:.0f}"
        _close_window(vm)
        yield vm
    finally:
        vm.limactl("delete", "--force", INSTANCE, check=False)


@parity.witness("L3")
class TestOnlyTheFolderAndTheReleasesCrossFromTheMac:
    def test_the_vm_mounts_the_folder_and_the_read_only_releases_alone(self, appliance):
        listing = appliance.sh("findmnt -rn -t virtiofs,9p,fuse.sshfs -o TARGET,OPTIONS || true").stdout
        mounts = {fields[0]: fields[1] for fields in (line.split() for line in listing.splitlines())}
        # Apple's Rosetta runtime, shared for a forced amd64 image (row 7).
        mounts.pop("/mnt/lima-rosetta", None)
        assert sorted(mounts) == sorted([WORKSPACE_MOUNT, RELEASE_MOUNT]), (
            f"row L3: Mac paths mounted in the appliance VM: {mounts}")
        assert "ro" in mounts[RELEASE_MOUNT].split(","), mounts
        assert "rw" in mounts[WORKSPACE_MOUNT].split(","), mounts

    def test_root_cannot_write_under_the_releases(self, appliance):
        probe = f"{RELEASE_MOUNT}/l3-probe"
        result = appliance.sh(f"touch {probe}")
        assert result.returncode != 0, "row L3: root in the VM wrote into the releases directory"
        assert appliance.sh(f"test -e {probe}").returncode != 0

    def test_the_vm_reads_nothing_of_the_mac_home_outside_them(self, appliance):
        secret = shlex.quote(str(appliance.outside / "secret.txt"))
        home = shlex.quote(str(Path.home()))
        inside = appliance.sh(f"cat {WORKSPACE_MOUNT}/l3-inside.txt").stdout.strip()
        # The in-session control: the same read of a file in the folder works.
        assert inside == "l3-inside"
        assert appliance.sh(f"cat {secret}").returncode != 0, (
            "row L3: the appliance VM reads a Mac file outside the folder by its Mac path")
        assert appliance.sh(f"test -d {home}").returncode != 0, "row L3: the Mac home is in the VM"


@parity.witness("M11")
class TestTheFirstLaunchMakesNoNetworkRequest:
    def test_the_first_launch_reached_a_healthy_stack(self, appliance):
        assert appliance.window["daemon"] == "healthy", appliance.window

    def test_nothing_left_the_vm_in_the_window(self, appliance):
        window = appliance.window
        assert (window["iptables_packets"], window["ip6tables_packets"]) == ("0", "0"), (
            f"row M11: the first launch sent packets past the VM:\n{window['dropped']}\n"
            f"apt timers: {window['apt_daily']}")

    def test_provision_ran_no_apt_get_update(self, appliance):
        calls = appliance.window["calls"]
        assert calls != "missing", "the window's apt-get record is gone"
        assert "apt-get update" not in calls, f"row M11: provision.sh refreshed apt's lists:\n{calls}"


class TestTheDiskGrowsWithTheVm:
    def test_the_root_filesystem_fills_a_disk_larger_than_the_image(self, appliance):
        size = int(appliance.sh("df -B1 --output=size / | tail -1").stdout.strip())
        assert size > (DISK_GIB - 3) * (1 << 30), f"/ is {size} bytes on a {DISK_GIB} GiB disk"
