"""The one-time import of the TOML work log into money.db.

The import is the only way existing work and invoices reach the database, and
it only goes ahead when what it wrote reads back identical to what it parsed.
Every refusal and failure here is checked for the same three things: no rows
in any work table, the ``work_store`` key unset, and every year file
byte-identical with the same mtime.
"""

import json
import logging
import re
import tomllib
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from istota.money import config_store, db, work
from istota.money.work_migrate import WORK_STORE_KEY, load_legacy, migrate_work_log

CONFIG_TOML = """
accounting_path = "."
invoice_output = "invoices/generated"
next_invoice_number = 10
default_entity = "personal"

[companies.personal]
name = "Me"

[companies.llc]
name = "Me LLC"

[clients.acme]
name = "Acme Corp"
entity = "llc"

[clients.beta]
name = "Beta Inc"

[services.dev]
display_name = "Development"
rate = 150
type = "hours"

[services.hosting]
display_name = "Hosting"
rate = 50
type = "flat"

[services.expenses]
display_name = "Expenses"
rate = 0
type = "other"
"""

YEAR_2025 = """\
[[entries]]
uid = "u1"
date = 2025-11-03
client = "acme"
service = "dev"
qty = 8
description = "Build the importer"
invoice = "INV-000001"
invoice_date = 2025-12-01
paid_date = 2025-12-20
ticket = 42

[[entries]]
uid = "u2"
date = 2025-11-20
client = "acme"
service = "hosting"
invoice = "INV-000001"
invoice_date = 2025-12-01
paid_date = 2025-12-20

[[entries]]
uid = "u3"
date = 2025-12-05
client = "beta"
service = "dev"
qty = 2.5
discount = 25
description = "Review"
invoice = "INV-000002"
"""

YEAR_2026 = """\
[[entries]]
uid = "u4"
date = 2026-01-10
client = "beta"
service = "_manual"
amount = 300
description = "Setup fee"
invoice = "INV-000003"
invoice_date = 2026-01-10

[[entries]]
uid = "u5"
date = 2026-01-15
client = "acme"
service = "expenses"
amount = 120.5
entity = "personal"
tags = ["travel", "q1"]
approved_on = 2026-01-16
"my key" = "kept"

[[entries]]
uid = "u6"
date = 2026-01-15
client = "acme"
service = "dev"
qty = 3

[entries.meta]
source = "hand"
"""


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


@pytest.fixture
def db_path(data_dir):
    path = data_dir / "data" / "money.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    config_store.init_db(path)
    cfg = config_store.invoicing_config_from_toml_dict(tomllib.loads(CONFIG_TOML))
    config_store.save_invoicing(path, cfg, replace_collections=True)
    return path


def _write_years(data_dir, years: dict[int, str]):
    wd = data_dir / "invoices" / "work"
    wd.mkdir(parents=True, exist_ok=True)
    for year, text in years.items():
        (wd / f"{year}.toml").write_text(text)


@pytest.fixture
def fixture_log(data_dir, db_path):
    _write_years(data_dir, {2025: YEAR_2025, 2026: YEAR_2026})
    pdf = data_dir / "invoices" / "generated" / "2025" / "Invoice-000001-12_01_2025.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4")
    return pdf


def _files(data_dir):
    """Every year file's bytes and mtime. The ``.work.lock`` anchor beside them
    is the old store's own flock file and carries no data."""
    wd = data_dir / "invoices" / "work"
    return {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(wd.glob("*.toml"))
    } if wd.is_dir() else {}


def _row_counts(db_path):
    db.init_db(db_path)
    with db.get_db(db_path) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("work_entries", "invoices", "invoice_lines")
        }


def _key(db_path):
    with db.get_db(db_path) as conn:
        return db.kv_get(conn, WORK_STORE_KEY)


def _set_aside(data_dir):
    """Every renamed-aside year file's bytes and mtime, by name."""
    wd = data_dir / "invoices" / "work"
    return {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(wd.glob("*.toml.migrated*"))
    } if wd.is_dir() else {}


def _assert_untouched(data_dir, db_path, before):
    assert _row_counts(db_path) == {"work_entries": 0, "invoices": 0, "invoice_lines": 0}
    assert _key(db_path) is None
    assert _files(data_dir) == before
    assert _set_aside(data_dir) == {}


