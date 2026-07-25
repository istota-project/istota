"""Tests for the work-entry routes: list, create, update, delete.

These operate on the file-based work-entry store (``data_dir``) plus a seeded
invoicing config (for service/client resolution), mirroring the shape of
``test_routes_invoices.py``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from istota.money.cli import UserContext
from istota.money.routes import get_user_config, require_auth, router, verify_origin
from istota.money.work import (
    add_work_entry,
    assign_invoice_number,
    entry_etag,
    load_work_entries,
    record_invoice_payment,
)


def _write_invoicing_config(data_dir: Path) -> Path:
    cfg = data_dir / "invoicing.toml"
    cfg.write_text(
        'accounting_path = "."\n'
        "next_invoice_number = 1\n"
        'invoice_output = "invoices/generated"\n\n'
        '[company]\nname = "My Co"\naddress = "123 Main"\n\n'
        '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
        '[clients.globex]\nname = "Globex"\nterms = 14\n\n'
        '[services.dev]\ndisplay_name = "Development"\nrate = 150\ntype = "hours"\n\n'
        '[services.retainer]\ndisplay_name = "Retainer"\nrate = 2000\ntype = "flat"\n'
    )
    return cfg


@pytest.fixture
def make_client(tmp_path: Path):
    def _factory(*, with_config: bool = True) -> TestClient:
        ctx = UserContext(
            data_dir=tmp_path,
            ledgers=[],
            db_path=tmp_path / "money.db",
            invoicing_config_path=_write_invoicing_config(tmp_path) if with_config else None,
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/money")
        app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
        app.dependency_overrides[get_user_config] = lambda: ctx
        app.dependency_overrides[verify_origin] = lambda: None
        return TestClient(app)

    return _factory


def _uid_of(data_dir: Path, description: str) -> str:
    for entry in load_work_entries(data_dir):
        if entry.description == description:
            return entry.uid
    raise AssertionError(f"no entry described {description!r}")


class TestListWork:
    def test_empty_store(self, make_client):
        resp = make_client().get("/api/money/work")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["entries"] == []
        assert data["totals"]["uninvoiced_count"] == 0

    def test_row_shape(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=3, description="API work")
        row = make_client().get("/api/money/work").json()["entries"][0]

        assert row["uid"]
        assert row["index"] == 1
        assert row["etag"]
        assert row["date"] == "2026-03-01"
        assert row["client"] == "acme"
        assert row["client_name"] == "Acme Corp"
        assert row["service"] == "dev"
        assert row["service_name"] == "Development"
        assert row["service_type"] == "hours"
        assert row["qty"] == 3
        assert row["computed_amount"] == 450.0
        assert row["invoice"] == ""
        assert row["paid_date"] is None
        assert row["editable"] is True
        assert row["warnings"] == []

    def test_computed_amount_honours_flat_service(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "retainer", qty=99)
        row = make_client().get("/api/money/work").json()["entries"][0]
        assert row["computed_amount"] == 2000.0

    def test_computed_amount_applies_discount(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=2, discount=50)
        row = make_client().get("/api/money/work").json()["entries"][0]
        assert row["computed_amount"] == 250.0

    def test_invoiced_entry_is_not_editable(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(tmp_path, [1], "INV-000001")
        row = make_client().get("/api/money/work").json()["entries"][0]
        assert row["invoice"] == "INV-000001"
        assert row["editable"] is False

    def test_paid_date_surfaced(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(tmp_path, [1], "INV-000001")
        record_invoice_payment(tmp_path, "INV-000001", "2026-04-15")
        row = make_client().get("/api/money/work").json()["entries"][0]
        assert row["paid_date"] == "2026-04-15"

    def test_unknown_service_warned_and_uncomputable(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "ghost", qty=3)
        row = make_client().get("/api/money/work").json()["entries"][0]
        assert "unknown_service" in row["warnings"]
        assert row["computed_amount"] is None
        assert row["service_name"] == "ghost"

    def test_unknown_client_warned_but_visible(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "nobody", "dev", qty=3)
        row = make_client().get("/api/money/work").json()["entries"][0]
        assert "unknown_client" in row["warnings"]
        assert row["client_name"] == "nobody"

    def test_entry_without_uid_is_not_editable(self, make_client, tmp_path):
        work_dir = tmp_path / "invoices" / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "2026.toml").write_text(
            '[[entries]]\ndate = 2026-03-01\nclient = "acme"\nservice = "dev"\nqty = 1\n'
        )
        row = make_client().get("/api/money/work").json()["entries"][0]
        assert row["uid"] == ""
        assert row["editable"] is False
        assert "no_uid" in row["warnings"]

    def test_filter_by_client(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1)
        add_work_entry(tmp_path, "2026-03-02", "globex", "dev", qty=1)
        data = make_client().get("/api/money/work?client=globex").json()
        assert [e["client"] for e in data["entries"]] == ["globex"]

    def test_filter_by_period_month(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1)
        add_work_entry(tmp_path, "2026-04-01", "acme", "dev", qty=1)
        data = make_client().get("/api/money/work?period=2026-03").json()
        assert [e["date"] for e in data["entries"]] == ["2026-03-01"]

    def test_filter_by_period_year(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2025-12-01", "acme", "dev", qty=1)
        add_work_entry(tmp_path, "2026-04-01", "acme", "dev", qty=1)
        data = make_client().get("/api/money/work?period=2026").json()
        assert [e["date"] for e in data["entries"]] == ["2026-04-01"]

    def test_status_filters(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="open")
        add_work_entry(tmp_path, "2026-03-02", "acme", "dev", qty=1, description="billed")
        add_work_entry(tmp_path, "2026-03-03", "acme", "dev", qty=1, description="settled")
        assign_invoice_number(tmp_path, [2], "INV-000001")
        assign_invoice_number(tmp_path, [3], "INV-000002")
        record_invoice_payment(tmp_path, "INV-000002", "2026-04-01")
        client = make_client()

        def descs(status):
            data = client.get(f"/api/money/work?status={status}").json()
            return [e["description"] for e in data["entries"]]

        assert descs("uninvoiced") == ["open"]
        assert descs("invoiced") == ["billed"]
        assert descs("paid") == ["settled"]
        assert sorted(descs("all")) == ["billed", "open", "settled"]

    def test_unknown_status_rejected(self, make_client, tmp_path):
        resp = make_client().get("/api/money/work?status=bogus")
        assert resp.status_code == 400

    def test_totals_ignore_status_filter(self, make_client, tmp_path):
        """The toolbar summary must stay meaningful while viewing one bucket."""
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=2)
        add_work_entry(tmp_path, "2026-03-02", "acme", "dev", qty=1)
        assign_invoice_number(tmp_path, [2], "INV-000001")

        data = make_client().get("/api/money/work?status=invoiced").json()
        assert data["totals"]["uninvoiced_count"] == 1
        assert data["totals"]["uninvoiced_amount"] == 300.0
        assert data["totals"]["invoiced_count"] == 1
        assert data["totals"]["paid_count"] == 0

    def test_totals_respect_client_filter(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=2)
        add_work_entry(tmp_path, "2026-03-02", "globex", "dev", qty=4)
        data = make_client().get("/api/money/work?client=acme").json()
        assert data["totals"]["uninvoiced_count"] == 1
        assert data["totals"]["uninvoiced_amount"] == 300.0

    def test_works_without_invoicing_config(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=3)
        row = make_client(with_config=False).get("/api/money/work").json()["entries"][0]
        assert row["client_name"] == "acme"
        assert row["computed_amount"] is None
        # No config to validate against — don't cry wolf about unknown keys.
        assert row["warnings"] == []


class TestCreateWork:
    def test_create(self, make_client, tmp_path):
        resp = make_client().post(
            "/api/money/work",
            json={
                "date": "2026-03-01",
                "client": "acme",
                "service": "dev",
                "qty": 4,
                "description": "Integration",
            },
        )
        assert resp.status_code == 200
        row = resp.json()["entry"]
        assert row["uid"]
        assert row["computed_amount"] == 600.0

        stored = load_work_entries(tmp_path)
        assert len(stored) == 1
        assert stored[0].uid == row["uid"]
        assert stored[0].description == "Integration"

    def test_create_lowercases_client(self, make_client, tmp_path):
        make_client().post(
            "/api/money/work",
            json={"date": "2026-03-01", "client": "ACME", "service": "dev", "qty": 1},
        )
        assert load_work_entries(tmp_path)[0].client == "acme"

    def test_create_finds_its_own_entry_among_siblings(self, make_client, tmp_path):
        """The created row is resolved by uid, not by the returned index."""
        add_work_entry(tmp_path, "2026-01-01", "acme", "dev", qty=1, description="older")
        resp = make_client().post(
            "/api/money/work",
            json={"date": "2026-03-01", "client": "acme", "service": "dev", "qty": 2},
        )
        row = resp.json()["entry"]
        assert row["index"] == 2
        assert row["qty"] == 2

    def test_create_rejects_bad_date(self, make_client):
        resp = make_client().post(
            "/api/money/work",
            json={"date": "01/03/2026", "client": "acme", "service": "dev", "qty": 1},
        )
        assert resp.status_code == 400
        assert "date" in resp.json()["error"].lower()

    def test_create_requires_client_and_service(self, make_client):
        resp = make_client().post("/api/money/work", json={"date": "2026-03-01"})
        assert resp.status_code == 400

    def test_create_rejects_unknown_service(self, make_client, tmp_path):
        """Silently unbillable work is the failure mode this exists to stop."""
        resp = make_client().post(
            "/api/money/work",
            json={"date": "2026-03-01", "client": "acme", "service": "ghost", "qty": 1},
        )
        assert resp.status_code == 400
        assert "service" in resp.json()["error"].lower()
        assert load_work_entries(tmp_path) == []

    def test_create_accepts_unknown_client_with_warning(self, make_client, tmp_path):
        resp = make_client().post(
            "/api/money/work",
            json={"date": "2026-03-01", "client": "nobody", "service": "dev", "qty": 1},
        )
        assert resp.status_code == 200
        assert "unknown_client" in resp.json()["entry"]["warnings"]
        assert len(load_work_entries(tmp_path)) == 1

    def test_create_without_config_skips_service_validation(self, make_client, tmp_path):
        resp = make_client(with_config=False).post(
            "/api/money/work",
            json={"date": "2026-03-01", "client": "acme", "service": "whatever", "qty": 1},
        )
        assert resp.status_code == 200
        assert len(load_work_entries(tmp_path)) == 1

    def test_create_ignores_unsettable_fields(self, make_client, tmp_path):
        resp = make_client().post(
            "/api/money/work",
            json={
                "date": "2026-03-01",
                "client": "acme",
                "service": "dev",
                "qty": 1,
                "uid": "forged",
                "invoice": "INV-000009",
                "paid_date": "2026-04-01",
            },
        )
        assert resp.status_code == 200
        stored = load_work_entries(tmp_path)[0]
        assert stored.uid != "forged"
        assert stored.invoice == ""
        assert stored.paid_date is None


class TestUpdateWork:
    def test_update(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="target")
        uid = _uid_of(tmp_path, "target")
        resp = make_client().patch(f"/api/money/work/{uid}", json={"qty": 7})
        assert resp.status_code == 200
        assert resp.json()["entry"]["qty"] == 7
        assert load_work_entries(tmp_path)[0].qty == 7

    def test_update_hits_right_entry_after_index_shift(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=1, description="target")
        uid = _uid_of(tmp_path, "target")
        add_work_entry(tmp_path, "2026-03-01", "globex", "dev", qty=5, description="intruder")

        make_client().patch(f"/api/money/work/{uid}", json={"qty": 42})

        by_desc = {e.description: e for e in load_work_entries(tmp_path)}
        assert by_desc["target"].qty == 42
        assert by_desc["intruder"].qty == 5

    def test_update_unknown_uid_404(self, make_client, tmp_path):
        resp = make_client().patch("/api/money/work/nope", json={"qty": 1})
        assert resp.status_code == 404

    def test_update_invoiced_409(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        uid = _uid_of(tmp_path, "t")
        assign_invoice_number(tmp_path, [1], "INV-000001")
        resp = make_client().patch(f"/api/money/work/{uid}", json={"qty": 9})
        assert resp.status_code == 409
        assert resp.json()["error"] == "entry is invoiced"

    def test_update_no_fields_400(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        uid = _uid_of(tmp_path, "t")
        resp = make_client().patch(f"/api/money/work/{uid}", json={})
        assert resp.status_code == 400

    def test_update_rejects_unknown_service(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        uid = _uid_of(tmp_path, "t")
        resp = make_client().patch(f"/api/money/work/{uid}", json={"service": "ghost"})
        assert resp.status_code == 400
        assert load_work_entries(tmp_path)[0].service == "dev"

    def test_update_can_clear_optional_fields(self, make_client, tmp_path):
        add_work_entry(
            tmp_path, "2026-03-01", "acme", "dev", qty=1, amount=99, description="t"
        )
        uid = _uid_of(tmp_path, "t")
        resp = make_client().patch(f"/api/money/work/{uid}", json={"amount": None})
        assert resp.status_code == 200
        assert load_work_entries(tmp_path)[0].amount is None

    def test_update_null_discount_reads_as_zero(self, make_client, tmp_path):
        """discount is not nullable — a null there would break every downstream
        amount computation."""
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=2, discount=50, description="t")
        uid = _uid_of(tmp_path, "t")
        resp = make_client().patch(f"/api/money/work/{uid}", json={"discount": None})
        assert resp.status_code == 200
        assert resp.json()["entry"]["computed_amount"] == 300.0
        assert load_work_entries(tmp_path)[0].discount == 0

    def test_update_rejects_non_numeric_qty(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        uid = _uid_of(tmp_path, "t")
        resp = make_client().patch(f"/api/money/work/{uid}", json={"qty": "lots"})
        assert resp.status_code == 400

    def test_update_accepts_numeric_string(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        uid = _uid_of(tmp_path, "t")
        resp = make_client().patch(f"/api/money/work/{uid}", json={"qty": "2.5"})
        assert resp.status_code == 200
        assert load_work_entries(tmp_path)[0].qty == 2.5

    def test_update_rejects_boolean_qty(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        uid = _uid_of(tmp_path, "t")
        resp = make_client().patch(f"/api/money/work/{uid}", json={"qty": True})
        assert resp.status_code == 400

    def test_update_moves_entry_across_years(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        uid = _uid_of(tmp_path, "t")
        resp = make_client().patch(f"/api/money/work/{uid}", json={"date": "2027-01-05"})
        assert resp.status_code == 200
        assert resp.json()["entry"]["date"] == "2027-01-05"
        assert (tmp_path / "invoices" / "work" / "2027.toml").exists()
        assert not (tmp_path / "invoices" / "work" / "2026.toml").exists()


class TestDeleteWork:
    def test_delete(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        uid = _uid_of(tmp_path, "t")
        resp = make_client().delete(f"/api/money/work/{uid}")
        assert resp.status_code == 200
        assert load_work_entries(tmp_path) == []

    def test_delete_unknown_uid_404(self, make_client):
        assert make_client().delete("/api/money/work/nope").status_code == 404

    def test_delete_invoiced_409(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        uid = _uid_of(tmp_path, "t")
        assign_invoice_number(tmp_path, [1], "INV-000001")
        resp = make_client().delete(f"/api/money/work/{uid}")
        assert resp.status_code == 409
        assert len(load_work_entries(tmp_path)) == 1


class TestEtagConcurrency:
    def test_update_with_current_etag_succeeds(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        entry = load_work_entries(tmp_path)[0]
        resp = make_client().patch(
            f"/api/money/work/{entry.uid}",
            json={"qty": 4, "etag": entry_etag(entry)},
        )
        assert resp.status_code == 200

    def test_update_with_stale_etag_409(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        entry = load_work_entries(tmp_path)[0]
        stale = entry_etag(entry)
        # Someone else (the agent) edits it first.
        make_client().patch(f"/api/money/work/{entry.uid}", json={"qty": 99})

        resp = make_client().patch(
            f"/api/money/work/{entry.uid}", json={"qty": 4, "etag": stale}
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "entry changed"
        # The conflict response carries the current server-side row.
        assert body["entry"]["qty"] == 99
        assert load_work_entries(tmp_path)[0].qty == 99

    def test_delete_with_stale_etag_409(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        entry = load_work_entries(tmp_path)[0]
        stale = entry_etag(entry)
        make_client().patch(f"/api/money/work/{entry.uid}", json={"qty": 99})

        resp = make_client().delete(f"/api/money/work/{entry.uid}?etag={stale}")
        assert resp.status_code == 409
        assert len(load_work_entries(tmp_path)) == 1

    def test_delete_with_current_etag_succeeds(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        entry = load_work_entries(tmp_path)[0]
        resp = make_client().delete(
            f"/api/money/work/{entry.uid}?etag={entry_etag(entry)}"
        )
        assert resp.status_code == 200
        assert load_work_entries(tmp_path) == []

    def test_listed_etag_is_accepted_verbatim(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        client = make_client()
        row = client.get("/api/money/work").json()["entries"][0]
        resp = client.patch(f"/api/money/work/{row['uid']}", json={"qty": 2, "etag": row["etag"]})
        assert resp.status_code == 200


class TestNoDataDir:
    def _client(self) -> TestClient:
        ctx = UserContext(data_dir=None, ledgers=[], db_path=None)
        app = FastAPI()
        app.include_router(router, prefix="/api/money")
        app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
        app.dependency_overrides[get_user_config] = lambda: ctx
        app.dependency_overrides[verify_origin] = lambda: None
        return TestClient(app)

    def test_list_returns_empty(self):
        data = self._client().get("/api/money/work").json()
        assert data["entries"] == []

    def test_create_404(self):
        resp = self._client().post(
            "/api/money/work", json={"date": "2026-03-01", "client": "a", "service": "b"}
        )
        assert resp.status_code == 404


class TestMutationsRequireCsrf:
    """Every mutating route must carry the ``verify_origin`` dependency."""

    def _client(self, tmp_path: Path) -> TestClient:
        ctx = UserContext(
            data_dir=tmp_path,
            ledgers=[],
            db_path=tmp_path / "money.db",
            invoicing_config_path=_write_invoicing_config(tmp_path),
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/money")
        app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
        app.dependency_overrides[get_user_config] = lambda: ctx

        def _reject():
            raise HTTPException(403, "bad origin")

        app.dependency_overrides[verify_origin] = _reject
        return TestClient(app, raise_server_exceptions=False)

    def test_create_blocked(self, tmp_path):
        resp = self._client(tmp_path).post(
            "/api/money/work",
            json={"date": "2026-03-01", "client": "acme", "service": "dev", "qty": 1},
        )
        assert resp.status_code == 403
        assert load_work_entries(tmp_path) == []

    def test_update_blocked(self, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        uid = _uid_of(tmp_path, "t")
        assert self._client(tmp_path).patch(
            f"/api/money/work/{uid}", json={"qty": 2}
        ).status_code == 403

    def test_delete_blocked(self, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=1, description="t")
        uid = _uid_of(tmp_path, "t")
        assert self._client(tmp_path).delete(f"/api/money/work/{uid}").status_code == 403


class TestRequiresAuth:
    def test_list_401_without_auth(self, tmp_path):
        """No auth override — the router's default session lookup denies."""
        app = FastAPI()
        app.include_router(router, prefix="/api/money")
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/api/money/work").status_code == 401


class TestDatePreservation:
    def test_create_stores_real_date_object(self, make_client, tmp_path):
        make_client().post(
            "/api/money/work",
            json={"date": "2026-03-01", "client": "acme", "service": "dev", "qty": 1},
        )
        assert load_work_entries(tmp_path)[0].date == date(2026, 3, 1)
