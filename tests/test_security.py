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
    _CREDENTIAL_SKILL_MAP,
    _LOOKUP_DENIED_VARS,
    _allowed_credentials_for_skills,
    _authorized_skills_from_credentials,
    _build_skill_credential_map,
    build_allowed_tools,
    build_clean_env,
    build_stripped_env,
)
from istota.skills._types import SkillMeta


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

    def test_preserves_istota_secret_key(self):
        # ISTOTA_SECRET_KEY matches the SECRET strip pattern but must survive:
        # module-skill subprocesses (feeds, money, location) need it to
        # decrypt per-user credentials from the secrets table.
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "ISTOTA_SECRET_KEY": "a" * 64,
            "OTHER_SECRET": "should-be-stripped",
        }, clear=True):
            env = build_stripped_env()
        assert env["ISTOTA_SECRET_KEY"] == "a" * 64
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


class TestCredentialSkillScoping:
    """Tests for per-skill credential scoping helpers."""

    def test_email_skills_get_email_credentials(self):
        result = _allowed_credentials_for_skills(["email"])
        assert result == {"SMTP_PASSWORD", "IMAP_PASSWORD"}

    def test_developer_skills_get_developer_credentials(self):
        result = _allowed_credentials_for_skills(["developer"])
        assert result == {"GITLAB_TOKEN", "GITHUB_TOKEN"}

    def test_calendar_gets_caldav_password(self):
        result = _allowed_credentials_for_skills(["calendar"])
        assert result == {"CALDAV_PASSWORD"}

    def test_location_gets_caldav_password(self):
        """Location skill needs CALDAV_PASSWORD for attendance subcommand."""
        result = _allowed_credentials_for_skills(["location"])
        assert result == {"CALDAV_PASSWORD"}

    def test_multiple_skills_union(self):
        result = _allowed_credentials_for_skills(["email", "developer", "calendar"])
        assert result == {
            "SMTP_PASSWORD", "IMAP_PASSWORD",
            "GITLAB_TOKEN", "GITHUB_TOKEN",
            "CALDAV_PASSWORD",
        }

    def test_skills_with_no_credentials(self):
        result = _allowed_credentials_for_skills(["browse", "transcribe", "markets"])
        assert result == set()

    def test_empty_skills_list(self):
        result = _allowed_credentials_for_skills([])
        assert result == set()

    def test_nextcloud_gets_nc_pass(self):
        result = _allowed_credentials_for_skills(["nextcloud"])
        assert result == {"NC_PASS"}

    def test_files_has_no_credentials(self):
        """files skill is doc-only (no CLI), works via mount — no creds needed."""
        result = _allowed_credentials_for_skills(["files"])
        assert result == set()

    def test_bookmarks_gets_karakeep(self):
        result = _allowed_credentials_for_skills(["bookmarks"])
        assert result == {"KARAKEEP_API_KEY"}

    def test_build_skill_credential_map_email(self):
        result = _build_skill_credential_map(["email"])
        assert result == {"email": {"SMTP_PASSWORD", "IMAP_PASSWORD"}}

    def test_build_skill_credential_map_developer(self):
        result = _build_skill_credential_map(["developer"])
        assert result == {"developer": {"GITLAB_TOKEN", "GITHUB_TOKEN"}}

    def test_build_skill_credential_map_no_creds(self):
        result = _build_skill_credential_map(["browse", "markets"])
        assert result == {}

    def test_build_skill_credential_map_mixed(self):
        result = _build_skill_credential_map(["email", "browse", "calendar"])
        assert "email" in result
        assert "calendar" in result
        assert "browse" not in result

    def test_credential_skill_map_covers_all_proxy_vars(self):
        """Every var in _PROXY_CREDENTIAL_VARS should appear in _CREDENTIAL_SKILL_MAP."""
        from istota.executor import _PROXY_CREDENTIAL_VARS
        mapped_vars = set(_CREDENTIAL_SKILL_MAP.keys())
        assert mapped_vars == _PROXY_CREDENTIAL_VARS

    def test_money_and_feeds_get_master_secret_key(self):
        """money and feeds resolve per-user secrets at runtime via
        secrets_store.get_secret(); they need ISTOTA_SECRET_KEY in their
        subprocess env. (Regression for ISSUE-082.)"""
        assert _allowed_credentials_for_skills(["money"]) == {"ISTOTA_SECRET_KEY"}
        assert _allowed_credentials_for_skills(["feeds"]) == {"ISTOTA_SECRET_KEY"}

    def test_unrelated_skills_do_not_get_master_secret_key(self):
        """Skills that don't decrypt secrets at runtime must not be
        authorized for ISTOTA_SECRET_KEY — narrower blast radius."""
        for skill in ["email", "calendar", "developer", "browse",
                      "markets", "transcribe", "bookmarks"]:
            assert "ISTOTA_SECRET_KEY" not in _allowed_credentials_for_skills([skill])

    def test_split_credential_env_routes_master_key_to_proxy(self):
        """ISTOTA_SECRET_KEY must be split out of Claude's clean env so the
        brain subprocess never sees it; only the proxy injects it into
        authorized skill subprocesses. (Regression for ISSUE-082.)"""
        from istota.executor import _split_credential_env
        env = {
            "PATH": "/usr/bin",
            "HOME": "/tmp",
            "ISTOTA_SECRET_KEY": "a" * 64,
            "ISTOTA_TASK_ID": "42",
        }
        credential_env, clean_env = _split_credential_env(env)
        assert "ISTOTA_SECRET_KEY" not in clean_env
        assert credential_env["ISTOTA_SECRET_KEY"] == "a" * 64
        # Non-credential vars stay in clean env
        assert clean_env["PATH"] == "/usr/bin"
        assert clean_env["ISTOTA_TASK_ID"] == "42"


