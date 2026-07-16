"""Tests for the user-scoped Nextcloud OAuth token custody module (web_tokens)."""

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from istota import db, web_tokens
from istota.config import Config, WebConfig

KEY = "x" * 64


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "istota.db"
    db.init_db(p)
    return p


@pytest.fixture
def config(db_path):
    return Config(
        db_path=db_path,
        web=WebConfig(
            enabled=True,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web",
            oauth2_client_secret="cs",
            token_storage="encrypted",
        ),
    )


@pytest.fixture(autouse=True)
def _reset_locks():
    web_tokens._refresh_locks.clear()
    yield
    web_tokens._refresh_locks.clear()


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(web_tokens._KEY_ENV_VAR, KEY)


def _row(db_path, user_id="alice"):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM web_user_tokens WHERE user_id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()


class TestKeyHandling:
    def test_key_available_false_without_env(self, monkeypatch):
        monkeypatch.delenv(web_tokens._KEY_ENV_VAR, raising=False)
        assert web_tokens.token_key_available() is False

    def test_key_available_false_when_short(self, monkeypatch):
        monkeypatch.setenv(web_tokens._KEY_ENV_VAR, "short")
        assert web_tokens.token_key_available() is False

    def test_key_available_true(self, keyed):
        assert web_tokens.token_key_available() is True

    def test_weak_key_raises_on_use(self, monkeypatch, db_path):
        monkeypatch.setenv(web_tokens._KEY_ENV_VAR, "tooshort")
        with pytest.raises(web_tokens.WebTokenKeyTooWeakError):
            web_tokens.store_tokens(db_path, "alice", "at", "rt", 3600)

    def test_missing_key_raises_on_store(self, monkeypatch, db_path):
        monkeypatch.delenv(web_tokens._KEY_ENV_VAR, raising=False)
        with pytest.raises(web_tokens.WebTokenKeyMissingError):
            web_tokens.store_tokens(db_path, "alice", "at", "rt", 3600)

    def test_keyless_reads_are_noop_safe(self, monkeypatch, db_path, config):
        monkeypatch.delenv(web_tokens._KEY_ENV_VAR, raising=False)
        assert web_tokens.get_access_token(db_path, config, "alice") is None
        assert web_tokens.token_status(db_path, "alice") is None
        assert web_tokens.delete_tokens(db_path, "alice") is False

    def test_distinct_salt_from_secrets_store(self):
        from istota import secrets_store
        assert web_tokens._SCRYPT_SALT != secrets_store._SCRYPT_SALT


class TestStoreTokens:
    def test_roundtrip(self, keyed, db_path, config):
        web_tokens.store_tokens(db_path, "alice", "the-access", "the-refresh", 3600)
        token = web_tokens.get_access_token(db_path, config, "alice")
        assert token == "the-access"

    def test_ciphertext_at_rest(self, keyed, db_path):
        web_tokens.store_tokens(db_path, "alice", "the-access", "the-refresh", 3600)
        row = _row(db_path)
        assert row is not None
        assert "the-access" not in row["access_token"]
        assert "the-refresh" not in row["refresh_token"]

    def test_expiry_math(self, keyed, db_path):
        before = datetime.now(timezone.utc) + timedelta(seconds=3600)
        web_tokens.store_tokens(db_path, "alice", "at", "rt", 3600)
        after = datetime.now(timezone.utc) + timedelta(seconds=3600)
        row = _row(db_path)
        expires = datetime.fromisoformat(row["expires_at"])
        assert before - timedelta(seconds=2) <= expires <= after + timedelta(seconds=2)

    def test_upsert_overwrites(self, keyed, db_path, config):
        web_tokens.store_tokens(db_path, "alice", "old-at", "old-rt", 3600)
        web_tokens.store_tokens(db_path, "alice", "new-at", "new-rt", 3600)
        assert web_tokens.get_access_token(db_path, config, "alice") == "new-at"
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM web_user_tokens").fetchone()[0]
        finally:
            conn.close()
        assert n == 1


