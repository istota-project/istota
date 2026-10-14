"""The container half of ``istota setup``: what it writes, and that it loads.

The container ``config.toml`` is written once and then owned by the operator,
so these tests are the whole of what stands between an answer and the key it
should have reached. Each answer is checked against the loaded ``Config``
rather than the text, because the loader is the reader that matters.
"""

from __future__ import annotations

import re
import stat
import tomllib
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import yaml

from istota import apply, setup_wizard
from istota.config import SchedulerConfig, load_config
from istota.lib import rclone_conf
from istota.setup_wizard import (
    SECRET_NAMES,
    ContainerAnswers,
    container_secret_values,
    render_container_config,
    render_stack_env,
    render_host_env,
)

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "docker" / "docker-compose.yml"


def _full_answers(**overrides) -> ContainerAnswers:
    """Every optional part on, every credential distinct, so an answer that
    lands on the wrong key is told apart from one that lands on the right one."""
    answers = ContainerAnswers(
        bot_name="Zed",
        user_id="alice",
        display_name="Alice Example",
        timezone="Europe/Berlin",
        user_email="alice@example.test",
        hostname="bot.example.test",
        ingress="proxied",
        upstream_proxy="10.0.0.5",
        listen_addr="10.0.0.20",
        listen_port=8080,
        nextcloud_url="http://nextcloud",
        nextcloud_public_url="https://cloud.example.test",
        nextcloud_username="zed-bot",
        nextcloud_app_password="nc-app-password",
        nextcloud_dav_prefix="Shared Files",
        nextcloud_auto_share_bot_dir=False,
        talk_enabled=True,
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
        brain_kind="native",
        native_base_url="https://llm.example.test/v1",
        native_model="model-x",
        native_api_key="native-key",
        email_enabled=True,
        imap_host="imap.example.test",
        imap_user="zed@example.test",
        imap_password="imap-password",
        smtp_host="smtp.example.test",
        bot_email="zed@example.test",
        location_enabled=True,
        money_enabled=False,
        developer_enabled=True,
        gitlab_token="gitlab-token",
        github_token="github-token",
        profiles=("browser", "signaling", "whatsapp-baileys"),
        session_secret="s" * 64,
    )
    for key, value in overrides.items():
        setattr(answers, key, value)
    return answers


def _load(tmp_path: Path, text: str, monkeypatch, env: dict[str, str] | None = None):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    for name, value in (env or {}).items():
        if value:
            monkeypatch.setenv(name.upper(), value)
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "admins"))
    return load_config(path)


class TestEveryAnswerReachesItsKey:
    def test_the_full_answer_set_loads_with_secrets_from_the_environment(
        self, tmp_path, monkeypatch,
    ):
        a = _full_answers()
        config = _load(
            tmp_path, render_container_config(a, inline_credentials=False),
            monkeypatch, env=container_secret_values(a),
        )

        assert config.bot_name == "Zed"
        assert str(config.db_path) == "/data/db/istota.db"
        assert str(config.workspace_path) == "/mnt/shared"
        assert str(config.workspace_mount_path) == "/mnt/shared"
        written = tomllib.loads(render_container_config(a, inline_credentials=False))
        assert written["workspace_mount_path"] == "/mnt/shared"
        assert "nextcloud_mount_path" not in written
        assert config.security.sandbox_enabled is True
        assert config.security.skill_proxy_enabled is True

        assert config.nextcloud.url == "http://nextcloud"
        assert config.nextcloud.username == "zed-bot"
        assert config.nextcloud.app_password == "nc-app-password"
        assert config.nextcloud.dav_prefix == "Shared Files"
        assert config.nextcloud.auto_share_bot_dir is False
        assert config.talk.enabled is True
        assert config.talk.bot_username == "zed-bot"
        assert config.talk.signaling.enabled is True
        assert config.talk.signaling.url == "http://signaling:8080"

        assert config.brain.kind == "native"
        assert config.brain.native.base_url == "https://llm.example.test/v1"
        assert config.brain.native.model == "model-x"
        assert config.brain.native.api_key == "native-key"

        assert config.email.enabled is True
        assert config.email.imap_host == "imap.example.test"
        assert config.email.imap_user == "zed@example.test"
        assert config.email.imap_password == "imap-password"
        assert config.email.smtp_host == "smtp.example.test"
        assert config.email.bot_email == "zed@example.test"

        assert config.location.enabled is True
        assert config.browser.enabled is True
        assert config.browser.api_url == "http://istota-browser:9223"
        assert config.whatsapp.enabled is True
        assert config.whatsapp.provider == "baileys"

        assert config.developer.enabled is True
        assert config.developer.repos_dir == "/data/repos"
        assert config.developer.gitlab_token == "gitlab-token"
        assert config.developer.github_token == "github-token"
        assert config.developer.gh_bin_path == "/usr/local/lib/istota_forge/gh"

        assert config.web.enabled is True
        assert config.web.auth == ["nextcloud", "email"]
        assert config.web.trusted_proxy_hops == 2
        assert config.web.token_storage == "encrypted"
        assert config.web.session_secret_key == "s" * 64
        assert config.web.oauth2_provider == "https://cloud.example.test"
        assert config.web.oauth2_client_id == "client-id"
        assert config.web.oauth2_client_secret == "client-secret"
        assert config.web.oauth2_token_endpoint == (
            "http://nextcloud/index.php/apps/oauth2/api/v1/token"
        )
        assert config.web.oauth2_redirect_uri == "https://bot.example.test/istota/callback"
        assert config.site.hostname == "bot.example.test"

        user = config.users["alice"]
        assert user.display_name == "Alice Example"
        assert user.timezone == "Europe/Berlin"
        assert user.email_addresses == ["alice@example.test"]
        assert user.disabled_modules == ["money"]

    def test_no_credential_is_in_the_file_when_secrets_are_files(self):
        a = _full_answers()
        text = render_container_config(a, inline_credentials=False)

        for name, value in container_secret_values(a).items():
            if value:
                assert value not in text, name

    def test_inline_credentials_load_without_any_environment(self, tmp_path, monkeypatch):
        a = _full_answers()
        config = _load(tmp_path, render_container_config(a, inline_credentials=True), monkeypatch)

        assert config.nextcloud.app_password == "nc-app-password"
        assert config.brain.native.api_key == "native-key"
        assert config.email.imap_password == "imap-password"
        assert config.web.oauth2_client_secret == "client-secret"
        assert config.web.session_secret_key == "s" * 64
        assert config.developer.gitlab_token == "gitlab-token"

    def test_local_storage_turns_talk_off_and_keeps_caldav(self, tmp_path, monkeypatch):
        a = ContainerAnswers(
            user_id="bob", caldav_url="https://dav.example.test", caldav_username="bob",
            caldav_password="dav-password", session_secret="t" * 64,
        )
        config = _load(
            tmp_path, render_container_config(a, inline_credentials=False),
            monkeypatch, env=container_secret_values(a),
        )

        assert config.storage_is_nextcloud is False
        assert config.talk.enabled is False
        assert config.caldav.url == "https://dav.example.test"
        assert config.caldav.password == "dav-password"
        assert config.web.auth == ["email"]
        assert config.web.trusted_proxy_hops == 1
        # The network proxy is left at the default, which is on, as on the
        # canonical shape; the wizard does not write the key at all.
        assert config.security.network.enabled is True
        assert "network" not in tomllib.loads(
            render_container_config(a, inline_credentials=False)
        ).get("security", {})

    def test_a_credential_with_toml_syntax_in_it_survives(self, tmp_path, monkeypatch):
        tricky = 'a"b\\c\nd\x1b'
        a = _full_answers(nextcloud_app_password=tricky)
        config = _load(tmp_path, render_container_config(a, inline_credentials=True), monkeypatch)
        assert config.nextcloud.app_password == tricky


class TestTheSecretFilesMatchCompose:
    def _compose(self) -> dict:
        return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    def test_compose_declares_exactly_the_wizards_secret_names(self):
        declared = set(self._compose().get("secrets", {}))
        assert declared == set(SECRET_NAMES)

    @pytest.mark.parametrize("service", ["daemon", "web"])
    def test_every_istota_image_service_mounts_every_secret(self, service):
        mounted = self._compose()["services"][service].get("secrets", [])
        assert sorted(mounted) == sorted(SECRET_NAMES)

    def test_each_secret_file_is_read_from_the_stack_secrets_dir(self):
        for name, spec in self._compose()["secrets"].items():
            assert spec == {"file": f"${{ISTOTA_SECRETS_DIR:-./secrets}}/{name}"}, name

    def test_each_name_is_a_variable_the_daemon_reads(self):
        """A secret file nothing reads is a credential written for nobody.

        The names come back uppercased into the environment by
        `istota-secrets`; every one but the two Claude Code credentials is a
        `load_config` override, and those two are read by the `claude` CLI.
        """
        source = (REPO / "src" / "istota" / "config.py").read_text(encoding="utf-8")
        for name in SECRET_NAMES:
            if name in ("anthropic_api_key", "claude_code_oauth_token"):
                continue
            assert f'"{name.upper()}"' in source, name


