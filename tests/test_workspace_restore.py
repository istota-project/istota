"""`istota migrate restore-workspace`: a stack rebuilt from its own workspace.

The local VM's recovery: the workspace (the Mac folder) holds the database
snapshots and the config copies `db_backup` wrote, the master key comes on
stdin from the Mac's Keychain, and the stack it goes onto is new. Each case
builds a source install, backs it up into the workspace through `db_backup`
itself, throws the install's state away, and restores into an empty data
directory, as a new VM has.
"""

from __future__ import annotations

import io
import re
import sqlite3
from pathlib import Path

import pytest

from istota import db
from istota.config import load_config
from istota.maintenance import db_backup, db_restore, workspace_restore

KEY = "5e" * 32
DATE = "2026-10-10"
OLDER = "2026-10-08"


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch):
    """A developer's own scheduler holding the flock must not turn these into refusals."""
    monkeypatch.setattr(db_restore, "_daemon_running", lambda *a, **k: False)
    monkeypatch.delenv("ISTOTA_WORKSPACE_MARKER_REQUIRED", raising=False)


class World:
    def __init__(self, root: Path):
        self.root = root
        self.workspace = root / "workspace"
        self.data = root / "data"
        self.config_dir = self.data / "config"
        self.workspace.mkdir()
        self.config_text = (
            f'db_path = "{self.data}/db/istota.db"\n'
            f'workspace_path = "{self.workspace}"\n'
            'bot_name = "Istota"\n'
        )
        self.admins_text = "alice\n"

    @property
    def db_path(self) -> Path:
        return self.data / "db" / "istota.db"

    def config_copy(self, date: str = DATE) -> Path:
        return self.workspace / "Backups" / "config" / date

    def back_up(self, date: str = DATE, *, tasks: int = 2, places: int = 1) -> None:
        """A running install with `tasks` tasks and a DB-only user, snapshotted on `date`."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "config.toml").write_text(self.config_text)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db.init_db(self.db_path)
        from istota import user_profiles

        user_profiles.ensure_profile(self.db_path, "alice")
        with db.get_db(self.db_path) as conn:
            for n in range(tasks):
                db.create_task(conn, prompt=f"task {n}", user_id="alice")
        config = load_config(self.config_dir / "config.toml")
        assert "alice" in config.users
        from istota.location.db import init_db as init_location

        location = config.module_db_path("alice", "location")
        init_location(location)
        conn = sqlite3.connect(location)
        for n in range(places):
            conn.execute("INSERT INTO places (name, lat, lon) VALUES (?, 1.0, 2.0)", (f"{date}-p{n}",))
        conn.commit()
        conn.close()
        results = db_backup.backup_databases(config, today=date, record_last_run=False)
        assert {r["status"] for r in results} <= {"ok", "skip_missing"}, results
        copy = self.config_copy(date)
        copy.mkdir(parents=True)
        (copy / "config.toml").write_text(self.config_text)
        (copy / "admins").write_text(self.admins_text)

    def lose_the_vm(self) -> None:
        import shutil

        shutil.rmtree(self.data)
        self.data.mkdir()
        self.config_dir.mkdir()

    def restore(self, *args: str, key: str = KEY) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        rc = workspace_restore.main(
            ["--workspace", str(self.workspace), "--config-dir", str(self.config_dir),
             "--data-dir", str(self.data), *args],
            stdin=io.StringIO(key + "\n"), out=out, err=err,
        )
        return rc, out.getvalue(), err.getvalue()

    def tasks(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
        finally:
            conn.close()


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


class TestARestoreOntoANewVm:
    def test_the_key_config_and_databases_come_back(self, world):
        world.back_up()
        world.lose_the_vm()
        rc, out, err = world.restore()
        assert rc == 0, out + err
        key = world.data / ".secret_key"
        assert key.read_text() == KEY
        assert key.stat().st_mode & 0o777 == 0o600
        assert (world.config_dir / "config.toml").read_text() == world.config_text
        assert (world.config_dir / "admins").read_text() == world.admins_text
        assert (world.config_dir / "config.toml").stat().st_mode & 0o777 == 0o600
        assert world.tasks() == 2

    def test_a_user_known_only_to_the_database_gets_their_module_databases(self, world):
        """config.toml names no user; the restored framework database does, so the
        module databases are restored after it, from the users it holds."""
        world.back_up(places=3)
        world.lose_the_vm()
        rc, out, err = world.restore()
        assert rc == 0, out + err
        config = load_config(world.config_dir / "config.toml")
        location = config.module_db_path("alice", "location")
        conn = sqlite3.connect(location)
        assert conn.execute("SELECT count(*) FROM places").fetchone()[0] == 3
        conn.close()
        assert "location:alice" in out

    def test_a_date_picks_that_days_config_and_snapshots(self, world):
        world.back_up(OLDER, tasks=1)
        world.back_up(DATE, tasks=1)
        world.lose_the_vm()
        rc, out, err = world.restore("--date", OLDER)
        assert rc == 0, out + err
        assert world.tasks() == 1
        assert OLDER in out

    def test_without_a_date_the_newest_copy_is_used(self, world):
        world.back_up(OLDER, tasks=1)
        world.config_text += "# newer\n"
        world.back_up(DATE, tasks=1)
        world.lose_the_vm()
        rc, out, err = world.restore()
        assert rc == 0, out + err
        assert (world.config_dir / "config.toml").read_text().endswith("# newer\n")

    def test_it_says_the_credentials_are_not_in_the_backups(self, world):
        world.back_up()
        world.lose_the_vm()
        rc, out, _ = world.restore()
        assert rc == 0
        assert "secrets/" in out


def _nothing_written(world: World) -> None:
    assert not (world.data / ".secret_key").exists()
    assert not (world.config_dir / "config.toml").exists()
    assert not world.db_path.exists()


class TestRefusalsWriteNothing:
    def test_no_config_copy(self, world):
        world.back_up()
        import shutil

        shutil.rmtree(world.workspace / "Backups" / "config")
        world.lose_the_vm()
        rc, _, err = world.restore()
        assert rc == 1
        assert "Backups/config" in err
        _nothing_written(world)

    def test_no_snapshot_of_that_date(self, world):
        world.back_up()
        world.lose_the_vm()
        world.config_copy(OLDER).mkdir(parents=True)
        (world.config_copy(OLDER) / "config.toml").write_text(world.config_text)
        rc, _, err = world.restore("--date", OLDER)
        assert rc == 1
        assert "snapshot" in err
        _nothing_written(world)

    def test_an_empty_framework_snapshot_needs_force(self, world, monkeypatch):
        """db_restore's own rule, read through its row count. A schema-only
        framework database counts its migration bookkeeping as rows, so the
        count is what is stood in for here."""
        world.back_up()
        world.lose_the_vm()
        monkeypatch.setattr(db_restore, "_data_row_count", lambda path: 0)
        rc, _, err = world.restore()
        assert rc == 1
        assert "--force" in err
        _nothing_written(world)
        rc, out, err = world.restore("--force")
        assert rc == 0, out + err

    @pytest.mark.parametrize("key", ["", "short", "x" * 40 + " y"])
    def test_a_key_that_is_not_one(self, world, key):
        world.back_up()
        world.lose_the_vm()
        rc, _, err = world.restore(key=key)
        assert rc == 1
        assert "master key" in err
        _nothing_written(world)

    def test_a_different_key_already_there(self, world):
        world.back_up()
        world.lose_the_vm()
        (world.data / ".secret_key").write_text("ab" * 32)
        rc, _, err = world.restore()
        assert rc == 1
        assert "different master key" in err
        assert (world.data / ".secret_key").read_text() == "ab" * 32
        assert not (world.config_dir / "config.toml").exists()

    def test_a_different_config_already_there(self, world):
        world.back_up()
        world.lose_the_vm()
        (world.config_dir / "config.toml").write_text("# someone's\n")
        rc, _, err = world.restore()
        assert rc == 1
        assert "config.toml" in err
        assert (world.config_dir / "config.toml").read_text() == "# someone's\n"
        assert not (world.data / ".secret_key").exists()

    def test_a_database_that_has_done_work(self, world):
        world.back_up()
        rc, _, err = world.restore()
        assert rc == 1
        assert "tasks" in err

    def test_a_link_in_place_of_the_config_copy(self, world):
        world.back_up()
        world.lose_the_vm()
        copy = world.config_copy() / "config.toml"
        elsewhere = world.root / "elsewhere.toml"
        elsewhere.write_text(world.config_text)
        copy.unlink()
        copy.symlink_to(elsewhere)
        rc, _, err = world.restore()
        assert rc == 1
        assert "not a plain file" in err
        _nothing_written(world)

    def test_a_config_copy_that_is_not_toml(self, world):
        world.back_up()
        world.lose_the_vm()
        (world.config_copy() / "config.toml").write_text("this is [not toml\n")
        rc, _, err = world.restore()
        assert rc == 1
        _nothing_written(world)

    def test_a_host_folder_without_its_marker(self, world, monkeypatch):
        world.back_up()
        world.lose_the_vm()
        monkeypatch.setenv("ISTOTA_WORKSPACE_MARKER_REQUIRED", "1")
        rc, _, err = world.restore()
        assert rc == 1
        assert ".istota-workspace" in err
        _nothing_written(world)
        (world.workspace / ".istota-workspace").write_text("")
        rc, out, err = world.restore()
        assert rc == 0, out + err

    def test_a_running_scheduler(self, world, monkeypatch):
        world.back_up()
        world.lose_the_vm()
        monkeypatch.setattr(db_restore, "_daemon_running", lambda *a, **k: True)
        rc, _, err = world.restore()
        assert rc == 1
        _nothing_written(world)


class TestARerun:
    def test_the_same_restore_again_changes_nothing_it_already_wrote(self, world):
        """The key and config a first run wrote are the same bytes on a second;
        only the database refusal stops it, since it now holds tasks."""
        world.back_up()
        world.lose_the_vm()
        assert world.restore()[0] == 0
        rc, _, err = world.restore()
        assert rc == 1
        assert "tasks" in err
        assert "different" not in err


class TestTheCli:
    def test_istota_migrate_restore_workspace_reaches_the_module(self, world, monkeypatch, capsys):
        world.back_up()
        world.lose_the_vm()
        import sys

        from istota import cli

        monkeypatch.setattr(sys, "argv", [
            "istota", "migrate", "restore-workspace", "--workspace", str(world.workspace),
            "--config-dir", str(world.config_dir), "--data-dir", str(world.data),
        ])
        monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        assert exit_info.value.code == 0, capsys.readouterr()
        assert (world.data / ".secret_key").read_text() == KEY


STACK_ENV = "COMPOSE_PROFILES=browser\nNGINX_PUBLISH=127.0.0.1:8181:80\nISTOTA_TAG=v1.0.0\n"
HOST_ENV = "STORAGE=host-folder\nINGRESS=local\nHOST_LOOPBACK_ALLOW=11434\n"


def _stack_files(world: World, date: str, stack: str = STACK_ENV, host: str = HOST_ENV) -> None:
    """What `istota-stack` copies in on update, setup and apply."""
    day = world.config_copy(date)
    day.mkdir(parents=True, exist_ok=True)
    (day / "stack.env").write_text(stack)
    (day / "host.env").write_text(host)


class TestTheStackFilesComeBack:
    """`.env` and `host.env` live on the VM, outside anything db_backup reads;
    `istota-stack` copies them into `Backups/config/<date>/` and the restore
    hands them back through `--vm-dir`, for `istota-stack` to put in place."""

    @pytest.fixture
    def vm_dir(self, world) -> Path:
        path = world.root / "vm"
        path.mkdir()
        return path

    def test_they_are_handed_back_private(self, world, vm_dir):
        world.back_up()
        _stack_files(world, DATE)
        world.lose_the_vm()
        rc, out, err = world.restore("--vm-dir", str(vm_dir))
        assert rc == 0, out + err
        assert (vm_dir / "stack.env").read_text() == STACK_ENV
        assert (vm_dir / "host.env").read_text() == HOST_ENV
        for name in ("stack.env", "host.env"):
            assert (vm_dir / name).stat().st_mode & 0o777 == 0o600

    def test_the_set_in_effect_on_the_day_restored_is_the_one(self, world, vm_dir):
        """Copied only when something changes them, so the set for day D is the
        newest at or before D, not one written after it."""
        _stack_files(world, OLDER, stack="COMPOSE_PROFILES=old\n")
        world.back_up(DATE)
        _stack_files(world, "2026-10-11", stack="COMPOSE_PROFILES=later\n")
        world.lose_the_vm()
        rc, out, err = world.restore("--date", DATE, "--vm-dir", str(vm_dir))
        assert rc == 0, out + err
        assert (vm_dir / "stack.env").read_text() == "COMPOSE_PROFILES=old\n"

    def test_with_none_at_or_before_the_day_the_newest_is_used(self, world, vm_dir):
        world.back_up(OLDER)
        _stack_files(world, DATE)
        world.lose_the_vm()
        rc, out, err = world.restore("--date", OLDER, "--vm-dir", str(vm_dir))
        assert rc == 0, out + err
        assert (vm_dir / "stack.env").read_text() == STACK_ENV
        assert DATE in out

    def test_with_no_copy_at_all_nothing_is_handed_back(self, world, vm_dir):
        world.back_up()
        world.lose_the_vm()
        rc, out, err = world.restore("--vm-dir", str(vm_dir))
        assert rc == 0, out + err
        assert list(vm_dir.iterdir()) == []
        assert "stack.env" in out

    @pytest.mark.parametrize("text", ["not a setting\n", "lower=case\n", "A=\x00\n"])
    def test_a_copy_that_is_not_an_env_file_is_refused(self, world, vm_dir, text):
        world.back_up()
        _stack_files(world, DATE, stack=text)
        world.lose_the_vm()
        rc, _, err = world.restore("--vm-dir", str(vm_dir))
        assert rc == 1, err
        assert "stack.env" in err
        _nothing_written(world)
        assert list(vm_dir.iterdir()) == []

    def test_a_link_in_place_of_a_copy_is_refused(self, world, vm_dir):
        world.back_up()
        _stack_files(world, DATE)
        target = world.config_copy(DATE) / "host.env"
        target.unlink()
        target.symlink_to(world.root / "elsewhere")
        world.lose_the_vm()
        rc, _, err = world.restore("--vm-dir", str(vm_dir))
        assert rc == 1
        _nothing_written(world)


def _all_credentials_answers():
    from istota.setup_wizard import ContainerAnswers

    return ContainerAnswers(
        bot_name="Zed", user_id="alice", hostname="bot.example.test", ingress="proxied",
        upstream_proxy="10.0.0.5", listen_addr="10.0.0.20", listen_port=8080,
        nextcloud_url="http://nextcloud", nextcloud_username="zed-bot",
        nextcloud_app_password="cred-nc-app-password", talk_enabled=True,
        oauth_client_id="client-id", oauth_client_secret="cred-oauth-secret",
        brain_kind="native", native_base_url="https://llm.example.test/v1", native_model="m",
        native_api_key="cred-native-key", email_enabled=True, imap_host="imap.example.test",
        imap_user="zed@example.test", imap_password="cred-imap-password",
        smtp_host="smtp.example.test", developer_enabled=True,
        gitlab_token="cred-gitlab-token", github_token="cred-github-token",
        profiles=("browser", "signaling", "whatsapp-baileys"), session_secret="cred-" + "s" * 60,
    )


#: Every key `istota setup` writes into the two files the stack copies into the
#: workspace. A key added to either has to be added here, after asking whether
#: it may sit in a Time Machine folder beside the snapshots.
STACK_ENV_KEYS = {
    "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "DOMAIN", "ISTOTA_SECRETS_DIR",
    "NGINX_PUBLISH", "NGINX_PUBLISH_TLS", "ISTOTA_WEB_INSECURE_COOKIES",
}
HOST_ENV_KEYS = {
    "STORAGE", "INGRESS", "DOMAIN", "TLS_CERT_SOURCE", "UPSTREAM_PROXY",
    "LISTEN_ADDR", "LISTEN_PORT", "RCLONE_REMOTE",
}


class TestNeitherFileHoldsACredential:
    """`.env` and `host.env` are copied into the workspace, which on the local
    VM is a Mac folder Time Machine backs up, and the spec keeps every
    credential out of it. A key or a value that would put one there fails here."""

    @staticmethod
    def _env_keys(text: str) -> set[str]:
        return {line.split("=", 1)[0] for line in text.splitlines()
                if "=" in line and not line.startswith("#")}

    def test_the_keys_setup_writes_are_the_reviewed_ones(self):
        from istota.setup_wizard import render_host_env, render_stack_env

        a = _all_credentials_answers()
        assert self._env_keys(render_stack_env(a)) == STACK_ENV_KEYS
        assert self._env_keys(render_host_env(a)) == HOST_ENV_KEYS

    def test_no_credential_value_reaches_either_file(self):
        from istota.setup_wizard import container_secret_values, render_host_env, render_stack_env

        a = _all_credentials_answers()
        text = render_stack_env(a) + render_host_env(a)
        credentials = [value for value in container_secret_values(a).values() if value]
        assert len(credentials) >= 6, credentials
        for value in credentials:
            assert value not in text

    def test_no_other_host_env_key_is_a_credential(self):
        """The keys provision.sh documents as host.env's, which istota-stack, the
        Mac CLI and the management role write besides setup. The release
        signing key is a public key."""
        from tests.test_management_role import _documented_host_env_keys

        credential_shaped = {
            key for key in _documented_host_env_keys()
            if re.search(r"PASSWORD|TOKEN|SECRET|_KEY$|CREDENTIAL", key)
        }
        assert credential_shaped == {"RELEASE_SIGNING_KEY"}
