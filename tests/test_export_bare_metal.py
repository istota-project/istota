"""`host/export-bare-metal.sh`: the source half of the bare-metal migration.

The host is a temporary tree: a home holding the checkout's config and a venv
python (this interpreter), a real `init_db` database, `/etc` replaced through
`ISTOTA_EXPORT_ETC`, and stubs for `systemctl` and `mountpoint` that record
their arguments. What the script must never get wrong is the order on a live
host: refuse while the update cron can bring the units back, stop them before
the cache check, refuse while an upload is pending, and leave no snapshot or
secrets copy behind. The last test takes the tarball through the import tool.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

from istota import db
from istota.lib import toml_write
from istota.maintenance import bare_metal_import as bmi
from istota.setup_wizard import SECRET_NAMES

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "host" / "export-bare-metal.sh"
FIXTURE = REPO / "tests" / "fixtures" / "bare_metal" / "role-config.toml"
SECRET_KEY = "d" * 64

# A unit named in STUB_MISSING_UNITS is one systemd has no file for: `show`
# reports it not-found and `stop` fails on it with exit 5, as systemctl does.
SYSTEMCTL = r"""#!/bin/bash
printf '%s\n' "$*" >> "$STUB_CALLS"
case "$1" in
  show)
    for unit in ${STUB_MISSING_UNITS:-}; do
      [ "$unit" = "${!#}" ] && { echo not-found; exit 0; }
    done
    echo loaded ;;
  is-active) exit 3 ;;
  stop)
    for unit in ${STUB_MISSING_UNITS:-}; do
      case " $* " in *" $unit "*) echo "Unit $unit.service not loaded." >&2; exit 5 ;; esac
    done
    if [ "$2" = "mount-nextcloud" ]; then rm -f "$STUB_MOUNTED"; fi ;;
esac
exit 0
"""

# Root, as far as the script can tell: `id -u` answers 0, `runuser` marks what
# it runs, and `chown` does nothing.
ID = r"""#!/bin/bash
if [ "$1" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi
"""

RUNUSER = r"""#!/bin/bash
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
UNDER_RUNUSER=1 exec "$@"
"""

CHOWN = "#!/bin/sh\nexit 0\n"

MOUNTPOINT = r"""#!/bin/bash
[ -e "$STUB_MOUNTED" ]
"""


class Host:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.home = tmp_path / "srv-app-istota"
        self.etc = tmp_path / "etc"
        self.mount = tmp_path / "mount"
        self.calls = tmp_path / "calls"
        self.mounted = tmp_path / "mounted"
        checkout = self.home / "istota"
        (checkout / "config").mkdir(parents=True)
        (checkout / ".venv" / "bin").mkdir(parents=True)
        # A wrapper, not a symlink: a symlinked interpreter finds no pyvenv.cfg
        # beside itself and loses the venv that has istota installed.
        python = checkout / ".venv" / "bin" / "python"
        # It records whether `runuser` started it, when PYTHON_LOG is set.
        python.write_text(
            '#!/bin/sh\n'
            'if [ -n "${PYTHON_LOG:-}" ]; then echo "${UNDER_RUNUSER:-unwrapped}" >> "$PYTHON_LOG"; fi\n'
            f'exec "{sys.executable}" "$@"\n'
        )
        python.chmod(0o755)

        config = tomllib.loads(FIXTURE.read_text())
        data = self.home / "data"
        config["db_path"] = str(data / "istota.db")
        config["module_data_dir"] = str(data / "modules")
        config["temp_dir"] = str(self.home / "tmp")
        config["workspace_path"] = config["nextcloud_mount_path"] = str(self.mount)
        config["developer"]["repos_dir"] = str(self.home / "code")
        (checkout / "config" / "config.toml").write_text(toml_write.dumps(config))

        data.mkdir()
        db.init_db(data / "istota.db")
        (self.home / "code" / "alice" / "acme").mkdir(parents=True)
        (self.home / "code" / "alice" / "acme" / "README").write_text("x")
        (data / "whatsapp-baileys-session").mkdir()
        (data / "whatsapp-baileys-session" / "creds.json").write_text("{}")
        self.mount.mkdir()
        self.mounted.write_text("")

        (self.etc / "istota").mkdir(parents=True)
        (self.etc / "istota" / "secrets.env").write_text(
            f"ISTOTA_SECRET_KEY={SECRET_KEY}\nCLAUDE_CODE_OAUTH_TOKEN=claude-token\n"
        )
        (self.etc / "istota" / "web-secrets.env").write_text("ISTOTA_WEB_TOKEN_KEY=" + "e" * 64 + "\n")
        (self.etc / "istota" / "admins").write_text("alice\n")
        (self.etc / "cron.d").mkdir()
        self.cron = self.etc / "cron.d" / "istota-update"
        self.cron.write_text('MAILTO=""\n*/2 * * * * root /usr/local/bin/istota-update.sh\n')

        stubs = self.stubs = tmp_path / "stubs"
        stubs.mkdir()
        for name, body in (("systemctl", SYSTEMCTL), ("mountpoint", MOUNTPOINT)):
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "ISTOTA_EXPORT_ETC": str(self.etc),
            "STUB_CALLS": str(self.calls),
            "STUB_MOUNTED": str(self.mounted),
        }

    def as_root(self) -> Path:
        """Stub the script into its root path; return the venv python's log."""
        for name, body in (("id", ID), ("runuser", RUNUSER), ("chown", CHOWN)):
            (self.stubs / name).write_text(body)
            (self.stubs / name).chmod(0o755)
        log = self.tmp / "python-log"
        self.env["PYTHON_LOG"] = str(log)
        return log

    def disable_cron(self) -> None:
        self.cron.write_text('MAILTO=""\n# */2 * * * * root /usr/local/bin/istota-update.sh\n')

    def dirty(self) -> None:
        meta = self.home / ".cache" / "rclone" / "vfsMeta" / "nextcloud" / "Users"
        meta.mkdir(parents=True)
        (meta / "a.md").write_text('{"Path": "Users/a.md", "Dirty": true}')

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), "--namespace", "istota", "--home", str(self.home), *args],
            capture_output=True, env=self.env, timeout=120,
        )

    def system_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def leftovers(self) -> list[Path]:
        return list((self.home / "data").glob(".export.*"))