def _args(tmp_path: Path, **kw):
    base = dict(
        shape="container", vm_dir=str(tmp_path / "vm"), data_dir=str(tmp_path / "data"),
        yes=True, force=False, user="alice", display_name=None, timezone=None,
        user_email=None, bot_name=None, hostname=None, ingress=None,
        upstream_proxy=None, listen_addr=None, listen_port=None, tls_cert_source=None,
        nextcloud_url=None, nextcloud_public_url=None, nextcloud_user=None,
        nextcloud_dav_prefix=None, no_nextcloud_auto_share=False, no_talk=False,
        workspace=None,
        oauth_client_id=None, no_email_login=False, brain="native",
        native_base_url=None, native_model="model-x", email=False,
        imap_host=None, imap_user=None, smtp_host=None, bot_email=None, authserv_id=None,
        caldav_url=None, caldav_username=None, location=False, developer=False,
        profile=None, no_money=False, no_health=False, no_feeds=False,
        no_briefings=False, storage=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestTheRun:
    @pytest.fixture(autouse=True)
    def _credentials(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        monkeypatch.delenv("ISTOTA_SETUP_SHAPE", raising=False)

    def test_it_writes_the_files_with_their_modes(self, tmp_path):
        assert setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None) == 0

        data, vm = tmp_path / "data", tmp_path / "vm"
        config = data / "config" / "config.toml"
        assert _mode(config) == 0o600
        assert _mode(data / ".secret_key") == 0o600
        assert len((data / ".secret_key").read_text()) == 64
        assert "alice" in (data / "config" / "admins").read_text().split()
        assert _mode(vm / "secrets") == 0o700
        for name in SECRET_NAMES:
            assert _mode(vm / "secrets" / name) == 0o400, name
        assert (vm / "secrets" / "istota_brain_native_api_key").read_text() == "native-key"
        assert (vm / "secrets" / "anthropic_api_key").read_text() == ""
        assert "native-key" not in config.read_text()
        env = (vm / ".env").read_text()
        assert "COMPOSE_PROFILES=" in env and "ISTOTA_SECRETS_DIR=./secrets" in env
        assert "STORAGE=local" in (vm / "host.env").read_text()

    def test_it_marks_every_resource_unmanaged(self, tmp_path):
        """A fresh setup hands the config, the secrets and the admins to
        whoever edits next; a management plan claims them back on its run."""
        import json

        assert setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None) == 0

        marker = tmp_path / "data" / "config" / "managed.json"
        assert _mode(marker) == 0o600
        entries = json.loads(marker.read_text())
        assert set(entries) == {"config", "secrets", "admins"}
        for entry in entries.values():
            assert (entry["managed"], entry["source"]) == (False, "setup")
            assert entry["at"]

    def test_an_existing_config_is_refused_without_force(self, tmp_path):
        setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None)
        with pytest.raises(setup_wizard.SetupError, match="already exists"):
            setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None)

    def test_a_forced_rerun_keeps_the_master_key_and_the_session_key(self, tmp_path):
        setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None)
        key = (tmp_path / "data" / ".secret_key").read_text()
        session = (tmp_path / "vm" / "secrets" / "istota_web_session_secret_key").read_text()

        setup_wizard.run_setup(_args(tmp_path, force=True), out=lambda *_: None)

        assert (tmp_path / "data" / ".secret_key").read_text() == key
        assert (
            tmp_path / "vm" / "secrets" / "istota_web_session_secret_key"
        ).read_text() == session

    def test_a_rerun_with_no_answer_keeps_a_working_credential(self, tmp_path, monkeypatch):
        """A credential the re-run was not given is one the operator did not
        mean to remove: `istota-stack setup --force` without the token in the
        environment rewrote every file from the new answers and emptied it."""
        monkeypatch.setenv("ISTOTA_DEVELOPER_GITLAB_TOKEN", "glpat-working")
        setup_wizard.run_setup(_args(tmp_path, developer=True), out=lambda *_: None)
        secret = tmp_path / "vm" / "secrets" / "istota_developer_gitlab_token"
        assert secret.read_text() == "glpat-working"
        monkeypatch.delenv("ISTOTA_DEVELOPER_GITLAB_TOKEN")

        setup_wizard.run_setup(_args(tmp_path, developer=True, force=True), out=lambda *_: None)

        assert secret.read_text() == "glpat-working"
        assert _mode(secret) == 0o400

    def test_a_rerun_with_a_new_answer_replaces_the_credential(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_DEVELOPER_GITLAB_TOKEN", "glpat-working")
        setup_wizard.run_setup(_args(tmp_path, developer=True), out=lambda *_: None)
        monkeypatch.setenv("ISTOTA_DEVELOPER_GITLAB_TOKEN", "glpat-rotated")

        setup_wizard.run_setup(_args(tmp_path, developer=True, force=True), out=lambda *_: None)

        assert (tmp_path / "vm" / "secrets" / "istota_developer_gitlab_token").read_text() == "glpat-rotated"

    def test_without_a_vm_dir_credentials_go_into_the_private_config(self, tmp_path):
        setup_wizard.run_setup(_args(tmp_path, vm_dir=None), out=lambda *_: None)

        config = tmp_path / "data" / "config" / "config.toml"
        assert 'api_key = "native-key"' in config.read_text()
        assert _mode(config) == 0o600
        assert not (tmp_path / "vm").exists()

    def test_the_image_selects_the_container_half(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SETUP_SHAPE", "container")
        args = _args(tmp_path, shape=None)
        assert setup_wizard.setup_shape(args) == "container"
        monkeypatch.delenv("ISTOTA_SETUP_SHAPE")
        assert setup_wizard.setup_shape(_args(tmp_path, shape=None)) == "container"

    def test_outside_the_image_it_refuses_and_names_the_local_vm(self, tmp_path):
        """The standalone half is gone. Outside the image, with no --vm-dir,
        setup refuses before writing anything and names what replaced it."""
        args = _args(tmp_path, shape=None, vm_dir=None)
        with pytest.raises(setup_wizard.SetupError) as excinfo:
            setup_wizard.run_setup(args, out=lambda *_: None)
        assert "install.sh --local" in str(excinfo.value)
        assert "import-standalone" in str(excinfo.value)
        assert not (tmp_path / "data").exists()

    def test_update_is_no_longer_a_command(self, monkeypatch, capsys):
        from istota import cli

        monkeypatch.setattr("sys.argv", ["istota", "update"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
        assert "invalid choice: 'update'" in capsys.readouterr().err

    def test_proxied_ingress_without_an_upstream_is_refused(self, tmp_path):
        with pytest.raises(setup_wizard.SetupError, match="upstream"):
            setup_wizard.run_setup(
                _args(tmp_path, hostname="bot.example.test", ingress="proxied"),
                out=lambda *_: None,
            )
        assert not (tmp_path / "data" / "config" / "config.toml").exists()

    def test_proxied_ingress_refuses_a_wildcard_listener(self, tmp_path):
        with pytest.raises(setup_wizard.SetupError, match="0.0.0.0"):
            setup_wizard.run_setup(
                _args(
                    tmp_path, hostname="bot.example.test", ingress="proxied",
                    upstream_proxy="10.0.0.5", listen_addr="0.0.0.0",
                ),
                out=lambda *_: None,
            )

    def test_interactive_answers_reach_the_config(self, tmp_path, monkeypatch):
        """The prompt path, driven end to end with a scripted terminal."""
        answers = iter([
            "", "carol", "Carol", "UTC", "", "localhost", "local",
            "y", "http://nextcloud", "", "", "", "Shared Files", "n", "y", "",
            "claude_code", "n", "n", "y", "y", "y", "y", "n", "n", "y", "n",
        ])
        secrets = iter(["nc-pass", "oauth-token"])
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY")
        # Every opt-out module asked about, whatever extras this venv has.
        monkeypatch.setattr("istota.modules.module_available", lambda _name: True)
        args = _args(tmp_path, yes=False, user=None, brain=None, native_model=None)

        assert setup_wizard.run_setup(
            args, input_fn=lambda _prompt: next(answers),
            getpass_fn=lambda _prompt: next(secrets), out=lambda *_: None,
        ) == 0

        doc = tomllib.loads((tmp_path / "data" / "config" / "config.toml").read_text())
        assert doc["users"]["carol"]["display_name"] == "Carol"
        assert doc["nextcloud"]["dav_prefix"] == "Shared Files"
        assert doc["nextcloud"]["auto_share_bot_dir"] is False
        assert doc["brain"]["kind"] == "claude_code"
        assert "signaling" in doc["talk"]
        secrets_dir = tmp_path / "vm" / "secrets"
        assert (secrets_dir / "istota_nextcloud_app_password").read_text() == "nc-pass"
        assert (secrets_dir / "claude_code_oauth_token").read_text() == "oauth-token"


class TestTheStackFiles:
    def test_the_env_names_the_profiles_and_no_credential(self):
        a = _full_answers()
        env = render_stack_env(a)
        assert "COMPOSE_PROFILES=browser,signaling,location,whatsapp-baileys" in env
        for value in container_secret_values(a).values():
            if value:
                assert value not in env

    def test_a_rerun_keeps_the_lines_the_operator_added(self):
        existing = (
            "# mine\nCOMPOSE_PROFILES=old\nBOT_PASSWORD=keep-me\n"
            "BROWSER_MEMORY_LIMIT=4G\nCOMPOSE_PROFILES=dup\n"
        )
        text = render_stack_env(_full_answers(), existing)

        assert text.startswith("# mine\n")
        assert "BOT_PASSWORD=keep-me" in text and "BROWSER_MEMORY_LIMIT=4G" in text
        assert text.count("COMPOSE_PROFILES=") == 1
        assert "COMPOSE_PROFILES=browser,signaling,location,whatsapp-baileys" in text
        assert "ISTOTA_SECRETS_DIR=./secrets" in text

    def test_the_host_env_carries_the_ingress_choices(self):
        text = render_host_env(_full_answers())
        values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
        assert values == {
            "STORAGE": "nextcloud", "INGRESS": "proxied", "DOMAIN": "bot.example.test",
            "TLS_CERT_SOURCE": "", "UPSTREAM_PROXY": "10.0.0.5",
            "LISTEN_ADDR": "10.0.0.20", "LISTEN_PORT": "8080", "RCLONE_REMOTE": "nextcloud",
        }


class TestTheLocalWorkspace:
    """Local storage is the default, and its workspace is on the state volume."""

    def test_no_nextcloud_is_local_storage_under_data(self, tmp_path, monkeypatch):
        a = _full_answers(nextcloud_url="", oauth_client_id="")
        config = _load(tmp_path, render_container_config(a, inline_credentials=True), monkeypatch)

        assert config.storage_backend == "local"
        assert str(config.workspace_path) == "/data/workspace"
        assert config.workspace_mount_path is None


class TestTheHostFolder:
    """`--storage host-folder`: the local VM's workspace is a folder on the Mac,
    shared in and bound at /mnt/shared. A mount, so the liveness check and the
    backup's guard cover it; not Nextcloud, so the vocabulary is "workspace"."""

    def test_it_writes_the_mount_as_the_workspace_and_no_nextcloud(self, tmp_path, monkeypatch):
        a = _full_answers(nextcloud_url="", oauth_client_id="", host_folder=True)
        text = render_container_config(a, inline_credentials=True)
        config = _load(tmp_path, text, monkeypatch)

        assert str(config.workspace_path) == "/mnt/shared"
        assert str(config.workspace_mount_path) == "/mnt/shared"
        assert config.nextcloud.url == ""
        assert config.storage_is_nextcloud is False
        assert config.storage_backend == "local"
        assert "nextcloud" not in tomllib.loads(text)

    def test_the_host_env_says_host_folder(self):
        a = _full_answers(nextcloud_url="", oauth_client_id="", host_folder=True)
        assert _env(render_host_env(a))["STORAGE"] == "host-folder"

    def test_the_flag_selects_it(self):
        args = SimpleNamespace(yes=True, user="alice", storage="host-folder")
        a = setup_wizard.collect_container_answers(
            args, input_fn=input, out=lambda *_: None, getpass_fn=None,
        )
        assert a.host_folder is True
        assert a.uses_nextcloud is False

    def test_it_is_refused_beside_a_nextcloud(self):
        args = SimpleNamespace(yes=True, user="alice", storage="host-folder",
                               nextcloud_url="https://cloud.example.test")
        with pytest.raises(setup_wizard.SetupError, match="host-folder"):
            setup_wizard.collect_container_answers(
                args, input_fn=input, out=lambda *_: None, getpass_fn=None,
            )

    def test_a_rerun_without_the_flag_keeps_the_folder(self, tmp_path, monkeypatch):
        """A plain `istota-stack setup --force` on the local VM must not move the
        workspace off the user's folder onto the state volume."""
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        monkeypatch.delenv("ISTOTA_SETUP_SHAPE", raising=False)
        assert setup_wizard.run_setup(_args(tmp_path, storage="host-folder"), out=lambda *_: None) == 0
        assert setup_wizard.run_setup(_args(tmp_path, force=True), out=lambda *_: None) == 0

        assert _env((tmp_path / "vm" / "host.env").read_text())["STORAGE"] == "host-folder"
        written = tomllib.loads((tmp_path / "data" / "config" / "config.toml").read_text())
        assert written["workspace_mount_path"] == "/mnt/shared"

    def test_local_storage_asked_for_explicitly_leaves_the_folder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        monkeypatch.delenv("ISTOTA_SETUP_SHAPE", raising=False)
        setup_wizard.run_setup(_args(tmp_path, storage="host-folder"), out=lambda *_: None)
        setup_wizard.run_setup(_args(tmp_path, force=True, storage="local"), out=lambda *_: None)

        assert _env((tmp_path / "vm" / "host.env").read_text())["STORAGE"] == "local"


class TestMissedJobsUnderLocalIngress:
    """Local ingress is the local VM, which pauses while the Mac sleeps. A job
    or briefing missed by up to twelve hours runs once on wake, an older one
    is skipped (Stage 1 measured both); a server keeps the dataclass default."""

    def test_local_ingress_catches_up_for_twelve_hours(self, tmp_path, monkeypatch):
        a = _full_answers(ingress="local", hostname="localhost:8080", nextcloud_url="",
                          oauth_client_id="", host_folder=True)
        config = _load(tmp_path, render_container_config(a, inline_credentials=True), monkeypatch)
        assert config.scheduler.cron_max_staleness_minutes == 720

    @pytest.mark.parametrize("ingress", ["direct", "proxied"])
    def test_a_server_keeps_the_default(self, tmp_path, monkeypatch, ingress):
        a = _full_answers(ingress=ingress)
        text = render_container_config(a, inline_credentials=True)
        assert "scheduler" not in tomllib.loads(text)
        default = SchedulerConfig().cron_max_staleness_minutes
        assert _load(tmp_path, text, monkeypatch).scheduler.cron_max_staleness_minutes == default


class TestTheWorkspaceChoice:
    """With Nextcloud, the workspace is the rclone mount by default, or local
    on the state volume when the operator chooses it."""

    @pytest.fixture(autouse=True)
    def _credentials(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        monkeypatch.setenv("ISTOTA_NEXTCLOUD_APP_PASSWORD", "nc-pass")
        monkeypatch.delenv("ISTOTA_SETUP_SHAPE", raising=False)

    def _run(self, tmp_path, **kw) -> tuple[dict, dict[str, str]]:
        assert setup_wizard.run_setup(_args(tmp_path, **kw), out=lambda *_: None) == 0
        doc = tomllib.loads((tmp_path / "data" / "config" / "config.toml").read_text())
        host_env = _env((tmp_path / "vm" / "host.env").read_text())
        return doc, host_env

    def test_a_local_workspace_with_nextcloud_loads_as_local_with_talk(self, tmp_path, monkeypatch):
        a = _full_answers(workspace="local", nextcloud_dav_prefix="", nextcloud_auto_share_bot_dir=False)
        config = _load(tmp_path, render_container_config(a, inline_credentials=True), monkeypatch)

        assert config.nextcloud_enabled is True
        assert config.storage_is_nextcloud is False
        assert config.storage_backend == "local"
        assert str(config.workspace_path) == "/data/workspace"
        assert config.workspace_mount_path is None
        assert config.talk.enabled is True
        assert render_host_env(a).count("STORAGE=local") == 1

    def test_a_url_without_the_flag_is_the_mount(self, tmp_path):
        doc, host_env = self._run(tmp_path, nextcloud_url="http://nextcloud")

        assert doc["workspace_path"] == "/mnt/shared"
        assert doc["workspace_mount_path"] == "/mnt/shared"
        assert doc["nextcloud"]["auto_share_bot_dir"] is True
        assert host_env["STORAGE"] == "nextcloud"

    def test_the_local_flag_skips_the_prefix_and_the_share(self, tmp_path):
        doc, host_env = self._run(
            tmp_path, nextcloud_url="http://nextcloud", workspace="local",
            nextcloud_dav_prefix="Shared Files",
        )

        assert doc["workspace_path"] == "/data/workspace"
        assert "workspace_mount_path" not in doc and "nextcloud_mount_path" not in doc
        assert doc["nextcloud"]["url"] == "http://nextcloud"
        assert doc["nextcloud"]["dav_prefix"] == ""
        assert doc["nextcloud"]["auto_share_bot_dir"] is False
        assert doc["talk"]["enabled"] is True
        assert host_env["STORAGE"] == "local"
        assert (tmp_path / "vm" / "secrets" / "istota_nextcloud_app_password").read_text() == "nc-pass"

    def test_answering_local_asks_neither_the_prefix_nor_the_share(self, tmp_path, monkeypatch):
        prompts: list[str] = []
        answers = iter([
            "", "carol", "Carol", "UTC", "", "localhost", "local",
            "y", "http://nextcloud", "", "", "local", "y", "",
            "claude_code", "n", "n", "y", "y", "y", "y", "n", "n", "y", "n",
        ])

        def ask(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY")
        monkeypatch.setattr("istota.modules.module_available", lambda _name: True)
        args = _args(tmp_path, yes=False, user=None, brain=None, native_model=None)

        assert setup_wizard.run_setup(
            args, input_fn=ask, getpass_fn=lambda _prompt: "", out=lambda *_: None,
        ) == 0

        doc = tomllib.loads((tmp_path / "data" / "config" / "config.toml").read_text())
        assert doc["workspace_path"] == "/data/workspace"
        assert doc["nextcloud"]["auto_share_bot_dir"] is False
        assert any(p.startswith("Workspace (mount/local)") for p in prompts)
        assert not any("holds the workspace" in p or "over OCS" in p for p in prompts)
        assert _env((tmp_path / "vm" / "host.env").read_text())["STORAGE"] == "local"

    def test_an_unknown_choice_is_refused(self, tmp_path):
        with pytest.raises(setup_wizard.SetupError, match="mount or local"):
            setup_wizard.run_setup(
                _args(tmp_path, nextcloud_url="http://nextcloud", workspace="/srv/files"),
                out=lambda *_: None,
            )
        assert not (tmp_path / "data" / "config" / "config.toml").exists()

    def test_a_mount_without_nextcloud_is_refused(self, tmp_path):
        with pytest.raises(setup_wizard.SetupError, match="needs Nextcloud"):
            setup_wizard.run_setup(_args(tmp_path, workspace="mount"), out=lambda *_: None)

    def test_local_without_nextcloud_is_the_default_local_install(self, tmp_path):
        doc, host_env = self._run(tmp_path, workspace="local")

        assert doc["workspace_path"] == "/data/workspace"
        assert host_env["STORAGE"] == "local"


def _image_default_log_file() -> str:
    dockerfile = (REPO / "docker" / "istota" / "Dockerfile").read_text()
    match = re.search(r"^ENV ISTOTA_DEFAULT_LOG_FILE=(\S+)$", dockerfile, re.M)
    assert match, "the image declares no ISTOTA_DEFAULT_LOG_FILE"
    return match.group(1)


class TestTheAppLog:
    """ISSUE-703: the image's default app log is where the web container reads it."""

    def test_a_setup_config_loads_with_the_image_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_DEFAULT_LOG_FILE", _image_default_log_file())
        config = _load(tmp_path, render_container_config(_full_answers(), inline_credentials=True), monkeypatch)

        assert config.logging.output == "both"
        assert config.logging.file == _image_default_log_file()

    def test_the_file_is_on_the_volume_daemon_and_web_both_mount(self):
        log = Path(_image_default_log_file())
        services = yaml.safe_load(COMPOSE.read_text())["services"]

        for name in ("daemon", "web"):
            mounts = [v for v in services[name]["volumes"] if isinstance(v, str)]
            assert any(m.endswith(f":{setup_wizard.CONTAINER_DATA_DIR}") for m in mounts), name
        assert log.is_relative_to(setup_wizard.CONTAINER_DATA_DIR)

    def test_the_file_is_outside_every_directory_a_task_or_a_sweep_reaches(self):
        log = Path(_image_default_log_file())
        for root in (
            setup_wizard.CONTAINER_TEMP_DIR,
            setup_wizard.CONTAINER_LOCAL_WORKSPACE,
            setup_wizard.CONTAINER_REPOS_DIR,
            # The session transcripts' root, which their sweep prunes.
            str(Path(setup_wizard.CONTAINER_DB_PATH).parent),
        ):
            assert not log.is_relative_to(root), root


def _env(text: str) -> dict[str, str]:
    return dict(
        line.split("=", 1) for line in text.splitlines()
        if "=" in line and not line.startswith("#")
    )


class TestWhatNginxPublishes:
    """The two compose port slots nginx is given, per ingress mode."""

    def test_direct_publishes_both_ports_on_every_address(self):
        a = _full_answers(ingress="direct", tls_cert_source="acme")
        values = _env(render_stack_env(a))

        assert values["NGINX_PUBLISH"] == "80:80"
        assert values["NGINX_PUBLISH_TLS"] == "443:443"
        assert _env(render_host_env(a))["TLS_CERT_SOURCE"] == "acme"

    def test_proxied_publishes_the_plain_slot_on_the_private_address(self):
        values = _env(render_stack_env(_full_answers()))

        assert values["NGINX_PUBLISH"] == "10.0.0.20:8080:80"
        assert values["NGINX_PUBLISH_TLS"] == "127.0.0.1::443"

    def test_proxied_with_files_publishes_the_tls_slot_instead(self):
        a = _full_answers(tls_cert_source="files")
        values = _env(render_stack_env(a))

        assert values["NGINX_PUBLISH"] == "127.0.0.1::80"
        assert values["NGINX_PUBLISH_TLS"] == "10.0.0.20:8080:443"
        assert _env(render_host_env(a))["TLS_CERT_SOURCE"] == "files"

    def test_proxied_with_a_self_signed_certificate_publishes_the_tls_slot(self):
        a = _full_answers(tls_cert_source="self-signed")
        values = _env(render_stack_env(a))

        assert values["NGINX_PUBLISH"] == "127.0.0.1::80"
        assert values["NGINX_PUBLISH_TLS"] == "10.0.0.20:8080:443"
        assert _env(render_host_env(a))["TLS_CERT_SOURCE"] == "self-signed"

    def test_a_self_signed_certificate_is_only_for_proxied(self):
        """A public client cannot pin a certificate it was never given."""
        a = _full_answers(ingress="direct", tls_cert_source="self-signed")
        assert _env(render_host_env(a))["TLS_CERT_SOURCE"] != "self-signed"
        args = SimpleNamespace(
            yes=True, user="alice", hostname="bot.example.test", ingress="direct",
            tls_cert_source="self-signed",
        )
        with pytest.raises(setup_wizard.SetupError, match="reverse proxy"):
            setup_wizard.collect_container_answers(
                args, input_fn=input, out=lambda *_: None, getpass_fn=None,
            )

    def test_proxied_takes_self_signed_from_the_flag(self):
        args = SimpleNamespace(
            yes=True, user="alice", hostname="bot.example.test", ingress="proxied",
            upstream_proxy="10.0.0.5", listen_addr="10.0.0.20", listen_port=8080,
            tls_cert_source="self-signed",
        )
        a = setup_wizard.collect_container_answers(
            args, input_fn=input, out=lambda *_: None, getpass_fn=None,
        )
        assert setup_wizard.tls_cert_source(a) == "self-signed"

    def test_local_publishes_loopback_at_the_hostnames_port(self):
        a = _full_answers(ingress="local", hostname="localhost:8282")
        values = _env(render_stack_env(a))

        assert values["NGINX_PUBLISH"] == "127.0.0.1:8282:80"
        assert values["NGINX_PUBLISH_TLS"] == "127.0.0.1::443"
        assert _env(render_host_env(a))["TLS_CERT_SOURCE"] == ""

    def test_proxied_refuses_acme(self):
        args = SimpleNamespace(
            yes=True, user="alice", hostname="bot.example.test", ingress="proxied",
            upstream_proxy="10.0.0.5", listen_addr="10.0.0.20", listen_port=8080,
            tls_cert_source="acme",
        )

        with pytest.raises(setup_wizard.SetupError, match="never ACME"):
            setup_wizard.collect_container_answers(
                args, input_fn=input, out=lambda *_: None, getpass_fn=None,
            )


# ---------------------------------------------------------------------------
# One validator and one writer for both front ends
# ---------------------------------------------------------------------------

#: Every credential's environment name, cleared so the terminal path reads only
#: what a case sets and a developer's shell cannot leak into a comparison.
_CREDENTIAL_ENV = tuple(name.upper() for name in SECRET_NAMES)


@pytest.fixture
def _no_credentials_in_env(monkeypatch):
    for name in _CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("istota.modules.module_available", lambda _name: True)


_WEB_BASE = {
    "user_id": "alice",
    "brain_kind": "native",
    "native_model": "model-x",
    "native_api_key": "native-key",
}

#: (id, terminal flag overrides, web payload overrides, field, message match).
#: Each refusal `collect_container_answers` raised before the validator existed.
_REFUSALS = [
    ("no-user", {"user": None}, {"user_id": ""}, "user_id", "user id is required"),
    ("unknown-ingress", {"ingress": "sideways"}, {"ingress": "sideways"}, "ingress", "Unknown ingress"),
    (
        "proxied-without-upstream",
        {"hostname": "bot.example.test", "ingress": "proxied"},
        {"hostname": "bot.example.test", "ingress": "proxied"},
        "upstream_proxy", "upstream proxy",
    ),
    (
        "proxied-wildcard-listener",
        {"hostname": "bot.example.test", "ingress": "proxied", "upstream_proxy": "10.0.0.5",
         "listen_addr": "0.0.0.0"},
        {"hostname": "bot.example.test", "ingress": "proxied", "upstream_proxy": "10.0.0.5",
         "listen_addr": "0.0.0.0"},
        "listen_addr", "never 0.0.0.0",
    ),
    (
        "proxied-port-out-of-range",
        {"hostname": "bot.example.test", "ingress": "proxied", "upstream_proxy": "10.0.0.5",
         "listen_addr": "10.0.0.20", "listen_port": 70000},
        {"hostname": "bot.example.test", "ingress": "proxied", "upstream_proxy": "10.0.0.5",
         "listen_addr": "10.0.0.20", "listen_port": 70000},
        "listen_port", "between 1 and 65535",
    ),
    (
        "proxied-acme",
        {"hostname": "bot.example.test", "ingress": "proxied", "upstream_proxy": "10.0.0.5",
         "listen_addr": "10.0.0.20", "tls_cert_source": "acme"},
        {"hostname": "bot.example.test", "ingress": "proxied", "upstream_proxy": "10.0.0.5",
         "listen_addr": "10.0.0.20", "tls_cert_source": "acme"},
        "tls_cert_source", "never ACME",
    ),
    (
        "direct-self-signed",
        {"hostname": "bot.example.test", "ingress": "direct", "tls_cert_source": "self-signed"},
        {"hostname": "bot.example.test", "ingress": "direct", "tls_cert_source": "self-signed"},
        "tls_cert_source", "reverse proxy",
    ),
    ("native-without-model", {"native_model": None}, {"native_model": ""}, "native_model", "needs a model"),
    ("native-without-key", {}, {"native_api_key": ""}, "native_api_key", "needs a model"),
    (
        "authserv-id-with-a-version",
        {"email": True, "imap_host": "imap.example.test", "imap_user": "zed@example.test",
         "authserv_id": "mx.example.com 1"},
        {"email_enabled": True, "imap_host": "imap.example.test", "imap_user": "zed@example.test",
         "authserv_id": "mx.example.com 1"},
        "authserv_id", "single name",
    ),
]


class TestOneValidatorForBothFrontEnds:
    """Every refusal the terminal makes is a field error the web setup shows,
    with the same words, so neither front end can grow a rule the other lacks."""

    @pytest.mark.parametrize(
        "flags,payload,field_name,match",
        [case[1:] for case in _REFUSALS], ids=[case[0] for case in _REFUSALS],
    )
    def test_each_refusal_is_the_same_on_both_paths(
        self, tmp_path, monkeypatch, _no_credentials_in_env, flags, payload, field_name, match,
    ):
        if "native_api_key" not in payload:
            monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        with pytest.raises(setup_wizard.SetupError, match=match) as raised:
            setup_wizard.collect_container_answers(
                _args(tmp_path, **flags), input_fn=input, out=lambda *_: None, getpass_fn=None,
            )

        errors = setup_wizard.validate_container_answers(
            setup_wizard.answers_from_payload({**_WEB_BASE, **payload}),
        )

        assert errors[0] == setup_wizard.FieldError(field_name, str(raised.value))

    @pytest.mark.parametrize(
        "profiles,match",
        [(("nope",), "Unknown profile"), (("signaling",), "needs Nextcloud Talk")],
    )
    def test_a_profile_refusal_is_a_field_error_too(
        self, tmp_path, monkeypatch, _no_credentials_in_env, profiles, match,
    ):
        """The web setup offers only the browser, so these two are driven
        through the validator directly rather than through a payload."""
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        with pytest.raises(setup_wizard.SetupError, match=match) as raised:
            setup_wizard.collect_container_answers(
                _args(tmp_path, profile=list(profiles)),
                input_fn=input, out=lambda *_: None, getpass_fn=None,
            )

        errors = setup_wizard.validate_container_answers(
            ContainerAnswers(user_id="alice", profiles=profiles),
        )

        assert errors == [setup_wizard.FieldError("profiles", str(raised.value))]

    def test_the_web_path_gets_every_error_not_only_the_first(self, _no_credentials_in_env):
        a = setup_wizard.answers_from_payload({
            "ingress": "proxied", "hostname": "bot.example.test",
            "brain_kind": "native", "timezone": "Mars/Olympus",
        })

        fields = [e.field for e in setup_wizard.validate_container_answers(a)]

        assert fields == [
            "user_id", "timezone", "upstream_proxy", "listen_addr",
            "native_model", "native_api_key",
        ]

    def test_an_invalid_timezone_still_falls_back_on_the_terminal(
        self, tmp_path, monkeypatch, _no_credentials_in_env,
    ):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        said: list[str] = []
        a = setup_wizard.collect_container_answers(
            _args(tmp_path, timezone="Mars/Olympus"), input_fn=input,
            out=said.append, getpass_fn=None,
        )
        assert a.timezone == "UTC"
        assert any("not a valid IANA timezone" in line for line in said)

    def test_a_calendar_without_its_password_is_dropped_on_the_terminal_and_refused_on_the_web(
        self, tmp_path, monkeypatch, _no_credentials_in_env,
    ):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        a = setup_wizard.collect_container_answers(
            _args(tmp_path, caldav_url="https://dav.example.test", caldav_username="alice"),
            input_fn=input, out=lambda *_: None, getpass_fn=None,
        )
        assert a.caldav_url == ""

        web = setup_wizard.answers_from_payload({
            **_WEB_BASE, "caldav_url": "https://dav.example.test", "caldav_username": "alice",
        })
        assert [e.field for e in setup_wizard.validate_container_answers(web)] == ["caldav_password"]


class TestThePayload:
    def test_an_unknown_key_is_refused_not_ignored(self):
        with pytest.raises(setup_wizard.PayloadError) as raised:
            setup_wizard.answers_from_payload({**_WEB_BASE, "developer_enabled": True})
        assert "developer_enabled" in str(raised.value)

    def test_a_wrong_type_is_a_field_error(self):
        with pytest.raises(setup_wizard.PayloadError) as raised:
            setup_wizard.answers_from_payload({**_WEB_BASE, "email_enabled": "yes", "listen_port": True})
        assert [e.field for e in raised.value.errors] == ["email_enabled", "listen_port"]

    def test_only_the_browser_profile_is_offered(self):
        with pytest.raises(setup_wizard.PayloadError) as raised:
            setup_wizard.answers_from_payload({**_WEB_BASE, "profiles": ["signaling"]})
        assert raised.value.errors[0].field == "profiles"

    def test_every_allowed_key_but_the_storage_choice_is_an_answers_field(self):
        attributes = set(ContainerAnswers.__dataclass_fields__)
        assert set(setup_wizard.WEB_SETUP_FIELDS) - attributes == {"storage"}

    def test_the_storage_choice_decides_not_the_presence_of_a_url(self, _no_credentials_in_env):
        """Switching back to local keeps the Nextcloud fields in the draft;
        they must not be written."""
        nextcloud = {
            "nextcloud_url": "https://cloud.example.test",
            "nextcloud_username": "zed-bot",
            "nextcloud_app_password": "nc-pass",
        }
        local = setup_wizard.answers_from_payload({**_WEB_BASE, **nextcloud, "storage": "local"})
        chosen = setup_wizard.answers_from_payload({**_WEB_BASE, **nextcloud, "storage": "nextcloud"})

        assert local.uses_nextcloud is False
        assert container_secret_values(local)["istota_nextcloud_app_password"] == ""
        assert chosen.uses_nextcloud is True

    def test_nextcloud_chosen_without_an_address_is_a_field_error(self):
        with pytest.raises(setup_wizard.PayloadError) as raised:
            setup_wizard.answers_from_payload({**_WEB_BASE, "storage": "nextcloud"})
        assert raised.value.errors[0].field == "nextcloud_url"

    def test_nextcloud_keeps_the_workspace_on_the_mount_unless_told(self, _no_credentials_in_env):
        nextcloud = {**_WEB_BASE, "storage": "nextcloud", "nextcloud_url": "https://cloud.example.test"}
        mount = setup_wizard.answers_from_payload(nextcloud)
        local = setup_wizard.answers_from_payload({**nextcloud, "workspace": "local"})

        assert mount.workspace_is_mount is True
        assert mount.nextcloud_auto_share_bot_dir is True
        assert local.uses_nextcloud is True
        assert local.workspace_is_mount is False
        assert local.nextcloud_auto_share_bot_dir is False

    def test_an_unknown_workspace_is_a_field_error(self):
        with pytest.raises(setup_wizard.PayloadError) as raised:
            setup_wizard.answers_from_payload({
                **_WEB_BASE, "storage": "nextcloud",
                "nextcloud_url": "https://cloud.example.test", "workspace": "attic",
            })
        assert [e.field for e in raised.value.errors] == ["workspace"]

    def test_a_null_is_an_unanswered_question(self, _no_credentials_in_env):
        a = setup_wizard.answers_from_payload({**_WEB_BASE, "bot_name": None, "timezone": None})
        assert (a.bot_name, a.timezone) == ("Istota", "UTC")


#: (id, terminal flags, terminal environment, web payload): the same answers
#: given to each front end.
_SHAPES = [
    (
        "local-api-key",
        {"bot_name": "Zed", "display_name": "Alice Example", "timezone": "Europe/Berlin",
         "user_email": "alice@example.test", "hostname": "localhost:8080",
         "native_model": "claude-sonnet-4-6", "profile": ["browser"]},
        {"ISTOTA_BRAIN_NATIVE_API_KEY": "native-key"},
        {"bot_name": "Zed", "user_id": "alice", "display_name": "Alice Example",
         "timezone": "Europe/Berlin", "user_email": "alice@example.test",
         "hostname": "localhost:8080", "ingress": "local", "brain_kind": "native",
         "native_base_url": setup_wizard.DEFAULT_ANTHROPIC_BASE_URL,
         "native_model": "claude-sonnet-4-6", "native_api_key": "native-key",
         "profiles": ["browser"]},
    ),
    (
        "direct-nextcloud-email",
        {"hostname": "bot.example.test", "ingress": "direct",
         "nextcloud_url": "https://cloud.example.test/", "nextcloud_user": "zed-bot",
         "email": True, "imap_host": "imap.example.test", "imap_user": "zed@example.test"},
        {"ISTOTA_BRAIN_NATIVE_API_KEY": "native-key",
         "ISTOTA_NEXTCLOUD_APP_PASSWORD": "nc-pass", "ISTOTA_EMAIL_IMAP_PASSWORD": "imap-pass"},
        {**_WEB_BASE, "hostname": "bot.example.test", "ingress": "direct",
         "tls_cert_source": "acme", "storage": "nextcloud",
         "nextcloud_url": "https://cloud.example.test/", "nextcloud_username": "zed-bot",
         "nextcloud_app_password": "nc-pass", "talk_enabled": True,
         "email_enabled": True, "imap_host": "imap.example.test",
         "imap_user": "zed@example.test", "imap_password": "imap-pass"},
    ),
    (
        "nextcloud-local-workspace",
        {"nextcloud_url": "https://cloud.example.test/", "nextcloud_user": "zed-bot",
         "workspace": "local"},
        {"ISTOTA_BRAIN_NATIVE_API_KEY": "native-key", "ISTOTA_NEXTCLOUD_APP_PASSWORD": "nc-pass"},
        {**_WEB_BASE, "storage": "nextcloud", "workspace": "local",
         "nextcloud_url": "https://cloud.example.test/", "nextcloud_username": "zed-bot",
         "nextcloud_app_password": "nc-pass", "talk_enabled": True},
    ),
    (
        "proxied-subscription",
        {"hostname": "bot.example.test", "ingress": "proxied", "upstream_proxy": "10.0.0.5",
         "listen_addr": "10.0.0.20", "listen_port": 8443, "tls_cert_source": "self-signed",
         "brain": "claude_code", "native_model": None},
        {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-token"},
        {"user_id": "alice", "hostname": "bot.example.test", "ingress": "proxied",
         "upstream_proxy": "10.0.0.5", "listen_addr": "10.0.0.20", "listen_port": 8443,
         "tls_cert_source": "self-signed", "brain_kind": "claude_code",
         "claude_code_oauth_token": "oauth-token"},
    ),
    (
        "every-module-off-with-a-calendar",
        {"no_money": True, "no_health": True, "no_feeds": True, "no_briefings": True,
         "caldav_url": "https://dav.example.test", "caldav_username": "alice"},
        {"ISTOTA_BRAIN_NATIVE_API_KEY": "native-key", "ISTOTA_CALDAV_PASSWORD": "dav-pass"},
        {**_WEB_BASE, "money_enabled": False, "health_enabled": False,
         "feeds_enabled": False, "briefings_enabled": False,
         "caldav_url": "https://dav.example.test", "caldav_username": "alice",
         "caldav_password": "dav-pass"},
    ),
]


class TestTheNextcloudMount:
    """A Nextcloud setup stages the VM's rclone.conf and names its remote in
    host.env, so the mount provision.sh installs can start without a hand step."""

    @pytest.fixture(autouse=True)
    def _credentials(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        monkeypatch.setenv("ISTOTA_NEXTCLOUD_APP_PASSWORD", "nc-app-pass")
        monkeypatch.delenv("ISTOTA_SETUP_SHAPE", raising=False)

    @staticmethod
    def _nextcloud(tmp_path: Path, **kw):
        return _args(
            tmp_path, nextcloud_url="https://cloud.example.test/", nextcloud_user="zed-bot", **kw,
        )

    @staticmethod
    def _host_env(vm: Path) -> dict[str, str]:
        text = (vm / "host.env").read_text()
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))

    def test_a_nextcloud_setup_stages_the_remote_and_names_it(self, tmp_path):
        assert setup_wizard.run_setup(self._nextcloud(tmp_path), out=lambda *_: None) == 0

        vm = tmp_path / "vm"
        conf = vm / "rclone.conf"
        assert _mode(conf) == 0o600
        lines = conf.read_text().splitlines()
        assert lines[:5] == [
            "[nextcloud]",
            "type = webdav",
            "url = https://cloud.example.test/remote.php/dav/files/zed-bot/",
            "vendor = nextcloud",
            "user = zed-bot",
        ]
        obscured = lines[5].removeprefix("pass = ")
        assert obscured != "nc-app-pass"
        assert rclone_conf.reveal(obscured) == "nc-app-pass"
        host_env = self._host_env(vm)
        assert host_env["STORAGE"] == "nextcloud"
        assert host_env["RCLONE_REMOTE"] == "nextcloud"

    def test_the_mount_is_rooted_at_the_dav_prefix(self, tmp_path):
        """The daemon's DAV calls add `dav_prefix`, so the mount has to serve the
        same folder: rooted at the account, a file written through it is at
        `/Users/...` while every DAV read asks `/<prefix>/Users/...`."""
        args = self._nextcloud(tmp_path, nextcloud_dav_prefix="Shared Files")
        assert setup_wizard.run_setup(args, out=lambda *_: None) == 0

        lines = (tmp_path / "vm" / "rclone.conf").read_text().splitlines()
        assert lines[0] == "[nextcloud-root]"
        assert lines[-3:] == ["[nextcloud]", "type = alias", "remote = nextcloud-root:Shared Files"]
        assert self._host_env(tmp_path / "vm")["RCLONE_REMOTE"] == "nextcloud"

    def test_a_rerun_without_the_password_obscures_the_kept_one(self, tmp_path, monkeypatch):
        setup_wizard.run_setup(self._nextcloud(tmp_path), out=lambda *_: None)
        monkeypatch.delenv("ISTOTA_NEXTCLOUD_APP_PASSWORD")

        setup_wizard.run_setup(self._nextcloud(tmp_path, force=True), out=lambda *_: None)

        last = (tmp_path / "vm" / "rclone.conf").read_text().splitlines()[-1]
        assert rclone_conf.reveal(last.removeprefix("pass = ")) == "nc-app-pass"

    def test_a_rerun_with_the_same_answers_keeps_the_file_byte_for_byte(self, tmp_path):
        """`provision.sh --digest` hashes rclone.conf, so a re-run that
        re-obscured the password reprovisioned the VM every time (#720)."""
        conf = tmp_path / "vm" / "rclone.conf"
        setup_wizard.run_setup(self._nextcloud(tmp_path), out=lambda *_: None)
        first = conf.read_bytes()

        setup_wizard.run_setup(self._nextcloud(tmp_path, force=True), out=lambda *_: None)

        assert conf.read_bytes() == first
        assert _mode(conf) == 0o600

    @pytest.mark.parametrize("change", ["password", "url", "username"])
    def test_a_rerun_with_a_changed_answer_rewrites_it(self, tmp_path, monkeypatch, change):
        conf = tmp_path / "vm" / "rclone.conf"
        setup_wizard.run_setup(self._nextcloud(tmp_path), out=lambda *_: None)
        first = conf.read_text()
        answers = {"nextcloud_url": "https://cloud.example.test/", "nextcloud_user": "zed-bot"}
        if change == "password":
            monkeypatch.setenv("ISTOTA_NEXTCLOUD_APP_PASSWORD", "nc-new-pass")
        elif change == "url":
            answers["nextcloud_url"] = "https://other.example.test/"
        else:
            answers["nextcloud_user"] = "other-bot"

        setup_wizard.run_setup(_args(tmp_path, force=True, **answers), out=lambda *_: None)

        text = conf.read_text()
        assert text != first
        password = rclone_conf.reveal(text.splitlines()[-1].removeprefix("pass = "))
        assert password == ("nc-new-pass" if change == "password" else "nc-app-pass")

    def test_local_storage_stages_no_remote(self, tmp_path):
        assert setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None) == 0

        assert not (tmp_path / "vm" / "rclone.conf").exists()
        assert self._host_env(tmp_path / "vm")["RCLONE_REMOTE"] == ""

    def test_nextcloud_with_a_local_workspace_stages_no_remote(self, tmp_path):
        assert setup_wizard.run_setup(self._nextcloud(tmp_path, workspace="local"), out=lambda *_: None) == 0

        assert not (tmp_path / "vm" / "rclone.conf").exists()
        host_env = self._host_env(tmp_path / "vm")
        assert host_env["STORAGE"] == "local"
        assert host_env["RCLONE_REMOTE"] == ""


def _written(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


class TestOneWriterForBothFrontEnds:
    @pytest.mark.parametrize(
        "flags,env,payload", [case[1:] for case in _SHAPES], ids=[case[0] for case in _SHAPES],
    )
    def test_the_same_answers_write_the_same_bytes(
        self, tmp_path, monkeypatch, _no_credentials_in_env, flags, env, payload,
    ):
        # The session key, the master key and rclone.conf's IV are the generated
        # values, and managed.json's stamp the clock's.
        monkeypatch.setattr(setup_wizard.secrets, "token_hex", lambda n: "k" * (2 * n))
        monkeypatch.setattr(rclone_conf, "_new_iv", lambda: b"i" * 16)
        monkeypatch.setattr(apply, "_utc_now", lambda: datetime(2026, 10, 12, tzinfo=timezone.utc))
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        terminal = setup_wizard.collect_container_answers(
            _args(tmp_path, **flags), input_fn=input, out=lambda *_: None, getpass_fn=None,
        )
        web = setup_wizard.answers_from_payload(payload)
        assert setup_wizard.validate_container_answers(terminal) == []
        assert setup_wizard.validate_container_answers(web) == []

        for name, answers in (("terminal", terminal), ("web", web)):
            setup_wizard.write_container_setup(
                answers, data_dir=tmp_path / name / "data", vm_dir=tmp_path / name / "vm",
                out=lambda *_: None,
            )

        terminal_files = _written(tmp_path / "terminal")
        assert {
            "data/config/config.toml", "data/config/admins", "data/config/managed.json",
            "data/.secret_key", "vm/.env", "vm/host.env",
        } | {f"vm/secrets/{name}" for name in SECRET_NAMES} | (
            {"vm/rclone.conf"} if terminal.workspace_is_mount else set()
        ) == set(terminal_files)
        assert _written(tmp_path / "web") == terminal_files

    def test_the_run_writes_through_the_shared_writer(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        monkeypatch.delenv("ISTOTA_SETUP_SHAPE", raising=False)
        monkeypatch.setattr(
            setup_wizard, "write_container_setup",
            lambda a, **kw: calls.append((a.user_id, kw["data_dir"], kw["vm_dir"])),
        )

        assert setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None) == 0

        assert calls == [("alice", tmp_path / "data", tmp_path / "vm")]


class TestARerunStartsFromTheStack:
    """#749: a `setup --force` re-run starts from what the stack's files say,
    not from first-run defaults."""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch, _no_credentials_in_env):
        monkeypatch.delenv("ISTOTA_SETUP_SHAPE", raising=False)
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")

    def test_a_yes_rerun_keeps_the_hostname_and_its_port(self, tmp_path, monkeypatch):
        assert setup_wizard.run_setup(
            _args(tmp_path, hostname="localhost:8181", storage="host-folder"), out=lambda *_: None,
        ) == 0
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY")

        assert setup_wizard.run_setup(
            _args(tmp_path, force=True, user=None, brain=None, native_model=None),
            out=lambda *_: None,
        ) == 0

        env = _env((tmp_path / "vm" / ".env").read_text())
        assert env["NGINX_PUBLISH"] == "127.0.0.1:8181:80"
        assert env["DOMAIN"] == "localhost:8181"
        written = tomllib.loads((tmp_path / "data" / "config" / "config.toml").read_text())
        assert written["site"]["hostname"] == "localhost:8181"

    def test_a_first_run_without_storage_keeps_a_host_folder_vm(self, tmp_path):
        """With no config there is no prior; host.env's STORAGE is the fallback."""
        vm = tmp_path / "vm"
        vm.mkdir()
        (vm / "host.env").write_text("STORAGE=host-folder\nINGRESS=local\n")

        assert setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None) == 0

        assert _env((vm / "host.env").read_text())["STORAGE"] == "host-folder"

    def test_a_brain_setup_cannot_write_is_refused_by_name(self, tmp_path):
        setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None)
        config = tmp_path / "data" / "config" / "config.toml"
        config.write_text(config.read_text().replace('kind = "native"', 'kind = "tmux_claude"'))
        before = config.read_bytes()

        with pytest.raises(setup_wizard.SetupError, match=r"brain\.kind = 'tmux_claude'.*Configuration page"):
            setup_wizard.run_setup(_args(tmp_path, force=True, brain=None), out=lambda *_: None)

        assert config.read_bytes() == before

    def test_a_profile_setup_does_not_know_is_refused_by_name(self, tmp_path):
        setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None)
        env = tmp_path / "vm" / ".env"
        env.write_text(env.read_text().replace("COMPOSE_PROFILES=", "COMPOSE_PROFILES=browser,extra-sidecar"))

        with pytest.raises(setup_wizard.SetupError, match="extra-sidecar"):
            setup_wizard.run_setup(_args(tmp_path, force=True), out=lambda *_: None)

    def test_a_config_that_does_not_parse_falls_back_to_first_run_defaults(self, tmp_path):
        setup_wizard.run_setup(_args(tmp_path, hostname="localhost:8181"), out=lambda *_: None)
        config = tmp_path / "data" / "config" / "config.toml"
        config.write_text("this is [not toml\n")
        said: list[str] = []

        assert setup_wizard.run_setup(_args(tmp_path, force=True), out=said.append) == 0

        assert any("does not parse" in line and "first-run defaults" in line for line in said)
        assert tomllib.loads(config.read_text())["site"]["hostname"] == "localhost"

    def test_two_admins_with_user_blocks_are_not_guessed_between(self, tmp_path):
        setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None)
        config_dir = tmp_path / "data" / "config"
        (config_dir / "admins").write_text("alice\nbob\n")
        config = config_dir / "config.toml"
        config.write_text(config.read_text() + '\n[users.bob]\ndisplay_name = "Bob"\n')

        assert setup_wizard.prior_answers(tmp_path / "data", tmp_path / "vm").user_id == ""
        assert setup_wizard.prior_user_ids(tomllib.loads(config.read_text()), config_dir / "admins") == [
            "alice", "bob",
        ]
        with pytest.raises(setup_wizard.SetupError, match="user id is required"):
            setup_wizard.run_setup(_args(tmp_path, force=True, user=None), out=lambda *_: None)

    def test_a_rerun_backs_the_config_up_and_keeps_the_newest_ten(self, tmp_path):
        setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None)
        config = tmp_path / "data" / "config" / "config.toml"
        first = config.read_bytes()
        for day in range(1, 12):
            stale = config.with_name(f"config.toml.bak.202601{day:02d}T000000.000000Z")
            stale.write_text("old\n")

        assert setup_wizard.run_setup(_args(tmp_path, force=True), out=lambda *_: None) == 0

        backups = sorted(config.parent.glob("config.toml.bak.*"))
        assert len(backups) == 10
        assert backups[-1].read_bytes() == first
        assert _mode(backups[-1]) == 0o600
        assert not config.with_name("config.toml.bak.20260101T000000.000000Z").exists()

    def test_a_first_run_writes_no_backup(self, tmp_path):
        setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None)
        assert not list((tmp_path / "data" / "config").glob("config.toml.bak.*"))