def _dump(db_path):
    with db.get_db(db_path) as conn:
        return {
            table: [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
            for table in ("work_entries", "invoices", "invoice_lines")
        }


class TestCleanImport:
    def test_reports_what_it_imported(self, data_dir, db_path, fixture_log):
        report = migrate_work_log(data_dir)
        assert report.status == "migrated"
        assert (report.entries, report.invoices, report.years) == (6, 3, ["2025", "2026"])

    def test_every_invoice_field(self, data_dir, db_path, fixture_log):
        migrate_work_log(data_dir)
        with db.get_db(db_path) as conn:
            rows = {r["number"]: dict(r) for r in conn.execute("SELECT * FROM invoices")}
            lines = sorted(tuple(r) for r in conn.execute(
                "SELECT invoice_number, entry_uid, active FROM invoice_lines"))

        assert set(rows) == {"INV-000001", "INV-000002", "INV-000003"}
        paid = rows["INV-000001"]
        assert (paid["client"], paid["entity"], paid["issue_date"]) == ("acme", "llc", "2025-12-01")
        assert paid["total"] == 1250.0  # 8h at 150 plus flat hosting at 50
        assert (paid["status"], paid["paid_date"]) == ("paid", "2025-12-20")
        assert paid["pdf_path"] == str(fixture_log)
        assert paid["manual"] == 0
        assert (paid["voided_at"], paid["was_paid"], paid["void_reason"]) == (None, 0, "")
        assert paid["replaced_by"] == "[]"

        legacy = rows["INV-000002"]
        # No invoice_date stamp: issued no earlier than the last work on it.
        assert legacy["issue_date"] == "2025-12-05"
        assert legacy["total"] == 350.0  # 2.5h at 150 less a 25 discount
        assert (legacy["client"], legacy["entity"]) == ("beta", "personal")
        assert (legacy["status"], legacy["paid_date"], legacy["pdf_path"]) == ("open", None, "")

        manual = rows["INV-000003"]
        assert (manual["total"], manual["manual"], manual["issue_date"]) == (300.0, 1, "2026-01-10")

        assert lines == [
            ("INV-000001", "u1", 1), ("INV-000001", "u2", 1),
            ("INV-000002", "u3", 1), ("INV-000003", "u4", 1),
        ]

    def test_every_entry_field(self, data_dir, db_path, fixture_log):
        migrate_work_log(data_dir)
        with db.get_db(db_path) as conn:
            rows = {r["uid"]: dict(r) for r in conn.execute("SELECT * FROM work_entries")}

        u3 = rows["u3"]
        assert (u3["date"], u3["client"], u3["service"]) == ("2025-12-05", "beta", "dev")
        assert (u3["qty"], u3["amount"], u3["discount"]) == (2.5, None, 25)
        assert (u3["description"], u3["entity"], u3["separate"]) == ("Review", "", 0)
        assert json.loads(u3["extra"]) == {}

        u5 = rows["u5"]
        assert (u5["qty"], u5["amount"], u5["entity"]) == (None, 120.5, "personal")
        assert json.loads(u5["extra"]) == {
            "approved_on": {"$date": "2026-01-16"},
            "my key": "kept",
            "tags": ["travel", "q1"],
        }
        assert json.loads(rows["u1"]["extra"]) == {"ticket": 42}
        assert json.loads(rows["u6"]["extra"]) == {"meta": {"source": "hand"}}

    def test_entries_read_back_equal_to_the_parsed_ones(self, data_dir, db_path, fixture_log):
        parsed = load_legacy(data_dir).entries
        migrate_work_log(data_dir)
        issue = {r.number: r.issue_date for r in work.list_invoices(data_dir)}

        expected = [
            replace(p, invoice_date=issue[p.invoice] if p.invoice else None, id=None)
            for p in parsed
        ]
        got = [replace(e, id=None) for e in work.load_work_entries(data_dir)]
        assert got == expected
        # Display order is the old one, same-date entries in file order.
        assert [e.uid for e in got] == ["u1", "u2", "u3", "u4", "u5", "u6"]

    def test_etags_of_uninvoiced_entries_are_unchanged(self, data_dir, db_path, fixture_log):
        """The web UI holds etags across the upgrade; an editable entry keeps its own."""
        before = {e.uid: work.entry_etag(e) for e in load_legacy(data_dir).entries if not e.invoice}
        migrate_work_log(data_dir)
        after = {e.uid: work.entry_etag(e) for e in work.load_work_entries(data_dir) if not e.invoice}
        assert after == before

    def test_the_year_files_are_renamed_aside_unchanged(self, data_dir, db_path, fixture_log):
        before = _files(data_dir)
        migrate_work_log(data_dir)
        assert _files(data_dir) == {}
        assert _set_aside(data_dir) == {
            f"{name}.migrated": content for name, content in before.items()
        }

    def test_records_the_key_with_the_source_hashes(self, data_dir, db_path, fixture_log):
        import hashlib

        migrate_work_log(data_dir)
        stored = json.loads(_key(db_path))
        assert stored["migrated_at"]
        assert stored["source_hashes"] == {
            year: hashlib.sha256(text.encode()).hexdigest()
            for year, text in (("2025", YEAR_2025), ("2026", YEAR_2026))
        }

    def test_without_invoicing_config_totals_only_manual_amounts(self, data_dir):
        _write_years(data_dir, {2026: YEAR_2026})
        report = migrate_work_log(data_dir)
        assert report.status == "migrated"
        (manual,) = work.list_invoices(data_dir)
        assert manual.total == 300.0
        assert manual.pdf_path == ""


class TestRefusals:
    """Each refusal writes nothing and leaves the year files alone."""

    def _refused(self, data_dir, db_path, match):
        before = _files(data_dir)
        report = migrate_work_log(data_dir)
        assert report.status == "refused"
        assert match in report.reason
        _assert_untouched(data_dir, db_path, before)
        return report

    def test_a_quarantined_year(self, data_dir, db_path, fixture_log):
        (data_dir / "invoices" / "work" / "2027.toml").write_text(
            '[[entries]]\nuid = "bad"\ndate = "not-a-date"\nclient = "acme"\nservice = "dev"\n'
        )
        report = self._refused(data_dir, db_path, "2027.toml")
        assert "uid=bad" in report.reason

    def test_an_entry_without_a_uid(self, data_dir, db_path, fixture_log):
        with (data_dir / "invoices" / "work" / "2026.toml").open("a") as f:
            f.write('\n[[entries]]\ndate = 2026-02-01\nclient = "acme"\nservice = "dev"\n')
        report = self._refused(data_dir, db_path, "no uid")
        assert "run `istota money work backfill-ids -u USER` first" in report.reason

    def test_a_paid_date_without_an_invoice(self, data_dir, db_path, fixture_log):
        with (data_dir / "invoices" / "work" / "2026.toml").open("a") as f:
            f.write(
                '\n[[entries]]\nuid = "u9"\ndate = 2026-02-01\nclient = "acme"\n'
                'service = "dev"\npaid_date = 2026-02-10\n'
            )
        report = self._refused(data_dir, db_path, "paid_date set but no invoice on u9")
        assert "remove the paid_date or add the invoice number" in report.reason

    def test_a_uid_shared_across_years(self, data_dir, db_path, fixture_log):
        with (data_dir / "invoices" / "work" / "2026.toml").open("a") as f:
            f.write('\n[[entries]]\nuid = "u1"\ndate = 2026-02-01\nclient = "acme"\nservice = "dev"\n')
        self._refused(data_dir, db_path, "uid u1 appears on more than one entry")

    def test_entries_on_one_invoice_disagreeing_on_paid_date(self, data_dir, db_path):
        _write_years(data_dir, {2025: YEAR_2025.replace(
            'invoice_date = 2025-12-01\npaid_date = 2025-12-20\n\n[[entries]]\nuid = "u3"',
            'invoice_date = 2025-12-01\n\n[[entries]]\nuid = "u3"',
        )})
        assert "u2" in (data_dir / "invoices" / "work" / "2025.toml").read_text()
        self._refused(data_dir, db_path, "INV-000001 disagree on paid_date")

    def test_a_refusal_blocks_every_work_function(self, data_dir, db_path, fixture_log):
        with (data_dir / "invoices" / "work" / "2026.toml").open("a") as f:
            f.write('\n[[entries]]\ndate = 2026-02-01\nclient = "acme"\nservice = "dev"\n')
        before = _files(data_dir)
        with pytest.raises(work.WorkStoreBlocked, match="^work log migration blocked: .*no uid"):
            work.add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1)
        _assert_untouched(data_dir, db_path, before)


