"""Tests for the new /money/config/* CRUD + import/export routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.money import config_store
from istota.money.cli import UserContext
from istota.money.routes import get_user_config, require_auth, router


@pytest.fixture
def ctx(tmp_path: Path) -> UserContext:
    data_dir = tmp_path / "money"
    db_path = data_dir / "data" / "money.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config_store.init_db(db_path)
    return UserContext(data_dir=data_dir, ledgers=[], db_path=db_path)


@pytest.fixture
def client(ctx: UserContext) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/istota/api/money")
    app.dependency_overrides[require_auth] = lambda: {"username": "stefan"}
    app.dependency_overrides[get_user_config] = lambda: ctx
    return TestClient(app)


# =============================================================================
# Invoicing — settings + collections
# =============================================================================


class TestInvoicingScalars:
    def test_get_defaults(self, client):
        resp = client.get("/istota/api/money/config/invoicing")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["settings"]["currency"] == "USD"

    def test_put_updates(self, ctx, client):
        resp = client.put(
            "/istota/api/money/config/invoicing",
            json={"next_invoice_number": 42, "currency": "EUR"},
        )
        assert resp.status_code == 200
        cfg = config_store.load_invoicing(ctx.db_path)
        assert cfg.next_invoice_number == 42
        assert cfg.currency == "EUR"

    def test_put_rejects_unknown(self, client):
        resp = client.put(
            "/istota/api/money/config/invoicing",
            json={"unknown_key": 1},
        )
        assert resp.status_code == 400


class TestCompanies:
    def test_create_list_delete(self, ctx, client):
        resp = client.post(
            "/istota/api/money/config/companies",
            json={"key": "ochotona", "name": "Ochotona LLC"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "created"

        resp = client.get("/istota/api/money/config/companies")
        assert resp.json()["companies"][0]["key"] == "ochotona"

        resp = client.put(
            "/istota/api/money/config/companies/ochotona",
            json={"address": "1 St"},
        )
        assert resp.json()["state"] == "updated"

        resp = client.delete("/istota/api/money/config/companies/ochotona")
        assert resp.json()["removed"] is True


class TestClients:
    def test_create_update(self, client):
        resp = client.post(
            "/istota/api/money/config/clients",
            json={"key": "acme", "name": "Acme Corp"},
        )
        assert resp.json()["state"] == "created"
        resp = client.put(
            "/istota/api/money/config/clients/acme",
            json={"terms": "NET 15"},
        )
        body = resp.json()
        assert body["state"] == "updated"
        assert body["client"]["terms"] == "NET 15"


class TestServices:
    def test_create(self, client):
        resp = client.post(
            "/istota/api/money/config/services",
            json={"key": "consulting", "display_name": "Consulting", "rate": 150},
        )
        assert resp.json()["state"] == "created"


# =============================================================================
# Tax
# =============================================================================


class TestTaxScalars:
    def test_put_get(self, ctx, client):
        resp = client.put(
            "/istota/api/money/config/tax",
            json={"tax_year": 2026, "w2_income": 90000},
        )
        assert resp.status_code == 200
        body = client.get("/istota/api/money/config/tax").json()
        assert body["tax"]["w2_income"] == 90000


class TestTaxYears:
    def test_upsert(self, ctx, client):
        resp = client.put(
            "/istota/api/money/config/tax/years/2026",
            json={"federal_standard_deduction": 30000, "ss_wage_base": 176100},
        )
        assert resp.json()["state"] == "created"
        years = client.get("/istota/api/money/config/tax/years").json()["years"]
        assert years[0]["tax_year"] == 2026

    def test_unknown_field_rejected(self, client):
        resp = client.put(
            "/istota/api/money/config/tax/years/2026",
            json={"unknown": 1},
        )
        assert resp.status_code == 400


class TestTaxPatterns:
    def test_replace_all(self, ctx, client):
        resp = client.put(
            "/istota/api/money/config/tax/patterns",
            json={"se_income": ["Income:Side"], "se_expense": ["Expenses:Biz"]},
        )
        assert resp.status_code == 200
        body = client.get("/istota/api/money/config/tax/patterns").json()
        assert body["patterns"]["se_income"] == ["Income:Side"]
        assert body["patterns"]["se_expense"] == ["Expenses:Biz"]


# =============================================================================
# Monarch
# =============================================================================


class TestMonarchProfiles:
    def test_create_then_account_map(self, ctx, client):
        resp = client.post(
            "/istota/api/money/config/monarch/profiles",
            json={"name": "cynium", "ledger": "cynium"},
        )
        assert resp.json()["state"] == "created"

        resp = client.put(
            "/istota/api/money/config/monarch/account-map?profile=cynium",
            json={"Cynium Visa": "Liabilities:Visa"},
        )
        assert resp.status_code == 200
        body = client.get(
            "/istota/api/money/config/monarch/account-map?profile=cynium",
        ).json()
        assert body["mapping"] == {"Cynium Visa": "Liabilities:Visa"}

    def test_create_without_ledger_400(self, client):
        resp = client.post(
            "/istota/api/money/config/monarch/profiles",
            json={"name": "x"},
        )
        assert resp.status_code == 400

    def test_global_scope(self, client):
        resp = client.put(
            "/istota/api/money/config/monarch/account-map?profile=global",
            json={"Bank": "Assets:Bank"},
        )
        assert resp.status_code == 200
        body = client.get(
            "/istota/api/money/config/monarch/account-map?profile=global",
        ).json()
        assert body["mapping"] == {"Bank": "Assets:Bank"}


class TestMonarchTagFilters:
    def test_replace(self, ctx, client):
        client.post(
            "/istota/api/money/config/monarch/profiles",
            json={"name": "cynium", "ledger": "cynium"},
        )
        resp = client.put(
            "/istota/api/money/config/monarch/tag-filters?profile=cynium",
            json={"include": ["Biz"], "exclude": ["Hide"]},
        )
        assert resp.status_code == 200
        body = client.get(
            "/istota/api/money/config/monarch/tag-filters?profile=cynium",
        ).json()
        assert body["tags"]["include"] == ["Biz"]
        assert body["tags"]["exclude"] == ["Hide"]


# =============================================================================
# Import / export
# =============================================================================


class TestExport:
    def test_section_invoicing(self, client):
        client.post(
            "/istota/api/money/config/clients",
            json={"key": "acme", "name": "Acme"},
        )
        resp = client.get("/istota/api/money/config/export?section=invoicing")
        assert resp.status_code == 200
        assert "[clients.acme]" in resp.text
        assert "Acme" in resp.text

    def test_combined(self, client):
        client.post(
            "/istota/api/money/config/clients",
            json={"key": "acme", "name": "Acme"},
        )
        client.put(
            "/istota/api/money/config/tax", json={"tax_year": 2026},
        )
        resp = client.get("/istota/api/money/config/export")
        assert resp.status_code == 200
        # The combined dump nests both [invoicing.*] and [tax].
        assert "tax_year" in resp.text


class TestImport:
    def test_dry_run(self, ctx, client):
        toml_text = '[clients.foo]\nname = "Foo"\n'
        resp = client.post(
            "/istota/api/money/config/import?section=invoicing&dry_run=1",
            json={"text": toml_text},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["sections"][0]["section"] == "invoicing"
        # Database not touched.
        cfg = config_store.load_invoicing(ctx.db_path)
        assert "foo" not in cfg.clients

    def test_apply(self, ctx, client):
        resp = client.post(
            "/istota/api/money/config/import?section=invoicing",
            json={"text": '[clients.foo]\nname = "Foo"\n'},
        )
        assert resp.status_code == 200
        cfg = config_store.load_invoicing(ctx.db_path)
        assert "foo" in cfg.clients

    def test_unparseable(self, client):
        resp = client.post(
            "/istota/api/money/config/import?section=invoicing",
            json={"text": "this is not toml = "},
        )
        assert resp.status_code == 400
