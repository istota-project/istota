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
    add_work_entry,
    assign_invoice_number,
    get_entries_for_invoice,
    mark_paid,
)


@pytest.fixture
def make_client(tmp_path: Path):
    def _factory(invoicing_config_path: Path | None = None) -> TestClient:
        ctx = UserContext(
            data_dir=tmp_path,
            ledgers=[],
            db_path=tmp_path / "data" / "money.db",
            invoicing_config_path=invoicing_config_path,
        )
        ctx.db_path.parent.mkdir(parents=True, exist_ok=True)
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
        """An invoice imported with no stored date was issued no earlier than
        its latest work; the earliest work is the wrong guess."""
        work_dir = tmp_path / "invoices" / "work"
        work_dir.mkdir(parents=True)
        (work_dir / "2026.toml").write_text(
            '[[entries]]\nuid = "a"\ndate = 2026-03-01\nclient = "acme"\n'
            'service = "dev"\nqty = 8\ninvoice = "INV-000001"\n\n'
            '[[entries]]\nuid = "b"\ndate = 2026-03-20\nclient = "acme"\n'
            'service = "dev"\nqty = 4\ninvoice = "INV-000001"\n'
        )

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
        mark_paid(tmp_path, "INV-000001", "2026-04-15")
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
            mark_paid(tmp_path, f"INV-{index:06d}", "2026-04-15")
        client = make_client(config)
        with money_db.get_db(tmp_path / "data" / "money.db") as conn:
            # The config is a TOML file the store does not read, so a hand
            # assignment stored no total; matching reads the stored one.
            conn.execute("UPDATE invoices SET total = 1200")
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
        with money_db.get_db(tmp_path / "data" / "money.db") as conn:
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


class TestInvoiceRowFields:
    """#755: GET /invoices carries the void record and whether a PDF exists."""

    def _issue(self, data_dir: Path, number: str, total: float, *, pdf: bool = True) -> str:
        from istota.money.work import create_invoice, load_work_entries, write_transaction

        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1, description=number)
        uid = next(e.uid for e in load_work_entries(data_dir) if e.description == number)
        pdf_path = ""
        if pdf:
            year_dir = data_dir / "invoices" / "2026"
            year_dir.mkdir(parents=True, exist_ok=True)
            path = year_dir / f"Invoice-{number[4:]}-03_02_2026.pdf"
            path.write_bytes(b"%PDF-1.4")
            pdf_path = str(path)
        with write_transaction(data_dir) as conn:
            create_invoice(
                conn, number=number, client="acme", entity="", group_name="",
                issue_date=date(2026, 3, 2), total=total, entry_uids=[uid], pdf_path=pdf_path,
            )
        return pdf_path

    def _client(self, make_client, tmp_path):
        from istota.money.core.invoicing import void_invoice_with_cleanup

        self._issue(tmp_path, "INV-000001", 2300.0)
        self._issue(tmp_path, "INV-000002", 1200.0)
        self._issue(tmp_path, "INV-000003", 1100.0, pdf=False)
        void_invoice_with_cleanup(
            tmp_path, "INV-000001", invoice_output_dir=tmp_path / "invoices",
            reason="two jobs", replaced_by=["INV-000002", "INV-000003"],
        )
        return make_client(_write_invoicing_config(tmp_path, "invoices"))

    def test_rows_carry_status_void_record_and_has_pdf(self, make_client, tmp_path):
        client = self._client(make_client, tmp_path)
        data = client.get("/api/money/invoices").json()
        rows = {row["invoice_number"]: row for row in data["invoices"]}

        voided = rows["INV-000001"]
        assert voided["status"] == "voided"
        assert voided["voided_at"]
        assert voided["void_reason"] == "two jobs"
        assert voided["replaced_by"] == ["INV-000002", "INV-000003"]
        assert voided["total"] == 2300.0
        # The renamed VOID copy is still the invoice's PDF.
        assert voided["has_pdf"] is True

        assert rows["INV-000002"]["status"] == "outstanding"
        assert rows["INV-000002"]["has_pdf"] is True
        assert rows["INV-000002"]["voided_at"] is None
        assert rows["INV-000002"]["void_reason"] == ""
        assert rows["INV-000002"]["replaced_by"] == []
        assert rows["INV-000003"]["has_pdf"] is False

        assert data["invoice_count"] == 3
        assert data["outstanding_count"] == 2

    def test_totals_are_the_stored_snapshot(self, make_client, tmp_path):
        client = self._client(make_client, tmp_path)
        rows = {r["invoice_number"]: r for r in client.get("/api/money/invoices").json()["invoices"]}
        # One hour of dev at the config's 150 would be 150; the invoice billed 1200.
        assert rows["INV-000002"]["total"] == 1200.0

    def test_has_pdf_reads_stored_paths_and_lists_the_tree_once(
        self, make_client, tmp_path, monkeypatch,
    ):
        from istota.money import routes
        from istota.money.core import invoicing

        self._issue(tmp_path, "INV-000001", 100.0)
        stored = Path(self._issue(tmp_path, "INV-000002", 100.0))
        stored.unlink()
        self._issue(tmp_path, "INV-000003", 100.0, pdf=False)
        self._issue(tmp_path, "INV-000004", 100.0, pdf=False)
        # A PDF on disk for a row that recorded none, as an import before the
        # path was stored would leave.
        (tmp_path / "invoices" / "2026" / "Invoice-000004-03_02_2026.pdf").write_bytes(b"%PDF")
        client = make_client(_write_invoicing_config(tmp_path, "invoices"))

        def no_glob(*args, **kwargs):
            raise AssertionError("has_pdf globbed per invoice")

        listings = []
        real_names = routes._invoice_pdf_names

        def counting(output_dir):
            listings.append(output_dir)
            return real_names(output_dir)

        monkeypatch.setattr(invoicing, "find_invoice_pdf", no_glob)
        monkeypatch.setattr(routes, "_invoice_pdf_names", counting)
        rows = {
            r["invoice_number"]: r["has_pdf"]
            for r in client.get("/api/money/invoices").json()["invoices"]
        }

        assert rows == {
            "INV-000001": True, "INV-000002": False,
            "INV-000003": False, "INV-000004": True,
        }
        assert len(listings) == 1

    def test_include_voided_false_filters_them_out(self, make_client, tmp_path):
        client = self._client(make_client, tmp_path)
        data = client.get("/api/money/invoices?include_voided=false").json()
        assert [row["invoice_number"] for row in data["invoices"]] == ["INV-000002", "INV-000003"]
        assert data["outstanding_count"] == 2

    def test_a_voided_invoice_cannot_be_marked_paid(self, make_client, tmp_path):
        client = self._client(make_client, tmp_path)
        resp = client.post("/api/money/invoices/INV-000001/mark-paid", json={})
        assert resp.status_code == 404

    def test_the_voided_pdf_still_downloads(self, make_client, tmp_path):
        client = self._client(make_client, tmp_path)
        resp = client.get("/api/money/invoices/INV-000001/pdf")
        assert resp.status_code == 200
        assert "VOID" in resp.headers["content-disposition"]