class TestVerification:
    """What reads back must equal what was parsed, or nothing is kept."""

    def test_a_dropped_entry_field_rolls_back(self, data_dir, db_path, fixture_log, monkeypatch):
        original = work.insert_entry

        def lossy(conn, entry, **kwargs):
            if entry.uid == "u5":
                entry = replace(entry, extra={})
            return original(conn, entry, **kwargs)

        monkeypatch.setattr(work, "insert_entry", lossy)
        before = _files(data_dir)
        report = migrate_work_log(data_dir)

        assert report.status == "failed"
        assert report.reason.startswith("verification failed: entry u5 field extra")
        _assert_untouched(data_dir, db_path, before)

    def test_an_altered_entry_field_rolls_back(self, data_dir, db_path, fixture_log, monkeypatch):
        original = work.insert_entry

        def lossy(conn, entry, **kwargs):
            if entry.uid == "u3":
                entry = replace(entry, discount=0)
            return original(conn, entry, **kwargs)

        monkeypatch.setattr(work, "insert_entry", lossy)
        before = _files(data_dir)
        report = migrate_work_log(data_dir)

        assert report.status == "failed"
        assert "entry u3 field discount" in report.reason
        _assert_untouched(data_dir, db_path, before)

    def test_an_altered_invoice_field_rolls_back(self, data_dir, db_path, fixture_log, monkeypatch):
        original = work.create_invoice

        def lossy(conn, **kwargs):
            if kwargs["number"] == "INV-000002":
                kwargs["entity"] = "llc"
            return original(conn, **kwargs)

        monkeypatch.setattr(work, "create_invoice", lossy)
        before = _files(data_dir)
        report = migrate_work_log(data_dir)

        assert report.status == "failed"
        assert "invoice INV-000002 field entity" in report.reason
        _assert_untouched(data_dir, db_path, before)

    def test_a_missing_invoice_line_rolls_back(self, data_dir, db_path, fixture_log, monkeypatch):
        original = work.create_invoice

        def lossy(conn, **kwargs):
            if kwargs["number"] == "INV-000001":
                kwargs["entry_uids"] = kwargs["entry_uids"][:1]
            return original(conn, **kwargs)

        monkeypatch.setattr(work, "create_invoice", lossy)
        before = _files(data_dir)
        report = migrate_work_log(data_dir)

        assert report.status == "failed"
        assert "verification failed: entry u2 field invoice" in report.reason
        _assert_untouched(data_dir, db_path, before)

    def test_the_import_succeeds_once_the_writer_is_right(self, data_dir, db_path, fixture_log, monkeypatch):
        monkeypatch.setattr(work, "insert_entry", lambda conn, entry, **k: None)
        assert migrate_work_log(data_dir).status == "failed"
        monkeypatch.undo()
        assert migrate_work_log(data_dir).status == "migrated"
        assert _row_counts(db_path)["work_entries"] == 6


