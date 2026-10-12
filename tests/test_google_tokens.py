"""The shared token path keeps refresh, rotation and stored grants intact."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from istota import db
from istota.config import Config
from istota.credentials.google_tokens import access_token_for


@pytest.mark.parametrize("rotated", [False, True])
def test_refresh_and_store(tmp_path, monkeypatch, rotated):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    config = Config(db_path=tmp_path / "test.db")
    config.google_workspace.enabled = True
    db.init_db(config.db_path)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    scopes = '["https://www.googleapis.com/auth/drive.readonly"]'
    body = {"access_token": "new-token", "expires_in": 1200}
    if rotated:
        body["refresh_token"] = "rotated-token"
    with db.get_db(config.db_path) as conn:
        db.upsert_google_token(conn, "alice", "old-token", "refresh-token", past, scopes)
        with patch("httpx.post", return_value=httpx.Response(200, json=body)) as post:
            assert access_token_for(conn, config, "alice") == "new-token"
        updated = db.get_google_token(conn, "alice")
        assert updated["refresh_token"] == ("rotated-token" if rotated else "refresh-token")
        assert updated["scopes"] == scopes
        assert 1198 < (datetime.fromisoformat(updated["token_expiry"]) - datetime.now(timezone.utc)).total_seconds() <= 1200
        with patch("httpx.post") as unused:
            assert access_token_for(conn, config, "alice") == "new-token"
            assert access_token_for(conn, config, "bob") is None
            unused.assert_not_called()
        post.assert_called_once()


def test_failed_refresh_keeps_row(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    config = Config(db_path=tmp_path / "test.db")
    config.google_workspace.enabled = True
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        db.upsert_google_token(conn, "alice", "old-token", "refresh-token", "invalid-expiry")
        original = db.get_google_token(conn, "alice")
        with patch("httpx.post", return_value=httpx.Response(401, json={"error": "invalid_grant"})):
            assert access_token_for(conn, config, "alice") is None
        assert db.get_google_token(conn, "alice") == original
