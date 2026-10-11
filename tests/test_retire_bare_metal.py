"""`istota-stack retire-bare-metal`: removing what the old Ansible role installed.

Run against a scratch root (`ISTOTA_RETIRE_ROOT`) with `systemctl`, `docker`,
`ufw`, `getent`, `userdel`, `groupdel` and `nginx` stubbed on PATH; each stub
records its calls. What a real host does with those calls is the vm tier's.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "host" / "istota-stack"

ROLE_FILES = [
    "etc/systemd/system/zorg-scheduler.service",
    "etc/systemd/system/zorg-web.service",
    "etc/systemd/system/zorg-webhooks.service",
    "etc/systemd/system/zorg-whatsapp-baileys.service",
    "etc/systemd/system/zorg-devbox-iptables.service",
    "etc/systemd/system/zorg-devbox-proxy@.service",
    "etc/cron.d/zorg-backup",
    "etc/logrotate.d/zorg",
    "etc/systemd/journald.conf.d/99-zorg.conf",
    "etc/tmpfiles.d/zorg-exec.conf",
    "etc/sysctl.d/99-zorg-sandbox.conf",
    "etc/modules-load.d/zorg-devbox.conf",
    "etc/nginx/conf.d/zorg.example.com.conf",
    "usr/local/bin/zorg",
    "usr/local/bin/zorg-run",
    "usr/local/bin/zorg-update.sh",
    "usr/local/sbin/istota-devbox-iptables",
    "etc/zorg/secrets.env",
    "var/lib/zorg/last-deployed-sha",
    "srv/app/zorg/data/istota.db",
]
KEPT_FILES = [
    "etc/systemd/system/mount-nextcloud.service",
    "etc/systemd/system/istota-stack.service",
    "etc/systemd/zram-generator.conf",
    "etc/sysctl.d/99-istota-sandbox.conf",
    "etc/cron.d/e2scrub_all",
    "etc/nginx/conf.d/other.example.com.conf",
    "srv/app/istota/host.env",
    "usr/local/bin/istota",
]

SYSTEMCTL = r"""#!/bin/bash
echo "systemctl $*" >> "$STUB_CALLS"
unit="${@: -1}"
case "$1 $2" in
  "is-active --quiet")
    [ "$unit" = istota-stack.service ] && exit "${STUB_STACK_INACTIVE:-0}"
    case " ${STUB_ACTIVE:-} " in *" $unit "*) exit 0 ;; esac; exit 3 ;;
  "is-enabled --quiet")
    case " ${STUB_ENABLED:-} " in *" $unit "*) exit 0 ;; esac; exit 1 ;;
esac
exit 0
"""
DOCKER = r"""#!/bin/bash
echo "docker $*" >> "$STUB_CALLS"
case "$*" in
  "ps -a --format {{.Names}}") printf '%s\n' zorg-browser istota-daemon-1 devbox-stefan zorgish ;;
  "volume ls --format {{.Name}}") printf '%s\n' zorg-devbox-home-stefan zorg_data istota_istota_data ;;
  "network ls --format {{.Name}}") printf '%s\n' bridge zorg_default zorg-devbox-net istota_default ;;
  "image ls --format {{.Repository}}:{{.Tag}}") printf '%s\n' zorg-browser:latest istota:t1 nginx:1 ;;
esac
exit 0
"""
UFW = r"""#!/bin/bash
echo "ufw $*" >> "$STUB_CALLS"
if [ "$*" = "status numbered" ]; then
  echo "[ 1] 22/tcp                     ALLOW IN    Anywhere"
  echo "[ 2] Anywhere                   ALLOW IN    172.18.0.0/16              # Docker containers (zorg)"
  echo "[ 3] 443/tcp                    ALLOW IN    192.168.100.10"