class TestFailureMidWrite:
    def test_an_exception_after_some_rows_rolls_back_completely(
        self, data_dir, db_path, fixture_log, monkeypatch,
    ):
        original = work.create_invoice
        calls = []

        def failing(conn, **kwargs):
            calls.append(kwargs["number"])
            if len(calls) == 2:
                raise OSError("disk full")
            return original(conn, **kwargs)

        monkeypatch.setattr(work, "create_invoice", failing)
        before = _files(data_dir)
        report = migrate_work_log(data_dir)

        assert calls == ["INV-000001", "INV-000002"]
        assert report.status == "failed"
        assert report.reason == "write failed: disk full"
        _assert_untouched(data_dir, db_path, before)


class TestDryRun:
    def test_verifies_reports_and_writes_nothing(self, data_dir, db_path, fixture_log):
        before = _files(data_dir)
        report = migrate_work_log(data_dir, dry_run=True)
        assert report.status == "dry_run"
        assert (report.entries, report.invoices) == (6, 3)
        _assert_untouched(data_dir, db_path, before)
        assert sorted(before) == ["2025.toml", "2026.toml"]

    def test_reports_a_refusal(self, data_dir, db_path, fixture_log):
        (data_dir / "invoices" / "work" / "2026.toml").write_text(
            '[[entries]]\ndate = 2026-02-01\nclient = "acme"\nservice = "dev"\n'
        )
        report = migrate_work_log(data_dir, dry_run=True)
        assert report.status == "refused"


