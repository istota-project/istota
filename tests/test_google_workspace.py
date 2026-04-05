"""Tests for Google Workspace skill — config, DB tokens, skill selection, setup_env, network."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from istota import config, db
from istota.skills._loader import load_skill_index, select_skills


# ============================================================================
# Helpers
# ============================================================================

def _empty_bundled(tmp_path: Path) -> Path:
    d = tmp_path / "_empty_bundled"
    d.mkdir(exist_ok=True)
    return d


def _init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    return db_path


def _make_config(**overrides) -> config.Config:
    c = config.Config()
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


# ============================================================================
# Config
# ============================================================================

class TestGoogleWorkspaceConfig:
    def test_defaults(self):
        c = config.GoogleWorkspaceConfig()
        assert c.enabled is False
        assert c.client_id == ""
        assert c.client_secret == ""
        assert len(c.scopes) == 5
        assert "https://www.googleapis.com/auth/drive.readonly" in c.scopes

    def test_on_main_config(self):
        c = config.Config()
        assert isinstance(c.google_workspace, config.GoogleWorkspaceConfig)
        assert c.google_workspace.enabled is False

    def test_load_from_toml(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text("""
[google_workspace]
enabled = true
client_id = "test-client-id"
client_secret = "test-secret"
scopes = ["https://www.googleapis.com/auth/drive"]
""")
        c = config.load_config(toml_file)
        assert c.google_workspace.enabled is True
        assert c.google_workspace.client_id == "test-client-id"
        assert c.google_workspace.client_secret == "test-secret"
        assert c.google_workspace.scopes == ["https://www.googleapis.com/auth/drive"]

    def test_env_var_override(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text("""
