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
    NtfyConfig,
    SecurityConfig,
    load_config,
)
from istota.executor import (
    _CREDENTIAL_ENV_PATTERNS,
    build_allowed_tools,
    build_clean_env,
    build_stripped_env,
)


class TestBuildCleanEnv:
    def test_permissive_mode_returns_full_env(self):
        config = Config(security=SecurityConfig(mode="permissive"))
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/test", "SECRET_KEY": "abc"}, clear=True):
            env = build_clean_env(config)
        assert env["PATH"] == "/usr/bin"
        assert env["SECRET_KEY"] == "abc"

    def test_restricted_mode_returns_minimal_env(self):
        config = Config(security=SecurityConfig(mode="restricted"))
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

    def test_restricted_mode_includes_passthrough_vars(self):
        config = Config(security=SecurityConfig(
            mode="restricted",
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

    def test_restricted_mode_skips_missing_passthrough_vars(self):
        config = Config(security=SecurityConfig(
            mode="restricted",
            passthrough_env_vars=["LANG", "TZ"],
        ))
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/test"}, clear=True):
            env = build_clean_env(config)
        assert "LANG" not in env
        assert "TZ" not in env

    def test_restricted_mode_default_path_when_missing(self):
        config = Config(security=SecurityConfig(mode="restricted"))
        with patch.dict(os.environ, {"HOME": "/home/test"}, clear=True):
            env = build_clean_env(config)
        # Should include default system paths and the venv bin dir
        assert "/usr/local/bin" in env["PATH"]
        assert "/usr/bin" in env["PATH"]
        import sys
        venv_bin = str(Path(sys.prefix).resolve() / "bin")
        assert venv_bin in env["PATH"]


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
            "ISTOTA_NC_APP_PASSWORD": "pw-123",
        }, clear=True):
            env = build_stripped_env()
        assert "ISTOTA_NC_APP_PASSWORD" not in env

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
            '[ntfy]\ntoken = "toml-ntfy-token"\npassword = "toml-ntfy-pw"\n'
        )
        return config_file

    def test_nc_app_password_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_NC_APP_PASSWORD": "env-password"}, clear=False):
            config = load_config(config_file)
        assert config.nextcloud.app_password == "env-password"

    def test_imap_password_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_IMAP_PASSWORD": "env-imap"}, clear=False):
            config = load_config(config_file)
        assert config.email.imap_password == "env-imap"

    def test_smtp_password_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_SMTP_PASSWORD": "env-smtp"}, clear=False):
            config = load_config(config_file)
        assert config.email.smtp_password == "env-smtp"

    def test_gitlab_token_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_GITLAB_TOKEN": "env-gl-token"}, clear=False):
            config = load_config(config_file)
        assert config.developer.gitlab_token == "env-gl-token"

    def test_ntfy_token_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_NTFY_TOKEN": "env-ntfy-tok"}, clear=False):
            config = load_config(config_file)
        assert config.ntfy.token == "env-ntfy-tok"

    def test_ntfy_password_override(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        with patch.dict(os.environ, {"ISTOTA_NTFY_PASSWORD": "env-ntfy-pw"}, clear=False):
            config = load_config(config_file)
        assert config.ntfy.password == "env-ntfy-pw"

    def test_missing_env_var_keeps_toml_value(self, tmp_path):
        config_file = self._write_minimal_config(tmp_path)
        # Ensure none of the override env vars are set
        env_clean = {
            k: v for k, v in os.environ.items()
            if k not in {
                "ISTOTA_NC_APP_PASSWORD", "ISTOTA_IMAP_PASSWORD", "ISTOTA_SMTP_PASSWORD",
                "ISTOTA_GITLAB_TOKEN", "ISTOTA_NTFY_TOKEN", "ISTOTA_NTFY_PASSWORD",
            }
        }
        with patch.dict(os.environ, env_clean, clear=True):
            config = load_config(config_file)
        assert config.nextcloud.app_password == "toml-password"
        assert config.email.imap_password == "toml-imap"
        assert config.email.smtp_password == "toml-smtp"
        assert config.developer.gitlab_token == "toml-token"
        assert config.ntfy.token == "toml-ntfy-token"
        assert config.ntfy.password == "toml-ntfy-pw"

    def test_security_config_parsed_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[security]\nmode = "restricted"\n')
        config = load_config(config_file)
        assert config.security.mode == "restricted"

    def test_security_config_default_permissive(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        config = load_config(config_file)
        assert config.security.mode == "permissive"
