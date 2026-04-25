"""Security-focused tests for moneyman API."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from money.api.auth import derive_user_key


@pytest.fixture
def tmp_ledger(tmp_path):
    ledger_dir = tmp_path / "ledgers"
    ledger_dir.mkdir()
    ledger = ledger_dir / "main.beancount"
    ledger.write_text("")
    return ledger


@pytest.fixture
def tmp_config(tmp_path, tmp_ledger):
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{tmp_path}"\n\n'
        f'[[ledgers]]\nname = "main"\npath = "{tmp_ledger}"\n'
    )
    return config


@pytest.fixture
def authed_client(tmp_config):
    import os
    os.environ["MONEYMAN_CONFIG"] = str(tmp_config)
    os.environ["MONEYMAN_API_KEY"] = "test-secret-key"

    from money.api.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c

    os.environ.pop("MONEYMAN_CONFIG", None)
    os.environ.pop("MONEYMAN_API_KEY", None)


@pytest.fixture
def client(tmp_config):
    import os
    os.environ["MONEYMAN_CONFIG"] = str(tmp_config)
    os.environ.pop("MONEYMAN_API_KEY", None)

    from money.api.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c

    os.environ.pop("MONEYMAN_CONFIG", None)


@pytest.fixture
def multi_user_client(tmp_path):
    import os

    bob_dir = tmp_path / "bob"
    bob_dir.mkdir()
    (bob_dir / "ledgers").mkdir()
    (bob_dir / "ledgers" / "business.beancount").write_text("")

    alice_dir = tmp_path / "alice"
    alice_dir.mkdir()
    (alice_dir / "ledgers").mkdir()
    (alice_dir / "ledgers" / "personal.beancount").write_text("")

    config = tmp_path / "config.toml"
    config.write_text(
        f'[users.bob]\n'
        f'data_dir = "{bob_dir}"\n\n'
        f'[[users.bob.ledgers]]\n'
        f'name = "business"\n'
        f'path = "{bob_dir / "ledgers" / "business.beancount"}"\n\n'
        f'[users.alice]\n'
        f'data_dir = "{alice_dir}"\n\n'
        f'[[users.alice.ledgers]]\n'
        f'name = "personal"\n'
        f'path = "{alice_dir / "ledgers" / "personal.beancount"}"\n'
    )

    os.environ["MONEYMAN_CONFIG"] = str(config)
    os.environ.pop("MONEYMAN_API_KEY", None)

    from money.api.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c

    os.environ.pop("MONEYMAN_CONFIG", None)


# =============================================================================
# Timing-safe auth
# =============================================================================


class TestTimingSafeAuth:
    def test_uses_hmac_compare_digest(self):
        """Verify auth module uses constant-time comparison."""
        import money.api.auth as auth_mod
        import inspect
        source = inspect.getsource(auth_mod.verify_api_key)
        assert "hmac.compare_digest" in source
        assert "!=" not in source or "not key" in source

    def test_empty_key_rejected(self, authed_client):
        resp = authed_client.get("/api/health", headers={"X-API-Key": ""})
        assert resp.status_code == 401

    def test_none_key_rejected(self, authed_client):
        resp = authed_client.get("/api/health")
        assert resp.status_code == 401


# =============================================================================
# Per-user derived API keys
# =============================================================================


@pytest.fixture
def authed_multi_user_client(tmp_path):
    """Multi-user client with API key configured."""
    import os

    bob_dir = tmp_path / "bob"
    bob_dir.mkdir()
    (bob_dir / "ledgers").mkdir()
    (bob_dir / "ledgers" / "business.beancount").write_text("")

    alice_dir = tmp_path / "alice"
    alice_dir.mkdir()
    (alice_dir / "ledgers").mkdir()
    (alice_dir / "ledgers" / "personal.beancount").write_text("")

    config = tmp_path / "config.toml"
    config.write_text(
        f'[users.bob]\n'
        f'data_dir = "{bob_dir}"\n\n'
        f'[[users.bob.ledgers]]\n'
        f'name = "business"\n'
        f'path = "{bob_dir / "ledgers" / "business.beancount"}"\n\n'
        f'[users.alice]\n'
        f'data_dir = "{alice_dir}"\n\n'
        f'[[users.alice.ledgers]]\n'
        f'name = "personal"\n'
        f'path = "{alice_dir / "ledgers" / "personal.beancount"}"\n'
    )

    os.environ["MONEYMAN_CONFIG"] = str(config)
    os.environ["MONEYMAN_API_KEY"] = "master-secret"

    from money.api.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c

    os.environ.pop("MONEYMAN_CONFIG", None)
    os.environ.pop("MONEYMAN_API_KEY", None)


class TestDerivedKeys:
    def test_derive_user_key_deterministic(self):
        k1 = derive_user_key("master", "bob")
        k2 = derive_user_key("master", "bob")
        assert k1 == k2

    def test_derive_user_key_differs_per_user(self):
        k1 = derive_user_key("master", "bob")
        k2 = derive_user_key("master", "alice")
        assert k1 != k2

    def test_derived_key_authenticates_as_user(self, authed_multi_user_client):
        key = derive_user_key("master-secret", "bob")
        resp = authed_multi_user_client.get("/api/ledgers", headers={
            "X-API-Key": key,
            "X-User": "bob",
        })
        assert resp.status_code == 200
        names = [l["name"] for l in resp.json()["ledgers"]]
        assert "business" in names

    def test_derived_key_scoped_to_user(self, authed_multi_user_client):
        """Derived key for bob cannot access alice's data."""
        bob_key = derive_user_key("master-secret", "bob")
        # Try to use bob's key with alice's X-User
        resp = authed_multi_user_client.get("/api/ledgers", headers={
            "X-API-Key": bob_key,
            "X-User": "alice",
        })
        assert resp.status_code == 401

    def test_derived_key_without_x_user_rejected(self, authed_multi_user_client):
        """Derived key alone (no X-User) doesn't match master, so rejected."""
        key = derive_user_key("master-secret", "bob")
        resp = authed_multi_user_client.get("/api/ledgers", headers={
            "X-API-Key": key,
        })
        assert resp.status_code == 401

    def test_master_key_still_works(self, authed_multi_user_client):
        """Master key with X-User continues to work (admin access)."""
        resp = authed_multi_user_client.get("/api/ledgers", headers={
            "X-API-Key": "master-secret",
            "X-User": "bob",
        })
        assert resp.status_code == 200

    def test_master_key_can_impersonate_any_user(self, authed_multi_user_client):
        """Master key can access both bob and alice."""
        resp_s = authed_multi_user_client.get("/api/ledgers", headers={
            "X-API-Key": "master-secret",
            "X-User": "bob",
        })
        resp_a = authed_multi_user_client.get("/api/ledgers", headers={
            "X-API-Key": "master-secret",
            "X-User": "alice",
        })
        assert resp_s.status_code == 200
        assert resp_a.status_code == 200
        assert resp_s.json()["ledgers"][0]["name"] == "business"
        assert resp_a.json()["ledgers"][0]["name"] == "personal"

    def test_wrong_master_key_rejected(self, authed_multi_user_client):
        resp = authed_multi_user_client.get("/api/ledgers", headers={
            "X-API-Key": "wrong-master",
            "X-User": "bob",
        })
        assert resp.status_code == 401


