"""The appliance VM: row L3 for a VM made from a release directory.

The Mac app's VM boots a prebuilt disk and installs its release from the
directory of unpacked releases, which the template mounts read-only at
`/mnt/istota-release` (the Mac app spec's L3', an extension of row L3). What
crosses from the Mac is then the workspace folder, writable, and that
directory, read-only, and nothing else: not the Mac home.

The VM is made the way the app makes it, by the Mac CLI's `create --appliance
DIR --no-setup`, in a LIMA_HOME, state directory and instance of its own, and
deleted at the end of the module. DIR is an unpacked release, `<releases>/<tag>/`,
with its disk, bundle, images archive and signed manifest:

| Variable | Effect |
|---|---|
| `ISTOTA_VM_APPLIANCE_DIR` | the release directory; the module skips without it |
| `ISTOTA_VM_APPLIANCE_KEY` | the public key its tag and manifest are signed with (the project's otherwise) |

Building one is `scripts/build-appliance.sh` (the Mac app spec, Stage 7), which
this module will build from when that lands.

Control (`scripts/test-vm-negative-control.sh`):

- `appliance-default-mounts`: the Mac CLI given a copy of the template with
  `template:_default/mounts` added to its `base:` (`ISTOTA_LIMA_TEMPLATE`),
  which mounts the Mac home read-only at its own path.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from tests.support import parity

from . import lima

pytestmark = pytest.mark.vm

INSTANCE = "istota-vm-appliance"
RELEASE_MOUNT = "/mnt/istota-release"
WORKSPACE_MOUNT = "/srv/mount/host-folder"
CONTROL = "appliance-default-mounts"
BASE = "base:\n  - template:_images/debian-13\n"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ApplianceVm:
    def __init__(self, env: dict[str, str], folder: Path, outside: Path):
        self.env = env
        self.folder = folder
        self.outside = outside

    def limactl(self, *args: str, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["limactl", *args], env=self.env, capture_output=True, text=True,
                              timeout=timeout, check=check)

    def sh(self, command: str, check: bool = False) -> subprocess.CompletedProcess:
        return self.limactl("shell", "--workdir", "/", INSTANCE, "sudo", "sh", "-c", command,
                            check=check, timeout=120)


@pytest.fixture(scope="module")
def appliance(tmp_path_factory) -> ApplianceVm:
    release = os.environ.get("ISTOTA_VM_APPLIANCE_DIR", "").strip()
    if not release:
        pytest.skip("ISTOTA_VM_APPLIANCE_DIR names no release directory to make the appliance VM from")
    if not lima.limactl_available() or shutil.which("zstd") is None:
        pytest.skip("the appliance VM needs limactl and zstd on this Mac")
    key = os.environ.get("ISTOTA_VM_APPLIANCE_KEY", "").strip() or (
        lima.REPO / "host" / "release-signing-key.pub").read_text().strip()

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
    }
    if lima.control() == CONTROL:
        text = lima.TEMPLATE.read_text()
        assert text.count(BASE) == 1, "the template's base moved; the control no longer applies"
        copy = work / "istota-control.yaml"
        copy.write_text(text.replace(BASE, f"{BASE}  - template:_default/mounts\n"))
        env["ISTOTA_LIMA_TEMPLATE"] = str(copy)
    shutil.rmtree(work / "config", ignore_errors=True)
    vm = ApplianceVm(env, folder, outside)
    vm.limactl("delete", "--force", INSTANCE, check=False)
    try:
        subprocess.run(
            ["/bin/bash", str(lima.MAC_CLI), "create", "--appliance", release, "--signing-key", key,
             "--workspace", str(folder), "--port", str(_free_port()),
             "--cpus", "4", "--memory", "4", "--disk", "40", "--no-setup"],
            env=env, stdin=subprocess.DEVNULL, check=True, timeout=lima.CREATE_TIMEOUT,
        )
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