class TestIdempotence:
    def test_a_second_run_does_nothing(self, data_dir, db_path, fixture_log):
        migrate_work_log(data_dir)
        rows, key, files = _dump(db_path), _key(db_path), _files(data_dir)

        # Even a changed year file is not read again once the key is set.
        (data_dir / "invoices" / "work" / "2026.toml").write_text("")
        files = _files(data_dir)
        report = migrate_work_log(data_dir)

        assert report.status == "already"
        assert _dump(db_path) == rows
        assert _key(db_path) == key
        assert _files(data_dir) == files

    def test_no_year_files_sets_the_key_on_an_empty_store(self, data_dir, db_path):
        report = migrate_work_log(data_dir)
        assert report.status == "migrated"
        assert report.entries == 0
        assert json.loads(_key(db_path))["source_hashes"] == {}
        assert _row_counts(db_path) == {"work_entries": 0, "invoices": 0, "invoice_lines": 0}
        assert not (data_dir / "invoices").exists()

    def test_the_first_work_call_runs_it(self, data_dir, db_path, fixture_log):
        assert len(work.load_work_entries(data_dir)) == 6
        assert _key(db_path) is not None
        assert migrate_work_log(data_dir).status == "already"


class TestLegacyLoader:
    """The old loader's coercions carry over into the import."""

    def _import_one(self, data_dir, lines):
        _write_years(data_dir, {2026: "[[entries]]\n" + "\n".join(lines) + "\n"})
        report = migrate_work_log(data_dir)
        assert report.status == "migrated", report.reason
        (entry,) = work.load_work_entries(data_dir)
        return entry

    def test_quoted_date_is_coerced(self, data_dir):
        e = self._import_one(data_dir, [
            'uid = "a"', 'date = "2026-01-05"', 'client = "acme"', 'service = "dev"'])
        assert e.date == date(2026, 1, 5)

    def test_datetime_date_is_narrowed(self, data_dir):
        e = self._import_one(data_dir, [
            'uid = "a"', 'date = 2026-01-05T09:30:00', 'client = "acme"', 'service = "dev"'])
        assert e.date == date(2026, 1, 5)

    def test_quoted_numbers_are_coerced(self, data_dir):
        e = self._import_one(data_dir, [
            'uid = "a"', 'date = 2026-01-05', 'client = "acme"', 'service = "dev"',
            'qty = "8"', 'amount = "500.50"', 'discount = "10"'])
        assert (e.qty, e.amount, e.discount) == (8.0, 500.5, 10.0)

    def test_an_unusable_number_is_dropped_but_the_entry_survives(self, data_dir):
        e = self._import_one(data_dir, [
            'uid = "a"', 'date = 2026-01-05', 'client = "acme"', 'service = "dev"',
            'qty = "eight"'])
        assert e.qty is None

    def test_quoted_and_dotted_extra_keys_stay_flat(self, data_dir):
        e = self._import_one(data_dir, [
            'uid = "a"', 'date = 2026-01-05', 'client = "acme"', 'service = "dev"',
            '"my key" = "hello"', '"a.b" = 1'])
        assert e.extra == {"my key": "hello", "a.b": 1}

    def test_quoted_paid_date_is_coerced(self, data_dir):
        e = self._import_one(data_dir, [
            'uid = "a"', 'date = 2026-01-05', 'client = "acme"', 'service = "dev"',
            'invoice = "INV-000001"', 'paid_date = "2026-02-01"'])
        assert e.paid_date == date(2026, 2, 1)
        assert work.list_invoices(data_dir)[0].status == "paid"