def _forwarding_vm(tmp_path: Path, port: str = "8181") -> Path:
    """A local VM's staged vm-dir: host.env as the Mac's `create` writes it."""
    vm = tmp_path / "vm"
    vm.mkdir(exist_ok=True)
    (vm / "host.env").write_text(
        f"STORAGE=host-folder\nINGRESS=local\nHOST_LOOPBACK_BLOCK=1\nLOCAL_FORWARD_PORT={port}\n"
    )
    return vm


class TestTheForwardedPort:
    """The local VM's nginx publishes on the port Lima forwards, which only
    the Mac knows and writes into host.env as LOCAL_FORWARD_PORT."""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch, _no_credentials_in_env):
        monkeypatch.delenv("ISTOTA_SETUP_SHAPE", raising=False)
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")

    @pytest.mark.parametrize("value, port", [
        ("8181", 8181), ("80", 80), ("", 0), ("eighty", 0), ("0", 0), ("08181", 0), ("70000", 0), ("-1", 0),
    ])
    def test_forwarded_port_reads_host_env(self, tmp_path, value, port):
        assert setup_wizard.forwarded_port(_forwarding_vm(tmp_path, value)) == port

    def test_no_host_env_or_no_vm_dir_is_no_forward(self, tmp_path):
        assert setup_wizard.forwarded_port(None) == 0
        assert setup_wizard.forwarded_port(tmp_path) == 0

    def test_the_749_rerun_keeps_publishing_on_the_forward(self, tmp_path, monkeypatch):
        """#749 as reported: a VM made with `--port 8181`, set up, then
        `setup --force --yes`. nginx must stay on 8181, which Lima forwards."""
        vm = _forwarding_vm(tmp_path)
        assert setup_wizard.run_setup(_args(tmp_path, hostname="localhost:8181"), out=lambda *_: None) == 0
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY")

        assert setup_wizard.run_setup(
            _args(tmp_path, force=True, user=None, brain=None, native_model=None),
            out=lambda *_: None,
        ) == 0

        env = _env((vm / ".env").read_text())
        assert env["NGINX_PUBLISH"] == "127.0.0.1:8181:80"
        assert env["DOMAIN"] == "localhost:8181"
        host = _env((vm / "host.env").read_text())
        assert host["LOCAL_FORWARD_PORT"] == "8181"
        assert host["STORAGE"] == "host-folder"

    def test_a_first_run_defaults_the_hostname_to_the_forward(self, tmp_path):
        vm = _forwarding_vm(tmp_path)
        assert setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None) == 0
        env = _env((vm / ".env").read_text())
        assert env["DOMAIN"] == "localhost:8181"
        assert env["NGINX_PUBLISH"] == "127.0.0.1:8181:80"

    def test_the_interactive_default_is_the_forward(self, tmp_path):
        _forwarding_vm(tmp_path)
        prompts: list[str] = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            return ""

        setup_wizard.collect_container_answers(
            _args(tmp_path, yes=False), input_fn=answer, out=lambda *_: None,
            getpass_fn=lambda _p: "", forwarded_port=8181,
        )
        assert any(p.startswith("Public hostname") and "[localhost:8181]" in p for p in prompts), prompts

    @pytest.mark.parametrize("hostname", ["localhost:8080", "localhost", "127.0.0.1:9000"])
    def test_a_local_hostname_on_another_port_is_refused(self, tmp_path, hostname):
        _forwarding_vm(tmp_path)
        with pytest.raises(
            setup_wizard.SetupError, match=r"This VM's port forward is 8181; use localhost:8181\.",
        ):
            setup_wizard.run_setup(_args(tmp_path, hostname=hostname), out=lambda *_: None)
        assert not (tmp_path / "data" / "config" / "config.toml").exists()

    def test_a_rerun_of_a_vm_set_up_on_the_wrong_port_is_refused(self, tmp_path, monkeypatch):
        """A stack set up before the forward was known (bare localhost, so 8080)
        is told the port to use rather than being re-published on 8080 again."""
        setup_wizard.run_setup(_args(tmp_path, hostname="localhost"), out=lambda *_: None)
        _forwarding_vm(tmp_path)
        with pytest.raises(setup_wizard.SetupError, match="use localhost:8181"):
            setup_wizard.run_setup(_args(tmp_path, force=True), out=lambda *_: None)

    def test_direct_ingress_is_not_held_to_the_forward(self, tmp_path):
        vm = _forwarding_vm(tmp_path)
        assert setup_wizard.run_setup(
            _args(tmp_path, hostname="istota.example.com", ingress="direct"), out=lambda *_: None,
        ) == 0
        assert _env((vm / ".env").read_text())["NGINX_PUBLISH"] == "80:80"

    def test_the_validator_holds_the_rule_for_both_front_ends(self):
        a = setup_wizard.ContainerAnswers(user_id="alice", hostname="localhost:8080", forwarded_port=8181)
        assert [(e.field, e.message) for e in setup_wizard.validate_container_answers(a)] == [
            ("hostname", "This VM's port forward is 8181; use localhost:8181."),
        ]
        a.hostname = "localhost:8181"
        assert setup_wizard.validate_container_answers(a) == []
        a.hostname, a.forwarded_port = "localhost:8080", 0
        assert setup_wizard.validate_container_answers(a) == []

    def test_the_forward_is_written_nowhere_by_setup(self, tmp_path):
        """host.env's key is the Mac's: setup reads it and owns no line of it."""
        a = setup_wizard.ContainerAnswers(user_id="alice", hostname="localhost:8181", forwarded_port=8181)
        b = setup_wizard.ContainerAnswers(user_id="alice", hostname="localhost:8181")
        assert "LOCAL_FORWARD_PORT" not in setup_wizard.host_env_values(a)
        assert setup_wizard.host_env_values(a) == setup_wizard.host_env_values(b)
        assert setup_wizard.stack_env_values(a) == setup_wizard.stack_env_values(b)
        assert setup_wizard.container_config_document(a, inline_credentials=False) == (
            setup_wizard.container_config_document(b, inline_credentials=False)
        )