# =============================================================================
# #756: generate, void and create from the web
# =============================================================================


@pytest.fixture
def fake_pdf(monkeypatch):
    """Render PDFs as a few bytes on disk, recording each path written."""
    from istota.money.core import invoicing as invoicing_mod

    written: list[Path] = []

    def _fake(html, output_path, **kwargs):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"%PDF-1.4")
        written.append(output_path)

    monkeypatch.setattr(invoicing_mod, "generate_invoice_pdf", _fake)
    return written


def _invoices_in_db(data_dir: Path) -> list[str]:
    from istota.money.work import list_invoices

    return [r.number for r in list_invoices(data_dir)]


def _entry(data_dir: Path, description: str):
    from istota.money.work import load_work_entries

    return next(e for e in load_work_entries(data_dir) if e.description == description)


def _add(data_dir: Path, description: str, *, day: str = "2026-03-01", qty: float = 8):
    add_work_entry(data_dir, day, "acme", "dev", qty=qty, description=description)
    return _entry(data_dir, description)


class TestGenerateRoute:
    URL = "/api/money/invoices/generate"

    def _client(self, make_client, tmp_path):
        return make_client(_write_invoicing_config(tmp_path, "invoices"))

    def test_dry_run_previews_and_writes_nothing(self, make_client, tmp_path, fake_pdf):
        first = _add(tmp_path, "first")
        second = _add(tmp_path, "second", qty=4)
        client = self._client(make_client, tmp_path)

        resp = client.post(self.URL, json={"dry_run": True})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["dry_run"] is True
        assert data["notifications"] == "none"
        [invoice] = data["invoices"]
        assert invoice["invoice_number"] == "INV-000001"
        assert invoice["total"] == 1800.0
        assert invoice["entry_uids"] == [first.uid, second.uid]
        assert invoice["entry_descriptions"] == ["first", "second"]
        assert "file" not in invoice
        assert fake_pdf == []
        assert _invoices_in_db(tmp_path) == []
        assert not _entry(tmp_path, "first").invoice

    def test_commit_issues_the_previewed_invoice(self, make_client, tmp_path, fake_pdf):
        _add(tmp_path, "first")
        _add(tmp_path, "second", qty=4)
        client = self._client(make_client, tmp_path)
        preview = client.post(self.URL, json={"dry_run": True}).json()

        resp = client.post(self.URL, json={"dry_run": False})

        assert resp.status_code == 200
        data = resp.json()
        assert data["dry_run"] is False
        assert [i["total"] for i in data["invoices"]] == [i["total"] for i in preview["invoices"]]
        assert _invoices_in_db(tmp_path) == ["INV-000001"]
        assert _entry(tmp_path, "first").invoice == "INV-000001"
        assert len(fake_pdf) == 1 and fake_pdf[0].is_file()
        listed = client.get("/api/money/invoices").json()["invoices"]
        assert listed[0]["has_pdf"] is True

    def test_named_entry_with_current_etag_bills_only_that_entry(
        self, make_client, tmp_path, fake_pdf,
    ):
        from istota.money.work import entry_etag

        chosen = _add(tmp_path, "chosen")
        _add(tmp_path, "other", day="2026-02-01")
        client = self._client(make_client, tmp_path)

        resp = client.post(self.URL, json={
            "entry_uids": [chosen.uid], "etags": {chosen.uid: entry_etag(chosen)},
            "dry_run": False,
        })

        assert resp.status_code == 200
        assert resp.json()["invoices"][0]["entry_uids"] == [chosen.uid]
        assert _entry(tmp_path, "chosen").invoice == "INV-000001"
        assert not _entry(tmp_path, "other").invoice

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_stale_etag_is_409_and_generates_nothing(
        self, make_client, tmp_path, fake_pdf, dry_run,
    ):
        from istota.money.work import entry_etag, update_work_entry_by_uid

        entry = _add(tmp_path, "edited")
        stale = entry_etag(entry)
        update_work_entry_by_uid(tmp_path, entry.uid, qty=9)
        client = self._client(make_client, tmp_path)

        resp = client.post(self.URL, json={
            "entry_uids": [entry.uid], "etags": {entry.uid: stale, "gone": "abc"},
            "dry_run": dry_run,
        })

        assert resp.status_code == 409
        assert sorted(resp.json()["stale"]) == sorted([entry.uid, "gone"])
        assert fake_pdf == []
        assert _invoices_in_db(tmp_path) == []

    def test_unknown_entry_is_404(self, make_client, tmp_path, fake_pdf):
        _add(tmp_path, "real")
        client = self._client(make_client, tmp_path)
        resp = client.post(self.URL, json={"entry_uids": ["nope"], "dry_run": False})
        assert resp.status_code == 404
        assert "nope" in resp.json()["error"]
        assert _invoices_in_db(tmp_path) == []

    def test_already_invoiced_entry_is_409(self, make_client, tmp_path, fake_pdf):
        entry = _add(tmp_path, "billed")
        client = self._client(make_client, tmp_path)
        assert client.post(self.URL, json={"dry_run": False}).status_code == 200

        resp = client.post(self.URL, json={"entry_uids": [entry.uid], "dry_run": False})

        assert resp.status_code == 409
        assert "already invoiced" in resp.json()["error"]
        assert _invoices_in_db(tmp_path) == ["INV-000001"]

    def test_nothing_to_bill_is_an_empty_list(self, make_client, tmp_path, fake_pdf):
        client = self._client(make_client, tmp_path)
        resp = client.post(self.URL, json={"dry_run": True})
        assert resp.status_code == 200
        assert resp.json()["invoices"] == []

    @pytest.mark.parametrize("body", [
        {},
        {"dry_run": "yes"},
        {"dry_run": False, "entry_uids": []},
        {"dry_run": False, "entry_uids": "u1"},
        {"dry_run": False, "entry_uids": [1]},
        {"dry_run": False, "period": "2026-13"},
        {"dry_run": False, "period": 202603},
        {"dry_run": False, "client": 3},
        {"dry_run": False, "etags": ["u1"]},
        {"dry_run": False, "etags": {"u1": 5}},
        ["dry_run"],
    ])
    def test_invalid_body_is_400(self, make_client, tmp_path, fake_pdf, body):
        _add(tmp_path, "work")
        client = self._client(make_client, tmp_path)
        resp = client.post(self.URL, json=body)
        assert resp.status_code == 400
        assert fake_pdf == []
        assert _invoices_in_db(tmp_path) == []

    def test_no_invoicing_config_is_400(self, make_client, tmp_path, fake_pdf):
        _add(tmp_path, "work")
        resp = make_client().post(self.URL, json={"dry_run": False})
        assert resp.status_code == 400
        assert _invoices_in_db(tmp_path) == []