fi
exit 0
"""
GETENT = r"""#!/bin/bash
[ "$1 $2" = "passwd zorg" ] && echo "zorg:x:999:989::${STUB_HOME:-/srv/app/zorg}:/bin/bash" && exit 0
exit 2
"""
RECORD = '#!/bin/bash\necho "$(basename "$0") $*" >> "$STUB_CALLS"\nexit 0\n'


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "root"
    for rel in ROLE_FILES + KEPT_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    (root / "srv/app/istota/host.env").write_text("DOMAIN=zorg.example.com\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("systemctl", SYSTEMCTL), ("docker", DOCKER), ("ufw", UFW), ("getent", GETENT),
                       ("userdel", RECORD), ("groupdel", RECORD), ("nginx", RECORD)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    calls = tmp_path / "calls"
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "ISTOTA_RETIRE_ROOT": str(root),
        "ISTOTA_STACK_DIR": str(root / "srv/app/istota"),
        "ISTOTA_MOUNT_DIR": str(root / "srv/mount/nextcloud/content"),
        "STUB_CALLS": str(calls),
        "LANG": "C.UTF-8",
    }

    class Host:
        pass

    h = Host()
    h.root, h.env, h.calls = root, env, calls

    def run(*args, **extra):
        return subprocess.run(["bash", str(SCRIPT), "retire-bare-metal", *args],
                              capture_output=True, text=True, env={**env, **extra}, timeout=60)

    def recorded():
        return calls.read_text().splitlines() if calls.exists() else []

    h.run, h.recorded = run, recorded
    return h


def _all_present(h, files):
    return [rel for rel in files if not (h.root / rel).exists()]


class TestRefusals:
    @pytest.mark.parametrize("ns", ["", "istota", "../etc", "Zorg"])
    def test_a_namespace_that_is_not_a_plain_old_install_name(self, host, ns):
        result = host.run("--namespace", ns)
        assert result.returncode in (1, 64), result.stderr
        assert _all_present(host, ROLE_FILES + KEPT_FILES) == []

    def test_while_the_stack_is_not_running(self, host):
        result = host.run("--namespace", "zorg", STUB_STACK_INACTIVE="3")
        assert result.returncode == 1
        assert "istota-stack" in result.stderr
        assert _all_present(host, ROLE_FILES) == []

    @pytest.mark.parametrize("state", ["STUB_ENABLED", "STUB_ACTIVE"])
    def test_while_an_old_unit_is_enabled_or_running(self, host, state):
        result = host.run("--namespace", "zorg", **{state: "zorg-scheduler.service"})
        assert result.returncode == 1
        assert "zorg-scheduler" in result.stderr
        assert _all_present(host, ROLE_FILES) == []
        assert not any(c.startswith(("docker", "ufw", "userdel")) and " rm" in c or c.startswith("userdel")
                       for c in host.recorded())

    def test_a_home_outside_srv_app_namespace_is_left(self, host):
        result = host.run("--namespace", "zorg", STUB_HOME="/srv/app/istota")
        assert result.returncode == 0, result.stderr
        assert (host.root / "srv/app/istota/host.env").exists()
        assert not any(c.startswith("userdel") for c in host.recorded())


class TestADryRun:
    def test_it_lists_everything_and_changes_nothing(self, host):
        result = host.run("--namespace", "zorg", "--dry-run")
        assert result.returncode == 0, result.stderr
        assert _all_present(host, ROLE_FILES + KEPT_FILES) == []
        assert "zorg-scheduler.service" in result.stdout
        assert "/srv/app/zorg" in result.stdout
        changing = [c for c in host.recorded()
                    if c.startswith(("userdel", "groupdel")) or " rm " in f"{c} " or "delete" in c]
        assert changing == []


class TestRetiring:
    def test_it_removes_what_the_role_installed_and_nothing_else(self, host):
        result = host.run("--namespace", "zorg")
        assert result.returncode == 0, result.stdout + result.stderr
        assert [rel for rel in ROLE_FILES if (host.root / rel).exists()] == []
        assert not (host.root / "srv/app/zorg").exists()
        assert not (host.root / "etc/zorg").exists()
        assert _all_present(host, KEPT_FILES) == []

    def test_the_old_containers_volumes_networks_and_images_go(self, host):
        assert host.run("--namespace", "zorg").returncode == 0
        calls = host.recorded()
        assert "docker rm -f zorg-browser" in calls
        assert "docker volume rm zorg-devbox-home-stefan" in calls
        assert "docker volume rm zorg_data" in calls
        assert "docker network rm zorg_default" in calls
        assert "docker network rm zorg-devbox-net" in calls
        assert "docker image rm zorg-browser:latest" in calls
        touched = " ".join(c for c in calls if c.startswith("docker") and "rm" in c)
        for kept in ("istota", "devbox-stefan", "zorgish", "bridge", "nginx:1"):
            assert kept not in touched

    def test_the_roles_docker_ufw_rule_goes_and_no_other(self, host):
        assert host.run("--namespace", "zorg").returncode == 0
        deletes = [c for c in host.recorded() if c.startswith("ufw") and "delete" in c]
        assert deletes == ["ufw --force delete 2"]

    def test_the_service_user_goes_and_nginx_reloads(self, host):
        assert host.run("--namespace", "zorg").returncode == 0
        calls = host.recorded()
        assert "userdel zorg" in calls
        assert "nginx -t" in calls
        assert "systemctl daemon-reload" in calls