def _members(stdout: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(stdout)) as archive:
        return {member.name.lstrip("./"): member for member in archive.getmembers()}


def _manifest(stdout: bytes) -> dict[str, str]:
    with tarfile.open(fileobj=io.BytesIO(stdout)) as archive:
        text = archive.extractfile("MANIFEST").read().decode()
    return dict(line.split("=", 1) for line in text.splitlines())


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def test_the_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


class TestTheStoppedExport:
    def test_an_enabled_update_cron_refuses_before_anything_stops(self, host):
        result = host.run()
        assert result.returncode == 1
        assert b"update cron" in result.stderr
        assert result.stdout == b""
        assert host.system_calls() == []

    def test_it_stops_the_units_releases_the_mount_and_writes_the_tarball(self, host):
        host.disable_cron()
        result = host.run()
        assert result.returncode == 0, result.stderr.decode()
        calls = host.system_calls()
        stop_units = calls.index(
            "stop istota-scheduler istota-web istota-webhooks istota-whatsapp-baileys"
        )
        assert "stop istota-devbox-proxy@*.service" in calls
        assert "disable istota-scheduler istota-web istota-webhooks istota-whatsapp-baileys" in calls
        assert calls.index("stop mount-nextcloud") > stop_units
        assert not host.mounted.exists()

        members = _members(result.stdout)
        for name in ("MANIFEST", "config/config.toml", "config/admins", "secrets/secrets.env",
                     "secrets/web-secrets.env", "repos/alice/acme/README",
                     "whatsapp-baileys-session/creds.json"):
            assert name in members, name
        assert any(n.startswith("db/") and n.endswith("framework/istota.db") for n in members)
        assert not any(n.startswith("browser-profile") for n in members)
        assert _manifest(result.stdout)["mode"] == "stopped"
        assert host.leftovers() == []

    def test_the_devbox_users_are_read_off_their_home_volumes(self, host):
        host.disable_cron()
        docker = host.stubs / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            "[ \"$1 $2\" = \"volume ls\" ] || exit 1\n"
            "printf '%s\\n' istota-devbox-home-alice other-devbox-home-eve istota-devbox-home-bob istota_data\n"
        )
        docker.chmod(0o755)
        result = host.run()
        assert result.returncode == 0, result.stderr.decode()
        assert _manifest(result.stdout)["devbox_users"] == "alice bob"

    def test_no_docker_means_no_devbox_users(self, host):
        host.disable_cron()
        docker = host.stubs / "docker"
        docker.write_text("#!/bin/sh\nexit 127\n")
        docker.chmod(0o755)
        result = host.run()
        assert result.returncode == 0, result.stderr.decode()
        assert _manifest(result.stdout)["devbox_users"] == ""

    def test_a_unit_systemd_does_not_know_is_not_stopped(self, host):
        """An install that never ran WhatsApp has no Baileys unit, and a stop
        naming it fails the whole stop."""
        host.disable_cron()
        host.env["STUB_MISSING_UNITS"] = "istota-whatsapp-baileys"
        result = host.run()
        assert result.returncode == 0, result.stderr.decode()
        calls = host.system_calls()
        assert "stop istota-scheduler istota-web istota-webhooks" in calls
        assert not any(c.startswith("stop") and "whatsapp-baileys" in c for c in calls)
        assert _manifest(result.stdout)["mode"] == "stopped"

    def test_the_venv_python_never_runs_as_root(self, host):
        """The service user can write the venv, and a `.pth` file there runs at
        interpreter start: root must not start that interpreter."""
        host.disable_cron()
        log = host.as_root()
        result = host.run()
        assert result.returncode == 0, result.stderr.decode()
        runs = log.read_text().split()
        assert len(runs) >= 5, runs  # four config reads and the snapshot
        assert set(runs) == {"1"}, runs

    def test_a_pending_upload_refuses_and_writes_nothing(self, host):
        host.disable_cron()
        host.dirty()
        result = host.run("--dirty-timeout", "0")
        assert result.returncode == 1
        assert b"Dirty" in result.stderr
        assert result.stdout == b""
        calls = host.system_calls()
        assert any(c.startswith("stop istota-scheduler") for c in calls)
        assert "stop mount-nextcloud" not in calls
        assert host.leftovers() == []

    def test_a_file_output_is_private(self, host):
        host.disable_cron()
        out = host.tmp / "export.tar"
        assert host.run("--output", str(out)).returncode == 0
        assert out.stat().st_mode & 0o777 == 0o600


