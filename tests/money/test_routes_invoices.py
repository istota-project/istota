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

from istota.money.cli import UserContext
from istota.money.routes import get_user_config, require_auth, router, verify_origin
from istota.money.work import (
    add_work_entry,
    assign_invoice_number,
    get_entries_for_invoice,
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
        app = FastAPI()
        app.include_router(router, prefix="/api/money")
        app.dependency_overrides[require_auth] = lambda: {"username": "stefan"}
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
