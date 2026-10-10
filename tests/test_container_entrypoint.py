"""The istota container's unprivileged phase, run on the host against a stub CLI.

`docker/istota/entrypoint.sh` treats `config.toml` as an input: it refuses to
start without one and never writes it. Everything else it does is small and
checked here by running the real script with `istota` and `istota-scheduler`
replaced by stubs that record their argv, and with the real Python on PATH so
its one inline block (which reads the config and the user table) runs as it
does in the image. `ISTOTA_DATA_DIR` points it at a temporary state volume.

`istota-secrets`, which the drop runs before this script, is tested at the
bottom: it is what turns the compose secret files into the environment.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from istota import db, user_profiles

REPO = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO / "docker" / "istota" / "entrypoint.sh"
SECRETS = REPO / "docker" / "istota" / "istota-secrets"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="the entrypoint is bash")

LOCAL_CONFIG = """\
db_path = "{db}"
temp_dir = "{tmp}"

[talk]
enabled = false

[users.alice]
display_name = "Alice"
email_addresses = ["alice@example.test"]
"""

NEXTCLOUD_CONFIG = """\
db_path = "{db}"
temp_dir = "{tmp}"

[nextcloud]
url = "http://nextcloud"
username = "istota"
app_password = "pw"

[talk]
enabled = true

[users.alice]
display_name = "Alice"
"""

STUB = """\
#!/bin/sh
echo "$0 $*" >> "{log}"
case "$*" in
  *provision-rooms*) exit {rooms_rc} ;;