class TestGetAccessToken:
    def test_no_row_returns_none(self, keyed, db_path, config):
        assert web_tokens.get_access_token(db_path, config, "alice") is None

    def test_fresh_token_no_refresh(self, keyed, db_path, config, monkeypatch):
        web_tokens.store_tokens(db_path, "alice", "at", "rt", 3600)
        boom = MagicMock(side_effect=AssertionError("should not refresh"))
        monkeypatch.setattr(web_tokens.httpx, "post", boom)
        assert web_tokens.get_access_token(db_path, config, "alice") == "at"
        boom.assert_not_called()

    def _mock_refresh_response(self, status=200, json_body=None):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_body or {}
        return resp

    def test_refreshes_within_margin(self, keyed, db_path, config, monkeypatch):
        web_tokens.store_tokens(db_path, "alice", "stale-at", "stale-rt", 30)
        calls = []

        def fake_post(url, data=None, timeout=None, **kw):
            calls.append((url, data))
            return self._mock_refresh_response(200, {
                "access_token": "new-at", "refresh_token": "new-rt",
                "expires_in": 3600,
            })

        monkeypatch.setattr(web_tokens.httpx, "post", fake_post)
        token = web_tokens.get_access_token(db_path, config, "alice")
        assert token == "new-at"
        assert len(calls) == 1
        url, data = calls[0]
        assert data["grant_type"] == "refresh_token"
        assert data["client_id"] == "istota-web"
        assert data["client_secret"] == "cs"
        assert data["refresh_token"] == "stale-rt"
        # Default endpoint derived from provider when oauth2_token_endpoint empty.
        assert url == "https://cloud.example.com/index.php/apps/oauth2/api/v1/token"
        # Rotated pair persisted: a second call within margin uses the new pair.
        assert web_tokens.get_access_token(db_path, config, "alice") == "new-at"
        assert len(calls) == 1

    def test_explicit_token_endpoint_wins(self, keyed, db_path, monkeypatch):
        config = Config(db_path=db_path, web=WebConfig(
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="c", oauth2_client_secret="s",
            oauth2_token_endpoint="http://internal:8080/token",
            token_storage="encrypted",
        ))
        web_tokens.store_tokens(db_path, "alice", "at", "rt", 10)
        calls = []

        def fake_post(url, data=None, timeout=None, **kw):
            calls.append(url)
            return self._mock_refresh_response(200, {
                "access_token": "a2", "refresh_token": "r2", "expires_in": 600,
            })

        monkeypatch.setattr(web_tokens.httpx, "post", fake_post)
        web_tokens.get_access_token(db_path, config, "alice")
        assert calls == ["http://internal:8080/token"]

    def test_invalid_grant_deletes_row(self, keyed, db_path, config, monkeypatch):
        web_tokens.store_tokens(db_path, "alice", "at", "rt", 10)
        monkeypatch.setattr(
            web_tokens.httpx, "post",
            lambda *a, **kw: self._mock_refresh_response(400, {"error": "invalid_grant"}),
        )
        assert web_tokens.get_access_token(db_path, config, "alice") is None
        assert _row(db_path) is None

    def test_transient_503_keeps_row(self, keyed, db_path, config, monkeypatch):
        web_tokens.store_tokens(db_path, "alice", "at", "rt", 10)
        monkeypatch.setattr(
            web_tokens.httpx, "post",
            lambda *a, **kw: self._mock_refresh_response(503),
        )
        assert web_tokens.get_access_token(db_path, config, "alice") is None
        assert _row(db_path) is not None

    def test_network_error_keeps_row(self, keyed, db_path, config, monkeypatch):
        web_tokens.store_tokens(db_path, "alice", "at", "rt", 10)

        def fake_post(*a, **kw):
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(web_tokens.httpx, "post", fake_post)
        assert web_tokens.get_access_token(db_path, config, "alice") is None
        assert _row(db_path) is not None

    def test_force_refresh(self, keyed, db_path, config, monkeypatch):
        web_tokens.store_tokens(db_path, "alice", "at", "rt", 3600)
        monkeypatch.setattr(
            web_tokens.httpx, "post",
            lambda *a, **kw: self._mock_refresh_response(200, {
                "access_token": "forced", "refresh_token": "r2", "expires_in": 600,
            }),
        )
        token = web_tokens.get_access_token(
            db_path, config, "alice", force_refresh=True,
        )
        assert token == "forced"

    def test_decrypt_failure_deletes_row(self, keyed, db_path, config, monkeypatch):
        web_tokens.store_tokens(db_path, "alice", "at", "rt", 3600)
        # Rotate the key: stored ciphertext no longer decrypts.
        monkeypatch.setenv(web_tokens._KEY_ENV_VAR, "y" * 64)
        assert web_tokens.get_access_token(db_path, config, "alice") is None
        assert _row(db_path) is None

    def test_concurrent_refresh_single_http_call(self, keyed, db_path, config, monkeypatch):
        web_tokens.store_tokens(db_path, "alice", "old-at", "old-rt", 10)
        calls = []
        gate = threading.Barrier(2, timeout=5)

        def fake_post(url, data=None, timeout=None, **kw):
            calls.append(data)
            return self._mock_refresh_response(200, {
                "access_token": "new-at", "refresh_token": "new-rt",
                "expires_in": 3600,
            })

        monkeypatch.setattr(web_tokens.httpx, "post", fake_post)
        results = []

        def worker():
            gate.wait()
            results.append(web_tokens.get_access_token(db_path, config, "alice"))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert results == ["new-at", "new-at"]
        assert len(calls) == 1