class TestCli:
    def _obj(self, data_dir, db_path):
        from istota.money.cli import Context, UserContext

        obj = Context()
        obj.users["default"] = UserContext(data_dir=data_dir, db_path=db_path)
        obj.activate_user("default")
        return obj

    def _invoke(self, data_dir, db_path, *args):
        from istota.money.cli import cli

        result = CliRunner().invoke(cli, ["-u", "default", *args], obj=self._obj(data_dir, db_path))
        return result.exit_code, json.loads(result.output)

    def test_migrate_dry_run_then_for_real(self, data_dir, db_path, fixture_log):
        code, out = self._invoke(data_dir, db_path, "work", "migrate", "--dry-run")
        assert code == 0
        assert out["migration"]["status"] == "dry_run"
        assert _key(db_path) is None

        code, out = self._invoke(data_dir, db_path, "work", "migrate")
        assert code == 0
        assert out["migration"]["status"] == "migrated"
        code, out = self._invoke(data_dir, db_path, "work", "list")
        assert out["count"] == 6

    def test_a_blocked_import_is_the_error_of_work_and_invoice_commands(
        self, data_dir, db_path, fixture_log,
    ):
        with (data_dir / "invoices" / "work" / "2026.toml").open("a") as f:
            f.write('\n[[entries]]\ndate = 2026-02-01\nclient = "acme"\nservice = "dev"\n')
        before = _files(data_dir)

        for args in (("work", "list"), ("invoice", "list"), ("invoice", "generate")):
            code, out = self._invoke(data_dir, db_path, *args)
            assert code == 1
            assert out["status"] == "error"
            assert out["error"].startswith("work log migration blocked: ")
        _assert_untouched(data_dir, db_path, before)

        code, out = self._invoke(data_dir, db_path, "work", "backfill-ids")
        assert (code, out["stamped"]) == (0, 1)
        code, out = self._invoke(data_dir, db_path, "work", "list")
        assert out["count"] == 7


def _block(data_dir):
    """Append an entry with no uid: the import refuses the log."""
    with (data_dir / "invoices" / "work" / "2026.toml").open("a") as f:
        f.write('\n[[entries]]\ndate = 2026-02-01\nclient = "acme"\nservice = "dev"\n')


