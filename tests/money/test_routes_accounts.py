"""Tests for the /money/accounts route — particularly the
freshly-seeded-ledger case where ``open`` directives exist but no
postings, so the BQL aggregation alone returns zero rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.money.cli import UserContext
from istota.money.routes import get_user_config, require_auth, router


def _seed_empty_ledger(path: Path) -> None:
    path.write_text(
        '2020-01-01 open Assets:Bank:Checking USD\n'
        '2020-01-01 open Expenses:Food USD\n'
        '2020-01-01 open Income:Salary USD\n'
        '2020-01-01 open Equity:Opening-Balances USD\n'
    )


def _seed_ledger_with_one_posting(path: Path) -> None:
    path.write_text(
        '2020-01-01 open Assets:Bank:Checking USD\n'
        '2020-01-01 open Expenses:Food USD\n'
        '2020-01-01 open Income:Salary USD\n'
        '2020-01-01 open Equity:Opening-Balances USD\n'
        '\n'
        '2024-06-01 * "Lunch"\n'
        '  Expenses:Food         12.50 USD\n'
        '  Assets:Bank:Checking\n'
    )


@pytest.fixture
def make_client(tmp_path: Path):
    def _factory(seed_fn) -> TestClient:
        ledger_path = tmp_path / "main.beancount"
        seed_fn(ledger_path)
        ctx = UserContext(
            data_dir=tmp_path,
            ledgers=[{"name": "main", "path": ledger_path}],
            db_path=tmp_path / "money.db",
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/money")
        app.dependency_overrides[require_auth] = lambda: {"username": "stefan"}
        app.dependency_overrides[get_user_config] = lambda: ctx
        return TestClient(app)
    return _factory


class TestAccountsRoute:
    def test_freshly_seeded_ledger_returns_opened_accounts(self, make_client):
        client = make_client(_seed_empty_ledger)
        resp = client.get("/api/money/accounts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        names = [r["account"] for r in data["accounts"]]
        assert "Assets:Bank:Checking" in names
        assert "Expenses:Food" in names
        assert "Income:Salary" in names
        assert "Equity:Opening-Balances" in names
        for row in data["accounts"]:
            assert row["sum(position)"] == ""

    def test_ledger_with_postings_keeps_balances_and_adds_unused(self, make_client):
        client = make_client(_seed_ledger_with_one_posting)
        resp = client.get("/api/money/accounts")
        assert resp.status_code == 200
        rows = {r["account"]: r["sum(position)"] for r in resp.json()["accounts"]}
        assert "Assets:Bank:Checking" in rows
        assert "Expenses:Food" in rows
        assert rows["Expenses:Food"]
        assert "Income:Salary" in rows
        assert rows["Income:Salary"] == ""

    def test_year_filter_does_not_inject_unposted_accounts(self, make_client):
        client = make_client(_seed_ledger_with_one_posting)
        resp = client.get("/api/money/accounts?year=2024")
        assert resp.status_code == 200
        rows = {r["account"]: r["sum(position)"] for r in resp.json()["accounts"]}
        assert "Expenses:Food" in rows
        assert "Income:Salary" not in rows

    def test_results_sorted_by_account(self, make_client):
        client = make_client(_seed_empty_ledger)
        resp = client.get("/api/money/accounts")
        names = [r["account"] for r in resp.json()["accounts"]]
        assert names == sorted(names)
