"""Tests for security hardening: clean env, stripped env, allowed tools, config overrides."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from istota.config import (
    Config,
    DeveloperConfig,
    EmailConfig,
    NextcloudConfig,
    SecurityConfig,
    load_config,
)
from istota.executor import (
    _CREDENTIAL_ENV_PATTERNS,
    _PROXY_LOOKUP_BLOCKED,
    _split_credential_env,
    build_allowed_tools,
    build_clean_env,
    build_stripped_env,
    derive_authorized_skills,
    derive_credential_set,
    derive_lookup_allowlist,
    derive_skill_credential_map,
)
from istota.skills._env import EnvContext
from istota.skills._types import EnvSpec, SkillMeta


class TestBuildCleanEnv:
    def test_returns_minimal_env(self):
        config = Config()
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "SECRET_KEY": "abc",
            "SOME_TOKEN": "xyz",
        }, clear=True):
            env = build_clean_env(config)
        # PATH includes the active venv bin dir + the original PATH
        import sys
        venv_bin = str(Path(sys.prefix).resolve() / "bin")
        assert venv_bin in env["PATH"]
        assert "/usr/bin" in env["PATH"]
        assert env["HOME"] == "/home/test"
        assert env["PYTHONUNBUFFERED"] == "1"
        assert "SECRET_KEY" not in env
        assert "SOME_TOKEN" not in env

    def test_includes_passthrough_vars(self):
        config = Config(security=SecurityConfig(
            passthrough_env_vars=["LANG", "TZ"],
        ))
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "LANG": "en_US.UTF-8",
            "TZ": "America/New_York",
            "OTHER_VAR": "should-not-appear",
        }, clear=True):
            env = build_clean_env(config)
        assert env["LANG"] == "en_US.UTF-8"
        assert env["TZ"] == "America/New_York"
        assert "OTHER_VAR" not in env

    def test_skips_missing_passthrough_vars(self):
        config = Config(security=SecurityConfig(
            passthrough_env_vars=["LANG", "TZ"],
        ))
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/test"}, clear=True):
            env = build_clean_env(config)
        assert "LANG" not in env
        assert "TZ" not in env

    def test_default_path_when_missing(self):
        config = Config()
        with patch.dict(os.environ, {"HOME": "/home/test"}, clear=True):
            env = build_clean_env(config)
        # Should include default system paths and the venv bin dir
        assert "/usr/local/bin" in env["PATH"]
        assert "/usr/bin" in env["PATH"]
        import sys
        venv_bin = str(Path(sys.prefix).resolve() / "bin")
        assert venv_bin in env["PATH"]


    def test_includes_oauth_token(self):
        """CLAUDE_CODE_OAUTH_TOKEN is passed through for auth."""
        config = Config()
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-secret",
        }, clear=True):
            env = build_clean_env(config)
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-secret"


class TestBuildStrippedEnv:
    def test_strips_password_vars(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "DB_PASSWORD": "secret",
            "IMAP_PASSWORD": "secret",
            "SMTP_PASSWORD": "secret",
        }, clear=True):
            env = build_stripped_env()
        assert "PATH" in env
        assert "HOME" in env
        assert "DB_PASSWORD" not in env
        assert "IMAP_PASSWORD" not in env
        assert "SMTP_PASSWORD" not in env

    def test_strips_token_vars(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "GITLAB_TOKEN": "glpat-xxx",
            "API_TOKEN": "tok-123",
        }, clear=True):
            env = build_stripped_env()
        assert "GITLAB_TOKEN" not in env
        assert "API_TOKEN" not in env

    def test_strips_secret_and_api_key_vars(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "MY_SECRET": "shh",
            "SERVICE_API_KEY": "key-123",
        }, clear=True):
            env = build_stripped_env()
        assert "MY_SECRET" not in env
        assert "SERVICE_API_KEY" not in env

    def test_strips_nc_pass(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "NC_PASS": "nextcloud-pw",
        }, clear=True):
            env = build_stripped_env()
        assert "NC_PASS" not in env

    def test_strips_app_password(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "ISTOTA_NEXTCLOUD_APP_PASSWORD": "pw-123",
        }, clear=True):
            env = build_stripped_env()
        assert "ISTOTA_NEXTCLOUD_APP_PASSWORD" not in env

    def test_strips_private_key(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "SSH_PRIVATE_KEY": "-----BEGIN",
        }, clear=True):
            env = build_stripped_env()
        assert "SSH_PRIVATE_KEY" not in env

    def test_preserves_non_credential_vars(self):
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "LANG": "en_US.UTF-8",
            "ISTOTA_TASK_ID": "42",
        }, clear=True):
            env = build_stripped_env()
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/test"
        assert env["LANG"] == "en_US.UTF-8"
        assert env["ISTOTA_TASK_ID"] == "42"

    def test_strips_istota_secret_key(self):
        # Phase 1.4 of the unified credential resolution refactor: the
        # master Fernet key never enters any subprocess env. Per-user
        # secrets are pre-resolved on the trusted side via skill manifest
        # ``env: from: "secret"`` blocks and routed through the proxy.
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "ISTOTA_SECRET_KEY": "a" * 64,
            "OTHER_SECRET": "should-be-stripped",
        }, clear=True):
            env = build_stripped_env()
        assert "ISTOTA_SECRET_KEY" not in env
        assert "OTHER_SECRET" not in env


class TestBuildAllowedTools:
    def test_includes_file_tools(self):
        tools = build_allowed_tools(is_admin=False, skill_names=[])
        for tool in ["Read", "Write", "Edit", "Grep", "Glob"]:
            assert tool in tools

    def test_includes_bash(self):
        """All bash commands allowed — clean env is the security boundary."""
        tools = build_allowed_tools(is_admin=False, skill_names=[])
        assert "Bash" in tools

    def test_returns_same_tools_regardless_of_admin(self):
        admin_tools = build_allowed_tools(is_admin=True, skill_names=[])
        non_admin_tools = build_allowed_tools(is_admin=False, skill_names=[])
        assert admin_tools == non_admin_tools

    def test_returns_same_tools_regardless_of_skills(self):
        base = build_allowed_tools(is_admin=False, skill_names=[])
        with_dev = build_allowed_tools(is_admin=False, skill_names=["developer"])
        assert base == with_dev


class TestConfigEnvVarOverrides:
    def _write_minimal_config(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[nextcloud]\nurl = "https://nc.example.com"\nusername = "istota"\n'
            'app_password = "toml-password"\n'
            '[email]\nimap_password = "toml-imap"\nsmtp_password = "toml-smtp"\n'
            '[developer]\ngitlab_token = "toml-token"\n'
        )
        return config_file

    def test_nc_app_password_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_NEXTCLOUD_APP_PASSWORD": "env-password"}, clear=False):
            config = load_config(config_file)
        assert config.nextcloud.app_password == "env-password"

    def test_imap_password_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_EMAIL_IMAP_PASSWORD": "env-imap"}, clear=False):
            config = load_config(config_file)
        assert config.email.imap_password == "env-imap"

    def test_smtp_password_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_EMAIL_SMTP_PASSWORD": "env-smtp"}, clear=False):
            config = load_config(config_file)
        assert config.email.smtp_password == "env-smtp"

    def test_gitlab_token_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_DEVELOPER_GITLAB_TOKEN": "env-gl-token"}, clear=False):
            config = load_config(config_file)
        assert config.developer.gitlab_token == "env-gl-token"

    def test_missing_env_var_keeps_toml_value(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        # Ensure none of the override env vars are set
        env_clean = {
            k: v for k, v in os.environ.items()
            if k not in {
                "ISTOTA_NEXTCLOUD_APP_PASSWORD", "ISTOTA_EMAIL_IMAP_PASSWORD", "ISTOTA_EMAIL_SMTP_PASSWORD",
                "ISTOTA_DEVELOPER_GITLAB_TOKEN",
            }
        }
        with patch.dict(os.environ, env_clean, clear=True):
            config = load_config(config_file)
        assert config.nextcloud.app_password == "toml-password"
        assert config.email.imap_password == "toml-imap"
        assert config.email.smtp_password == "toml-smtp"
        assert config.developer.gitlab_token == "toml-token"

    def test_security_config_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        config = load_config(config_file)
        assert config.security.sandbox_enabled is True
        assert config.security.skill_proxy_enabled is True

    def test_security_config_overrides(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[security]\nsandbox_enabled = false\nskill_proxy_enabled = false\n'
        )
        config = load_config(config_file)
        assert config.security.sandbox_enabled is False
        assert config.security.skill_proxy_enabled is False


def _bundled_skill_index():
    """Load the real bundled skill manifests for credential-derivation tests."""
    from istota.skills._loader import load_skill_index
    return load_skill_index(Path("config/skills"), bundled_dir=None)


def _ctx_with_config(config: Config) -> EnvContext:
    """Minimal EnvContext for resolution tests."""
    class _T:
        user_id = "alice"
    return EnvContext(
        config=config,
        task=_T(),
        user_resources=[],
        user_config=None,
        user_temp_dir=Path("/tmp"),
        is_admin=False,
    )


class TestDeriveSkillCredentialMap:
    """Per-skill credential map derived from manifests."""

    def test_email_skills_get_email_credentials(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["email"], idx)
        assert result == {"email": {"SMTP_PASSWORD", "IMAP_PASSWORD"}}

    def test_developer_skills_get_developer_credentials(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["developer"], idx)
        assert result == {"developer": {"GITLAB_TOKEN", "GITHUB_TOKEN"}}

    def test_calendar_gets_caldav_password(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["calendar"], idx)
        assert result == {"calendar": {"CALDAV_PASSWORD"}}

    def test_location_gets_caldav_password(self):
        """Location skill needs CALDAV_PASSWORD for attendance subcommand."""
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["location"], idx)
        assert result == {"location": {"CALDAV_PASSWORD"}}

    def test_no_creds_returns_empty(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["browse", "markets"], idx)
        assert result == {}

    def test_empty_skill_list(self):
        idx = _bundled_skill_index()
        assert derive_skill_credential_map([], idx) == {}

    def test_nextcloud_gets_nc_pass(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["nextcloud"], idx)
        assert "NC_PASS" in result["nextcloud"]

    def test_bookmarks_gets_karakeep(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["bookmarks"], idx)
        assert result == {"bookmarks": {"KARAKEEP_API_KEY"}}

    def test_money_gets_monarch_credentials(self):
        """money's Monarch credentials are pre-resolved on the trusted
        side via the manifest ``from: "secret"`` blocks."""
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(["money"], idx)
        assert result == {
            "money": {"MONARCH_EMAIL", "MONARCH_PASSWORD", "MONARCH_SESSION_TOKEN"},
        }

    def test_feeds_gets_tumblr_key(self):
        idx = _bundled_skill_index()
        assert derive_skill_credential_map(["feeds"], idx) == {
            "feeds": {"TUMBLR_API_KEY"},
        }


class TestDeriveLookupAllowlist:
    """Lookup allowlist is the union of per-skill credentials, minus the
    instance-wide block list."""

    def test_email_skills_get_email_credentials(self):
        idx = _bundled_skill_index()
        assert derive_lookup_allowlist(["email"], idx) == {
            "SMTP_PASSWORD", "IMAP_PASSWORD",
        }

    def test_multiple_skills_union(self):
        idx = _bundled_skill_index()
        assert derive_lookup_allowlist(
            ["email", "developer", "calendar"], idx,
        ) == {
            "SMTP_PASSWORD", "IMAP_PASSWORD",
            "GITLAB_TOKEN", "GITHUB_TOKEN",
            "CALDAV_PASSWORD",
        }

    def test_skills_with_no_credentials(self):
        idx = _bundled_skill_index()
        assert derive_lookup_allowlist(
            ["browse", "transcribe", "markets"], idx,
        ) == set()

    def test_master_key_blocked_even_if_injected(self):
        """Defense-in-depth: a setup_env hook injecting ISTOTA_SECRET_KEY
        cannot make it through the lookup endpoint. Build an ad-hoc skill
        that declares the var sensitive and verify subtraction."""
        idx = {
            "evil": SkillMeta(
                name="evil",
                description="",
                env_specs=[EnvSpec(
                    var="ISTOTA_SECRET_KEY", source="setup_env",
                    sensitive=True,
                )],
            ),
        }
        assert derive_lookup_allowlist(["evil"], idx) == set()


class TestSplitCredentialEnv:
    """``_split_credential_env`` takes the credential set and partitions
    the env dict."""

    def test_routes_pre_resolved_secrets(self):
        env = {
            "PATH": "/usr/bin",
            "HOME": "/tmp",
            "MONARCH_SESSION_TOKEN": "tok-abc",
            "TUMBLR_API_KEY": "tk-xyz",
            "ISTOTA_TASK_ID": "42",
        }
        credential_env, clean_env = _split_credential_env(
            env, frozenset({"MONARCH_SESSION_TOKEN", "TUMBLR_API_KEY"}),
        )
        assert credential_env == {
            "MONARCH_SESSION_TOKEN": "tok-abc",
            "TUMBLR_API_KEY": "tk-xyz",
        }
        assert "MONARCH_SESSION_TOKEN" not in clean_env
        assert "TUMBLR_API_KEY" not in clean_env
        assert clean_env["PATH"] == "/usr/bin"
        assert clean_env["ISTOTA_TASK_ID"] == "42"

    def test_empty_credential_set_passes_env_through(self):
        env = {"PATH": "/usr/bin", "TOKEN": "tok"}
        credential_env, clean_env = _split_credential_env(env, frozenset())
        assert credential_env == {}
        assert clean_env == env


class TestProxyLookupBlocked:
    """ISTOTA_SECRET_KEY is on the defense-in-depth block list so a
    setup_env hook injecting it cannot leak it via credential-fetch.

    (Regression for the c1055d0 follow-up: pre-patch, money/feeds were
    auto-authorized on every host because the master key was set
    instance-wide, and `.developer/credential-fetch ISTOTA_SECRET_KEY`
    returned the raw Fernet key.)
    """

    def test_master_key_in_block_set(self):
        assert "ISTOTA_SECRET_KEY" in _PROXY_LOOKUP_BLOCKED


class TestPhase1MasterKeyEgress:
    """Phase 1 acceptance: ISTOTA_SECRET_KEY must never enter any
    subprocess env after Phase 1.4."""

    def test_credential_set_excludes_master_key(self):
        """ISTOTA_SECRET_KEY is not declared sensitive on any skill
        manifest, so it never appears in the derived credential set —
        it stays on the clean env (the trusted daemon needs it)."""
        idx = _bundled_skill_index()
        assert "ISTOTA_SECRET_KEY" not in derive_credential_set(idx)

    def test_skill_credential_map_excludes_master_key(self):
        idx = _bundled_skill_index()
        result = derive_skill_credential_map(list(idx.keys()), idx)
        for creds in result.values():
            assert "ISTOTA_SECRET_KEY" not in creds

    def test_build_clean_env_excludes_master_key(self):
        """Even with the master key in the parent env, Claude's clean env
        omits it. (build_clean_env strictly allowlists what flows through.)"""
        from istota.executor import build_clean_env
        from istota.config import Config
        with patch.dict(os.environ, {
            "ISTOTA_SECRET_KEY": "k" * 64,
            "PATH": "/usr/bin",
        }, clear=True):
            env = build_clean_env(Config())
        assert "ISTOTA_SECRET_KEY" not in env

    def test_build_stripped_env_excludes_master_key(self):
        """Phase 1.4 — build_stripped_env (heartbeat / command-task path)
        also strips the master key. Operator-defined heartbeat shells that
        called istota-skill feeds/money directly stop working; documented
        in CHANGELOG."""
        with patch.dict(os.environ, {
            "ISTOTA_SECRET_KEY": "k" * 64,
            "PATH": "/usr/bin",
        }, clear=True):
            env = build_stripped_env()
        assert "ISTOTA_SECRET_KEY" not in env