# =============================================================================
# Rate limiting
# =============================================================================


class TestRateLimiting:
    def test_rate_limit_after_many_failures(self, authed_client):
        from money.api.auth import _failure_log
        _failure_log.clear()

        for _ in range(10):
            resp = authed_client.get("/api/health", headers={"X-API-Key": "wrong"})
            assert resp.status_code == 401

        # 11th attempt should be rate limited
        resp = authed_client.get("/api/health", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 429

    def test_correct_key_not_rate_limited(self, authed_client):
        """Correct key works even after failures from other attempts."""
        from money.api.auth import _failure_log
        _failure_log.clear()

        resp = authed_client.get("/api/health", headers={"X-API-Key": "test-secret-key"})
        assert resp.status_code == 200


# =============================================================================
# Path traversal (CSV import)
# =============================================================================


class TestCsvPathTraversal:
    def test_rejects_absolute_path_outside_data_dir(self, client):
        resp = client.post("/api/transactions/import-csv", json={
            "file": "/etc/passwd",
            "account": "Assets:Bank",
        })
        assert resp.status_code == 400
        assert "data directory" in resp.json()["detail"]

    def test_rejects_relative_traversal(self, client):
        resp = client.post("/api/transactions/import-csv", json={
            "file": "../../etc/passwd",
            "account": "Assets:Bank",
        })
        assert resp.status_code == 400
        assert "data directory" in resp.json()["detail"]

    def test_accepts_file_in_data_dir(self, client, tmp_path):
        csv_file = tmp_path / "import.csv"
        csv_file.write_text("date,amount\n2025-01-01,100\n")
        with patch("money.core.transactions.import_csv", return_value={"status": "ok"}):
            resp = client.post("/api/transactions/import-csv", json={
                "file": str(csv_file),
                "account": "Assets:Bank",
            })
            assert resp.status_code == 200


# =============================================================================
# BQL injection
# =============================================================================


class TestBqlInjection:
    def test_balances_single_quote_escaped(self):
        from money.core.ledger import _sanitize_bql_string
        assert _sanitize_bql_string("Assets'") == "Assets''"
        assert _sanitize_bql_string("x' OR '1'='1") == "x'' OR ''1''=''1"

    def test_balances_with_injection_attempt(self, client):
        with patch("money.core.ledger.run_bean_query", return_value=[]) as mock:
            resp = client.get("/api/balances", params={
                "account": "x' GROUP BY 1; --"
            })
            assert resp.status_code == 200
            # Verify the query was called with escaped value
            query_arg = mock.call_args[0][1]
            assert "'x'' GROUP BY 1; --'" in query_arg

    def test_lots_with_injection_attempt(self, client):
        with patch("money.core.ledger.run_bean_query", return_value=[]) as mock:
            resp = client.get("/api/lots/A'B")
            assert resp.status_code == 200
            query_arg = mock.call_args[0][1]
            assert "A''B" in query_arg


# =============================================================================
# Cross-user data isolation
# =============================================================================


class TestCrossUserIsolation:
    def test_bob_cannot_see_alice_ledgers(self, multi_user_client):
        resp = multi_user_client.get("/api/ledgers", headers={"X-User": "bob"})
        ledger_names = [l["name"] for l in resp.json()["ledgers"]]
        assert "business" in ledger_names
        assert "personal" not in ledger_names

    def test_alice_cannot_see_bob_ledgers(self, multi_user_client):
        resp = multi_user_client.get("/api/ledgers", headers={"X-User": "alice"})
        ledger_names = [l["name"] for l in resp.json()["ledgers"]]
        assert "personal" in ledger_names
        assert "business" not in ledger_names

    def test_work_entries_isolated(self, multi_user_client):
        multi_user_client.post("/api/work", json={
            "date": "2026-01-15", "client": "acme", "service": "dev", "qty": 8.0,
        }, headers={"X-User": "bob"})

        resp = multi_user_client.get("/api/work", headers={"X-User": "alice"})
        assert resp.json()["count"] == 0

    def test_unknown_user_rejected(self, multi_user_client):
        resp = multi_user_client.get("/api/ledgers", headers={"X-User": "mallory"})
        assert resp.status_code == 400


# =============================================================================
# No filesystem paths in responses
# =============================================================================


class TestNoPathLeakage:
    def test_ledger_list_no_paths(self, client):
        resp = client.get("/api/ledgers")
        data = resp.json()
        for ledger in data["ledgers"]:
            assert "path" not in ledger

    def test_check_not_found_no_path(self, client, tmp_path):
        """Error for missing ledger should not leak the filesystem path."""
        from money.core.ledger import check
        result = check(tmp_path / "nonexistent.beancount")
        assert result["status"] == "error"
        assert "/" not in result["error"]
