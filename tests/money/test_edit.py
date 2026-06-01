"""Tests for money.core.edit — stable IDs, ledger lock, backfill, edit."""

from __future__ import annotations

import threading
import time
from datetime import date
from pathlib import Path


from istota.money.core.edit import (
    _ledger_lock,
    backfill_ledger_ids,
    new_txn_id,
)
from istota.money.core.transactions import format_beancount_transaction


def _write_ledger(tmp_path: Path) -> Path:
    """A multi-file include ledger: root + transactions/2024.beancount."""
    (tmp_path / "transactions").mkdir()
    root = tmp_path / "main.beancount"
    root.write_text(
        '2024-01-01 open Assets:Bank:Checking\n'
        '2024-01-01 open Expenses:Food:Coffee\n'
        '2024-01-01 open Expenses:Food:Groceries\n'
        'include "transactions/2024.beancount"\n'
        '\n'
        '2024-02-01 * "Acme" "Coffee"\n'
        '  ; an inline comment\n'
        '  Expenses:Food:Coffee   5.00 USD\n'
        '  Assets:Bank:Checking\n'
    )
    (tmp_path / "transactions" / "2024.beancount").write_text(
        '2024-03-01 * "Market" "Groceries" #food\n'
        '  Expenses:Food:Groceries   20.00 USD\n'
        '  Assets:Bank:Checking\n'
        '\n'
        '2024-03-05 * "Already" "Has id"\n'
        '  id: "preexisting"\n'
        '  Expenses:Food:Coffee   3.00 USD\n'
        '  Assets:Bank:Checking\n'
    )
    return root


def _txn_ids(ledger_path: Path) -> dict:
    from beancount.core.data import Transaction
    from beancount.loader import load_file

    entries, _errors, _ = load_file(str(ledger_path))
    out = {}
    for e in entries:
        if isinstance(e, Transaction):
            out[(e.payee, e.narration)] = e.meta.get("id")
    return out


class TestNewTxnId:
    def test_returns_hex_string(self):
        tid = new_txn_id()
        assert isinstance(tid, str)
        assert len(tid) >= 12
        int(tid, 16)  # hex-parseable

    def test_unique(self):
        ids = {new_txn_id() for _ in range(1000)}
        assert len(ids) == 1000


class TestFormatBeancountMetadata:
    def test_no_metadata_unchanged(self):
        result = format_beancount_transaction(
            txn_date=date(2024, 2, 1),
            payee="Acme",
            narration="Coffee",
            posting_account="Expenses:Food:Coffee",
            contra_account="Assets:Bank:Checking",
            amount=-5.0,
        )
        assert "id:" not in result
        lines = result.split("\n")
        assert lines[0] == '2024-02-01 * "Acme" "Coffee"'

    def test_metadata_after_header_before_postings(self):
        result = format_beancount_transaction(
            txn_date=date(2024, 2, 1),
            payee="Acme",
            narration="Coffee",
            posting_account="Expenses:Food:Coffee",
            contra_account="Assets:Bank:Checking",
            amount=-5.0,
            metadata={"id": "abc123", "monarch-id": "m-999"},
        )
        lines = result.split("\n")
        assert lines[0] == '2024-02-01 * "Acme" "Coffee"'
        assert lines[1] == '  id: "abc123"'
        assert lines[2] == '  monarch-id: "m-999"'
        # postings come after metadata
        assert any(ln.strip().startswith("Expenses:Food:Coffee") for ln in lines[3:])

    def test_metadata_parses_with_beancount(self):
        from beancount.parser import parser

        entry_text = format_beancount_transaction(
            txn_date=date(2024, 2, 1),
            payee='Quote "Inside"',
            narration="Coffee",
            posting_account="Expenses:Food:Coffee",
            contra_account="Assets:Bank:Checking",
            amount=12.5,
            metadata={"id": "abc123"},
        )
        preamble = (
            "2024-01-01 open Assets:Bank:Checking\n"
            "2024-01-01 open Expenses:Food:Coffee\n\n"
        )
        entries, errors, _ = parser.parse_string(preamble + entry_text + "\n")
        assert not errors
        txn = [e for e in entries if hasattr(e, "postings")][0]
        assert txn.meta.get("id") == "abc123"