class TestTheOnlineExport:
    def test_no_stop_leaves_everything_running_and_says_so(self, host):
        result = host.run("--no-stop")
        assert result.returncode == 0, result.stderr.decode()
        assert host.system_calls() == []
        assert host.mounted.exists()
        assert _manifest(result.stdout)["mode"] == "online"
        assert host.leftovers() == []


@pytest.mark.parametrize("args", [(), ("--no-stop",)], ids=["stopped", "online"])
def test_the_export_leaves_the_install_backup_clock_alone(host, args):
    """An online export runs beside the live scheduler, which seeds its backup
    clock from this file; moving it would defer the install's own backup."""
    host.disable_cron()
    clock = host.home / "data" / ".db_backup_last_run"
    clock.write_text("1700000000.0")
    result = host.run(*args)
    assert result.returncode == 0, result.stderr.decode()
    assert clock.read_text() == "1700000000.0"


class TestTheRoundTrip:
    def test_the_import_reads_what_the_export_wrote(self, host, tmp_path):
        host.disable_cron()
        result = host.run()
        assert result.returncode == 0, result.stderr.decode()
        unpacked = tmp_path / "unpacked"
        unpacked.mkdir()
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            archive.extractall(unpacked, filter="data")

        data = tmp_path / "data"
        secrets = tmp_path / "secrets"
        (data / "config").mkdir(parents=True)
        secrets.mkdir()
        for name in SECRET_NAMES:
            (secrets / name).write_text("")
        rc = bmi.main([
            "--source", str(unpacked), "--ingress", "proxied",
            "--config", str(data / "config" / "config.toml"),
            "--secrets-dir", str(secrets), "--data-dir", str(data),
        ])
        assert rc == bmi.EXIT_OK
        assert (data / ".secret_key").read_text() == SECRET_KEY
        assert (secrets / "claude_code_oauth_token").read_text() == "claude-token"
        assert (data / "db" / "istota.db").exists()
        assert (data / "repos" / "alice" / "acme" / "README").exists()
        assert (data / "db" / "whatsapp-baileys-session" / "creds.json").exists()
