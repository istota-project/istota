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
from types import SimpleNamespace

import pytest
import yaml

from istota import setup_wizard
from istota.config import load_config
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
        assert str(config.nextcloud_mount_path) == "/mnt/shared"
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
        assert "STORAGE=local" in (vm / "host.env").read_text()

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
        assert config.nextcloud_mount_path is None


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
            assert f"istota_data:{setup_wizard.CONTAINER_DATA_DIR}" in mounts, name
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
        # The session key, the master key and rclone.conf's IV are the generated values.
        monkeypatch.setattr(setup_wizard.secrets, "token_hex", lambda n: "k" * (2 * n))
        monkeypatch.setattr(rclone_conf, "_new_iv", lambda: b"i" * 16)
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
            "data/config/config.toml", "data/config/admins", "data/.secret_key",
            "vm/.env", "vm/host.env",
        } | {f"vm/secrets/{name}" for name in SECRET_NAMES} | (
            {"vm/rclone.conf"} if terminal.uses_nextcloud else set()
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
