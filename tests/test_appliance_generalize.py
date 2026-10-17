"""`host/appliance-generalize.sh`: a provisioned VM made into the appliance disk.

Run against a scratch root (`ISTOTA_GENERALIZE_ROOT`) laid out as a provisioned
build VM, with `docker`, `systemctl`, `cloud-init`, `journalctl`, `apt-get`,
`systemd-run` and the finish script's `pkill`, `userdel` and `fstrim` stubbed on
PATH; each stub records its calls. What the commands do to a real disk is the
release build's (istota-mac's `scripts/build-appliance.sh`, which reads this
script from the tag) and that repository's vm-tier appliance mode.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "host" / "appliance-generalize.sh"

FSTAB = (
    "UUID=abc / ext4 rw,discard,errors=remount-ro,x-systemd.growfs 0 1\n"
    "mount0 /srv/mount/host-folder virtiofs rw,nofail,comment=cloudconfig 0 0\n"
)
PASSWD = (
    "root:x:0:0:root:/root:/bin/bash\n"
    "_apt:x:42:65534::/nonexistent:/usr/sbin/nologin\n"
    "build:x:1000:1000:Istota appliance build:/home/build.linux:/bin/bash\n"
    "istota:x:10001:10001::/srv/app/istota:/usr/sbin/nologin\n"
    "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
)

# What a provisioned build VM holds that names the build, a user or this host.
REMOVED = [
    "srv/app/istota/host.env",
    "srv/app/istota/allowed_signers",
    "srv/app/istota/.env",
    "srv/app/istota/.provisioned",
    "srv/app/istota/.lima-bootstrapped",
    "srv/app/istota/secrets/istota_secret_key",
    "srv/app/istota/config/config.toml",
    "srv/app/istota/data/istota.db",
    "srv/app/istota/requests/config-apply",
    "srv/app/istota/local-layers/claude-code.state",
    "etc/systemd/system/istota-stack.service.d/host-folder.conf",
    "etc/apt/apt.conf.d/50istota-snapshot",
    "var/lib/apt/lists/snapshot.debian.org_archive_debian_20261016T000000Z_dists_trixie_InRelease",
    "var/lib/systemd/timers/stamp-apt-daily.timer",
    "var/lib/systemd/timers/stamp-apt-daily-upgrade.timer",
    "var/lib/apt/periodic/update-success-stamp",
    "etc/ssh/ssh_host_ed25519_key",
    "etc/ssh/ssh_host_ed25519_key.pub",
    "var/lib/systemd/random-seed",
    "var/lib/docker/engine-id",
    "var/lib/docker/network/files/local-kv.db",
    "var/lib/docker/tmp/buildkit-mount123",
    "var/log/syslog.1",
    "var/log/apt/history.log.1.gz",
    "var/tmp/build-scratch",
    "root/.bash_history",
]
# The shape provision.sh made, which the first boot reuses.
KEPT = [
    "srv/app/istota/src/.git/HEAD",
    "srv/app/istota/src/host/provision.sh",
    "etc/systemd/system/istota-stack.service",
    "etc/docker/daemon.json",
    "etc/apt/sources.list.d/docker.sources",
    "etc/apt/keyrings/docker.asc",
    "etc/apt/apt.conf.d/20auto-upgrades",
    "var/lib/apt/lists/lock",
    "usr/local/sbin/istota-stack",
]
# Emptied, not removed: a log file a service holds open stays its file.
TRUNCATED = ["var/log/cloud-init-output.log", "var/log/syslog"]

STUB = r"""#!/bin/bash
echo "$(basename "$0") $*" >> "$STUB_CALLS"
"""


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    for rel in REMOVED + KEPT + TRUNCATED:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{rel} build-machine-detail\n")
    (root / "etc" / "fstab").write_text(FSTAB)
    (root / "etc" / "passwd").write_text(PASSWD)
    (root / "root").mkdir(exist_ok=True)
    (root / "home" / "build.linux").mkdir(parents=True)
    (root / "home" / "build.linux" / ".bash_history").write_text("limactl things\n")
    (root / "etc" / "sudoers.d").mkdir(parents=True)
    (root / "etc" / "sudoers.d" / "90-lima-users").write_text("build ALL=(ALL) NOPASSWD:ALL\n")
    return root


def _bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("docker", "systemctl", "cloud-init", "journalctl", "apt-get", "systemd-run",
                 "pkill", "userdel", "fstrim", "sync", "sleep"):
        path = bindir / name
        path.write_text(STUB)
        path.chmod(0o755)
    return bindir


def _run(script: Path, root: Path, bindir: Path, calls: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "ISTOTA_GENERALIZE_ROOT": str(root),
        "STUB_CALLS": str(calls),
    }
    return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=60)


def _generalize(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = _root(tmp_path)
    bindir = _bin(tmp_path)
    calls = tmp_path / "calls"
    calls.touch()
    result = _run(SCRIPT, root, bindir, calls)
    assert result.returncode == 0, result.stderr
    return root, bindir, calls


@pytest.fixture(scope="module")
def generalized(tmp_path_factory):
    """One run, read by every test that does not run anything after it."""
    return _generalize(tmp_path_factory.mktemp("generalize"))


class TestWhatTheDiskKeeps:
    @pytest.mark.parametrize("rel", REMOVED)
    def test_what_names_the_build_is_removed(self, generalized, rel):
        root, _, _ = generalized
        assert not (root / rel).exists(), rel

    @pytest.mark.parametrize("rel", KEPT)
    def test_the_shape_provision_made_stays(self, generalized, rel):
        root, _, _ = generalized
        assert (root / rel).is_file(), rel

    @pytest.mark.parametrize("rel", TRUNCATED)
    def test_logs_are_emptied(self, generalized, rel):
        root, _, _ = generalized
        assert (root / rel).read_text() == ""

    def test_the_stack_directory_holds_the_checkout_alone(self, generalized):
        root, _, _ = generalized
        assert sorted(p.name for p in (root / "srv/app/istota").iterdir()) == ["src"]

    def test_limas_mount_lines_leave_fstab_and_the_root_stays(self, generalized):
        root, _, _ = generalized
        fstab = (root / "etc" / "fstab").read_text()
        assert "comment=cloudconfig" not in fstab
        assert fstab.startswith("UUID=abc / ext4")


class TestTheCommandsItRuns:
    def test_docker_is_emptied_and_stopped_and_cloud_init_forgets_the_instance(self, generalized):
        _, _, calls = generalized
        lines = calls.read_text().splitlines()
        assert "docker system prune -af --volumes" in lines
        assert "systemctl stop docker.service docker.socket containerd.service" in lines
        assert "cloud-init clean --logs --seed --machine-id" in lines
        assert "apt-get clean" in lines

    def test_the_finish_runs_detached_from_root_not_run(self, generalized):
        root, _, calls = generalized
        started = [line for line in calls.read_text().splitlines() if line.startswith("systemd-run")]
        # /run is noexec on Debian 13: the script is bash's argument, under /root.
        assert started == ["systemd-run --no-block --unit=istota-generalize-finish --collect "
                           "/bin/bash /root/istota-generalize-finish.sh"]
        assert (root / "root" / "istota-generalize-finish.sh").is_file()


class TestTheFinish:
    def test_it_removes_the_build_user_trims_and_powers_off(self, tmp_path):
        root, bindir, calls = _generalize(tmp_path)
        calls.write_text("")
        finish = root / "root" / "istota-generalize-finish.sh"
        result = _run(finish, root, bindir, calls)
        assert result.returncode == 0, result.stderr
        lines = calls.read_text().splitlines()
        # Only the login account under /home: not root, not istota at 10001.
        assert [line for line in lines if line.startswith("userdel")] == ["userdel -r -f build"]
        assert "pkill -KILL -u build" in lines
        assert lines.index("fstrim -av") < lines.index("systemctl poweroff")
        assert not (root / "etc" / "sudoers.d" / "90-lima-users").exists()
        assert not (root / "home" / "build.linux").exists()
        assert not finish.exists(), "the finish script stays on the disk"


def test_it_refuses_a_machine_with_no_checkout(tmp_path):
    root = tmp_path / "root"
    (root / "srv/app/istota").mkdir(parents=True)
    calls = tmp_path / "calls"
    result = _run(SCRIPT, root, _bin(tmp_path), calls)
    assert result.returncode == 1
    assert "not a checkout" in result.stderr
    assert not calls.exists() or "docker" not in calls.read_text()
