"""Tests for money.api REST API."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def tmp_ledger(tmp_path):
    """Create a minimal valid beancount ledger file."""
    ledger_dir = tmp_path / "ledgers"
    ledger_dir.mkdir()
    ledger = ledger_dir / "main.beancount"
    ledger.write_text("")
    return ledger


@pytest.fixture
def tmp_config(tmp_path, tmp_ledger):
    """Create a minimal config.toml with ledger in tmp_path."""
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{tmp_path}"\n\n'
        f'[[ledgers]]\nname = "main"\npath = "{tmp_ledger}"\n'
    )
    return config


@pytest.fixture
def client(tmp_config):
    """TestClient with no API key configured."""
    import os
    os.environ["MONEY_CONFIG"] = str(tmp_config)
    os.environ.pop("MONEY_API_KEY", None)

    from istota.money.api.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c

    os.environ.pop("MONEY_CONFIG", None)


@pytest.fixture
def authed_client(tmp_config):
    """TestClient with API key configured on server."""
    import os
    os.environ["MONEY_CONFIG"] = str(tmp_config)
    os.environ["MONEY_API_KEY"] = "test-secret-key"

    from istota.money.api.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c

    os.environ.pop("MONEY_CONFIG", None)
    os.environ.pop("MONEY_API_KEY", None)


# =============================================================================
# Health
# =============================================================================


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# =============================================================================
# Auth
# =============================================================================


class TestAuth:
    def test_no_key_configured_allows_all(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_missing_key_returns_401(self, authed_client):
        resp = authed_client.get("/api/health")
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, authed_client):
        resp = authed_client.get("/api/health", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_correct_key_returns_200(self, authed_client):
        resp = authed_client.get("/api/health", headers={"X-API-Key": "test-secret-key"})
        assert resp.status_code == 200


# =============================================================================
# Ledger endpoints
# =============================================================================


class TestLedgerEndpoints:
    def test_list_ledgers(self, client):
        resp = client.get("/api/ledgers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["ledger_count"] == 1
        assert data["ledgers"][0]["name"] == "main"

    def test_check_ledger(self, client):
        with patch("istota.money.core.ledger.run_bean_check", return_value=(True, [])):
            resp = client.get("/api/check")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"

    def test_check_ledger_with_name(self, client):
        with patch("istota.money.core.ledger.run_bean_check", return_value=(True, [])):
            resp = client.get("/api/check", params={"ledger": "main"})
            assert resp.status_code == 200

    def test_check_ledger_not_found(self, client):
        resp = client.get("/api/check", params={"ledger": "nonexistent"})
        assert resp.status_code == 400

    def test_balances(self, client):
        with patch("istota.money.core.ledger.run_bean_query", return_value=[]):
            resp = client.get("/api/balances")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"

    def test_balances_with_account(self, client):
        with patch("istota.money.core.ledger.run_bean_query", return_value=[
            {"account": "Assets:Bank", "sum_position": "1000 USD"}
        ]):
            resp = client.get("/api/balances", params={"account": "Assets:Bank"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["account_count"] == 1

    def test_query(self, client):
        with patch("istota.money.core.ledger.run_bean_query", return_value=[
            {"account": "Assets:Bank", "amount": "500 USD"}
        ]):
            resp = client.post("/api/query", json={"bql": "SELECT account, sum(position)"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["row_count"] == 1

    def test_query_missing_bql(self, client):
        resp = client.post("/api/query", json={})
        assert resp.status_code == 422

    def test_report(self, client):
        with patch("istota.money.core.ledger.run_bean_query", return_value=[]):
            resp = client.get("/api/reports/income-statement")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["report_type"] == "income-statement"

    def test_report_with_year(self, client):
        with patch("istota.money.core.ledger.run_bean_query", return_value=[]):
            resp = client.get("/api/reports/income-statement", params={"year": 2025})
            assert resp.status_code == 200
            data = resp.json()
            assert data["year"] == 2025

    def test_report_invalid_type(self, client):
        resp = client.get("/api/reports/invalid-type")
        assert resp.status_code == 400

    def test_lots(self, client):
        with patch("istota.money.core.ledger.run_bean_query", return_value=[]):
            resp = client.get("/api/lots/AAPL")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["symbol"] == "AAPL"

    def test_wash_sales(self, client):
        with patch("istota.money.core.ledger.run_bean_query", return_value=[]):
            resp = client.get("/api/wash-sales")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"

    def test_wash_sales_with_year(self, client):
        with patch("istota.money.core.ledger.run_bean_query", return_value=[]):
            resp = client.get("/api/wash-sales", params={"year": 2025})
            assert resp.status_code == 200
            data = resp.json()
            assert data["year"] == 2025


# =============================================================================
# Work endpoints
# =============================================================================


class TestWorkEndpoints:
    def test_list_empty(self, client):
        resp = client.get("/api/work")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["count"] == 0
        assert data["entries"] == []

    def test_add_entry(self, client):
        resp = client.post("/api/work", json={
            "date": "2025-03-15",
            "client": "acme",
            "service": "consulting",
            "qty": 8.0,
            "description": "Did some work",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["id"] == 1

    def test_add_and_list(self, client):
        client.post("/api/work", json={
            "date": "2025-03-15",
            "client": "acme",
            "service": "dev",
            "qty": 4.0,
        })
        resp = client.get("/api/work")
        data = resp.json()
        assert data["count"] == 1
        assert data["entries"][0]["client"] == "acme"

    def test_add_invalid_date(self, client):
        resp = client.post("/api/work", json={
            "date": "not-a-date",
            "client": "acme",
            "service": "dev",
        })
        assert resp.status_code == 400

    def test_update_entry(self, client):
        client.post("/api/work", json={
            "date": "2025-03-15",
            "client": "acme",
            "service": "dev",
            "qty": 4.0,
        })
        resp = client.put("/api/work/1", json={"qty": 8.0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_update_nonexistent(self, client):
        resp = client.put("/api/work/999", json={"qty": 8.0})
        assert resp.status_code == 404

    def test_update_no_fields(self, client):
        client.post("/api/work", json={
            "date": "2025-03-15",
            "client": "acme",
            "service": "dev",
        })
        resp = client.put("/api/work/1", json={})
        assert resp.status_code == 400

    def test_delete_entry(self, client):
        client.post("/api/work", json={
            "date": "2025-03-15",
            "client": "acme",
            "service": "dev",
            "qty": 4.0,
        })
        resp = client.delete("/api/work/1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

        # Confirm deleted
        resp = client.get("/api/work")
        assert resp.json()["count"] == 0

    def test_delete_nonexistent(self, client):
        resp = client.delete("/api/work/999")
        assert resp.status_code == 404

    def test_list_filter_client(self, client):
        client.post("/api/work", json={
            "date": "2025-03-15", "client": "acme", "service": "dev", "qty": 4.0,
        })
        client.post("/api/work", json={
            "date": "2025-03-16", "client": "other", "service": "dev", "qty": 2.0,
        })
        resp = client.get("/api/work", params={"client": "acme"})
        data = resp.json()
        assert data["count"] == 1
        assert data["entries"][0]["client"] == "acme"

    def test_list_filter_period(self, client):
        client.post("/api/work", json={
            "date": "2025-03-15", "client": "acme", "service": "dev", "qty": 4.0,
        })
        client.post("/api/work", json={
            "date": "2025-04-15", "client": "acme", "service": "dev", "qty": 2.0,
        })
        resp = client.get("/api/work", params={"period": "2025-03"})
        data = resp.json()
        assert data["count"] == 1


# =============================================================================
# Transaction endpoints
# =============================================================================


class TestTransactionEndpoints:
    def test_add_transaction(self, client):
        with patch("istota.money.core.ledger.run_bean_check", return_value=(True, [])):
            resp = client.post("/api/transactions", json={
                "date": "2025-03-15",
                "payee": "Coffee Shop",
                "narration": "Morning coffee",
                "debit": "Expenses:Food:Coffee",
                "credit": "Assets:Bank:Checking",
                "amount": 5.50,
                "currency": "USD",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["payee"] == "Coffee Shop"
            assert data["amount"] == 5.5

    def test_add_transaction_invalid_date(self, client):
        resp = client.post("/api/transactions", json={
            "date": "not-a-date",
            "payee": "Test",
            "narration": "Test",
            "debit": "Expenses:Test",
            "credit": "Assets:Test",
            "amount": 10.0,
        })
        assert resp.status_code == 400

    def test_add_transaction_missing_fields(self, client):
        resp = client.post("/api/transactions", json={
            "date": "2025-03-15",
            "payee": "Test",
        })
        assert resp.status_code == 422


# =============================================================================
# Invoice endpoints
# =============================================================================


class TestInvoiceEndpoints:
    def test_list_invoices_no_config(self, client):
        resp = client.get("/api/invoices")
        assert resp.status_code == 400

    def test_generate_no_config(self, client):
        resp = client.post("/api/invoices/generate", json={})
        assert resp.status_code == 400

    def test_void_invoice_not_found(self, client):
        resp = client.post("/api/invoices/void", json={"invoice_number": "INV-999999"})
        assert resp.status_code == 404

    def test_void_invoice_success(self, client):
        # Create a work entry and assign an invoice
        client.post("/api/work", json={
            "date": "2025-03-15", "client": "acme", "service": "dev", "qty": 8.0,
        })
        from istota.money.work import assign_invoice_number, get_entries_for_invoice, load_work_entries
        # Get data_dir from the app state
        data_dir = client.app.state.ctx.data_dir
        assign_invoice_number(data_dir, [1], "INV-000001")

        resp = client.post("/api/invoices/void", json={"invoice_number": "INV-000001"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["entries_voided"] == 1
        assert data["was_paid"] is False

        # Verify entry is now uninvoiced
        entries = load_work_entries(data_dir)
        assert entries[0].invoice == ""

    def test_void_paid_invoice_blocked(self, client):
        client.post("/api/work", json={
            "date": "2025-03-15", "client": "acme", "service": "dev", "qty": 8.0,
        })
        data_dir = client.app.state.ctx.data_dir
        from istota.money.work import assign_invoice_number, record_invoice_payment
        assign_invoice_number(data_dir, [1], "INV-000001")
        record_invoice_payment(data_dir, "INV-000001", "2025-04-15")

        resp = client.post("/api/invoices/void", json={"invoice_number": "INV-000001"})
        assert resp.status_code == 400
        assert "paid" in resp.json()["detail"].lower()

    def test_void_paid_invoice_with_force(self, client):
        client.post("/api/work", json={
            "date": "2025-03-15", "client": "acme", "service": "dev", "qty": 8.0,
        })
        data_dir = client.app.state.ctx.data_dir
        from istota.money.work import assign_invoice_number, record_invoice_payment
        assign_invoice_number(data_dir, [1], "INV-000001")
        record_invoice_payment(data_dir, "INV-000001", "2025-04-15")

        resp = client.post("/api/invoices/void", json={
            "invoice_number": "INV-000001",
            "force": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["entries_voided"] == 1
        assert data["was_paid"] is True


# =============================================================================
# load_context
# =============================================================================


class TestLoadContext:
    def test_load_context_returns_context(self, tmp_config):
        import os
        os.environ["MONEY_CONFIG"] = str(tmp_config)
        try:
            from istota.money.cli import load_context
            ctx = load_context()
            assert ctx.data_dir is not None
            assert len(ctx.ledgers) == 1
            assert ctx.ledgers[0]["name"] == "main"
        finally:
            os.environ.pop("MONEY_CONFIG", None)

    def test_load_context_with_path(self, tmp_config):
        from istota.money.cli import load_context
        ctx = load_context(config_path=str(tmp_config))
        assert ctx.data_dir is not None

    def test_load_context_no_config(self, tmp_path):
        import os
        os.environ.pop("MONEY_CONFIG", None)
        # Change to a dir with no config.toml
        from istota.money.cli import load_context
        ctx = load_context(config_path=None)
        # Should return empty context (no ledgers, etc.)
        assert ctx.ledgers == []

    def test_api_key_from_env(self, tmp_config):
        import os
        os.environ["MONEY_CONFIG"] = str(tmp_config)
        os.environ["MONEY_API_KEY"] = "env-key-123"
        try:
            from istota.money.cli import load_context
            ctx = load_context()
            assert ctx.api_key == "env-key-123"
        finally:
            os.environ.pop("MONEY_CONFIG", None)
            os.environ.pop("MONEY_API_KEY", None)

    def test_api_key_from_secrets(self, tmp_path, tmp_ledger):
        secrets = tmp_path / "secrets.toml"
        secrets.write_text('[api]\napi_key = "secret-key-456"\n')
        config = tmp_path / "config.toml"
        config.write_text(
            f'data_dir = "{tmp_path}"\n'
            f'secrets_file = "{secrets}"\n\n'
            f'[[ledgers]]\nname = "main"\npath = "{tmp_ledger}"\n'
        )
        import os
        os.environ.pop("MONEY_API_KEY", None)
        from istota.money.cli import load_context
        ctx = load_context(config_path=str(config))
        assert ctx.api_key == "secret-key-456"

    def test_api_key_env_overrides_secrets(self, tmp_path, tmp_ledger):
        secrets = tmp_path / "secrets.toml"
        secrets.write_text('[api]\napi_key = "secret-key"\n')
        config = tmp_path / "config.toml"
        config.write_text(
            f'data_dir = "{tmp_path}"\n'
            f'secrets_file = "{secrets}"\n\n'
            f'[[ledgers]]\nname = "main"\npath = "{tmp_ledger}"\n'
        )
        import os
        os.environ["MONEY_API_KEY"] = "env-key"
        try:
            from istota.money.cli import load_context
            ctx = load_context(config_path=str(config))
            assert ctx.api_key == "env-key"
        finally:
            os.environ.pop("MONEY_API_KEY", None)



# =============================================================================
# resolve_ledger (renamed from _resolve_ledger)
# =============================================================================


class TestResolveLedger:
    def test_resolve_ledger_default(self, tmp_path):
        from istota.money.cli import resolve_ledger
        ledgers = [{"name": "main", "path": tmp_path / "main.beancount"}]
        assert resolve_ledger(None, ledgers) == tmp_path / "main.beancount"

    def test_resolve_ledger_by_name(self, tmp_path):
        from istota.money.cli import resolve_ledger
        ledgers = [
            {"name": "main", "path": tmp_path / "main.beancount"},
            {"name": "secondary", "path": tmp_path / "secondary.beancount"},
        ]
        assert resolve_ledger("secondary", ledgers) == tmp_path / "secondary.beancount"

    def test_resolve_ledger_not_found(self):
        from istota.money.cli import resolve_ledger
        import click
        with pytest.raises(click.ClickException):
            resolve_ledger("nope", [{"name": "main", "path": Path("/x")}])

    def test_resolve_ledger_no_ledgers(self):
        from istota.money.cli import resolve_ledger
        import click
        with pytest.raises(click.ClickException):
            resolve_ledger(None, [])


# =============================================================================
# Multi-user API
# =============================================================================


@pytest.fixture
def multi_user_config(tmp_path):
    """Create a multi-user config.toml with two users."""
    bob_dir = tmp_path / "bob"
    bob_dir.mkdir()
    bob_ledger_dir = bob_dir / "ledgers"
    bob_ledger_dir.mkdir()
    bob_ledger = bob_ledger_dir / "business.beancount"
    bob_ledger.write_text("")

    alice_dir = tmp_path / "alice"
    alice_dir.mkdir()
    alice_ledger_dir = alice_dir / "ledgers"
    alice_ledger_dir.mkdir()
    alice_ledger = alice_ledger_dir / "personal.beancount"
    alice_ledger.write_text("")

    config = tmp_path / "config.toml"
    config.write_text(
        f'[users.bob]\n'
        f'data_dir = "{bob_dir}"\n\n'
        f'[[users.bob.ledgers]]\n'
        f'name = "business"\n'
        f'path = "{bob_ledger}"\n\n'
        f'[users.alice]\n'
        f'data_dir = "{alice_dir}"\n\n'
        f'[[users.alice.ledgers]]\n'
        f'name = "personal"\n'
        f'path = "{alice_ledger}"\n'
    )
    return config


@pytest.fixture
def multi_user_client(multi_user_config):
    """TestClient with multi-user config, no API key."""
    import os
    os.environ["MONEY_CONFIG"] = str(multi_user_config)
    os.environ.pop("MONEY_API_KEY", None)

    from istota.money.api.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c

    os.environ.pop("MONEY_CONFIG", None)


class TestMultiUserApi:
    def test_x_user_header_routes_to_correct_user(self, multi_user_client):
        resp = multi_user_client.get("/api/ledgers", headers={"X-User": "bob"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ledger_count"] == 1
        assert data["ledgers"][0]["name"] == "business"

    def test_x_user_header_alice(self, multi_user_client):
        resp = multi_user_client.get("/api/ledgers", headers={"X-User": "alice"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ledgers"][0]["name"] == "personal"

    def test_missing_header_multi_user_returns_400(self, multi_user_client):
        resp = multi_user_client.get("/api/ledgers")
        assert resp.status_code == 400
        assert "X-User" in resp.json()["detail"]

    def test_unknown_user_returns_400(self, multi_user_client):
        resp = multi_user_client.get("/api/ledgers", headers={"X-User": "nobody"})
        assert resp.status_code == 400
        assert "Unknown user" in resp.json()["detail"]

    def test_single_user_auto_selects(self, client):
        """Single-user config works without X-User header."""
        resp = client.get("/api/ledgers")
        assert resp.status_code == 200
        assert resp.json()["ledger_count"] == 1

    def test_work_isolated_per_user(self, multi_user_client):
        """Work entries are isolated between users."""
        # Add work for bob
        resp = multi_user_client.post("/api/work", json={
            "date": "2026-03-15", "client": "acme", "service": "dev", "qty": 8.0,
        }, headers={"X-User": "bob"})
        assert resp.status_code == 200

        # Bob sees it
        resp = multi_user_client.get("/api/work", headers={"X-User": "bob"})
        assert resp.json()["count"] == 1

        # Alice does not
        resp = multi_user_client.get("/api/work", headers={"X-User": "alice"})
        assert resp.json()["count"] == 0

    def test_health_works_without_user_header(self, multi_user_client):
        """Health endpoint doesn't need user context."""
        resp = multi_user_client.get("/api/health")
        assert resp.status_code == 200