[google_workspace]
enabled = true
client_id = "id"
""")
        with mock.patch.dict("os.environ", {"ISTOTA_GOOGLE_CLIENT_SECRET": "from-env"}):
            c = config.load_config(toml_file)
        assert c.google_workspace.client_secret == "from-env"


# ============================================================================
# DB tokens
# ============================================================================

class TestGoogleTokenDB:
    def test_get_returns_none_when_empty(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            assert db.get_google_token(conn, "alice") is None

    def test_upsert_and_get(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.upsert_google_token(
                conn, "alice", "access123", "refresh456",
                "2025-12-31T00:00:00+00:00", '["drive"]',
            )
            token = db.get_google_token(conn, "alice")
        assert token is not None
        assert token["access_token"] == "access123"
        assert token["refresh_token"] == "refresh456"
        assert token["scopes"] == '["drive"]'

    def test_upsert_updates_existing(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.upsert_google_token(conn, "alice", "old", "refresh", "2025-01-01T00:00:00+00:00")
            db.upsert_google_token(conn, "alice", "new", "refresh2", "2025-12-31T00:00:00+00:00")
            token = db.get_google_token(conn, "alice")
        assert token["access_token"] == "new"
        assert token["refresh_token"] == "refresh2"

    def test_delete(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.upsert_google_token(conn, "alice", "a", "r", "2025-01-01T00:00:00+00:00")
            assert db.delete_google_token(conn, "alice") is True
            assert db.get_google_token(conn, "alice") is None

    def test_delete_nonexistent(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            assert db.delete_google_token(conn, "nobody") is False

    def test_per_user_isolation(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.upsert_google_token(conn, "alice", "a-token", "a-refresh", "2025-01-01T00:00:00+00:00")
            db.upsert_google_token(conn, "bob", "b-token", "b-refresh", "2025-01-01T00:00:00+00:00")
            assert db.get_google_token(conn, "alice")["access_token"] == "a-token"
            assert db.get_google_token(conn, "bob")["access_token"] == "b-token"


# ============================================================================
# Skill selection
# ============================================================================

class TestGoogleWorkspaceSkillSelection:
    """Skill selection via keyword triggers."""

    def _index(self):
        bundled = Path(__file__).parent.parent / "src" / "istota" / "skills"
        return load_skill_index(skills_dir=Path("/dev/null"), bundled_dir=bundled)

    def test_google_drive_triggers_skill(self):
        idx = self._index()
        selected = select_skills(
            "upload this file to google drive", "talk", set(),
            idx, is_admin=True,
        )
        assert "google_workspace" in selected

    def test_spreadsheet_triggers_skill(self):
        idx = self._index()
        selected = select_skills(
            "create a spreadsheet with my expenses", "talk", set(),
            idx, is_admin=True,
        )
        assert "google_workspace" in selected

    def test_gws_triggers_skill(self):
        idx = self._index()
        selected = select_skills(
            "use gws to list my files", "talk", set(),
            idx, is_admin=True,
        )
        assert "google_workspace" in selected

    def test_bare_calendar_does_not_trigger(self):
        """'calendar' alone should not trigger google_workspace."""
        idx = self._index()
        selected = select_skills(
            "show me my calendar events", "talk", {"calendar"},
            idx, is_admin=True,
        )
        assert "google_workspace" not in selected

    def test_bare_email_does_not_trigger(self):
        """'email' alone should not trigger google_workspace."""
        idx = self._index()
        selected = select_skills(
            "check my email inbox", "talk", {"email_folder"},
            idx, is_admin=True,
        )
        assert "google_workspace" not in selected


# ============================================================================
# setup_env hook
# ============================================================================

class TestSetupEnv:
    def _make_ctx(self, tmp_path, gw_enabled=True, user_id="alice"):
        db_path = _init_db(tmp_path)
        c = _make_config(
            db_path=db_path,
            google_workspace=config.GoogleWorkspaceConfig(
                enabled=gw_enabled,
                client_id="cid",
                client_secret="csecret",
            ),
        )
        task = mock.MagicMock()
        task.user_id = user_id
        from istota.skills._env import EnvContext
        return EnvContext(
            config=c, task=task, user_resources=[],
            user_config=None, user_temp_dir=tmp_path, is_admin=True,
        )

    def test_returns_empty_when_disabled(self, tmp_path):
        from istota.skills.google_workspace import setup_env
        ctx = self._make_ctx(tmp_path, gw_enabled=False)
        assert setup_env(ctx) == {}

    def test_returns_empty_when_no_token(self, tmp_path):
        from istota.skills.google_workspace import setup_env
        ctx = self._make_ctx(tmp_path)
        assert setup_env(ctx) == {}

    def test_returns_token_when_valid(self, tmp_path):
        from istota.skills.google_workspace import setup_env
        ctx = self._make_ctx(tmp_path)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with db.get_db(ctx.config.db_path) as conn:
            db.upsert_google_token(conn, "alice", "valid-token", "refresh", future)
        result = setup_env(ctx)
        assert result["GOOGLE_WORKSPACE_CLI_TOKEN"] == "valid-token"
        assert "GOOGLE_WORKSPACE_CLI_CONFIG_DIR" in result

    def test_refreshes_expired_token(self, tmp_path):
        from istota.skills.google_workspace import setup_env
        ctx = self._make_ctx(tmp_path)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with db.get_db(ctx.config.db_path) as conn:
            db.upsert_google_token(conn, "alice", "old-token", "refresh-tok", past)

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new-token",
            "expires_in": 3600,
        }

        with mock.patch("httpx.post", return_value=mock_response) as mock_post:
            result = setup_env(ctx)

        assert result["GOOGLE_WORKSPACE_CLI_TOKEN"] == "new-token"
        assert "GOOGLE_WORKSPACE_CLI_CONFIG_DIR" in result
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["data"]["grant_type"] == "refresh_token"
        assert call_kwargs[1]["data"]["refresh_token"] == "refresh-tok"

        # Verify token was updated in DB
        with db.get_db(ctx.config.db_path) as conn:
            updated = db.get_google_token(conn, "alice")
        assert updated["access_token"] == "new-token"

    def test_returns_empty_on_refresh_failure(self, tmp_path):
        from istota.skills.google_workspace import setup_env
        ctx = self._make_ctx(tmp_path)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with db.get_db(ctx.config.db_path) as conn:
            db.upsert_google_token(conn, "alice", "old", "refresh", past)

        mock_response = mock.MagicMock()
        mock_response.status_code = 401
        mock_response.text = "invalid_grant"

        with mock.patch("httpx.post", return_value=mock_response):
            result = setup_env(ctx)
        assert result == {}


# ============================================================================
# Network allowlist
# ============================================================================

class TestNetworkAllowlist:
    def test_google_hosts_added_when_skill_selected(self):
        from istota.executor import _build_network_allowlist
        c = _make_config()
        hosts = _build_network_allowlist(c, ["google_workspace"])
        assert "drive.googleapis.com:443" in hosts
        assert "sheets.googleapis.com:443" in hosts
        assert "gmail.googleapis.com:443" in hosts
        assert "oauth2.googleapis.com:443" in hosts

    def test_google_hosts_not_added_without_skill(self):
        from istota.executor import _build_network_allowlist
        c = _make_config()
        hosts = _build_network_allowlist(c, ["calendar"])
        assert "drive.googleapis.com:443" not in hosts


# ============================================================================
# Credential proxy mapping
# ============================================================================

class TestCredentialProxy:
    def test_token_in_proxy_vars(self):
        from istota.executor import _PROXY_CREDENTIAL_VARS
        assert "GOOGLE_WORKSPACE_CLI_TOKEN" in _PROXY_CREDENTIAL_VARS

    def test_token_mapped_to_skill(self):
        from istota.executor import _CREDENTIAL_SKILL_MAP
        assert "GOOGLE_WORKSPACE_CLI_TOKEN" in _CREDENTIAL_SKILL_MAP
        assert "google_workspace" in _CREDENTIAL_SKILL_MAP["GOOGLE_WORKSPACE_CLI_TOKEN"]
