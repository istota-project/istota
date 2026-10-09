"""Tests for the invoice action routes: mark-paid, mark-pending, PDF download.

These operate on the file-based work-entry store (``data_dir``) and the
generated-PDF directory, so they don't need a seeded invoicing config.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.money import db as money_db
from istota.money.cli import UserContext
from istota.money.routes import get_user_config, require_auth, router, verify_origin
from istota.money.work import (
    _save_entries,
    add_work_entry,
    assign_invoice_number,
    get_entries_for_invoice,
    load_work_entries,
    record_invoice_payment,
)


@pytest.fixture
def make_client(tmp_path: Path):
    def _factory(invoicing_config_path: Path | None = None) -> TestClient:
        ctx = UserContext(
            data_dir=tmp_path,
            ledgers=[],
            db_path=tmp_path / "money.db",
            invoicing_config_path=invoicing_config_path,
        )
        money_db.init_db(ctx.db_path)
        app = FastAPI()
        app.include_router(router, prefix="/api/money")
        app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
        app.dependency_overrides[get_user_config] = lambda: ctx
        app.dependency_overrides[verify_origin] = lambda: None
        return TestClient(app)
    return _factory


def _write_invoicing_config(data_dir: Path, invoice_output: str) -> Path:
    cfg = data_dir / "invoicing.toml"
    cfg.write_text(
        'accounting_path = "."\n'
        'next_invoice_number = 1\n'
        f'invoice_output = "{invoice_output}"\n\n'
        '[company]\nname = "My Co"\naddress = "123 Main"\n\n'
        '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
        '[services.dev]\ndisplay_name = "Dev"\nrate = 150\n'
    )
    return cfg


def _seed_invoice(data_dir: Path, number: str = "INV-000001") -> None:
    add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
    add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
    assign_invoice_number(data_dir, [1, 2], number)


class TestInvoiceListDate:
    """ISSUE-256: the `date` column is the invoice's date, not its first work.

    It used to be the *earliest* work billed, which on a month invoiced in
    arrears is weeks off and is not the invoice's date under any reading.
    """

    def test_shows_the_stored_issue_date(self, make_client, tmp_path):
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(tmp_path, "2026-03-20", "acme", "dev", qty=4)
        assign_invoice_number(tmp_path, [1, 2], "INV-000001", date(2026, 4, 1))

        client = make_client(_write_invoicing_config(tmp_path, "invoices"))
        resp = client.get("/api/money/invoices")
        assert resp.status_code == 200
        invoices = resp.json()["invoices"]
        assert invoices[0]["date"] == "2026-04-01"

    def test_a_legacy_invoice_shows_its_latest_work(self, make_client, tmp_path):
        """No stored date to show, and the earliest work is the wrong guess."""
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(tmp_path, "2026-03-20", "acme", "dev", qty=4)
        assign_invoice_number(tmp_path, [1, 2], "INV-000001")
        entries = load_work_entries(tmp_path)
        for entry in entries:
            entry.invoice_date = None
        _save_entries(tmp_path, entries)

        client = make_client(_write_invoicing_config(tmp_path, "invoices"))
        invoices = client.get("/api/money/invoices").json()["invoices"]
        assert invoices[0]["date"] == "2026-03-20"


class TestMarkPaid:
    def test_mark_paid_sets_paid_date(self, make_client, tmp_path):
        _seed_invoice(tmp_path)
        client = make_client()
        resp = client.post("/api/money/invoices/INV-000001/mark-paid", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["count"] == 2
        entries = get_entries_for_invoice(tmp_path, "INV-000001")
        assert all(e.paid_date is not None for e in entries)

    def test_mark_paid_with_explicit_date(self, make_client, tmp_path):
        _seed_invoice(tmp_path)
        client = make_client()
        resp = client.post(
            "/api/money/invoices/INV-000001/mark-paid",
            json={"paid_date": "2026-04-15"},
        )
        assert resp.status_code == 200
        assert resp.json()["paid_date"] == "2026-04-15"
        entries = get_entries_for_invoice(tmp_path, "INV-000001")
        assert all(e.paid_date == date(2026, 4, 15) for e in entries)

    def test_mark_paid_unknown_invoice_404(self, make_client, tmp_path):
        _seed_invoice(tmp_path)
        client = make_client()
        resp = client.post("/api/money/invoices/INV-999999/mark-paid", json={})
        assert resp.status_code == 404


class TestMarkPending:
    def test_mark_pending_clears_paid_date_keeps_invoice(self, make_client, tmp_path):
        _seed_invoice(tmp_path)
        record_invoice_payment(tmp_path, "INV-000001", "2026-04-15")
        client = make_client()
        resp = client.post("/api/money/invoices/INV-000001/mark-pending", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["count"] == 2
        entries = get_entries_for_invoice(tmp_path, "INV-000001")
        assert len(entries) == 2
        assert all(e.paid_date is None for e in entries)
        assert all(e.invoice == "INV-000001" for e in entries)

    def test_mark_pending_unknown_invoice_404(self, make_client, tmp_path):
        _seed_invoice(tmp_path)
        client = make_client()
        resp = client.post("/api/money/invoices/INV-999999/mark-pending", json={})
        assert resp.status_code == 404


class TestInvoicePdf:
    def _make_pdf(self, data_dir: Path) -> Path:
        year_dir = data_dir / "invoices" / "generated" / "2026"
        year_dir.mkdir(parents=True)
        pdf = year_dir / "Invoice-000001-04_15_2026.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        return pdf

    def test_download_existing_pdf(self, make_client, tmp_path):
        self._make_pdf(tmp_path)
        client = make_client()
        resp = client.get("/api/money/invoices/INV-000001/pdf")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")

    def test_download_missing_pdf_404(self, make_client, tmp_path):
        client = make_client()
        resp = client.get("/api/money/invoices/INV-000099/pdf")
        assert resp.status_code == 404

    def test_download_honors_config_invoice_output(self, make_client, tmp_path):
        # A non-default invoice_output (relative) must resolve under data_dir,
        # not the hardcoded "invoices/generated" fallback.
        cfg = _write_invoicing_config(tmp_path, "invoices/custom-pdfs")
        year_dir = tmp_path / "invoices" / "custom-pdfs" / "2026"
        year_dir.mkdir(parents=True)
        (year_dir / "Invoice-000001-04_15_2026.pdf").write_bytes(b"%PDF-1.4 fake")

        client = make_client(invoicing_config_path=cfg)
        resp = client.get("/api/money/invoices/INV-000001/pdf")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")


class TestInvoicePaymentMatches:
    def test_lists_live_reviews_and_sync_dates_with_one_match_read(self, make_client, tmp_path, monkeypatch):
        config = _write_invoicing_config(tmp_path, "invoices")
        for index in range(1, 5):
            add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)
            assign_invoice_number(tmp_path, [index], f"INV-{index:06d}", date(2026, 3, 2))
        for index in range(2, 5):
            record_invoice_payment(tmp_path, f"INV-{index:06d}", "2026-04-15")
        client = make_client(config)
        with money_db.get_db(tmp_path / "money.db") as conn:
            for index, status, decided_by in [(2, "settled", "auto"), (3, "settled", "user"), (4, "reverted", "auto")]:
                money_db.record_payment_match(
                    conn, ledger_txn_id=str(index) * 32, txn_date="2026-04-15",
                    amount=1200, status=status, decided_by=decided_by,
                    invoice_number=f"INV-{index:06d}",
                )
            for index, candidates in [(5, ["INV-000001", "INV-000002"]), (6, ["INV-000002"])]:
                money_db.record_payment_match(
                    conn, ledger_txn_id=str(index) * 32, txn_date="2026-04-15",
                    amount=1200, status="review", payee="Acme payment",
                    account="Assets:Bank:Checking", candidates=candidates,
                )
        reads = []
        original = money_db.list_payment_matches

        def counted(conn, **kwargs):
            reads.append(kwargs)
            return original(conn, **kwargs)

        monkeypatch.setattr(money_db, "list_payment_matches", counted)
        response = client.get("/api/money/invoices?show_all=true")
        assert response.status_code == 200
        data = response.json()
        assert data["payment_reviews"][0]["ledger_txn_id"] == "5" * 32
        assert len(data["payment_reviews"]) == 1
        assert data["payment_reviews"][0]["candidate_details"] == [
            {"invoice_number": "INV-000001", "client": "acme", "total": 1200},
        ]
        invoices = {row["invoice_number"]: row for row in data["invoices"]}
        assert invoices["INV-000002"]["paid_by_sync_date"] == "2026-04-15"
        for number in ["INV-000001", "INV-000003", "INV-000004"]:
            assert "paid_by_sync_date" not in invoices[number]
        assert reads == [{"status": None}]

    @pytest.mark.parametrize("action", ["settle", "dismiss"])
    def test_list_review_actions_update_the_same_record(self, make_client, tmp_path, action):
        from istota import db
        from istota.config import Config

        _seed_invoice(tmp_path)
        client = make_client(_write_invoicing_config(tmp_path, "invoices"))
        framework = tmp_path / "framework.db"
        db.init_db(framework)
        client.app.state.istota_config = Config(db_path=framework)
        ident = "a" * 32
        with money_db.get_db(tmp_path / "money.db") as conn:
            money_db.record_payment_match(
                conn, ledger_txn_id=ident, txn_date="2026-04-15", amount=1800,
                status="review", account="Assets:Bank:Checking", candidates=["INV-000001"],
            )
        reviews = client.get("/api/money/invoices").json()["payment_reviews"]
        assert len(reviews) == 1
        response = client.post(
            f"/api/money/invoices/review/{reviews[0]['ledger_txn_id']}/{action}",
            json={"invoice_number": reviews[0]["candidate_details"][0]["invoice_number"]},
        )
        assert response.status_code == 200
        data = client.get("/api/money/invoices?show_all=true").json()
        assert data["payment_reviews"] == []
        assert "paid_by_sync_date" not in data["invoices"][0]
        assert data["invoices"][0]["status"] == ("paid" if action == "settle" else "outstanding")

    def test_no_config_has_no_reviews(self, make_client):
        assert make_client().get("/api/money/invoices").json()["payment_reviews"] == []
