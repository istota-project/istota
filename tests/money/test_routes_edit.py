"""Tests for the transaction edit route: POST /transactions/update."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.money.cli import UserContext
from istota.money.routes import get_user_config, require_auth, router, verify_origin


def _seed_ledger(tmp_path: Path) -> Path:
    ledgers_dir = tmp_path / "ledgers"
    ledgers_dir.mkdir(parents=True, exist_ok=True)
    ledger = ledgers_dir / "main.beancount"
    ledger.write_text(
        "2024-01-01 open Assets:Bank:Checking\n"
        "2024-01-01 open Expenses:Food:Coffee\n"
        "2024-01-01 open Expenses:Food:Restaurants\n"
        "2024-01-01 open Income:Consulting\n\n"
        '2024-02-01 * "Acme" "Coffee"\n'
        '  id: "txn-coffee"\n'
        "  Expenses:Food:Coffee   5.00 USD\n"
        "  Assets:Bank:Checking\n\n"
        '2024-02-02 * "Client" "Payment"\n'
        '  id: "txn-pay"\n'
        "  Assets:Bank:Checking   100.00 USD\n"
        "  Income:Consulting     -100.00 USD\n"
    )
    return ledger


@pytest.fixture
def client(tmp_path: Path):
    ledger = _seed_ledger(tmp_path)
    ctx = UserContext(
        data_dir=tmp_path,
        ledgers=[{"name": "main", "path": ledger}],
        db_path=tmp_path / "money.db",
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/money")
    app.dependency_overrides[require_auth] = lambda: {"username": "stefan"}
    app.dependency_overrides[get_user_config] = lambda: ctx
    app.dependency_overrides[verify_origin] = lambda: None
    c = TestClient(app)
    c._ledger = ledger  # type: ignore[attr-defined]
    return c


class TestTransactionUpdate:
    def test_recategorize(self, client):
        resp = client.post("/api/money/transactions/update", json={
            "id": "txn-coffee",
            "old_account": "Expenses:Food:Coffee",
            "old_position": "5.00 USD",
            "new_account": "Expenses:Food:Restaurants",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"
        assert "Expenses:Food:Restaurants" in client._ledger.read_text()

    def test_transactions_list_includes_id(self, client):
        resp = client.get("/api/money/transactions")
        assert resp.status_code == 200
        rows = resp.json()["transactions"]
        assert rows
        assert all("id" in r for r in rows)
        assert any(r["id"] == "txn-coffee" for r in rows)

    def test_missing_id_is_400(self, client):
        resp = client.post("/api/money/transactions/update", json={
            "new_account": "Expenses:Food:Restaurants",
        })
        assert resp.status_code == 400

    def test_not_found_is_404(self, client):
        resp = client.post("/api/money/transactions/update", json={
            "id": "nope", "new_account": "Expenses:Food:Restaurants",
        })
        assert resp.status_code == 404
        assert resp.json()["status"] == "error"

    def test_unbalanced_edit_rolls_back_422(self, client):
        before = client._ledger.read_text()
        resp = client.post("/api/money/transactions/update", json={
            "id": "txn-pay",
            "old_account": "Assets:Bank:Checking",
            "old_position": "100.00 USD",
            "new_position": "999.00 USD",
        })
        assert resp.status_code == 422
        body = resp.json()
        assert body["status"] == "error"
        assert body.get("validation_errors")
        # Ledger restored.
        assert client._ledger.read_text() == before