def _issue(client, *, qty: float = 8) -> str:
    resp = client.post("/api/money/invoices", json={
        "client": "acme", "items": [{"service": "dev", "qty": qty, "description": "work"}],
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["invoice"]["invoice_number"]


class TestVoidRoute:
    def _client(self, make_client, tmp_path):
        return make_client(_write_invoicing_config(tmp_path, "invoices"))

    def test_void_open_invoice(self, make_client, tmp_path, fake_pdf):
        client = self._client(make_client, tmp_path)
        number = _issue(client)
        replacement = _issue(client, qty=4)

        resp = client.post(f"/api/money/invoices/{number}/void", json={
            "reason": "wrong hours", "replaced_by": [replacement],
        })

        assert resp.status_code == 200
        invoice = resp.json()["invoice"]
        assert invoice["invoice_number"] == number
        assert invoice["status"] == "voided"
        assert invoice["voided_at"]
        assert invoice["void_reason"] == "wrong hours"
        assert invoice["replaced_by"] == [replacement]
        assert invoice["total"] == 1200.0
        assert invoice["has_pdf"] is True
        assert not fake_pdf[0].exists()
        assert fake_pdf[0].with_name(f"{fake_pdf[0].stem}-VOID.pdf").is_file()
        assert resp.json()["entries_released"] == 1

    def test_void_with_no_body(self, make_client, tmp_path, fake_pdf):
        client = self._client(make_client, tmp_path)
        number = _issue(client)
        resp = client.post(f"/api/money/invoices/{number}/void")
        assert resp.status_code == 200
        assert resp.json()["invoice"]["status"] == "voided"

    def test_delete_pdf(self, make_client, tmp_path, fake_pdf):
        client = self._client(make_client, tmp_path)
        number = _issue(client)
        resp = client.post(f"/api/money/invoices/{number}/void", json={"delete_pdf": True})
        assert resp.status_code == 200
        assert resp.json()["invoice"]["has_pdf"] is False
        assert list((tmp_path / "invoices").rglob("*.pdf")) == []

    def test_paid_without_force_is_409(self, make_client, tmp_path, fake_pdf):
        client = self._client(make_client, tmp_path)
        number = _issue(client)
        mark_paid(tmp_path, number, "2026-04-15")

        resp = client.post(f"/api/money/invoices/{number}/void", json={})

        assert resp.status_code == 409
        assert resp.json()["code"] == "paid"
        rows = client.get("/api/money/invoices?show_all=true").json()["invoices"]
        assert rows[0]["status"] == "paid"
        assert fake_pdf[0].is_file()

    def test_paid_with_force(self, make_client, tmp_path, fake_pdf):
        client = self._client(make_client, tmp_path)
        number = _issue(client)
        mark_paid(tmp_path, number, "2026-04-15")

        resp = client.post(f"/api/money/invoices/{number}/void", json={"force": True})

        assert resp.status_code == 200
        assert resp.json()["invoice"]["status"] == "voided"
        assert resp.json()["invoice"]["was_paid"] is True

    def test_already_voided_is_409(self, make_client, tmp_path, fake_pdf):
        client = self._client(make_client, tmp_path)
        number = _issue(client)
        assert client.post(f"/api/money/invoices/{number}/void", json={}).status_code == 200

        resp = client.post(f"/api/money/invoices/{number}/void", json={})

        assert resp.status_code == 409
        assert resp.json()["code"] == "already_voided"
        assert "already voided on" in resp.json()["error"]

    def test_unknown_invoice_is_404(self, make_client, tmp_path, fake_pdf):
        client = self._client(make_client, tmp_path)
        resp = client.post("/api/money/invoices/INV-999999/void", json={})
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_unknown_replacement_is_400(self, make_client, tmp_path, fake_pdf):
        client = self._client(make_client, tmp_path)
        number = _issue(client)
        resp = client.post(
            f"/api/money/invoices/{number}/void", json={"replaced_by": ["INV-999999"]},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "unknown_replacement"
        assert client.get("/api/money/invoices").json()["invoices"][0]["status"] == "outstanding"

    @pytest.mark.parametrize("body", [
        {"reason": 5},
        {"replaced_by": "INV-000002"},
        {"replaced_by": [2]},
        {"force": "yes"},
        {"delete_pdf": 1},
        ["force"],
    ])
    def test_invalid_body_is_400(self, make_client, tmp_path, fake_pdf, body):
        client = self._client(make_client, tmp_path)
        number = _issue(client)
        resp = client.post(f"/api/money/invoices/{number}/void", json=body)
        assert resp.status_code == 400
        assert client.get("/api/money/invoices").json()["invoices"][0]["status"] == "outstanding"

    def test_malformed_json_is_400(self, make_client, tmp_path, fake_pdf):
        client = self._client(make_client, tmp_path)
        number = _issue(client)
        resp = client.post(
            f"/api/money/invoices/{number}/void", content=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400


class TestInvoiceDetails:
    URL = "/api/money/invoice-details"

    def _client(self, make_client, tmp_path):
        return make_client(_write_invoicing_config(tmp_path, "invoices"))

    def test_open_invoice_lists_its_lines(self, make_client, tmp_path):
        _seed_invoice(tmp_path)
        client = self._client(make_client, tmp_path)

        resp = client.get(self.URL, params={"invoice_number": "INV-000001"})

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [i["quantity"] for i in items] == [8, 4]
        assert sum(i["amount"] for i in items) == 1800.0

    def test_voided_invoice_still_lists_the_work_it_billed(
        self, make_client, tmp_path, fake_pdf,
    ):
        client = self._client(make_client, tmp_path)
        number = _issue(client, qty=6)
        assert client.post(f"/api/money/invoices/{number}/void", json={}).status_code == 200

        resp = client.get(self.URL, params={"invoice_number": number})

        assert resp.status_code == 200
        [item] = resp.json()["items"]
        assert item["quantity"] == 6
        assert item["amount"] == 900.0

    def test_unknown_invoice_is_404(self, make_client, tmp_path):
        client = self._client(make_client, tmp_path)
        resp = client.get(self.URL, params={"invoice_number": "INV-999999"})
        assert resp.status_code == 404


class TestCreateRoute:
    URL = "/api/money/invoices"

    def test_manual_create(self, make_client, tmp_path, fake_pdf):
        client = make_client(_write_invoicing_config(tmp_path, "invoices"))

        resp = client.post(self.URL, json={
            "client": "acme",
            "items": [
                {"service": "dev", "qty": 2, "description": "setup"},
                {"description": "Hardware", "amount": 99.5},
            ],
        })

        assert resp.status_code == 200
        invoice = resp.json()["invoice"]
        assert invoice["invoice_number"] == "INV-000001"
        assert invoice["status"] == "outstanding"
        assert invoice["total"] == 399.5
        assert invoice["has_pdf"] is True
        listed = client.get("/api/money/invoices").json()
        assert [i["invoice_number"] for i in listed["invoices"]] == ["INV-000001"]
        assert listed["outstanding_count"] == 1

    def test_unknown_client_is_400(self, make_client, tmp_path, fake_pdf):
        client = make_client(_write_invoicing_config(tmp_path, "invoices"))
        resp = client.post(self.URL, json={
            "client": "nobody", "items": [{"description": "x", "amount": 1}],
        })
        assert resp.status_code == 400
        assert fake_pdf == []
        assert _invoices_in_db(tmp_path) == []

    @pytest.mark.parametrize("body", [
        {"items": [{"description": "x", "amount": 1}]},
        {"client": "", "items": [{"description": "x", "amount": 1}]},
        {"client": "acme"},
        {"client": "acme", "items": []},
        {"client": "acme", "items": {"description": "x"}},
        {"client": "acme", "items": ["x"]},
        {"client": "acme", "items": [{"description": "x", "amount": "lots"}]},
        {"client": "acme", "items": [{"service": "nope", "qty": 1}]},
        {"client": "acme", "entity": 3, "items": [{"description": "x", "amount": 1}]},
        ["acme"],
    ])
    def test_invalid_body_is_400(self, make_client, tmp_path, fake_pdf, body):
        client = make_client(_write_invoicing_config(tmp_path, "invoices"))
        resp = client.post(self.URL, json=body)
        assert resp.status_code == 400
        assert fake_pdf == []
        assert _invoices_in_db(tmp_path) == []

    def test_no_invoicing_config_is_400(self, make_client, tmp_path, fake_pdf):
        resp = make_client().post(self.URL, json={
            "client": "acme", "items": [{"description": "x", "amount": 1}],
        })
        assert resp.status_code == 400
