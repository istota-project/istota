"""Tests for money.work_export: work entries and invoices out of money.db."""

import csv
import io
import json
from datetime import date

import pytest
from click.testing import CliRunner

from istota.money import work_export
from istota.money.work import (
    add_work_entry,
    assign_invoice_number,
    list_invoices,
    load_work_entries,
    mark_paid,
    record_voided_invoice,
    void_invoice,
)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


def _rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def _seed(data_dir) -> None:
    """One paid entry, one re-billed after a void, one on the void's replacement."""
    add_work_entry(data_dir, "2025-12-20", "acme", "dev", qty=2, description="December")
    add_work_entry(data_dir, "2026-01-05", "acme", "dev", qty=3.5, description="January")
    add_work_entry(data_dir, "2026-02-01", "globex", "retainer", amount=2000,
                   description="Retainer")
    assign_invoice_number(data_dir, [1], "INV-000001", "2026-01-02")
    mark_paid(data_dir, "INV-000001", "2026-01-20")
    assign_invoice_number(data_dir, [2], "INV-000002", "2026-02-01")
    assign_invoice_number(data_dir, [3], "INV-000003", "2026-02-02")
    void_invoice(data_dir, "INV-000002", reason="wrong rate", replaced_by=["INV-000003"])
    assign_invoice_number(data_dir, [2], "INV-000004", "2026-02-03")


class TestCsv:
    def test_one_row_per_entry_with_its_invoice(self, data_dir):
        _seed(data_dir)
        export = work_export.collect(data_dir)
        rows = _rows(work_export.render(export, "csv"))

        assert [r["description"] for r in rows] == ["December", "January", "Retainer"]
        paid, rebilled, retainer = rows
        assert paid["invoice"] == "INV-000001"
        assert paid["invoice_status"] == "paid"
        assert paid["invoice_date"] == "2026-01-02"
        assert paid["paid_date"] == "2026-01-20"
        assert rebilled["invoice"] == "INV-000004"
        assert rebilled["invoice_status"] == "open"
        assert rebilled["voided_invoices"] == "INV-000002"
        assert retainer["invoice"] == "INV-000003"
        assert retainer["voided_invoices"] == ""
        assert retainer["qty"] == ""
        assert retainer["amount"] == "2000"
        assert rebilled["qty"] == "3.5"

    def test_header_is_stable(self, data_dir):
        text = work_export.render(work_export.collect(data_dir), "csv")
        assert text.splitlines()[0].split(",") == list(work_export.CSV_COLUMNS)

    def test_a_text_cell_that_reads_as_a_formula_is_quoted_as_text(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1,
                       description='=HYPERLINK("http://x","y")')
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=1, description="-1 fix")
        rows = _rows(work_export.render(work_export.collect(data_dir), "csv"))
        assert rows[0]["description"] == "'=HYPERLINK(\"http://x\",\"y\")"
        assert rows[1]["description"] == "'-1 fix"

    def test_an_uninvoiced_entry_has_empty_invoice_cells(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1)
        row = _rows(work_export.render(work_export.collect(data_dir), "csv"))[0]
        assert (row["invoice"], row["invoice_status"], row["paid_date"]) == ("", "", "")

    def test_year_keeps_that_years_entries(self, data_dir):
        _seed(data_dir)
        rows = _rows(work_export.render(work_export.collect(data_dir, year=2026), "csv"))
        assert [r["description"] for r in rows] == ["January", "Retainer"]