class TestLedgerLock:
    def test_serializes_writers(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("; ledger\n")
        order: list[str] = []

        def worker(name: str, hold: float):
            with _ledger_lock(ledger):
                order.append(f"{name}-enter")
                time.sleep(hold)
                order.append(f"{name}-exit")

        t1 = threading.Thread(target=worker, args=("a", 0.2))
        t1.start()
        time.sleep(0.05)
        t2 = threading.Thread(target=worker, args=("b", 0.0))
        t2.start()
        t1.join()
        t2.join()

        # b must not enter until a has exited (no interleaving).
        assert order == ["a-enter", "a-exit", "b-enter", "b-exit"]

    def test_lock_file_is_sibling_not_ledger(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("; ledger\n")
        with _ledger_lock(ledger):
            assert (tmp_path / ".ledger.lock").exists()
        # ledger file content untouched by locking
        assert ledger.read_text() == "; ledger\n"


class TestBackfillLedgerIds:
    def test_stamps_all_missing_ids(self, tmp_path):
        root = _write_ledger(tmp_path)
        result = backfill_ledger_ids(root)
        assert result["status"] == "ok"
        # 2 of 3 transactions lacked an id (one pre-existing).
        assert result["stamped"] == 2

        ids = _txn_ids(root)
        # Every transaction now has an id.
        assert all(v for v in ids.values()), ids
        # Pre-existing id preserved verbatim.
        assert ids[("Already", "Has id")] == "preexisting"
        # All ids unique.
        vals = list(ids.values())
        assert len(set(vals)) == len(vals)

    def test_ledger_still_valid_after_backfill(self, tmp_path):
        root = _write_ledger(tmp_path)
        backfill_ledger_ids(root)
        from istota.money.core.ledger import run_bean_check

        ok, errors = run_bean_check(root)
        assert ok, errors

    def test_preserves_comments_and_other_lines(self, tmp_path):
        root = _write_ledger(tmp_path)
        backfill_ledger_ids(root)
        text = root.read_text()
        # The inline comment survives.
        assert "; an inline comment" in text
        # The open directives survive.
        assert "2024-01-01 open Assets:Bank:Checking" in text
        assert 'include "transactions/2024.beancount"' in text

    def test_idempotent(self, tmp_path):
        root = _write_ledger(tmp_path)
        backfill_ledger_ids(root)
        first = _txn_ids(root)
        result2 = backfill_ledger_ids(root)
        assert result2["stamped"] == 0
        assert _txn_ids(root) == first

    def test_rollback_on_validation_failure(self, tmp_path, monkeypatch):
        root = _write_ledger(tmp_path)
        before = {
            p: p.read_text()
            for p in (root, tmp_path / "transactions" / "2024.beancount")
        }

        import istota.money.core.edit as edit_mod

        monkeypatch.setattr(
            edit_mod, "run_bean_check", lambda _p: (False, ["boom"])
        )
        result = backfill_ledger_ids(root)
        assert result["status"] == "error"
        assert "boom" in str(result.get("validation_errors", ""))
        # Files restored byte-for-byte.
        for p, original in before.items():
            assert p.read_text() == original


class TestWritersStampIds:
    def test_add_transaction_stamps_id(self, tmp_path):
        from istota.money.core.transactions import add_transaction

        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            "2024-01-01 open Assets:Bank:Checking\n"
            "2024-01-01 open Expenses:Food\n"
        )
        result = add_transaction(
            ledger, date(2024, 2, 1), "Acme", "Coffee",
            "Expenses:Food", "Assets:Bank:Checking", 5.0,
        )
        assert result["status"] == "ok"
        txn_file = (tmp_path / "transactions" / "2024.beancount").read_text()
        assert 'id: "' in txn_file

    def test_recategorization_expense_has_id(self):
        from istota.money.core.transactions import format_recategorization_entry

        out = format_recategorization_entry(
            txn_date=date(2024, 2, 1), merchant="Acme",
            posted_account="Expenses:Biz", contra_account="Assets:Bank",
            amount=50.0,
        )
        assert 'id: "' in out

    def test_recategorization_income_has_id(self):
        from istota.money.core.transactions import format_recategorization_entry

        out = format_recategorization_entry(
            txn_date=date(2024, 2, 1), merchant="Acme",
            posted_account="Income:Consulting", contra_account="Assets:Bank",
            amount=500.0,
        )
        assert out is not None
        assert 'id: "' in out

    def test_category_change_has_id(self):
        from istota.money.core.transactions import format_category_change_entry

        out = format_category_change_entry(
            txn_date=date(2024, 2, 1), merchant="Acme",
            old_account="Expenses:A", new_account="Expenses:B", amount=10.0,
        )
        assert 'id: "' in out

    def test_income_posting_has_id(self):
        from istota.money.core.invoicing import create_income_posting

        out = create_income_posting(
            invoice_number="000123", client_name="Foo",
            income_lines={"Income:Consulting": 100.0}, payment_date=date(2024, 2, 1),
        )
        assert 'id: "' in out

    def test_sync_stamps_id_and_monarch_id(self, tmp_path):

        from istota.money.core.models import (
            MonarchConfig, MonarchCredentials, MonarchSyncSettings,
            MonarchTagFilters,
        )
        from istota.money.core.transactions import sync_monarch

        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            "2024-01-01 open Assets:Bank:Checking\n"
            "2024-01-01 open Expenses:Shopping\n"
        )
        config = MonarchConfig(
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(default_account="Assets:Bank:Checking"),
            accounts={}, categories={}, tags=MonarchTagFilters(),
        )
        txns = [{
            "id": "mon-777", "date": "2024-02-01",
            "merchant": {"name": "Office Store"},
            "category": {"name": "Shopping"},
            "account": {"displayName": "Chase"},
            "amount": -50.0, "notes": "", "tags": [],
        }]
        result = sync_monarch(ledger, config, transactions=txns)
        assert result["status"] == "ok"
        text = ledger.read_text()
        assert 'id: "' in text
        assert 'monarch-id: "mon-777"' in text

    def test_csv_import_stamps_id(self, tmp_path):
        from istota.money.core.transactions import import_csv

        ledger = tmp_path / "main.beancount"
        ledger.write_text("2026-01-01 open Assets:Bank:Checking USD\n")
        csv_file = tmp_path / "export.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Whole Foods,Groceries,Chase,WHOLE FOODS,,-85.50,,Stefan\n"
        )
        result = import_csv(ledger, csv_file, "Assets:Bank:Checking")
        assert result["status"] == "ok"
        assert 'id: "' in ledger.read_text()