#: First-run answer sets for the identity table: (id, answers). Each is
#: written once, then re-run with no flags; every file must come out the same.
_FIRST_RUNS = [
    ("local-host-folder-native", dict(
        hostname="localhost:8181", ingress="local", host_folder=True, brain_kind="native",
        native_base_url="https://llm.example.test/v1", native_model="model-x",
        native_api_key="native-key", profiles=("browser",))),
    ("local-state-volume-token", dict(
        hostname="localhost", ingress="local", claude_code_oauth_token="oauth-token",
        display_name="Alice Example", timezone="Europe/Berlin", user_email="alice@example.test",
        money_enabled=False, feeds_enabled=False)),
    ("direct-acme-api-key", dict(
        hostname="bot.example.test", ingress="direct", tls_cert_source="acme",
        anthropic_api_key="api-key")),
    ("direct-files", dict(
        hostname="bot.example.test", ingress="direct", tls_cert_source="files",
        claude_code_oauth_token="oauth-token")),
    ("proxied-self-signed", dict(
        hostname="bot.example.test", ingress="proxied", upstream_proxy="10.0.0.5",
        listen_addr="10.0.0.20", listen_port=8443, tls_cert_source="self-signed",
        claude_code_oauth_token="oauth-token")),
    ("proxied-plain", dict(
        hostname="bot.example.test", ingress="proxied", upstream_proxy="10.0.0.5,10.0.0.6",
        listen_addr="10.0.0.20", listen_port=8080, tls_cert_source="",
        claude_code_oauth_token="oauth-token")),
    ("nextcloud-mount-prefix-oauth-signaling", dict(
        hostname="bot.example.test", ingress="direct", nextcloud_url="https://cloud.example.test",
        nextcloud_public_url="https://files.example.test", nextcloud_username="zed-bot",
        nextcloud_app_password="nc-pass", nextcloud_dav_prefix="Shared Files",
        nextcloud_auto_share_bot_dir=False, oauth_client_id="client-id",
        oauth_client_secret="client-secret", email_login=False,
        claude_code_oauth_token="oauth-token", profiles=("signaling", "whatsapp-baileys"))),
    ("nextcloud-local-workspace-no-talk", dict(
        hostname="localhost:8080", ingress="local", nextcloud_url="https://cloud.example.test",
        nextcloud_public_url="https://cloud.example.test", nextcloud_app_password="nc-pass",
        workspace="local", nextcloud_auto_share_bot_dir=False, talk_enabled=False,
        claude_code_oauth_token="oauth-token")),
    ("email-ports-authserv-calendar", dict(
        hostname="localhost", ingress="local", claude_code_oauth_token="oauth-token",
        email_enabled=True, imap_host="imap.example.test", imap_port=143,
        imap_user="zed@example.test", imap_password="imap-pass", smtp_host="smtp.example.test",
        smtp_port=465, bot_email="bot@example.test", authserv_id="mx.example.test",
        caldav_url="https://dav.example.test", caldav_username="alice",
        caldav_password="dav-pass")),
    ("developer-location-every-module-off", dict(
        hostname="localhost", ingress="local", claude_code_oauth_token="oauth-token",
        developer_enabled=True, gitlab_token="gitlab-token", github_token="github-token",
        location_enabled=True, money_enabled=False, health_enabled=False,
        feeds_enabled=False, briefings_enabled=False, profiles=("browser", "whatsapp-baileys"))),
]