class TestLookupDeniedVars:
    """ISTOTA_SECRET_KEY may flow into specific skill subprocess envs but must
    never be returned by the proxy's credential-lookup endpoint, and must not
    auto-authorize its mapped skills via credential presence alone.

    (Regression for the c1055d0 follow-up: pre-patch, money/feeds were
    auto-authorized on every host because the master key is set instance-wide,
    and `bash -c '.developer/credential-fetch ISTOTA_SECRET_KEY'` returned the
    raw Fernet key.)
    """

    def test_master_key_in_lookup_denied_set(self):
        assert "ISTOTA_SECRET_KEY" in _LOOKUP_DENIED_VARS

    def _index(self):
        return {
            name: SkillMeta(name=name, description="", cli=True)
            for name in (
                "money", "feeds", "bookmarks", "email",
                "calendar", "developer", "nextcloud",
            )
        }

    def test_master_key_alone_does_not_auto_authorize_money_or_feeds(self):
        """Instance-wide vars (master key) must not auto-authorize any
        skill — the auto-authorization safety net is for per-user signals."""
        result = _authorized_skills_from_credentials(
            self._index(), {"ISTOTA_SECRET_KEY": "k" * 64},
        )
        assert "money" not in result
        assert "feeds" not in result
        assert result == []

    def test_other_creds_still_auto_authorize_their_skills(self):
        """Per-user creds keep auto-authorizing their skills even when the
        master key is also present (the common production env shape)."""
        result = _authorized_skills_from_credentials(self._index(), {
            "ISTOTA_SECRET_KEY": "k" * 64,
            "KARAKEEP_API_KEY": "x",
            "GITLAB_TOKEN": "y",
        })
        assert sorted(result) == ["bookmarks", "developer"]
        assert "money" not in result
        assert "feeds" not in result

    def test_money_still_gets_master_key_in_credential_map(self):
        """Selection-driven authorization (executor unions selected CLI
        skills into cred_skill_names) is still expected to give money/feeds
        their master key via skill_credential_map.  This pins the underlying
        per-skill mapping: when money is in cred_skill_names, its credential
        set still includes the master key."""
        assert _build_skill_credential_map(["money"]) == {
            "money": {"ISTOTA_SECRET_KEY"},
        }
        assert _build_skill_credential_map(["feeds"]) == {
            "feeds": {"ISTOTA_SECRET_KEY"},
        }