def _editable_ledger(tmp_path: Path) -> Path:
    ledger = tmp_path / "main.beancount"
    ledger.write_text(
        "2024-01-01 open Assets:Bank:Checking\n"
        "2024-01-01 open Expenses:Food:Coffee\n"
        "2024-01-01 open Expenses:Food:Restaurants\n"
        "2024-01-01 open Income:Consulting\n"
        "\n"
        '2024-02-01 * "Acme" "Coffee" #personal ^ref1\n'
        '  id: "txn-coffee"\n'
        "  ; a note\n"
        "  Expenses:Food:Coffee   5.00 USD\n"
        "  Assets:Bank:Checking\n"
        "\n"
        '2024-02-02 * "Client" "Payment"\n'
        '  id: "txn-pay"\n'
        "  Assets:Bank:Checking   100.00 USD\n"
        "  Income:Consulting     -100.00 USD\n"
    )
    return ledger


def _block_for_id(ledger_path: Path, txn_id: str) -> str:
    """Return the text block of the transaction carrying ``txn_id``."""
    text = ledger_path.read_text()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if f'id: "{txn_id}"' in line:
            # walk back to header (col-0 date line)
            start = i
            while start > 0 and lines[start][:1].isspace():
                start -= 1
            end = i
            while end + 1 < len(lines) and (
                lines[end + 1][:1].isspace() or lines[end + 1].strip() == ""
            ):
                end += 1
                if lines[end].strip() == "":
                    break
            return "\n".join(lines[start:end + 1])
    return ""


class TestEditTransaction:
    def test_recategorize_account(self, tmp_path):
        from istota.money.core.edit import edit_transaction

        ledger = _editable_ledger(tmp_path)
        result = edit_transaction(
            ledger, "txn-coffee",
            old_account="Expenses:Food:Coffee", old_position="5.00 USD",
            new_account="Expenses:Food:Restaurants",
        )
        assert result["status"] == "ok", result
        block = _block_for_id(ledger, "txn-coffee")
        assert "Expenses:Food:Restaurants" in block
        assert "Expenses:Food:Coffee" not in block
        from istota.money.core.ledger import run_bean_check
        ok, errors = run_bean_check(ledger)
        assert ok, errors

    def test_preserves_id_tags_links_comment_other_postings(self, tmp_path):
        from istota.money.core.edit import edit_transaction

        ledger = _editable_ledger(tmp_path)
        edit_transaction(
            ledger, "txn-coffee",
            old_account="Expenses:Food:Coffee", old_position="5.00 USD",
            new_account="Expenses:Food:Restaurants",
        )
        block = _block_for_id(ledger, "txn-coffee")
        assert 'id: "txn-coffee"' in block
        assert "#personal" in block
        assert "^ref1" in block
        assert "; a note" in block
        assert "Assets:Bank:Checking" in block

    def test_stamps_edited_metadata(self, tmp_path):
        from istota.money.core.edit import edit_transaction

        ledger = _editable_ledger(tmp_path)
        edit_transaction(
            ledger, "txn-coffee",
            old_account="Expenses:Food:Coffee", old_position="5.00 USD",
            new_narration="Morning coffee",
        )
        block = _block_for_id(ledger, "txn-coffee")
        assert "edited:" in block

    def test_edit_payee_narration_date(self, tmp_path):
        from istota.money.core.edit import edit_transaction

        ledger = _editable_ledger(tmp_path)
        result = edit_transaction(
            ledger, "txn-coffee",
            old_account="Expenses:Food:Coffee", old_position="5.00 USD",
            new_payee="Blue Bottle", new_narration="Latte", new_date="2024-02-03",
        )
        assert result["status"] == "ok", result
        block = _block_for_id(ledger, "txn-coffee")
        assert '"Blue Bottle"' in block
        assert '"Latte"' in block
        assert block.startswith("2024-02-03")
        # tags/links preserved across header rewrite
        assert "#personal" in block and "^ref1" in block

    def test_edit_amount_rebalances(self, tmp_path):
        from istota.money.core.edit import edit_transaction
        from istota.money.core.ledger import run_bean_check

        ledger = _editable_ledger(tmp_path)
        result = edit_transaction(
            ledger, "txn-coffee",
            old_account="Expenses:Food:Coffee", old_position="5.00 USD",
            new_position="7.50 USD",
        )
        assert result["status"] == "ok", result
        block = _block_for_id(ledger, "txn-coffee")
        assert "7.50 USD" in block
        ok, errors = run_bean_check(ledger)
        assert ok, errors

    def test_edit_amount_unbalanced_rolls_back(self, tmp_path):
        from istota.money.core.edit import edit_transaction

        ledger = _editable_ledger(tmp_path)
        before = ledger.read_text()
        # txn-pay has two explicit legs; changing one unbalances the entry.
        result = edit_transaction(
            ledger, "txn-pay",
            old_account="Assets:Bank:Checking", old_position="100.00 USD",
            new_position="999.00 USD",
        )
        assert result["status"] == "error"
        assert result.get("validation_errors")
        # File restored byte-for-byte.
        assert ledger.read_text() == before

    def test_id_not_found(self, tmp_path):
        from istota.money.core.edit import edit_transaction

        ledger = _editable_ledger(tmp_path)
        result = edit_transaction(
            ledger, "nonexistent",
            old_account="Expenses:Food:Coffee", old_position="5.00 USD",
            new_account="Expenses:Food:Restaurants",
        )
        assert result["status"] == "error"
        assert "not found" in result["error"].lower()