def _bare_args(tmp_path: Path, **kw) -> SimpleNamespace:
    """`istota setup --vm-dir … --force` and nothing else: every answer unasked."""
    return SimpleNamespace(
        shape="container", vm_dir=str(tmp_path / "vm"), data_dir=str(tmp_path / "data"),
        force=True, **kw,
    )


class TestARerunWritesWhatIsThere:
    """The inverse reader held in step with the writer: a re-run with no
    flags, `--yes` or Enter at every prompt, rewrites every file byte for byte
    (the new config backup aside). A field the writer gains without the
    reader fails here."""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch, _no_credentials_in_env):
        monkeypatch.delenv("ISTOTA_SETUP_SHAPE", raising=False)
        # Every module toggle is recorded, whatever extras this venv has.
        monkeypatch.setattr("istota.modules.module_available", lambda _name: True)

    @staticmethod
    def _first_run(tmp_path: Path, fields: dict) -> dict[str, bytes]:
        a = ContainerAnswers(user_id="alice", **fields)
        assert setup_wizard.validate_container_answers(a) == []
        setup_wizard.write_container_setup(
            a, data_dir=tmp_path / "data", vm_dir=tmp_path / "vm", out=lambda *_: None,
        )
        return _written(tmp_path)

    @staticmethod
    def _without_backups(files: dict[str, bytes]) -> dict[str, bytes]:
        return {name: data for name, data in files.items() if ".bak." not in name}

    @pytest.mark.parametrize(
        "fields", [case[1] for case in _FIRST_RUNS], ids=[case[0] for case in _FIRST_RUNS],
    )
    def test_a_yes_rerun_writes_the_same_bytes(self, tmp_path, fields):
        before = self._first_run(tmp_path, fields)

        assert setup_wizard.run_setup(_bare_args(tmp_path, yes=True), out=lambda *_: None) == 0

        after = _written(tmp_path)
        assert self._without_backups(after) == before
        assert [name for name in after if ".bak." in name] != []

    @pytest.mark.parametrize(
        "fields", [case[1] for case in _FIRST_RUNS], ids=[case[0] for case in _FIRST_RUNS],
    )
    def test_enter_at_every_prompt_writes_the_same_bytes(self, tmp_path, fields):
        before = self._first_run(tmp_path, fields)

        assert setup_wizard.run_setup(
            _bare_args(tmp_path, yes=False), input_fn=lambda _prompt: "",
            getpass_fn=lambda _prompt: "", out=lambda *_: None,
        ) == 0

        assert self._without_backups(_written(tmp_path)) == before

    def test_the_prompts_offer_the_current_values(self, tmp_path):
        self._first_run(tmp_path, dict(_FIRST_RUNS[0][1], bot_name="Zed"))
        prompts: list[str] = []
        secret_prompts: list[str] = []

        def ask(prompt: str) -> str:
            prompts.append(prompt)
            return ""

        def ask_secret(prompt: str) -> str:
            secret_prompts.append(prompt)
            return ""

        said: list[str] = []
        setup_wizard.run_setup(
            _bare_args(tmp_path, yes=False), input_fn=ask, getpass_fn=ask_secret, out=said.append,
        )

        assert "Bot name [Zed]: " in prompts
        assert "Public hostname (as browsers reach it) [localhost:8181]: " in prompts
        assert "Ingress (direct/proxied/local) [local]: " in prompts
        assert "Model backend (claude_code/native) [native]: " in prompts
        assert "Model id [model-x]: " in prompts
        assert "Run the browser container? [Y/n]: " in prompts
        assert "API key (Enter keeps the current one): " in secret_prompts
        # The user is kept and shown, not asked; nor are their settings.
        assert not any("user id" in p or "Timezone" in p or "money module" in p for p in prompts)
        assert any("First admin: alice" in line for line in said)

    def test_a_credential_with_nothing_stored_still_says_skip(self, tmp_path):
        self._first_run(tmp_path, dict(_FIRST_RUNS[1][1]))
        secret_prompts: list[str] = []

        def ask_secret(prompt: str) -> str:
            secret_prompts.append(prompt)
            return ""

        setup_wizard.run_setup(
            _bare_args(tmp_path, yes=False), input_fn=lambda _prompt: "",
            getpass_fn=ask_secret, out=lambda *_: None,
        )

        assert "Claude Code OAuth token (Enter keeps the current one): " in secret_prompts
        assert not any("Anthropic API key" in p for p in secret_prompts)

    def test_a_new_answer_still_replaces_the_prior(self, tmp_path, monkeypatch):
        self._first_run(tmp_path, dict(_FIRST_RUNS[0][1]))
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "rotated-key")

        setup_wizard.run_setup(
            _bare_args(tmp_path, yes=True, hostname="localhost:9090"), out=lambda *_: None,
        )

        assert (tmp_path / "vm" / "secrets" / "istota_brain_native_api_key").read_text() == "rotated-key"
        assert _env((tmp_path / "vm" / ".env").read_text())["NGINX_PUBLISH"] == "127.0.0.1:9090:80"


