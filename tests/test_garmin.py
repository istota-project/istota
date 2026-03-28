"""Tests for Garmin skill — garmin_login auth and token caching."""

from unittest.mock import MagicMock, patch, call
import os
import pytest

from istota.skills.garmin import garmin_login, load_config, _get_client


class TestGarminLogin:
    """Tests for garmin_login() token caching and error logging."""

    def test_token_login_succeeds_no_password_fallback(self, tmp_path, capsys):
        """When token login works, should return client without password login."""
        token_dir = str(tmp_path / "tokens")

        mock_client = MagicMock()
        with patch("istota.skills.garmin.garminconnect") as mock_gc:
            mock_gc.Garmin.return_value = mock_client
            mock_client.login.return_value = None  # token login succeeds

            result = garmin_login("a@b.com", "pass", token_dir)

        assert result is mock_client
        # Only one Garmin() call (token-based), no second with credentials
        mock_gc.Garmin.assert_called_once_with()
        mock_client.login.assert_called_once_with(token_dir)

        stderr = capsys.readouterr().err
        assert "[garmin] Token login succeeded" in stderr

    def test_token_login_fails_falls_back_to_password(self, tmp_path, capsys):
        """When token login fails, should fall back to email/password."""
        token_dir = str(tmp_path / "tokens")

        mock_token_client = MagicMock()
        mock_token_client.login.side_effect = Exception("no tokens found")

        mock_pw_client = MagicMock()
        mock_pw_client.garth = MagicMock()

        with patch("istota.skills.garmin.garminconnect") as mock_gc:
            mock_gc.Garmin.side_effect = [mock_token_client, mock_pw_client]

            result = garmin_login("a@b.com", "pass", token_dir)

        assert result is mock_pw_client
        # First call: no-arg (token attempt), second: with credentials
        assert mock_gc.Garmin.call_args_list == [call(), call("a@b.com", "pass")]

        stderr = capsys.readouterr().err
        assert "[garmin] Token login failed: no tokens found" in stderr
        assert "[garmin] Password login succeeded" in stderr

    def test_token_dump_failure_logged(self, tmp_path, capsys):
        """When garth.dump() fails, the error should be logged to stderr."""
        token_dir = str(tmp_path / "tokens")

        mock_token_client = MagicMock()
        mock_token_client.login.side_effect = Exception("no tokens")

        mock_pw_client = MagicMock()
        mock_pw_client.garth.dump.side_effect = Exception("dump borked")

        with patch("istota.skills.garmin.garminconnect") as mock_gc:
            mock_gc.Garmin.side_effect = [mock_token_client, mock_pw_client]

            result = garmin_login("a@b.com", "pass", token_dir)

        assert result is mock_pw_client
        stderr = capsys.readouterr().err
        assert "[garmin] Token dump failed: dump borked" in stderr

    def test_token_dump_success_logged(self, tmp_path, capsys):
        """When garth.dump() succeeds, should log the files written."""
        token_dir = str(tmp_path / "tokens")
        os.makedirs(token_dir)
        # Simulate token files written by garth.dump()
        for name in ["oauth1_token.json", "oauth2_token.json"]:
            (tmp_path / "tokens" / name).write_text("{}")

        mock_token_client = MagicMock()
        mock_token_client.login.side_effect = Exception("no tokens")

        mock_pw_client = MagicMock()
        mock_pw_client.garth.dump.return_value = None

        with patch("istota.skills.garmin.garminconnect") as mock_gc:
            mock_gc.Garmin.side_effect = [mock_token_client, mock_pw_client]

            garmin_login("a@b.com", "pass", token_dir)

        stderr = capsys.readouterr().err
        assert "[garmin] Tokens dumped to" in stderr
        assert "oauth1_token.json" in stderr
        assert "oauth2_token.json" in stderr

    def test_creates_token_dir(self, tmp_path, capsys):
        """Token dir should be created if it doesn't exist."""
        token_dir = str(tmp_path / "new_dir" / "tokens")

        mock_client = MagicMock()
        with patch("istota.skills.garmin.garminconnect") as mock_gc:
            mock_gc.Garmin.return_value = mock_client
            garmin_login("a@b.com", "pass", token_dir)

        assert os.path.isdir(token_dir)

    def test_garminconnect_not_installed(self, tmp_path):
        """Should raise ImportError when garminconnect is not installed."""
        with patch("istota.skills.garmin.garminconnect", None):
            with pytest.raises(ImportError, match="garminconnect not installed"):
                garmin_login("a@b.com", "pass", str(tmp_path))


class TestGetClient:
    """Tests for _get_client() token dir resolution."""

    def test_uses_deferred_dir_for_tokens(self, tmp_path):
        """Token dir should default to ISTOTA_DEFERRED_DIR/garmin_tokens."""
        env = {
            "GARMIN_EMAIL": "a@b.com",
            "GARMIN_PASSWORD": "pass",
            "ISTOTA_DEFERRED_DIR": str(tmp_path),
        }
        mock_client = MagicMock()
        with patch.dict(os.environ, env, clear=False), \
             patch("istota.skills.garmin.garmin_login", return_value=mock_client) as mock_login:
            args = MagicMock(config=None)
            client, cfg = _get_client(args)

        assert client is mock_client
        mock_login.assert_called_once_with(
            "a@b.com", "pass", str(tmp_path / "garmin_tokens")
        )

    def test_config_token_dir_overrides_default(self, tmp_path):
        """Token dir from config file should override the default."""
        config_file = tmp_path / "GARMIN.md"
        config_file.write_text(
            '```toml\n[garmin]\nemail = "x@y.com"\npassword = "pw"\n'
            f'token_dir = "{tmp_path}/custom_tokens"\n```\n'
        )

        env = {"GARMIN_CONFIG": str(config_file)}
        mock_client = MagicMock()
        with patch.dict(os.environ, env, clear=False), \
             patch("istota.skills.garmin.garmin_login", return_value=mock_client) as mock_login:
            # Clear credential env vars so it falls back to config file
            os.environ.pop("GARMIN_EMAIL", None)
            args = MagicMock(config=None)
            client, cfg = _get_client(args)

        mock_login.assert_called_once_with(
            "x@y.com", "pw", f"{tmp_path}/custom_tokens"
        )


class TestLoadConfig:
    """Tests for load_config() credential resolution."""

    def test_env_vars_take_precedence(self):
        """GARMIN_EMAIL/PASSWORD env vars should override config file."""
        env = {"GARMIN_EMAIL": "env@b.com", "GARMIN_PASSWORD": "envpw"}
        with patch.dict(os.environ, env, clear=False):
            cfg = load_config()
        assert cfg["email"] == "env@b.com"
        assert cfg["password"] == "envpw"

    def test_config_file_fallback(self, tmp_path):
        """Should load credentials from config file when env vars missing."""
        config_file = tmp_path / "GARMIN.md"
        config_file.write_text(
            '```toml\n[garmin]\nemail = "file@b.com"\npassword = "filepw"\n```\n'
        )

        env = {"GARMIN_CONFIG": str(config_file)}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("GARMIN_EMAIL", None)
            os.environ.pop("GARMIN_PASSWORD", None)
            cfg = load_config()

        assert cfg["email"] == "file@b.com"
        assert cfg["password"] == "filepw"

    def test_no_credentials_raises(self):
        """Should raise ValueError when no credentials available."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GARMIN_EMAIL", None)
            os.environ.pop("GARMIN_CONFIG", None)
            with pytest.raises(ValueError, match="GARMIN_EMAIL"):
                load_config()