class TestRenameAside:
    """After a committed import the year files are renamed, never read again."""

    def test_an_existing_migrated_name_gets_a_stamp(self, data_dir, db_path, fixture_log):
        wd = data_dir / "invoices" / "work"
        (wd / "2025.toml.migrated").write_text("an older copy\n")
        before = _files(data_dir)

        assert migrate_work_log(data_dir).status == "migrated"

        aside = _set_aside(data_dir)
        assert aside["2025.toml.migrated"][0] == b"an older copy\n"
        assert aside["2026.toml.migrated"] == before["2026.toml"]
        (stamped,) = [n for n in aside if n.startswith("2025.toml.migrated-")]
        assert re.fullmatch(r"2025\.toml\.migrated-\d{8}T\d{6}Z", stamped)
        assert aside[stamped] == before["2025.toml"]

    def test_a_failed_rename_keeps_the_import_and_the_next_open_retries(
        self, data_dir, db_path, fixture_log, monkeypatch, caplog,
    ):
        before = _files(data_dir)

        def refuse(self, target):
            raise PermissionError("read-only mount")

        monkeypatch.setattr(Path, "rename", refuse)
        with caplog.at_level(logging.ERROR, logger="istota.money.work_migrate"):
            report = migrate_work_log(data_dir)

        assert report.status == "migrated"
        assert _key(db_path) is not None
        assert _row_counts(db_path)["work_entries"] == 6
        assert _files(data_dir) == before
        failed = [r for r in caplog.records if "work_year_file_rename_failed" in r.getMessage()]
        assert len(failed) == 2
        assert all(r.levelno == logging.ERROR for r in failed)

        monkeypatch.undo()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="istota.money.work_migrate"):
            assert len(work.load_work_entries(data_dir)) == 6

        assert _files(data_dir) == {}
        assert _set_aside(data_dir) == {
            f"{name}.migrated": content for name, content in before.items()
        }
        # The import's own files: a retry, not a reappearance.
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_a_reappearing_year_file_is_moved_aside_unread(
        self, data_dir, db_path, fixture_log, caplog,
    ):
        migrate_work_log(data_dir)
        rows = _dump(db_path)
        returned = data_dir / "invoices" / "work" / "2026.toml"
        returned.write_text(
            '[[entries]]\nuid = "ghost"\ndate = 2026-03-01\nclient = "acme"\n'
            'service = "dev"\nqty = 9\n'
        )

        with caplog.at_level(logging.WARNING, logger="istota.money.work_migrate"):
            entries = work.load_work_entries(data_dir)

        assert "ghost" not in {e.uid for e in entries}
        assert _dump(db_path) == rows
        assert not returned.exists()
        stamped = [n for n in _set_aside(data_dir) if n.startswith("2026.toml.migrated-")]
        assert len(stamped) == 1
        (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert "work_year_file_reappeared" in warning.getMessage()
        assert str(returned) in warning.getMessage()

    def test_a_refused_import_renames_nothing(self, data_dir, db_path, fixture_log):
        _block(data_dir)
        before = _files(data_dir)
        with pytest.raises(work.WorkStoreBlocked):
            work.load_work_entries(data_dir)
        _assert_untouched(data_dir, db_path, before)


class TestBlockedIsReportedOnce:
    """A refusal is kept per process until a year file changes."""

    def test_a_second_open_does_not_parse_again(
        self, data_dir, db_path, fixture_log, monkeypatch, caplog,
    ):
        from istota.money import work_migrate

        _block(data_dir)
        parses = []
        original = work_migrate.load_legacy

        def counting(d):
            parses.append(d)
            return original(d)

        monkeypatch.setattr(work_migrate, "load_legacy", counting)

        with caplog.at_level(logging.WARNING, logger="istota.money"):
            for _ in range(3):
                with pytest.raises(work.WorkStoreBlocked, match="no uid") as raised:
                    work.load_work_entries(data_dir)

        assert len(parses) == 1
        assert raised.value.repeated is True
        refused = [r for r in caplog.records if "work_log_migration_refused" in r.getMessage()]
        assert len(refused) == 1

    def test_a_failed_import_is_not_retried_on_every_open(
        self, data_dir, db_path, fixture_log, monkeypatch,
    ):
        from istota.money import work_migrate

        monkeypatch.setattr(work, "insert_entry", lambda conn, entry, **k: None)
        parses = []
        original = work_migrate.load_legacy

        def counting(d):
            parses.append(d)
            return original(d)

        monkeypatch.setattr(work_migrate, "load_legacy", counting)
        for _ in range(3):
            with pytest.raises(work.WorkStoreBlocked, match="write failed") as raised:
                work.load_work_entries(data_dir)

        assert len(parses) == 1
        assert raised.value.repeated is True

    def test_fixing_the_file_runs_the_import_again(self, data_dir, db_path, fixture_log):
        _block(data_dir)
        with pytest.raises(work.WorkStoreBlocked):
            work.load_work_entries(data_dir)
        assert work.backfill_work_ids(data_dir) == 1
        assert len(work.load_work_entries(data_dir)) == 7

    def test_the_per_request_hook_warns_once(self, data_dir, db_path, fixture_log, caplog):
        from istota.money._migrate import import_work_log
        from istota.money.cli import UserContext

        _block(data_dir)
        ctx = UserContext(data_dir=data_dir, db_path=db_path)
        with caplog.at_level(logging.DEBUG, logger="istota.money._migrate"):
            import_work_log(ctx)
            import_work_log(ctx)
        blocked = [r for r in caplog.records if "money_work_log_import_blocked" in r.getMessage()]
        assert [r.levelno for r in blocked] == [logging.WARNING, logging.DEBUG]


class TestRestoredDatabase:
    """A money.db lost or restored from before the import must not import an empty log."""

    def _lose_the_database(self, db_path):
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)

    def test_set_aside_files_without_the_key_refuse(self, data_dir, db_path, fixture_log):
        assert migrate_work_log(data_dir).status == "migrated"
        aside = _set_aside(data_dir)
        self._lose_the_database(db_path)

        with pytest.raises(work.WorkStoreBlocked, match="restored from a backup") as raised:
            work.ensure_store(data_dir)

        message = str(raised.value)
        assert "2025.toml.migrated" in message
        assert "back to {year}.toml" in message
        assert "`istota money work migrate -u USER`" in message
        assert _key(db_path) is None
        assert _row_counts(db_path)["work_entries"] == 0
        assert _set_aside(data_dir) == aside

    def test_renaming_the_files_back_recovers(self, data_dir, db_path, fixture_log):
        assert migrate_work_log(data_dir).status == "migrated"
        self._lose_the_database(db_path)
        with pytest.raises(work.WorkStoreBlocked):
            work.ensure_store(data_dir)

        for path in (data_dir / "invoices" / "work").glob("*.toml.migrated"):
            path.rename(path.with_name(path.name.removesuffix(".migrated")))

        assert len(work.load_work_entries(data_dir)) == 6
        assert _key(db_path) is not None

    def test_one_year_back_and_one_still_aside_refuses(self, data_dir, db_path, fixture_log):
        assert migrate_work_log(data_dir).status == "migrated"
        self._lose_the_database(db_path)
        wd = data_dir / "invoices" / "work"
        (wd / "2026.toml.migrated").rename(wd / "2026.toml")

        with pytest.raises(work.WorkStoreBlocked, match="2025.toml.migrated"):
            work.ensure_store(data_dir)
        assert _row_counts(db_path)["work_entries"] == 0

    def test_a_copy_that_came_back_is_not_evidence(self, data_dir, db_path):
        _write_years(data_dir, {2026: YEAR_2026})
        wd = data_dir / "invoices" / "work"
        (wd / "2025.toml.migrated-20260101T000000Z").write_text(YEAR_2025)

        assert migrate_work_log(data_dir).status == "migrated"