class TestTheBrowserPrompt:
    """Decided 2026-10-12: the browser is on by default in the interactive
    wizard; a `--yes` run without `--profile` still runs without it."""

    def test_enter_at_the_browser_prompt_runs_it(self, tmp_path, _no_credentials_in_env):
        prompts: list[str] = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            return "carol" if "user id" in prompt else ""

        a = setup_wizard.collect_container_answers(
            _args(tmp_path, yes=False, user=None, brain=None, native_model=None),
            input_fn=answer, out=lambda *_: None, getpass_fn=lambda _prompt: "",
        )

        assert any("browser container? [Y/n]" in p for p in prompts)
        assert a.profiles == ("browser",)

    def test_a_yes_run_without_a_profile_has_no_browser(self, tmp_path, monkeypatch, _no_credentials_in_env):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        a = setup_wizard.collect_container_answers(
            _args(tmp_path), input_fn=input, out=lambda *_: None, getpass_fn=None,
        )
        assert a.profiles == ()


class TestTheSenderCheck:
    """ISSUE-746: setup asks for the mail server's authserv-id, and a value
    turns the sender check from trusting `From:` to reading the server's
    verdict."""

    _EMAIL = {"email": True, "imap_host": "imap.example.test", "imap_user": "zed@example.test"}

    def _written(self, tmp_path, monkeypatch, a: ContainerAnswers):
        return _load(tmp_path, render_container_config(a, inline_credentials=True), monkeypatch)

    def test_an_authserv_id_turns_on_verify(self, tmp_path, monkeypatch, _no_credentials_in_env):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        a = setup_wizard.collect_container_answers(
            _args(tmp_path, **self._EMAIL, authserv_id=" mx.example.test "),
            input_fn=input, out=lambda *_: None, getpass_fn=None,
        )

        config = self._written(tmp_path, monkeypatch, a)

        assert config.email.authserv_id == "mx.example.test"
        assert config.email.confirm_sender_match == "verify"
        assert config.email.dmarc_canary_warn_on_missing is True

    def test_no_authserv_id_keeps_the_from_line_trusted(self, tmp_path, monkeypatch, _no_credentials_in_env):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        a = setup_wizard.collect_container_answers(
            _args(tmp_path, **self._EMAIL), input_fn=input, out=lambda *_: None, getpass_fn=None,
        )

        written = tomllib.loads(render_container_config(a, inline_credentials=True))["email"]
        config = self._written(tmp_path, monkeypatch, a)

        assert "authserv_id" not in written and "confirm_sender_match" not in written
        assert config.email.confirm_sender_match == "off"
        assert config.email.dmarc_canary_warn_on_missing is False

    def test_it_is_not_written_without_email(self, tmp_path, monkeypatch, _no_credentials_in_env):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")
        a = setup_wizard.collect_container_answers(
            _args(tmp_path, authserv_id="mx.example.test"),
            input_fn=input, out=lambda *_: None, getpass_fn=None,
        )
        assert a.authserv_id == ""
        assert tomllib.loads(render_container_config(a, inline_credentials=True))["email"] == {"enabled": False}

    def test_the_question_says_what_it_is_and_where_to_find_it(self, tmp_path, _no_credentials_in_env):
        prompts: list[str] = []
        said: list[str] = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            if "user id" in prompt:
                return "carol"
            if "Enable email" in prompt:
                return "y"
            if "authserv-id" in prompt:
                return "mx.example.test"
            return ""

        a = setup_wizard.collect_container_answers(
            _args(tmp_path, yes=False, user=None, brain=None, native_model=None),
            input_fn=answer, out=said.append, getpass_fn=lambda _prompt: "",
        )

        assert a.authserv_id == "mx.example.test"
        explanation = "\n".join(said)
        assert "Authentication-Results" in explanation
        assert "before the first semicolon" in explanation
        assert "raw" in explanation and "source" in explanation
        assert "From:" in explanation

    def test_the_web_payload_carries_it_with_email_only(self, _no_credentials_in_env):
        on = setup_wizard.answers_from_payload({
            **_WEB_BASE, "email_enabled": True, "imap_host": "imap.example.test",
            "authserv_id": " mx.example.test ",
        })
        off = setup_wizard.answers_from_payload({**_WEB_BASE, "authserv_id": "mx.example.test"})

        assert on.authserv_id == "mx.example.test"
        assert off.authserv_id == ""