class TestJson:
    def test_carries_voided_invoices_and_their_entries(self, data_dir):
        _seed(data_dir)
        doc = json.loads(work_export.render(work_export.collect(data_dir), "json"))

        assert doc["format"] == work_export.JSON_FORMAT
        assert doc["version"] == 1
        assert doc["year"] is None
        assert len(doc["entries"]) == 3
        uids = {e["description"]: e["uid"] for e in doc["entries"]}
        by_number = {i["number"]: i for i in doc["invoices"]}
        assert set(by_number) == {"INV-000001", "INV-000002", "INV-000003", "INV-000004"}

        voided = by_number["INV-000002"]
        assert voided["status"] == "voided"
        assert voided["void_reason"] == "wrong rate"
        assert voided["replaced_by"] == ["INV-000003"]
        assert voided["entry_uids"] == [uids["January"]]
        assert voided["total"] == list_invoices(data_dir)[1].total
        assert by_number["INV-000001"]["paid_date"] == "2026-01-20"

        january = next(e for e in doc["entries"] if e["uid"] == uids["January"])
        assert january["invoice"] == "INV-000004"
        assert january["qty"] == 3.5

    def test_a_voided_record_with_no_entries_is_kept_by_its_issue_year(self, data_dir):
        add_work_entry(data_dir, "2025-12-20", "acme", "dev", qty=2)
        add_work_entry(data_dir, "2026-01-05", "acme", "dev", qty=3)
        assign_invoice_number(data_dir, [1], "INV-000001", "2025-12-21")
        assign_invoice_number(data_dir, [2], "INV-000003", "2026-01-06")
        record_voided_invoice(
            data_dir, "INV-000002", client="acme", entity="", total=10.0,
            issue_date=date(2026, 1, 2), voided_on=date(2026, 1, 3), reason="typo",
        )
        doc = json.loads(work_export.render(work_export.collect(data_dir, year=2026), "json"))
        # INV-000001 covers only the December entry, so the 2026 slice drops it.
        assert {i["number"] for i in doc["invoices"]} == {"INV-000002", "INV-000003"}
        assert doc["year"] == 2026

    def test_a_date_kept_in_extra_is_written_in_the_stores_tagged_form(self, data_dir):
        from istota.money.work import _untag, write_transaction

        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1)
        with write_transaction(data_dir) as conn:
            conn.execute(
                """UPDATE work_entries SET extra = '{"logged_on": {"$date": "2026-03-02"}}'""")
        doc = json.loads(work_export.render(work_export.collect(data_dir), "json"))
        extra = doc["entries"][0]["extra"]
        assert extra == {"logged_on": {"$date": "2026-03-02"}}
        assert _untag(extra) == {"logged_on": date(2026, 3, 2)}

    def test_pdf_paths_are_relative_to_the_data_dir(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1)
        assign_invoice_number(data_dir, [1], "INV-000001", "2026-03-02")
        pdf = data_dir / "invoices" / "generated" / "INV-000001.pdf"
        void_invoice(data_dir, "INV-000001", pdf_path=pdf)
        doc = json.loads(work_export.render(work_export.collect(data_dir), "json"))
        assert doc["invoices"][0]["pdf"] == "invoices/generated/INV-000001.pdf"

    def test_reads_nothing_into_the_store(self, data_dir):
        _seed(data_dir)
        before = load_work_entries(data_dir), list_invoices(data_dir)
        work_export.render(work_export.collect(data_dir), "json")
        assert (load_work_entries(data_dir), list_invoices(data_dir)) == before


def test_filename_names_the_year_and_format():
    assert work_export.filename("csv") == "work.csv"
    assert work_export.filename("json", 2026) == "work-2026.json"


class TestCli:
    def _invoke(self, data_dir, *args):
        from istota.money.cli import Context, UserContext, cli

        obj = Context()
        obj.users["alice"] = UserContext(data_dir=data_dir, db_path=data_dir / "data" / "money.db")
        obj.activate_user("alice")
        return CliRunner().invoke(cli, ["-u", "alice", "work", "export", *args], obj=obj)

    def test_writes_csv_to_stdout(self, data_dir):
        result = self._invoke(data_dir / "x")
        assert result.exit_code == 0, result.output
        assert result.output.splitlines()[0].startswith("uid,date,client")

    def test_out_writes_the_file_and_reports_it(self, tmp_path):
        data_dir = tmp_path / "money"
        out = tmp_path / "exports" / "work.json"
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1)
        result = self._invoke(data_dir, "--format", "json", "--out", str(out))
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["status"] == "ok"
        assert report["entries"] == 1
        assert json.loads(out.read_text())["entries"][0]["client"] == "acme"