class TestBlockedRoutes:
    """While blocked, the web work and invoice routes answer 503 with the CLI's text."""

    @pytest.fixture
    def blocked_client(self, data_dir, db_path, fixture_log):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from istota.money.cli import UserContext
        from istota.money.routes import get_user_config, require_auth, router, verify_origin

        _block(data_dir)
        db.init_db(db_path)  # what ensure_initialised does before any route runs
        ctx = UserContext(data_dir=data_dir, ledgers=[], db_path=db_path)
        app = FastAPI()
        app.include_router(router, prefix="/api/money")
        app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
        app.dependency_overrides[get_user_config] = lambda: ctx
        app.dependency_overrides[verify_origin] = lambda: None
        return TestClient(app, raise_server_exceptions=False)

    @pytest.mark.parametrize("method,path,body", [
        ("get", "/api/money/work", None),
        ("post", "/api/money/work",
         {"date": "2026-03-01", "client": "acme", "service": "dev", "qty": 1}),
        ("patch", "/api/money/work/u6", {"qty": 2}),
        ("delete", "/api/money/work/u6", None),
        ("get", "/api/money/invoices", None),
        ("get", "/api/money/invoice-details?invoice_number=INV-000002", None),
        ("post", "/api/money/invoices/INV-000002/mark-paid", {"paid_date": "2026-03-02"}),
        ("post", "/api/money/invoices/INV-000001/mark-pending", None),
        ("post", "/api/money/invoices/generate", {"dry_run": False}),
        ("post", "/api/money/invoices/INV-000001/void", {"force": True}),
        ("post", "/api/money/invoices",
         {"client": "acme", "items": [{"description": "x", "amount": 1}]}),
    ])
    def test_answers_503_and_writes_nothing(
        self, data_dir, db_path, blocked_client, method, path, body,
    ):
        before = _files(data_dir)
        kwargs = {"json": body} if body is not None else {}
        resp = getattr(blocked_client, method)(path, **kwargs)

        assert resp.status_code == 503
        payload = resp.json()
        assert payload["status"] == "error"
        assert payload["error"].startswith("work log migration blocked: ")
        assert "no uid" in payload["error"]
        _assert_untouched(data_dir, db_path, before)


class TestFixtureThroughTheCli:
    """The multi-year fixture end to end through the CLI: import, then read back.

    ``tests/money/fixtures/`` holds no work log, so this fixture stands in.
    """

    def test_work_and_invoice_lists_match_the_fixture(self, data_dir, db_path, fixture_log):
        cli = TestCli()
        code, out = cli._invoke(data_dir, db_path, "work", "migrate")
        assert (code, out["migration"]["status"]) == (0, "migrated")
        assert set(_set_aside(data_dir)) == {"2025.toml.migrated", "2026.toml.migrated"}

        code, out = cli._invoke(data_dir, db_path, "work", "list")
        assert (code, out["count"]) == (0, 6)

        code, out = cli._invoke(data_dir, db_path, "invoice", "list", "--all")
        assert (code, out["status"]) == (0, "ok")
        listed = json.dumps(out)
        for number in ("INV-000001", "INV-000002", "INV-000003"):
            assert number in listed

        code, out = cli._invoke(data_dir, db_path, "work", "migrate")
        assert (code, out["migration"]["status"]) == (0, "already")