class TestSyncRespectsEdits:
    def _setup(self, tmp_path: Path, *, edited: bool):
        import sqlite3

        from istota.money.db import init_db, track_monarch_transactions_batch

        ledger = tmp_path / "main.beancount"
        edited_line = '  edited: "2024-03-01"\n' if edited else ""
        ledger.write_text(
            "2024-01-01 open Assets:Bank:Checking\n"
            "2024-01-01 open Expenses:Shopping\n"
            "2024-01-01 open Expenses:Travel\n\n"
            '2024-02-01 * "Store" "Corrected note"\n'
            '  id: "x1"\n'
            '  monarch-id: "m1"\n'
            f"{edited_line}"
            "  Expenses:Shopping   50.00 USD\n"
            "  Assets:Bank:Checking\n"
        )
        db_path = tmp_path / "money.db"
        init_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        track_monarch_transactions_batch(conn, [{
            "id": "m1", "amount": -50.0, "merchant": "Store",
            "posted_account": "Expenses:Shopping",
            "contra_account": "Assets:Bank:Checking",
            "txn_date": "2024-02-01",
        }])
        conn.commit()
        return ledger, conn

    def _config(self):
        from istota.money.core.models import (
            MonarchConfig, MonarchCredentials, MonarchSyncSettings,
            MonarchTagFilters,
        )
        return MonarchConfig(
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(default_account="Assets:Bank:Checking"),
            accounts={}, categories={}, tags=MonarchTagFilters(),
        )

    def _monarch_txn_travel(self):
        # Monarch now reports a different category than the DB-tracked Shopping.
        return [{
            "id": "m1", "date": "2024-02-01", "merchant": {"name": "Store"},
            "category": {"name": "Travel"}, "account": {"displayName": "Chase"},
            "amount": -50.0, "notes": "", "tags": [],
        }]

    def test_edited_entry_not_recategorized(self, tmp_path):
        from istota.money.core.transactions import sync_monarch

        ledger, conn = self._setup(tmp_path, edited=True)
        result = sync_monarch(
            ledger, self._config(), db_conn=conn,
            transactions=self._monarch_txn_travel(),
        )
        assert result["category_changed_count"] == 0
        assert result.get("edited_respected_count") == 1
        assert "Recategorized in Monarch" not in ledger.read_text()

    def test_unedited_entry_still_recategorized(self, tmp_path):
        from istota.money.core.transactions import sync_monarch

        ledger, conn = self._setup(tmp_path, edited=False)
        result = sync_monarch(
            ledger, self._config(), db_conn=conn,
            transactions=self._monarch_txn_travel(),
        )
        # No edited marker → the normal category-change correction still fires.
        assert result["category_changed_count"] == 1
        assert result.get("edited_respected_count", 0) == 0
        assert "Recategorized in Monarch" in ledger.read_text()
