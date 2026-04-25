"""Tests for money.core.dedup module."""

from pathlib import Path

from money.core.dedup import compute_transaction_hash, parse_ledger_transactions


class TestComputeTransactionHash:
    def test_basic_hash(self):
        h = compute_transaction_hash("2026-01-15", 85.50, "Whole Foods")
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256

    def test_deterministic(self):
        h1 = compute_transaction_hash("2026-01-15", 85.50, "Whole Foods")
        h2 = compute_transaction_hash("2026-01-15", 85.50, "Whole Foods")
        assert h1 == h2

    def test_case_insensitive_merchant(self):
        h1 = compute_transaction_hash("2026-01-15", 85.50, "Whole Foods")
        h2 = compute_transaction_hash("2026-01-15", 85.50, "whole foods")
        assert h1 == h2

    def test_strips_whitespace(self):
        h1 = compute_transaction_hash("2026-01-15", 85.50, "Whole Foods")
        h2 = compute_transaction_hash("2026-01-15", 85.50, " Whole Foods ")
        assert h1 == h2

    def test_different_values_different_hash(self):
        h1 = compute_transaction_hash("2026-01-15", 85.50, "Whole Foods")
        h2 = compute_transaction_hash("2026-01-16", 85.50, "Whole Foods")
        assert h1 != h2

    def test_with_account(self):
        h1 = compute_transaction_hash("2026-01-15", 85.50, "Whole Foods")
        h2 = compute_transaction_hash("2026-01-15", 85.50, "Whole Foods", "Assets:Bank")
        assert h1 != h2


class TestParseLedgerTransactions:
    def test_empty_file(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        assert parse_ledger_transactions(ledger) == set()

    def test_nonexistent_file(self, tmp_path):
        ledger = tmp_path / "missing.beancount"
        assert parse_ledger_transactions(ledger) == set()

    def test_extracts_hashes(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-01-15 * "Whole Foods" "Groceries"\n'
            '  Expenses:Food:Groceries  85.50 USD\n'
            '  Assets:Bank:Checking\n'
        )
        hashes = parse_ledger_transactions(ledger)
        assert len(hashes) == 1

        # Should match the hash we'd compute
        expected = compute_transaction_hash("2026-01-15", 85.50, "Whole Foods")
        assert expected in hashes

    def test_scans_imports_dir(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")

        imports_dir = tmp_path / "imports"
        imports_dir.mkdir()
        (imports_dir / "import_001.beancount").write_text(
            '2026-02-01 * "Amazon" "Shopping"\n'
            '  Expenses:Shopping  42.99 USD\n'
            '  Assets:Bank:Checking\n'
        )

        hashes = parse_ledger_transactions(ledger)
        assert len(hashes) == 1