class TestThePrompts:
    """The terminal wizard's prompt helpers, which the container half kept when
    the standalone half went."""

    def test_yes_no_empty_takes_the_default(self):
        assert setup_wizard._ask_yes_no(lambda _p: "", "Q?", True) is True
        assert setup_wizard._ask_yes_no(lambda _p: "", "Q?", False) is False

    def test_yes_no_re_asks_rather_than_reading_as_no(self):
        """On a default-Yes prompt a typo used to flip *away* from the `[Y/n]`
        just printed and disable a module durably."""
        answers = iter(["yeah", "1", "y"])
        said: list[str] = []
        assert setup_wizard._ask_yes_no(lambda _p: next(answers), "Q?", True, out=said.append) is True
        assert any("answer y or n" in line for line in said)

    def test_yes_no_is_bounded_and_falls_back_to_the_default(self):
        calls: list[str] = []

        def ask(prompt):
            calls.append(prompt)
            return "maybe"

        assert setup_wizard._ask_yes_no(ask, "Q?", True) is True
        assert len(calls) == 3

    def test_port_re_asks_on_a_non_number_and_out_of_range(self):
        answers = iter(["me@example.test", "70000", "9000"])
        said: list[str] = []
        assert setup_wizard._ask_port(lambda _p: next(answers), "Port", 8080, out=said.append) == 9000
        assert any("whole number" in line for line in said)
        assert any("not a usable port" in line for line in said)

    def test_port_gives_up_on_the_default_rather_than_looping(self):
        assert setup_wizard._ask_port(lambda _p: "nope", "Port", 8080) == 8080

    def test_timezone_validation(self):
        assert setup_wizard._is_valid_timezone("America/Los_Angeles")
        assert setup_wizard._is_valid_timezone("UTC")
        assert not setup_wizard._is_valid_timezone("PDT")
        assert not setup_wizard._is_valid_timezone("")


