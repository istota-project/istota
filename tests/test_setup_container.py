"""The container half of ``istota setup``: what it writes, and that it loads.

The container ``config.toml`` is written once and then owned by the operator,
so these tests are the whole of what stands between an answer and the key it
should have reached. Each answer is checked against the loaded ``Config``
rather than the text, because the loader is the reader that matters.
"""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from istota import setup_wizard
from istota.config import load_config
from istota.setup_wizard import (
    SECRET_NAMES,
    ContainerAnswers,
    container_secret_values,
    render_container_config,
    render_stack_env,
    render_vm_env,
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

    @pytest.mark.parametrize("service", ["istota", "web", "webhooks"])
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
        oauth_client_id=None, no_email_login=False, brain="native",
        native_base_url=None, native_model="model-x", email=False,
        imap_host=None, imap_user=None, smtp_host=None, bot_email=None,
        caldav_url=None, caldav_username=None, location=False, developer=False,
        profile=None, no_money=False, no_health=False, no_feeds=False,
        no_briefings=False,
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
        assert "STORAGE=local" in (vm / "vm.env").read_text()

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
        assert setup_wizard.setup_shape(_args(tmp_path, shape=None, vm_dir=None)) == "standalone"

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
            "y", "http://nextcloud", "", "", "Shared Files", "n", "y", "",
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

    def test_the_vm_env_carries_the_ingress_choices(self):
        text = render_vm_env(_full_answers())
        values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
        assert values == {
            "STORAGE": "nextcloud", "INGRESS": "proxied", "DOMAIN": "bot.example.test",
            "TLS_CERT_SOURCE": "acme", "UPSTREAM_PROXY": "10.0.0.5",
            "LISTEN_ADDR": "10.0.0.20", "LISTEN_PORT": "8080",
        }