esac
exit 0
"""

SCHEDULER_STUB = """\
#!/bin/sh
echo "scheduler $*" >> "{log}"
echo "secret_key=${{ISTOTA_SECRET_KEY:+set}}" >> "{log}"
echo "web_token_key=${{ISTOTA_WEB_TOKEN_KEY:+set}}" >> "{log}"
echo "admins=$ISTOTA_ADMINS_FILE" >> "{log}"
"""


class Volume:
    def __init__(self, tmp_path: Path, config: str | None, *, admins: str = "alice\n",
                 rooms_rc: int = 0):
        self.root = tmp_path / "data"
        (self.root / "config").mkdir(parents=True)
        (self.root / "db").mkdir()
        self.db = self.root / "db" / "istota.db"
        self.log = tmp_path / "calls.log"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.config = self.root / "config" / "config.toml"
        if config is not None:
            self.config.write_text(config.format(db=self.db, tmp=tmp_path / "t"))
        (self.root / "config" / "admins").write_text(admins)
        for name, body in (
            ("istota", STUB.format(log=self.log, rooms_rc=rooms_rc)),
            ("istota-scheduler", SCHEDULER_STUB.format(log=self.log)),
        ):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        # `istota init` is stubbed, so the schema the inline block reads comes
        # from here, made the way `init` makes it.
        db.init_db(self.db)

    def run(self, **env: str) -> subprocess.CompletedProcess:
        environment = {
            "PATH": f"{self.bin}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}/usr/bin:/bin",
            "HOME": str(self.home),
            "ISTOTA_DATA_DIR": str(self.root),
            **env,
        }
        return subprocess.run(
            ["bash", str(ENTRYPOINT)], capture_output=True, text=True,
            env=environment, timeout=120,
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line.split(" ", 1)[1] if line.startswith("/") else line
                for line in self.log.read_text().splitlines()]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestTheConfigIsAnInput:
    def test_no_config_is_a_refusal_that_writes_nothing(self, tmp_path):
        volume = Volume(tmp_path, None)

        result = volume.run()

        assert result.returncode == 78, result.stdout + result.stderr
        assert "istota setup" in result.stderr
        assert volume.calls() == []
        assert not (volume.root / ".secret_key").exists()

    def test_the_config_is_never_modified(self, tmp_path):
        volume = Volume(tmp_path, LOCAL_CONFIG)
        before = _digest(volume.config)

        result = volume.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert _digest(volume.config) == before
        assert sorted(p.name for p in volume.config.parent.iterdir()) == ["admins", "config.toml"]


class TestTheBoot:
    def test_a_fresh_volume_gets_keys_init_and_the_first_admin(self, tmp_path):
        volume = Volume(tmp_path, LOCAL_CONFIG)

        result = volume.run()

        assert result.returncode == 0, result.stdout + result.stderr
        key = volume.root / ".secret_key"
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert len(key.read_text()) == 64
        assert stat.S_IMODE((volume.root / ".web_token_key").stat().st_mode) == 0o600
        calls = volume.calls()
        assert calls[0].endswith(f"-c {volume.config} init")
        # The first admin's row exists, seeded from their [users.alice] block:
        # a row owns every field once it exists, so an unseeded one would
        # drop the address the config gives them.
        profile = user_profiles.get_profile(volume.db, "alice")
        assert profile is not None
        assert profile.display_name == "Alice"
        assert profile.email_addresses == ["alice@example.test"]
        assert not any("provision-rooms" in c for c in calls)
        assert f"scheduler --daemon -c {volume.config}" in calls
        # The scheduler gets the master key and never the web-only one.
        assert "secret_key=set" in calls
        assert "web_token_key=" in calls
        assert f"admins={volume.root}/config/admins" in calls

    def test_an_existing_user_table_is_left_alone(self, tmp_path):
        volume = Volume(tmp_path, LOCAL_CONFIG)
        user_profiles.ensure_profile(volume.db, "bob", display_name="Bob")

        result = volume.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert user_profiles.get_profile(volume.db, "alice") is None

    def test_an_existing_master_key_is_kept(self, tmp_path):
        volume = Volume(tmp_path, LOCAL_CONFIG)
        (volume.root / ".secret_key").write_text("k" * 64)

        assert volume.run().returncode == 0

        assert (volume.root / ".secret_key").read_text() == "k" * 64

    def test_an_empty_master_key_file_is_a_refusal_not_a_new_key(self, tmp_path):
        volume = Volume(tmp_path, LOCAL_CONFIG)
        (volume.root / ".secret_key").write_text("")

        result = volume.run()

        assert result.returncode == 78
        assert (volume.root / ".secret_key").read_text() == ""
        assert volume.calls() == []

    def test_a_key_from_the_environment_is_used_and_no_file_is_made(self, tmp_path):
        volume = Volume(tmp_path, LOCAL_CONFIG)

        assert volume.run(ISTOTA_SECRET_KEY="e" * 64).returncode == 0

        assert not (volume.root / ".secret_key").exists()

    def test_nobody_in_the_admins_file_warns_and_ensures_nobody(self, tmp_path):
        volume = Volume(tmp_path, LOCAL_CONFIG, admins="# nobody yet\n")

        result = volume.run()

        assert result.returncode == 0
        assert "names nobody" in result.stdout
        assert user_profiles.list_profiles(volume.db) == {}


class TestTalkRooms:
    def test_a_configured_nextcloud_provisions_the_first_admins_rooms(self, tmp_path):
        volume = Volume(tmp_path, NEXTCLOUD_CONFIG)

        assert volume.run().returncode == 0

        assert any(c.endswith("nextcloud provision-rooms --user alice") for c in volume.calls())

    def test_a_failed_provisioning_does_not_stop_the_daemon(self, tmp_path):
        volume = Volume(tmp_path, NEXTCLOUD_CONFIG, rooms_rc=1)

        result = volume.run()

        assert result.returncode == 0
        assert "provisioning for alice failed" in result.stdout
        assert any(c.startswith("scheduler --daemon") for c in volume.calls())


class TestClaudeCode:
    def test_the_token_becomes_a_private_credentials_file_and_is_not_printed(self, tmp_path):
        volume = Volume(tmp_path, LOCAL_CONFIG)

        result = volume.run(CLAUDE_CODE_OAUTH_TOKEN="tok-not-printed")

        assert result.returncode == 0
        credentials = volume.home / ".claude" / ".credentials.json"
        assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
        assert '"accessToken": "tok-not-printed"' in credentials.read_text()
        assert "tok-not-printed" not in result.stdout + result.stderr


def test_the_entrypoint_is_under_the_spec_target():
    # The one-deployment-shape spec's target for this file: under 250 lines,
    # from 788. The bash room provisioning and the config render are gone.
    assert len(ENTRYPOINT.read_text().splitlines()) < 250


class TestIstotaSecrets:
    def _run(self, directory: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SECRETS), "env"], capture_output=True, text=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", "ISTOTA_SECRETS_DIR": str(directory)},
        )

    def test_each_file_becomes_its_uppercase_variable(self, tmp_path):
        (tmp_path / "istota_nextcloud_app_password").write_text("pw with spaces\n")
        (tmp_path / "claude_code_oauth_token").write_text("tok")

        result = self._run(tmp_path)

        assert result.returncode == 0, result.stderr
        seen = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        assert seen["ISTOTA_NEXTCLOUD_APP_PASSWORD"] == "pw with spaces"
        assert seen["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"

    def test_an_empty_file_exports_nothing(self, tmp_path):
        (tmp_path / "anthropic_api_key").write_text("")

        result = self._run(tmp_path)

        assert "ANTHROPIC_API_KEY=" not in result.stdout

    def test_a_name_that_is_not_a_plain_lowercase_word_is_ignored(self, tmp_path):
        (tmp_path / "Path").write_text("/evil")
        (tmp_path / "ld-preload").write_text("/evil.so")

        result = self._run(tmp_path)

        assert result.returncode == 0
        assert "/evil" not in result.stdout

    def test_no_directory_runs_the_command_unchanged(self, tmp_path):
        assert self._run(tmp_path / "absent").returncode == 0

    @pytest.mark.requires_dac
    def test_an_unreadable_file_is_a_refusal(self, tmp_path):
        path = tmp_path / "istota_email_imap_password"
        path.write_text("x")
        path.chmod(0)

        result = self._run(tmp_path)

        assert result.returncode == 78
        assert "chown 10001:10001" in result.stderr