class TestTheModuleQuestions:
    """``_collect_modules`` on the container path: an unavailable module is
    left alone, and an explicit flag beats unavailability."""

    def _collect(self, monkeypatch, *, interactive, **flags):
        monkeypatch.setattr("istota.modules.module_available", lambda name: name != "money")
        asked: list[str] = []
        out: list[str] = []
        a = ContainerAnswers()
        args = SimpleNamespace(**{
            f"no_{m}": flags.get(f"no_{m}", False)
            for m in ("money", "health", "feeds", "briefings")
        })

        def input_fn(prompt):
            asked.append(prompt)
            return ""

        setup_wizard._collect_modules(
            a, args, interactive=interactive, input_fn=input_fn, out=out.append,
        )
        return a, asked, out

    def test_an_unavailable_module_is_neither_asked_about_nor_recorded(self, monkeypatch):
        """Recording it as disabled would turn a missing extra into a stored
        decision that outlives installing it."""
        a, asked, out = self._collect(monkeypatch, interactive=True)
        assert not any("money module" in p for p in asked)
        assert any("health module" in p for p in asked)
        assert a.money_enabled is True
        assert any("money module's optional dependencies" in line for line in out)

    def test_an_explicit_flag_still_wins_over_unavailability(self, monkeypatch):
        a, _, _ = self._collect(monkeypatch, interactive=False, no_money=True)
        assert a.money_enabled is False


class TestTheAdminsFile:
    """``_ensure_admins_file`` only ever creates, and asks the reader."""

    def test_an_existing_file_not_naming_the_user_is_never_widened(self, tmp_path):
        path = tmp_path / "admins"
        existing = "# hand-edited\nbob\n"
        path.write_text(existing)
        out: list[str] = []
        setup_wizard._ensure_admins_file(path, "alice", out=out.append)
        assert path.read_text() == existing
        # Refusing silently would lock the user out with no way to know why.
        assert any("does not name 'alice'" in line for line in out)

    def test_the_membership_test_is_load_admin_users(self, tmp_path):
        """Writer and reader must not drift on what a line means."""
        path = tmp_path / "admins"
        existing = "# owner\n\n  alice  \n"
        path.write_text(existing)
        out: list[str] = []
        setup_wizard._ensure_admins_file(path, "alice", out=out.append)
        assert path.read_text() == existing
        assert out == []

    def test_a_missing_file_is_created_naming_the_user(self, tmp_path):
        from istota.config import load_admin_users

        path = tmp_path / "admins"
        setup_wizard._ensure_admins_file(path, "alice", out=lambda *_: None)
        assert load_admin_users(str(path)) == {"alice"}


class TestTheRerunSnapshot:
    """`istota setup --snapshot` / `--restore`: what an abandoned web re-run
    puts back. uid 10001's work, since root acts on nothing under config/ or
    secrets/."""

    @pytest.fixture
    def stack(self, tmp_path, monkeypatch, _no_credentials_in_env):
        monkeypatch.delenv("ISTOTA_SETUP_SHAPE", raising=False)
        setup_wizard.write_container_setup(
            ContainerAnswers(user_id="alice", **_FIRST_RUNS[0][1]),
            data_dir=tmp_path / "data", vm_dir=tmp_path / "vm", out=lambda *_: None,
        )
        return tmp_path

    @staticmethod
    def _what_restore_covers(tmp_path: Path) -> dict[str, bytes]:
        config = tmp_path / "data" / "config"
        files = {
            name: (config / name).read_bytes()
            for name in ("config.toml", "managed.json") if (config / name).exists()
        }
        for path in sorted((tmp_path / "vm" / "secrets").iterdir()):
            files[f"secrets/{path.name}"] = path.read_bytes()
        return files

    @staticmethod
    def _run(tmp_path: Path, **kw) -> int:
        args = SimpleNamespace(
            shape="container", vm_dir=str(tmp_path / "vm"), data_dir=str(tmp_path / "data"), **kw,
        )
        return setup_wizard.run_setup(args, out=lambda *_: None)

    def test_a_round_trip_puts_back_what_a_finish_changed(self, stack):
        snapshot = stack / "data" / "config" / ".setup-rerun"
        before = self._what_restore_covers(stack)

        assert self._run(stack, snapshot=str(snapshot)) == 0
        assert _mode(snapshot) == 0o700
        assert all(_mode(p) == 0o600 for p in snapshot.rglob("*") if p.is_file())

        config = stack / "data" / "config" / "config.toml"
        config.write_text('bot_name = "changed"\n')
        (stack / "data" / "config" / "managed.json").write_text("{}\n")
        secrets_dir = stack / "vm" / "secrets"
        (secrets_dir / "istota_brain_native_api_key").unlink()
        setup_wizard._write_secret_file(secrets_dir / "istota_email_imap_password", "new")
        setup_wizard._write_secret_file(secrets_dir / "something_new", "extra")

        assert self._run(stack, restore=str(snapshot)) == 0

        assert self._what_restore_covers(stack) == before
        assert not (secrets_dir / "something_new").exists()
        assert _mode(config) == 0o600
        assert _mode(secrets_dir / "istota_brain_native_api_key") == 0o400
        assert not snapshot.exists()

    def test_a_managed_file_the_snapshot_lacks_is_removed(self, stack):
        managed = stack / "data" / "config" / "managed.json"
        managed.unlink()
        snapshot = stack / "data" / "config" / ".setup-rerun"
        setup_wizard.snapshot_for_rerun(snapshot, stack / "data", stack / "vm" / "secrets")
        managed.write_text("{}\n")

        setup_wizard.restore_rerun_snapshot(snapshot, stack / "data", stack / "vm" / "secrets")

        assert not managed.exists()

    @pytest.mark.parametrize("linked", ["secrets/istota_caldav_password", "config.toml"])
    def test_a_link_is_refused_and_nothing_is_taken(self, stack, linked):
        target = stack / "elsewhere"
        target.write_text("not setup's\n")
        path = (stack / "vm" / linked) if linked.startswith("secrets/") else (stack / "data" / "config" / linked)
        path.unlink()
        path.symlink_to(target)
        snapshot = stack / "data" / "config" / ".setup-rerun"

        with pytest.raises(setup_wizard.SetupError, match="link"):
            setup_wizard.snapshot_for_rerun(snapshot, stack / "data", stack / "vm" / "secrets")
        assert not snapshot.exists()
        assert [p.name for p in snapshot.parent.iterdir() if p.name.startswith(".setup-rerun")] == []

    def test_an_earlier_snapshot_is_not_overwritten(self, stack):
        snapshot = stack / "data" / "config" / ".setup-rerun"
        setup_wizard.snapshot_for_rerun(snapshot, stack / "data", stack / "vm" / "secrets")
        kept = (snapshot / "config.toml").read_bytes()
        (stack / "data" / "config" / "config.toml").write_text('bot_name = "half-written"\n')

        with pytest.raises(setup_wizard.SetupError, match="--restore"):
            setup_wizard.snapshot_for_rerun(snapshot, stack / "data", stack / "vm" / "secrets")
        assert (snapshot / "config.toml").read_bytes() == kept

    def test_restore_refuses_a_directory_that_is_not_a_snapshot(self, stack):
        empty = stack / "data" / "config" / ".setup-rerun"
        empty.mkdir()
        before = self._what_restore_covers(stack)

        with pytest.raises(setup_wizard.SetupError, match="snapshot"):
            setup_wizard.restore_rerun_snapshot(empty, stack / "data", stack / "vm" / "secrets")
        assert self._what_restore_covers(stack) == before

    def test_the_cli_parses_both_flags(self, stack, monkeypatch, capsys):
        from istota import cli

        snapshot = stack / "data" / "config" / ".setup-rerun"
        for flag in ("--snapshot", "--restore"):
            monkeypatch.setattr("sys.argv", [
                "istota", "setup", flag, str(snapshot),
                "--vm-dir", str(stack / "vm"), "--data-dir", str(stack / "data"),
            ])
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
            assert excinfo.value.code == 0, capsys.readouterr().err
        assert not snapshot.exists()

    def test_both_at_once_is_refused(self, stack):
        snapshot = str(stack / "snap")
        with pytest.raises(setup_wizard.SetupError, match="--snapshot"):
            self._run(stack, snapshot=snapshot, restore=snapshot)