class TestDeleteAndStatus:
    def test_delete(self, keyed, db_path):
        web_tokens.store_tokens(db_path, "alice", "at", "rt", 3600)
        assert web_tokens.delete_tokens(db_path, "alice") is True
        assert _row(db_path) is None
        assert web_tokens.delete_tokens(db_path, "alice") is False

    def test_status_connected(self, keyed, db_path):
        web_tokens.store_tokens(db_path, "alice", "at", "rt", 3600)
        status = web_tokens.token_status(db_path, "alice")
        assert status is not None
        assert status["connected"] is True
        assert status["expires_at"]

    def test_status_none_without_row(self, keyed, db_path):
        assert web_tokens.token_status(db_path, "alice") is None

    def test_status_does_not_need_decrypt(self, keyed, db_path, monkeypatch):
        web_tokens.store_tokens(db_path, "alice", "at", "rt", 3600)
        monkeypatch.delenv(web_tokens._KEY_ENV_VAR, raising=False)
        # Keyless status is None (feature off) — but with a *different* key the
        # row still reports connected: status never decrypts.
        monkeypatch.setenv(web_tokens._KEY_ENV_VAR, "z" * 64)
        status = web_tokens.token_status(db_path, "alice")
        assert status is not None and status["connected"] is True


class TestFeatureGate:
    def test_enabled_requires_both(self, keyed, config):
        assert web_tokens.feature_enabled(config) is True

    def test_disabled_when_ephemeral(self, keyed, config):
        config.web.token_storage = "ephemeral"
        assert web_tokens.feature_enabled(config) is False

    def test_disabled_without_key(self, monkeypatch, config):
        monkeypatch.delenv(web_tokens._KEY_ENV_VAR, raising=False)
        assert web_tokens.feature_enabled(config) is False


class TestMissingTable:
    """A web process pointed at a pre-migration DB degrades, never crashes."""

    def test_reads_survive_missing_table(self, keyed, tmp_path, config):
        import sqlite3
        bare = tmp_path / "bare.db"
        sqlite3.connect(bare).close()
        assert web_tokens.get_access_token(bare, config, "alice") is None
        assert web_tokens.token_status(bare, "alice") is None
        assert web_tokens.delete_tokens(bare, "alice") is False

    def test_store_creates_table(self, keyed, tmp_path, config):
        import sqlite3
        bare = tmp_path / "bare.db"
        sqlite3.connect(bare).close()
        config.db_path = bare
        web_tokens.store_tokens(bare, "alice", "at", "rt", 3600)
        assert web_tokens.get_access_token(bare, config, "alice") == "at"
