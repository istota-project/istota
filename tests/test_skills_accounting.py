"""Tests for skills/accounting.py module."""

import csv
import json
import subprocess
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.accounting import (
    MONARCH_CATEGORY_MAP,
    MonarchConfig,
    MonarchCredentials,
    MonarchSyncSettings,
    MonarchTagFilters,
    PurchaseTransaction,
    SaleTransaction,
    _detect_wash_sales,
    _extract_toml_from_markdown,
    _filter_by_tags,
    _format_beancount_transaction,
    _format_category_change_entry,
    _format_recategorization_entry,
    _generate_invoice_html,
    _get_ledger_path,
    _map_monarch_category,
    _parse_monarch_csv,
    _parse_tags,
    _run_bean_check,
    _run_bean_query,
    build_parser,
    cmd_add_transaction,
    cmd_balances,
    cmd_check,
    cmd_import_monarch,
    cmd_invoice_generate,
    cmd_invoice_list,
    cmd_invoice_paid,
    cmd_invoice_create,
    cmd_list_ledgers,
    cmd_lots,
    cmd_query,
    cmd_report,
    cmd_sync_monarch,
    cmd_wash_sales,
    main,
    parse_accounting_config,
)


class TestLedgerPath:
    def test_get_ledger_path_from_env(self, monkeypatch):
        monkeypatch.setenv("LEDGER_PATH", "/path/to/ledger.beancount")
        result = _get_ledger_path()
        assert result == Path("/path/to/ledger.beancount")

    def test_get_ledger_path_missing_raises(self, monkeypatch):
        monkeypatch.delenv("LEDGER_PATH", raising=False)
        monkeypatch.delenv("LEDGER_PATHS", raising=False)
        with pytest.raises(ValueError, match="LEDGER_PATH"):
            _get_ledger_path()

    def test_get_ledger_path_by_name(self, monkeypatch):
        ledgers = json.dumps([
            {"name": "Personal", "path": "/path/personal.beancount"},
            {"name": "Business", "path": "/path/business.beancount"},
        ])
        monkeypatch.setenv("LEDGER_PATHS", ledgers)
        monkeypatch.setenv("LEDGER_PATH", "/path/personal.beancount")

        result = _get_ledger_path("Business")
        assert result == Path("/path/business.beancount")

    def test_get_ledger_path_by_name_case_insensitive(self, monkeypatch):
        ledgers = json.dumps([
            {"name": "Personal", "path": "/path/personal.beancount"},
        ])
        monkeypatch.setenv("LEDGER_PATHS", ledgers)
        monkeypatch.setenv("LEDGER_PATH", "/path/personal.beancount")

        result = _get_ledger_path("personal")  # lowercase
        assert result == Path("/path/personal.beancount")

    def test_get_ledger_path_by_name_not_found(self, monkeypatch):
        ledgers = json.dumps([
            {"name": "Personal", "path": "/path/personal.beancount"},
        ])
        monkeypatch.setenv("LEDGER_PATHS", ledgers)
        monkeypatch.setenv("LEDGER_PATH", "/path/personal.beancount")

        with pytest.raises(ValueError, match="not found.*Available: Personal"):
            _get_ledger_path("NonExistent")

    def test_get_ledger_path_defaults_to_first(self, monkeypatch):
        """Without a name, should use LEDGER_PATH (first ledger)."""
        ledgers = json.dumps([
            {"name": "Personal", "path": "/path/personal.beancount"},
            {"name": "Business", "path": "/path/business.beancount"},
        ])
        monkeypatch.setenv("LEDGER_PATHS", ledgers)
        monkeypatch.setenv("LEDGER_PATH", "/path/personal.beancount")

        result = _get_ledger_path(None)
        assert result == Path("/path/personal.beancount")


class TestBeanCheck:
    @patch("istota.skills.accounting.subprocess.run")
    def test_bean_check_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        success, errors = _run_bean_check(Path("/test/ledger.beancount"))

        assert success is True
        assert errors == []
        mock_run.assert_called_once()
        assert "bean-check" in mock_run.call_args[0][0]

    @patch("istota.skills.accounting.subprocess.run")
    def test_bean_check_errors(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="ledger.beancount:10: Invalid account 'Foo'\nledger.beancount:20: Missing narration"
        )

        success, errors = _run_bean_check(Path("/test/ledger.beancount"))

        assert success is False
        assert len(errors) == 2
        assert "Invalid account" in errors[0]

    @patch("istota.skills.accounting.subprocess.run")
    def test_bean_check_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()

        with pytest.raises(ValueError, match="bean-check not found"):
            _run_bean_check(Path("/test/ledger.beancount"))

    @patch("istota.skills.accounting.subprocess.run")
    def test_bean_check_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="bean-check", timeout=60)

        with pytest.raises(ValueError, match="timed out"):
            _run_bean_check(Path("/test/ledger.beancount"))


class TestBeanQuery:
    @patch("istota.skills.accounting.subprocess.run")
    def test_bean_query_success(self, mock_run):
        csv_output = "account,sum(position)\nAssets:Bank,1000 USD\nExpenses:Food,500 USD\n"
        mock_run.return_value = MagicMock(returncode=0, stdout=csv_output, stderr="")

        result = _run_bean_query(Path("/test/ledger.beancount"), "SELECT account, sum(position)")

        assert len(result) == 2
        assert result[0]["account"] == "Assets:Bank"
        assert result[0]["sum(position)"] == "1000 USD"

    @patch("istota.skills.accounting.subprocess.run")
    def test_bean_query_empty_result(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = _run_bean_query(Path("/test/ledger.beancount"), "SELECT * WHERE 1=0")

        assert result == []

    @patch("istota.skills.accounting.subprocess.run")
    def test_bean_query_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Invalid query syntax")

        with pytest.raises(ValueError, match="Invalid query syntax"):
            _run_bean_query(Path("/test/ledger.beancount"), "INVALID QUERY")


class TestCategoryMapping:
    def test_exact_match(self):
        assert _map_monarch_category("Groceries") == "Expenses:Food:Groceries"
        assert _map_monarch_category("Income") == "Income:Salary"

    def test_case_insensitive_match(self):
        assert _map_monarch_category("groceries") == "Expenses:Food:Groceries"
        assert _map_monarch_category("GROCERIES") == "Expenses:Food:Groceries"

    def test_unknown_category(self):
        result = _map_monarch_category("Unknown Category")
        assert result == "Expenses:Uncategorized:UnknownCategory"

    def test_all_mapped_categories_have_valid_accounts(self):
        for category, account in MONARCH_CATEGORY_MAP.items():
            assert ":" in account
            assert account.startswith(("Income:", "Expenses:", "Assets:", "Liabilities:", "Equity:"))


class TestMonarchImport:
    def test_parse_monarch_csv(self, tmp_path):
        """Test parsing CSV with actual Monarch export columns."""
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Whole Foods,Groceries,Chase Checking,WHOLE FOODS #123,Weekly groceries,-85.50,Personal,Alice\n"
            "2026-01-16,Employer,Income,Chase Checking,PAYROLL DEPOSIT,Paycheck,5000.00,Business,Alice\n"
            "01/17/2026,Amazon,Shopping,Chase Checking,AMAZON.COM,,-42.99,,Alice\n"
        )

        transactions = _parse_monarch_csv(csv_file)

        assert len(transactions) == 3
        assert transactions[0]["date"] == date(2026, 1, 15)
        assert transactions[0]["merchant"] == "Whole Foods"
        assert transactions[0]["category"] == "Groceries"
        assert transactions[0]["amount"] == -85.50
        assert transactions[0]["notes"] == "Weekly groceries"
        assert transactions[0]["original_statement"] == "WHOLE FOODS #123"
        assert transactions[0]["tags"] == ["Personal"]
        assert transactions[0]["owner"] == "Alice"

        # Test MM/DD/YYYY format
        assert transactions[2]["date"] == date(2026, 1, 17)
        # Empty tags should be empty list
        assert transactions[2]["tags"] == []

    def test_parse_monarch_csv_skips_invalid_dates(self, tmp_path):
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "invalid-date,Store,Shopping,Account,STORE,-10.00,,,\n"
            "2026-01-15,Valid Store,Shopping,Account,VALID STORE,,-20.00,,Alice\n"
        )

        transactions = _parse_monarch_csv(csv_file)

        assert len(transactions) == 1
        assert transactions[0]["merchant"] == "Valid Store"

    def test_parse_monarch_csv_with_tag_filter_include(self, tmp_path):
        """Test filtering by include tags."""
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Store A,Shopping,Account,STORE A,,-10.00,Business,Alice\n"
            "2026-01-16,Store B,Shopping,Account,STORE B,,-20.00,Personal,Alice\n"
            "2026-01-17,Store C,Shopping,Account,STORE C,,-30.00,\"Business, Travel\",Alice\n"
        )

        transactions = _parse_monarch_csv(csv_file, include_tags=["Business"])

        assert len(transactions) == 2
        assert transactions[0]["merchant"] == "Store A"
        assert transactions[1]["merchant"] == "Store C"

    def test_parse_monarch_csv_with_tag_filter_exclude(self, tmp_path):
        """Test filtering by exclude tags."""
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Store A,Shopping,Account,STORE A,,-10.00,Business,Alice\n"
            "2026-01-16,Store B,Shopping,Account,STORE B,,-20.00,Personal,Alice\n"
            "2026-01-17,Store C,Shopping,Account,STORE C,,-30.00,,Alice\n"
        )

        transactions = _parse_monarch_csv(csv_file, exclude_tags=["Personal"])

        assert len(transactions) == 2
        assert transactions[0]["merchant"] == "Store A"
        assert transactions[1]["merchant"] == "Store C"

    def test_parse_monarch_csv_with_both_tag_filters(self, tmp_path):
        """Test include applied first, then exclude."""
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Store A,Shopping,Account,STORE A,,-10.00,\"Business, Tax\",Alice\n"
            "2026-01-16,Store B,Shopping,Account,STORE B,,-20.00,Business,Alice\n"
            "2026-01-17,Store C,Shopping,Account,STORE C,,-30.00,Personal,Alice\n"
        )

        # Include Business, but exclude Tax
        transactions = _parse_monarch_csv(
            csv_file,
            include_tags=["Business"],
            exclude_tags=["Tax"],
        )

        assert len(transactions) == 1
        assert transactions[0]["merchant"] == "Store B"


class TestBeancountFormatting:
    def test_format_expense_transaction(self):
        result = _format_beancount_transaction(
            txn_date=date(2026, 1, 15),
            payee="Whole Foods",
            narration="Weekly groceries",
            posting_account="Expenses:Food:Groceries",
            contra_account="Assets:Bank:Checking",
            amount=-85.50,
        )

        assert '2026-01-15 * "Whole Foods" "Weekly groceries"' in result
        assert "Expenses:Food:Groceries  85.50 USD" in result
        assert "Assets:Bank:Checking" in result

    def test_format_income_transaction(self):
        result = _format_beancount_transaction(
            txn_date=date(2026, 1, 16),
            payee="Employer",
            narration="Paycheck",
            posting_account="Income:Salary",
            contra_account="Assets:Bank:Checking",
            amount=5000.00,
        )

        assert '2026-01-16 * "Employer" "Paycheck"' in result
        assert "Assets:Bank:Checking  5000.00 USD" in result
        assert "Income:Salary" in result

    def test_format_escapes_quotes(self):
        result = _format_beancount_transaction(
            txn_date=date(2026, 1, 15),
            payee='Store "Best"',
            narration='Item "Special"',
            posting_account="Expenses:Shopping",
            contra_account="Assets:Bank:Checking",
            amount=-10.00,
        )

        assert '\\"Best\\"' in result
        assert '\\"Special\\"' in result

    def test_format_recategorization_entry(self):
        result = _format_recategorization_entry(
            txn_date=date(2026, 2, 7),
            merchant="Starbucks",
            original_account="Expenses:Food:Coffee",
            recategorize_account="Expenses:Personal-Expense",
            amount=5.50,
        )

        assert '2026-02-07 * "Starbucks" "Recategorized: business tag removed in Monarch"' in result
        assert "Expenses:Personal-Expense  5.50 USD" in result
        assert "Expenses:Food:Coffee  -5.50 USD" in result

    def test_format_recategorization_entry_escapes_quotes(self):
        result = _format_recategorization_entry(
            txn_date=date(2026, 2, 7),
            merchant='Store "Best"',
            original_account="Expenses:Shopping",
            recategorize_account="Expenses:Personal-Expense",
            amount=25.00,
        )

        assert '\\"Best\\"' in result

    def test_format_category_change_entry(self):
        result = _format_category_change_entry(
            txn_date=date(2026, 2, 14),
            merchant="PayPal",
            old_account="Expenses:Office-Supplies",
            new_account="Expenses:Entertainment:Recreation",
            amount=25.00,
        )

        assert '2026-02-14 * "PayPal" "Recategorized in Monarch"' in result
        assert "Expenses:Entertainment:Recreation  25.00 USD" in result
        assert "Expenses:Office-Supplies  -25.00 USD" in result

    def test_format_category_change_entry_escapes_quotes(self):
        result = _format_category_change_entry(
            txn_date=date(2026, 2, 14),
            merchant='Shop "N" Save',
            old_account="Expenses:Food:Groceries",
            new_account="Expenses:Shopping",
            amount=50.00,
        )

        assert '\\"N\\"' in result


class TestWashSaleDetection:
    def test_detect_wash_sale_within_30_days(self):
        sales = [
            SaleTransaction(
                date=date(2026, 6, 15),
                account="Assets:Investment",
                symbol="AAPL",
                units=10,
                proceeds=1400.0,
                cost_basis=1500.0,
                gain_loss=-100.0,
            )
        ]
        purchases = [
            PurchaseTransaction(
                date=date(2026, 6, 20),
                account="Assets:Investment",
                symbol="AAPL",
                units=5,
                cost=700.0,
            )
        ]

        violations = _detect_wash_sales(sales, purchases, 2026)

        assert len(violations) == 1
        assert violations[0]["symbol"] == "AAPL"
        assert violations[0]["loss_amount"] == -100.0
        assert len(violations[0]["triggering_purchases"]) == 1
        assert violations[0]["triggering_purchases"][0]["days_from_sale"] == 5

    def test_no_wash_sale_outside_30_days(self):
        sales = [
            SaleTransaction(
                date=date(2026, 6, 15),
                account="Assets:Investment",
                symbol="AAPL",
                units=10,
                proceeds=1400.0,
                cost_basis=1500.0,
                gain_loss=-100.0,
            )
        ]
        purchases = [
            PurchaseTransaction(
                date=date(2026, 7, 20),  # 35 days later
                account="Assets:Investment",
                symbol="AAPL",
                units=5,
                cost=700.0,
            )
        ]

        violations = _detect_wash_sales(sales, purchases, 2026)

        assert len(violations) == 0

    def test_no_wash_sale_different_symbol(self):
        sales = [
            SaleTransaction(
                date=date(2026, 6, 15),
                account="Assets:Investment",
                symbol="AAPL",
                units=10,
                proceeds=1400.0,
                cost_basis=1500.0,
                gain_loss=-100.0,
            )
        ]
        purchases = [
            PurchaseTransaction(
                date=date(2026, 6, 20),
                account="Assets:Investment",
                symbol="GOOGL",  # Different symbol
                units=5,
                cost=700.0,
            )
        ]

        violations = _detect_wash_sales(sales, purchases, 2026)

        assert len(violations) == 0

    def test_wash_sale_before_sale(self):
        """Purchase before sale should also trigger wash sale."""
        sales = [
            SaleTransaction(
                date=date(2026, 6, 15),
                account="Assets:Investment",
                symbol="AAPL",
                units=10,
                proceeds=1400.0,
                cost_basis=1500.0,
                gain_loss=-100.0,
            )
        ]
        purchases = [
            PurchaseTransaction(
                date=date(2026, 6, 1),  # 14 days before
                account="Assets:Investment",
                symbol="AAPL",
                units=5,
                cost=700.0,
            )
        ]

        violations = _detect_wash_sales(sales, purchases, 2026)

        assert len(violations) == 1
        assert violations[0]["triggering_purchases"][0]["days_from_sale"] == -14

    def test_gain_not_flagged(self):
        """Sales with gains should not be flagged."""
        sales = [
            SaleTransaction(
                date=date(2026, 6, 15),
                account="Assets:Investment",
                symbol="AAPL",
                units=10,
                proceeds=1600.0,
                cost_basis=1500.0,
                gain_loss=100.0,  # Gain, not loss
            )
        ]
        purchases = [
            PurchaseTransaction(
                date=date(2026, 6, 20),
                account="Assets:Investment",
                symbol="AAPL",
                units=5,
                cost=700.0,
            )
        ]

        # Gains shouldn't be in the sales list in real usage,
        # but verify detection ignores wrong-year sales
        violations = _detect_wash_sales(sales, purchases, 2025)  # Wrong year

        assert len(violations) == 0


class TestInvoiceGeneration:
    def test_generate_invoice_html(self):
        html = _generate_invoice_html(
            client="Acme Corp",
            items=[
                {"description": "Consulting", "amount": 150, "quantity": 8},
                {"description": "Expenses", "amount": 50, "quantity": 1},
            ],
            invoice_number="INV-20260201-ACM",
            invoice_date=date(2026, 2, 1),
            due_date=date(2026, 3, 1),
            notes="Payment via ACH",
            from_name="My Company",
            from_address="123 Main St",
        )

        assert "INVOICE" in html
        assert "INV-20260201-ACM" in html
        assert "Acme Corp" in html
        assert "Consulting" in html
        assert "$150.00" in html
        assert "$1,250.00" in html or "$1250.00" in html  # Total
        assert "My Company" in html
        assert "Payment via ACH" in html

    def test_generate_invoice_without_notes(self):
        html = _generate_invoice_html(
            client="Client",
            items=[{"description": "Service", "amount": 100, "quantity": 1}],
            invoice_number="INV-001",
            invoice_date=date(2026, 1, 1),
            due_date=date(2026, 1, 31),
        )

        assert "INVOICE" in html
        assert 'class="notes"' not in html


class TestCLICommands:
    @patch("istota.skills.accounting._get_ledger_path")
    @patch("istota.skills.accounting._run_bean_check")
    def test_cmd_check_success(self, mock_check, mock_path, tmp_path):
        # Use a real path that exists
        ledger_file = tmp_path / "ledger.beancount"
        ledger_file.write_text("")
        mock_path.return_value = ledger_file
        mock_check.return_value = (True, [])

        result = cmd_check(MagicMock())

        assert result["status"] == "ok"
        assert result["error_count"] == 0

    @patch("istota.skills.accounting._get_ledger_path")
    @patch("istota.skills.accounting._run_bean_check")
    def test_cmd_check_with_errors(self, mock_check, mock_path, tmp_path):
        ledger_file = tmp_path / "ledger.beancount"
        ledger_file.write_text("")
        mock_path.return_value = ledger_file
        mock_check.return_value = (False, ["Error 1", "Error 2"])

        result = cmd_check(MagicMock())

        assert result["status"] == "error"
        assert result["error_count"] == 2
        assert "Error 1" in result["errors"]

    @patch("istota.skills.accounting._get_ledger_path")
    def test_cmd_check_file_not_found(self, mock_path, tmp_path):
        # Use a path that doesn't exist
        mock_path.return_value = tmp_path / "nonexistent.beancount"

        result = cmd_check(MagicMock())

        assert result["status"] == "error"
        assert "not found" in result["error"]

    @patch("istota.skills.accounting._get_ledger_path")
    @patch("istota.skills.accounting._run_bean_query")
    def test_cmd_balances(self, mock_query, mock_path):
        mock_path.return_value = Path("/test/ledger.beancount")
        mock_query.return_value = [
            {"account": "Assets:Bank", "sum(position)": "1000 USD"},
            {"account": "Expenses:Food", "sum(position)": "500 USD"},
        ]

        args = MagicMock()
        args.account = None

        result = cmd_balances(args)

        assert result["status"] == "ok"
        assert result["account_count"] == 2

    @patch("istota.skills.accounting._get_ledger_path")
    @patch("istota.skills.accounting._run_bean_query")
    def test_cmd_balances_with_filter(self, mock_query, mock_path):
        mock_path.return_value = Path("/test/ledger.beancount")
        mock_query.return_value = [{"account": "Assets:Bank", "sum(position)": "1000 USD"}]

        args = MagicMock()
        args.account = "Assets:Bank"

        result = cmd_balances(args)

        assert result["status"] == "ok"
        # Verify the query includes the filter
        call_args = mock_query.call_args[0]
        assert "Assets:Bank" in call_args[1]

    @patch("istota.skills.accounting._get_ledger_path")
    @patch("istota.skills.accounting._run_bean_query")
    def test_cmd_query(self, mock_query, mock_path):
        mock_path.return_value = Path("/test/ledger.beancount")
        mock_query.return_value = [{"date": "2026-01-15", "payee": "Store"}]

        args = MagicMock()
        args.query = "SELECT date, payee LIMIT 1"

        result = cmd_query(args)

        assert result["status"] == "ok"
        assert result["row_count"] == 1

    @patch("istota.skills.accounting._get_ledger_path")
    @patch("istota.skills.accounting._run_bean_query")
    def test_cmd_report_income_statement(self, mock_query, mock_path):
        mock_path.return_value = Path("/test/ledger.beancount")
        mock_query.return_value = [
            {"account": "Income:Salary", "sum(position)": "-60000 USD"},
            {"account": "Expenses:Food", "sum(position)": "6000 USD"},
        ]

        args = MagicMock()
        args.type = "income-statement"
        args.year = 2026

        result = cmd_report(args)

        assert result["status"] == "ok"
        assert result["report_type"] == "income-statement"
        assert result["year"] == 2026

    @patch("istota.skills.accounting._get_ledger_path")
    @patch("istota.skills.accounting._run_bean_query")
    def test_cmd_lots(self, mock_query, mock_path):
        mock_path.return_value = Path("/test/ledger.beancount")
        mock_query.return_value = [
            {"account": "Assets:Investment", "units(position)": "10 AAPL", "cost(position)": "1500 USD"}
        ]

        args = MagicMock()
        args.symbol = "aapl"

        result = cmd_lots(args)

        assert result["status"] == "ok"
        assert result["symbol"] == "AAPL"
        assert result["lot_count"] == 1

    @patch("istota.skills.accounting._get_ledger_path")
    def test_cmd_import_monarch(self, mock_path, tmp_path):
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text("2026-01-01 open Assets:Bank:Checking USD\n")
        mock_path.return_value = ledger_file

        csv_file = tmp_path / "export.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Whole Foods,Groceries,Chase,WHOLE FOODS,,-85.50,,Alice\n"
        )

        args = MagicMock()
        args.file = str(csv_file)
        args.account = "Assets:Bank:Checking"
        args.tag = None
        args.exclude_tag = None

        result = cmd_import_monarch(args)

        assert result["status"] == "ok"
        assert result["transaction_count"] == 1
        assert "staging_file" in result

        # Verify staging file was created
        staging_file = Path(result["staging_file"])
        assert staging_file.exists()
        content = staging_file.read_text()
        assert "Whole Foods" in content
        assert "Expenses:Food:Groceries" in content

        # Verify entries were appended to main ledger
        ledger_content = ledger_file.read_text()
        assert "Whole Foods" in ledger_content
        assert "Expenses:Food:Groceries" in ledger_content

        # Verify backup was created
        backups_dir = ledger_dir / "backups"
        assert backups_dir.exists()
        backups = list(backups_dir.glob("main.beancount.*"))
        assert len(backups) == 1

    # Invoice subcommand tests moved to tests/test_skills_invoicing.py


class TestAddTransaction:
    def test_add_transaction_success(self, tmp_path, monkeypatch):
        """Test successful transaction addition."""
        # Setup ledger directory structure
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        txn_dir = ledger_dir / "transactions"
        txn_dir.mkdir()

        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text(
            'include "transactions/*.beancount"\n'
            "2026-01-01 open Assets:Bank:Checking USD\n"
            "2026-01-01 open Expenses:Food:Groceries USD\n"
        )

        monkeypatch.setenv("LEDGER_PATH", str(ledger_file))

        args = MagicMock()
        args.ledger = None
        args.date = "2026-02-04"
        args.payee = "Test Store"
        args.narration = "Test purchase"
        args.debit = "Expenses:Food:Groceries"
        args.credit = "Assets:Bank:Checking"
        args.amount = "25.00"
        args.currency = "USD"

        with patch("istota.skills.accounting._run_bean_check") as mock_check:
            mock_check.return_value = (True, [])
            result = cmd_add_transaction(args)

        assert result["status"] == "ok"
        assert result["date"] == "2026-02-04"
        assert result["payee"] == "Test Store"
        assert result["amount"] == 25.00
        assert result["currency"] == "USD"
        assert result["debit"] == "Expenses:Food:Groceries"
        assert result["credit"] == "Assets:Bank:Checking"
        assert "file" in result

        # Verify the file was created
        txn_file = txn_dir / "2026.beancount"
        assert txn_file.exists()
        content = txn_file.read_text()
        assert 'Test Store' in content
        assert 'Expenses:Food:Groceries  25.00 USD' in content

    def test_add_transaction_invalid_date(self, tmp_path, monkeypatch):
        """Test error handling for invalid date format."""
        ledger_file = tmp_path / "main.beancount"
        ledger_file.write_text("")
        monkeypatch.setenv("LEDGER_PATH", str(ledger_file))

        args = MagicMock()
        args.ledger = None
        args.date = "02-04-2026"  # Wrong format
        args.payee = "Store"
        args.narration = "Purchase"
        args.debit = "Expenses:Food"
        args.credit = "Assets:Bank"
        args.amount = "10.00"
        args.currency = None

        result = cmd_add_transaction(args)

        assert result["status"] == "error"
        assert "Invalid date format" in result["error"]

    def test_add_transaction_invalid_amount(self, tmp_path, monkeypatch):
        """Test error handling for invalid amount."""
        ledger_file = tmp_path / "main.beancount"
        ledger_file.write_text("")
        monkeypatch.setenv("LEDGER_PATH", str(ledger_file))

        args = MagicMock()
        args.ledger = None
        args.date = "2026-02-04"
        args.payee = "Store"
        args.narration = "Purchase"
        args.debit = "Expenses:Food"
        args.credit = "Assets:Bank"
        args.amount = "not-a-number"
        args.currency = None

        result = cmd_add_transaction(args)

        assert result["status"] == "error"
        assert "Invalid amount" in result["error"]

    def test_add_transaction_negative_amount(self, tmp_path, monkeypatch):
        """Test error handling for negative amount."""
        ledger_file = tmp_path / "main.beancount"
        ledger_file.write_text("")
        monkeypatch.setenv("LEDGER_PATH", str(ledger_file))

        args = MagicMock()
        args.ledger = None
        args.date = "2026-02-04"
        args.payee = "Store"
        args.narration = "Purchase"
        args.debit = "Expenses:Food"
        args.credit = "Assets:Bank"
        args.amount = "-10.00"
        args.currency = None

        result = cmd_add_transaction(args)

        assert result["status"] == "error"
        assert "Amount must be positive" in result["error"]

    def test_add_transaction_validation_failure(self, tmp_path, monkeypatch):
        """Test what happens when bean-check fails after adding."""
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()

        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text('include "transactions/*.beancount"\n')
        monkeypatch.setenv("LEDGER_PATH", str(ledger_file))

        args = MagicMock()
        args.ledger = None
        args.date = "2026-02-04"
        args.payee = "Store"
        args.narration = "Purchase"
        args.debit = "Expenses:Unknown:Account"  # Account doesn't exist
        args.credit = "Assets:Bank:Checking"
        args.amount = "10.00"
        args.currency = "USD"

        with patch("istota.skills.accounting._run_bean_check") as mock_check:
            mock_check.return_value = (False, ["Invalid account 'Expenses:Unknown:Account'"])
            result = cmd_add_transaction(args)

        assert result["status"] == "error"
        assert "validation failed" in result["error"]
        assert "validation_errors" in result
        assert len(result["validation_errors"]) > 0

    def test_add_transaction_default_currency(self, tmp_path, monkeypatch):
        """Test that USD is used as default currency."""
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()

        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text('include "transactions/*.beancount"\n')
        monkeypatch.setenv("LEDGER_PATH", str(ledger_file))

        args = MagicMock()
        args.ledger = None
        args.date = "2026-02-04"
        args.payee = "Store"
        args.narration = "Purchase"
        args.debit = "Expenses:Food"
        args.credit = "Assets:Bank"
        args.amount = "10.00"
        args.currency = None  # Should default to USD

        with patch("istota.skills.accounting._run_bean_check") as mock_check:
            mock_check.return_value = (True, [])
            result = cmd_add_transaction(args)

        assert result["status"] == "ok"
        assert result["currency"] == "USD"

    def test_add_transaction_escapes_quotes(self, tmp_path, monkeypatch):
        """Test that quotes in payee/narration are escaped."""
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()

        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text('include "transactions/*.beancount"\n')
        monkeypatch.setenv("LEDGER_PATH", str(ledger_file))

        args = MagicMock()
        args.ledger = None
        args.date = "2026-02-04"
        args.payee = 'Store "Best"'
        args.narration = 'Item "Special"'
        args.debit = "Expenses:Food"
        args.credit = "Assets:Bank"
        args.amount = "10.00"
        args.currency = "USD"

        with patch("istota.skills.accounting._run_bean_check") as mock_check:
            mock_check.return_value = (True, [])
            result = cmd_add_transaction(args)

        assert result["status"] == "ok"

        # Check the file content has escaped quotes
        txn_file = ledger_dir / "transactions" / "2026.beancount"
        content = txn_file.read_text()
        assert '\\"Best\\"' in content
        assert '\\"Special\\"' in content


class TestCLIMain:
    @patch("istota.skills.accounting._get_ledger_path")
    @patch("istota.skills.accounting._run_bean_check")
    def test_main_check(self, mock_check, mock_path, capsys, tmp_path):
        ledger_file = tmp_path / "ledger.beancount"
        ledger_file.write_text("")
        mock_path.return_value = ledger_file
        mock_check.return_value = (True, [])

        main(["check"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"

    @patch("istota.skills.accounting._get_ledger_path")
    @patch("istota.skills.accounting._run_bean_query")
    def test_main_balances(self, mock_query, mock_path, capsys):
        mock_path.return_value = Path("/test/ledger.beancount")
        mock_query.return_value = []

        main(["balances"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"

    @patch("istota.skills.accounting._get_ledger_path")
    @patch("istota.skills.accounting._run_bean_query")
    def test_main_query(self, mock_query, mock_path, capsys):
        mock_path.return_value = Path("/test/ledger.beancount")
        mock_query.return_value = []

        main(["query", "SELECT * LIMIT 1"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"

    def test_main_missing_command(self):
        with pytest.raises(SystemExit):
            main([])

    @patch("istota.skills.accounting._get_ledger_path")
    def test_main_error_output(self, mock_path, capsys):
        mock_path.side_effect = ValueError("LEDGER_PATH not set")

        with pytest.raises(SystemExit) as exc_info:
            main(["check"])

        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "error"
        assert "LEDGER_PATH" in output["error"]


class TestListLedgers:
    def test_list_multiple_ledgers(self, monkeypatch):
        ledgers = json.dumps([
            {"name": "Personal", "path": "/path/personal.beancount"},
            {"name": "Business", "path": "/path/business.beancount"},
        ])
        monkeypatch.setenv("LEDGER_PATHS", ledgers)

        result = cmd_list_ledgers(MagicMock())

        assert result["status"] == "ok"
        assert result["ledger_count"] == 2
        assert result["ledgers"][0]["name"] == "Personal"
        assert result["ledgers"][1]["name"] == "Business"

    def test_list_single_ledger(self, monkeypatch):
        monkeypatch.delenv("LEDGER_PATHS", raising=False)
        monkeypatch.setenv("LEDGER_PATH", "/path/ledger.beancount")

        result = cmd_list_ledgers(MagicMock())

        assert result["status"] == "ok"
        assert result["ledger_count"] == 1
        assert result["ledgers"][0]["name"] == "default"

    def test_list_no_ledgers(self, monkeypatch):
        monkeypatch.delenv("LEDGER_PATHS", raising=False)
        monkeypatch.delenv("LEDGER_PATH", raising=False)

        result = cmd_list_ledgers(MagicMock())

        assert result["status"] == "error"
        assert "No ledgers configured" in result["error"]


class TestBuildParser:
    def test_parser_has_all_commands(self):
        parser = build_parser()

        # Test each command can be parsed
        for cmd in ["list", "check", "balances", "query", "report", "lots", "wash-sales", "import-monarch", "add-transaction", "invoice"]:
            # These commands require different args, just verify they exist
            assert cmd in parser.format_help()

    def test_parser_list_command(self):
        parser = build_parser()
        args = parser.parse_args(["list"])
        assert args.command == "list"

    def test_parser_global_ledger_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--ledger", "Business", "check"])
        assert args.ledger == "Business"
        assert args.command == "check"

    def test_parser_ledger_short_flag(self):
        parser = build_parser()
        args = parser.parse_args(["-l", "Trading", "balances"])
        assert args.ledger == "Trading"
        assert args.command == "balances"

    def test_parser_check_command(self):
        parser = build_parser()
        args = parser.parse_args(["check"])
        assert args.command == "check"

    def test_parser_balances_command(self):
        parser = build_parser()
        args = parser.parse_args(["balances", "--account", "Assets:"])
        assert args.command == "balances"
        assert args.account == "Assets:"

    def test_parser_query_command(self):
        parser = build_parser()
        args = parser.parse_args(["query", "SELECT * LIMIT 1"])
        assert args.command == "query"
        assert args.query == "SELECT * LIMIT 1"

    def test_parser_report_command(self):
        parser = build_parser()
        args = parser.parse_args(["report", "income-statement", "--year", "2025"])
        assert args.command == "report"
        assert args.type == "income-statement"
        assert args.year == 2025

    def test_parser_lots_command(self):
        parser = build_parser()
        args = parser.parse_args(["lots", "VTI"])
        assert args.command == "lots"
        assert args.symbol == "VTI"

    def test_parser_wash_sales_command(self):
        parser = build_parser()
        args = parser.parse_args(["wash-sales", "--year", "2025"])
        assert args.command == "wash-sales"
        assert args.year == 2025

    def test_parser_import_monarch_command(self):
        parser = build_parser()
        args = parser.parse_args(["import-monarch", "export.csv", "--account", "Assets:Bank"])
        assert args.command == "import-monarch"
        assert args.file == "export.csv"
        assert args.account == "Assets:Bank"

    def test_parser_add_transaction_command(self):
        parser = build_parser()
        args = parser.parse_args([
            "add-transaction",
            "--date", "2026-02-04",
            "--payee", "Test Store",
            "--narration", "Test purchase",
            "--debit", "Expenses:Food:Groceries",
            "--credit", "Assets:Bank:Checking",
            "--amount", "25.50",
            "--currency", "EUR",
        ])
        assert args.command == "add-transaction"
        assert args.date == "2026-02-04"
        assert args.payee == "Test Store"
        assert args.narration == "Test purchase"
        assert args.debit == "Expenses:Food:Groceries"
        assert args.credit == "Assets:Bank:Checking"
        assert args.amount == "25.50"
        assert args.currency == "EUR"

    def test_parser_add_transaction_short_flags(self):
        parser = build_parser()
        args = parser.parse_args([
            "add-transaction",
            "-d", "2026-02-04",
            "-p", "Store",
            "-n", "Purchase",
            "--debit", "Expenses:Food",
            "--credit", "Assets:Bank",
            "-a", "10.00",
        ])
        assert args.command == "add-transaction"
        assert args.date == "2026-02-04"
        assert args.payee == "Store"
        assert args.narration == "Purchase"
        assert args.currency == "USD"  # Default

    def test_parser_invoice_command(self):
        parser = build_parser()
        args = parser.parse_args([
            "invoice", "generate",
            "--period", "2026-02",
            "--client", "acme",
        ])
        assert args.command == "invoice"
        assert args.invoice_command == "generate"
        assert args.period == "2026-02"
        assert args.client == "acme"

    def test_parser_import_monarch_with_tags(self):
        parser = build_parser()
        args = parser.parse_args([
            "import-monarch", "transactions.csv",
            "--account", "Assets:Bank:Checking",
            "--tag", "Business",
            "--tag", "Tax",
            "--exclude-tag", "Personal",
        ])
        assert args.command == "import-monarch"
        assert args.file == "transactions.csv"
        assert args.tag == ["Business", "Tax"]
        assert args.exclude_tag == ["Personal"]

    def test_parser_sync_monarch(self):
        parser = build_parser()
        args = parser.parse_args(["sync-monarch"])
        assert args.command == "sync-monarch"
        assert args.dry_run is False

    def test_parser_sync_monarch_dry_run(self):
        parser = build_parser()
        args = parser.parse_args(["sync-monarch", "--dry-run"])
        assert args.command == "sync-monarch"
        assert args.dry_run is True


class TestTagParsing:
    """Tests for tag parsing and filtering utilities."""

    def test_parse_tags_empty(self):
        assert _parse_tags("") == []
        assert _parse_tags("  ") == []

    def test_parse_tags_single(self):
        assert _parse_tags("Business") == ["Business"]

    def test_parse_tags_multiple(self):
        assert _parse_tags("Business, Personal") == ["Business", "Personal"]
        assert _parse_tags("A,B,C") == ["A", "B", "C"]

    def test_parse_tags_strips_whitespace(self):
        assert _parse_tags("  Business  ,  Personal  ") == ["Business", "Personal"]

    def test_filter_by_tags_no_filters(self):
        assert _filter_by_tags(["A", "B"], None, None) is True

    def test_filter_by_tags_include_match(self):
        assert _filter_by_tags(["Business", "Travel"], ["Business"], None) is True

    def test_filter_by_tags_include_no_match(self):
        assert _filter_by_tags(["Personal"], ["Business"], None) is False

    def test_filter_by_tags_exclude_match(self):
        assert _filter_by_tags(["Business", "Personal"], None, ["Personal"]) is False

    def test_filter_by_tags_exclude_no_match(self):
        assert _filter_by_tags(["Business"], None, ["Personal"]) is True

    def test_filter_by_tags_include_then_exclude(self):
        # Has Business (passes include) but also has Personal (fails exclude)
        assert _filter_by_tags(["Business", "Personal"], ["Business"], ["Personal"]) is False

    def test_filter_by_tags_empty_tags_with_include(self):
        # Empty tags should fail include filter
        assert _filter_by_tags([], ["Business"], None) is False


class TestAccountingConfigParsing:
    """Tests for ACCOUNTING.md config parsing."""

    def test_extract_toml_from_markdown(self):
        content = """# Config

Some text here.

```toml
[monarch]
email = "test@example.com"
```

More text.
"""
        toml = _extract_toml_from_markdown(content)
        assert '[monarch]' in toml
        assert 'email = "test@example.com"' in toml

    def test_extract_toml_multiple_blocks(self):
        content = """
```toml
key1 = "value1"
```

```toml
key2 = "value2"
```
"""
        toml = _extract_toml_from_markdown(content)
        assert 'key1 = "value1"' in toml
        assert 'key2 = "value2"' in toml

    def test_parse_accounting_config_full(self, tmp_path):
        config_file = tmp_path / "ACCOUNTING.md"
        config_file.write_text('''# Accounting Config

```toml
[monarch]
email = "user@example.com"
password = "secret123"

[monarch.sync]
lookback_days = 60
default_account = "Assets:Bank:Primary"

[monarch.accounts]
"Chase Checking" = "Assets:Bank:Chase"
"Amex Gold" = "Liabilities:CreditCard:Amex"

[monarch.categories]
"Custom Cat" = "Expenses:Custom"

[monarch.tags]
include = ["business"]
exclude = ["personal"]
```
''')

        config = parse_accounting_config(config_file)

        assert config.credentials.email == "user@example.com"
        assert config.credentials.password == "secret123"
        assert config.credentials.session_token is None
        assert config.sync.lookback_days == 60
        assert config.sync.default_account == "Assets:Bank:Primary"
        assert config.accounts["Chase Checking"] == "Assets:Bank:Chase"
        assert config.categories["Custom Cat"] == "Expenses:Custom"
        assert config.tags.include == ["business"]
        assert config.tags.exclude == ["personal"]

    def test_parse_accounting_config_session_token(self, tmp_path):
        config_file = tmp_path / "ACCOUNTING.md"
        config_file.write_text('''# Config
```toml
[monarch]
session_token = "my-token-123"
```
''')

        config = parse_accounting_config(config_file)

        assert config.credentials.session_token == "my-token-123"
        assert config.credentials.email is None

    def test_parse_accounting_config_defaults(self, tmp_path):
        """Test that missing sections use defaults."""
        config_file = tmp_path / "ACCOUNTING.md"
        config_file.write_text('''# Config
```toml
[monarch]
email = "test@test.com"
```
''')

        config = parse_accounting_config(config_file)

        assert config.sync.lookback_days == 30
        assert config.sync.default_account == "Assets:Bank:Checking"
        assert config.sync.recategorize_account == "Expenses:Personal-Expense"
        assert config.accounts == {}
        assert config.categories == {}
        assert config.tags.include == []
        assert config.tags.exclude == []

    def test_parse_accounting_config_custom_recategorize_account(self, tmp_path):
        """Test that custom recategorize_account is parsed."""
        config_file = tmp_path / "ACCOUNTING.md"
        config_file.write_text('''# Config
```toml
[monarch]
email = "test@test.com"

[monarch.sync]
recategorize_account = "Expenses:Personal:Other"
```
''')

        config = parse_accounting_config(config_file)
        assert config.sync.recategorize_account == "Expenses:Personal:Other"

    def test_parse_accounting_config_empty_toml_raises(self, tmp_path):
        config_file = tmp_path / "ACCOUNTING.md"
        config_file.write_text("# Config\n\nNo TOML here!")

        with pytest.raises(ValueError, match="No TOML content"):
            parse_accounting_config(config_file)


class TestDeduplicationFunctions:
    """Tests for transaction deduplication in db.py."""

    def test_compute_transaction_hash(self):
        from istota.db import compute_transaction_hash

        hash1 = compute_transaction_hash("2026-01-15", -85.50, "Whole Foods", "Chase Checking")
        hash2 = compute_transaction_hash("2026-01-15", -85.50, "Whole Foods", "Chase Checking")
        hash3 = compute_transaction_hash("2026-01-15", -85.50, "Whole Foods", "Amex")

        assert hash1 == hash2  # Same inputs = same hash
        assert hash1 != hash3  # Different account = different hash
        assert len(hash1) == 64  # SHA-256 hex is 64 chars

    def test_compute_transaction_hash_normalizes(self):
        from istota.db import compute_transaction_hash

        # Should normalize case and whitespace
        hash1 = compute_transaction_hash("2026-01-15", -10.00, "Store", "Account")
        hash2 = compute_transaction_hash("2026-01-15", -10.00, "  STORE  ", "  ACCOUNT  ")

        assert hash1 == hash2

    def test_monarch_transaction_tracking(self, tmp_path):
        from istota.db import (
            get_db,
            init_db,
            is_monarch_transaction_synced,
            track_monarch_transaction,
            track_monarch_transactions_batch,
        )

        db_path = tmp_path / "test.db"
        init_db(db_path)

        with get_db(db_path) as conn:
            # Initially not tracked
            assert is_monarch_transaction_synced(conn, "user1", "txn-123") is False

            # Track it with metadata
            track_monarch_transaction(
                conn, "user1", "txn-123",
                tags_json='["Business"]',
                amount=50.0,
                merchant="Coffee Shop",
                posted_account="Expenses:Food:Coffee",
                txn_date="2026-02-01",
            )

            # Now it's tracked
            assert is_monarch_transaction_synced(conn, "user1", "txn-123") is True

            # Different user doesn't see it
            assert is_monarch_transaction_synced(conn, "user2", "txn-123") is False

            # Batch tracking with dicts
            count = track_monarch_transactions_batch(conn, "user1", [
                {"id": "txn-456", "amount": 25.0, "merchant": "Store"},
                {"id": "txn-789", "amount": 100.0, "merchant": "Restaurant"},
                {"id": "txn-123", "amount": 50.0, "merchant": "Coffee Shop"},  # Already exists
            ])
            # txn-123 already exists but gets updated, all 3 rows affected
            assert count == 3

    def test_monarch_reconciliation_tracking(self, tmp_path):
        from istota.db import (
            get_db,
            init_db,
            track_monarch_transaction,
            get_active_monarch_synced_transactions,
            mark_monarch_transaction_recategorized,
        )

        db_path = tmp_path / "test.db"
        init_db(db_path)

        with get_db(db_path) as conn:
            # Track a transaction with full metadata
            track_monarch_transaction(
                conn, "user1", "txn-123",
                tags_json='["Consulting"]',
                amount=50.0,
                merchant="Coffee Shop",
                posted_account="Expenses:Food:Coffee",
                txn_date="2026-02-01",
            )
            track_monarch_transaction(
                conn, "user1", "txn-456",
                tags_json='["Consulting", "Travel"]',
                amount=200.0,
                merchant="Hotel",
                posted_account="Expenses:Travel:Hotels",
                txn_date="2026-02-02",
            )

        # Get active synced transactions
        with get_db(db_path) as conn:
            active = get_active_monarch_synced_transactions(conn, "user1")
            assert len(active) == 2
            assert active[0].merchant == "Coffee Shop"
            assert active[0].amount == 50.0
            assert active[0].posted_account == "Expenses:Food:Coffee"

        # Mark one as recategorized
        with get_db(db_path) as conn:
            result = mark_monarch_transaction_recategorized(conn, "user1", "txn-123")
            assert result is True

        # Now only one is active
        with get_db(db_path) as conn:
            active = get_active_monarch_synced_transactions(conn, "user1")
            assert len(active) == 1
            assert active[0].monarch_transaction_id == "txn-456"

        # Marking again returns False (no row updated)
        with get_db(db_path) as conn:
            result = mark_monarch_transaction_recategorized(conn, "user1", "txn-123")
            assert result is False

    def test_csv_transaction_tracking(self, tmp_path):
        from istota.db import (
            get_db,
            init_db,
            is_csv_transaction_imported,
            track_csv_transaction,
            track_csv_transactions_batch,
        )

        db_path = tmp_path / "test.db"
        init_db(db_path)

        with get_db(db_path) as conn:
            hash1 = "abc123"
            hash2 = "def456"

            # Initially not tracked
            assert is_csv_transaction_imported(conn, "user1", hash1) is False

            # Track it
            track_csv_transaction(conn, "user1", hash1, "file.csv")

            # Now it's tracked
            assert is_csv_transaction_imported(conn, "user1", hash1) is True

            # Batch tracking
            count = track_csv_transactions_batch(conn, "user1", [hash1, hash2])
            # hash1 already exists, so only 1 new
            assert count == 1

    def test_compute_transaction_hash_without_account(self):
        from istota.db import compute_transaction_hash

        # Hash without account should be consistent
        hash1 = compute_transaction_hash("2026-01-15", -85.50, "Whole Foods")
        hash2 = compute_transaction_hash("2026-01-15", -85.50, "Whole Foods")
        assert hash1 == hash2
        assert len(hash1) == 64

        # Hash without account differs from hash with account
        hash_with = compute_transaction_hash("2026-01-15", -85.50, "Whole Foods", "Chase")
        assert hash1 != hash_with

    def test_is_content_hash_synced(self, tmp_path):
        from istota.db import (
            get_db,
            init_db,
            is_content_hash_synced,
            track_csv_transaction,
            track_monarch_transactions_batch,
        )

        db_path = tmp_path / "test.db"
        init_db(db_path)

        with get_db(db_path) as conn:
            # Initially not found
            assert is_content_hash_synced(conn, "user1", "hash-abc") is False

            # Track via CSV table
            track_csv_transaction(conn, "user1", "hash-csv", "file.csv")
            assert is_content_hash_synced(conn, "user1", "hash-csv") is True

            # Track via Monarch table
            track_monarch_transactions_batch(conn, "user1", [
                {"id": "txn-1", "content_hash": "hash-monarch"},
            ])
            assert is_content_hash_synced(conn, "user1", "hash-monarch") is True

            # Different user can't see it
            assert is_content_hash_synced(conn, "user2", "hash-csv") is False


class TestParseLedgerTransactions:
    """Tests for _parse_ledger_transactions() ledger scanning."""

    def test_parses_expense_transactions(self, tmp_path):
        from istota.skills.accounting import _parse_ledger_transactions
        from istota.db import compute_transaction_hash

        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-01-15 * "Whole Foods" "Groceries"\n'
            '  Expenses:Food:Groceries  85.50 USD\n'
            '  Assets:Bank:Checking\n'
            '\n'
            '2026-01-20 * "Shell Gas" "Fuel"\n'
            '  Expenses:Transport:Fuel  45.00 USD\n'
            '  Assets:Bank:Checking\n'
        )

        hashes = _parse_ledger_transactions(ledger)
        assert len(hashes) == 2

        expected = compute_transaction_hash("2026-01-15", 85.50, "Whole Foods")
        assert expected in hashes

        expected2 = compute_transaction_hash("2026-01-20", 45.00, "Shell Gas")
        assert expected2 in hashes

    def test_parses_income_transactions(self, tmp_path):
        from istota.skills.accounting import _parse_ledger_transactions
        from istota.db import compute_transaction_hash

        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-02-01 * "Employer Inc" "Salary"\n'
            '  Assets:Bank:Checking  5000.00 USD\n'
            '  Income:Salary\n'
        )

        hashes = _parse_ledger_transactions(ledger)
        assert len(hashes) == 1

        expected = compute_transaction_hash("2026-02-01", 5000.00, "Employer Inc")
        assert expected in hashes

    def test_empty_ledger(self, tmp_path):
        from istota.skills.accounting import _parse_ledger_transactions

        ledger = tmp_path / "main.beancount"
        ledger.write_text("; Empty ledger\n")

        assert _parse_ledger_transactions(ledger) == set()

    def test_nonexistent_ledger(self, tmp_path):
        from istota.skills.accounting import _parse_ledger_transactions

        assert _parse_ledger_transactions(tmp_path / "nope.beancount") == set()

    def test_includes_staging_files(self, tmp_path):
        from istota.skills.accounting import _parse_ledger_transactions
        from istota.db import compute_transaction_hash

        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-01-01 * "Store A" "Cat"\n'
            '  Expenses:Misc  10.00 USD\n'
            '  Assets:Bank\n'
        )

        imports_dir = tmp_path / "imports"
        imports_dir.mkdir()
        staging = imports_dir / "monarch_sync_20260201.beancount"
        staging.write_text(
            '2026-02-01 * "Store B" "Cat"\n'
            '  Expenses:Misc  20.00 USD\n'
            '  Assets:Bank\n'
        )

        hashes = _parse_ledger_transactions(ledger)
        assert len(hashes) == 2
        assert compute_transaction_hash("2026-01-01", 10.00, "Store A") in hashes
        assert compute_transaction_hash("2026-02-01", 20.00, "Store B") in hashes

    def test_handles_escaped_quotes_in_payee(self, tmp_path):
        from istota.skills.accounting import _parse_ledger_transactions

        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-01-15 * "Joe\'s Diner" "Food"\n'
            '  Expenses:Food  25.00 USD\n'
            '  Assets:Bank\n'
        )

        hashes = _parse_ledger_transactions(ledger)
        assert len(hashes) == 1

    def test_handles_comma_amounts(self, tmp_path):
        from istota.skills.accounting import _parse_ledger_transactions
        from istota.db import compute_transaction_hash

        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-01-15 * "Big Purchase" "Item"\n'
            '  Expenses:Misc  1,250.00 USD\n'
            '  Assets:Bank\n'
        )

        hashes = _parse_ledger_transactions(ledger)
        assert len(hashes) == 1
        assert compute_transaction_hash("2026-01-15", 1250.00, "Big Purchase") in hashes

    def test_applies_abs_to_negative_amounts(self, tmp_path):
        """Parsed amounts should use abs() so hashes match callers who use abs(amount)."""
        from istota.skills.accounting import _parse_ledger_transactions
        from istota.db import compute_transaction_hash

        ledger = tmp_path / "main.beancount"
        ledger.write_text(
            '2026-01-15 * "Refund Co" "Return"\n'
            '  Assets:Bank:Checking  -50.00 USD\n'
            '  Income:Refunds\n'
        )

        hashes = _parse_ledger_transactions(ledger)
        assert len(hashes) == 1
        # Caller computes with abs(amount), ledger parser should match
        assert compute_transaction_hash("2026-01-15", 50.00, "Refund Co") in hashes


class TestAppendToLedger:
    def test_appends_entries_to_ledger(self, tmp_path):
        from istota.skills.accounting import _append_to_ledger

        ledger = tmp_path / "main.beancount"
        ledger.write_text("2026-01-01 open Assets:Bank USD\n")

        entries = [
            '2026-01-15 * "Store" "Groceries"\n  Expenses:Food  50.00 USD\n  Assets:Bank',
            '2026-01-16 * "Gas" "Fuel"\n  Expenses:Transport  30.00 USD\n  Assets:Bank',
        ]

        _append_to_ledger(ledger, entries)

        content = ledger.read_text()
        assert "Store" in content
        assert "Gas" in content
        assert content.startswith("2026-01-01 open Assets:Bank USD\n")

    def test_noop_on_empty_list(self, tmp_path):
        from istota.skills.accounting import _append_to_ledger

        ledger = tmp_path / "main.beancount"
        original = "2026-01-01 open Assets:Bank USD\n"
        ledger.write_text(original)

        _append_to_ledger(ledger, [])

        assert ledger.read_text() == original
        # No backup should be created for empty list
        assert not (tmp_path / "backups").exists()


class TestRestartFava:
    def test_calls_systemctl_restart(self, monkeypatch):
        from unittest.mock import patch

        from istota.skills.accounting import _restart_fava

        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        with patch("istota.skills.accounting.subprocess.run") as mock_run:
            _restart_fava()
            mock_run.assert_called_once_with(
                [
                    "sudo",
                    "--non-interactive",
                    "systemctl",
                    "restart",
                    "istota-fava-alice.service",
                ],
                capture_output=True,
                timeout=10,
            )

    def test_noop_without_user_id(self, monkeypatch):
        from unittest.mock import patch

        from istota.skills.accounting import _restart_fava

        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        with patch("istota.skills.accounting.subprocess.run") as mock_run:
            _restart_fava()
            mock_run.assert_not_called()

    def test_ignores_failures(self, monkeypatch):
        from unittest.mock import patch

        from istota.skills.accounting import _restart_fava

        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        with patch(
            "istota.skills.accounting.subprocess.run",
            side_effect=subprocess.TimeoutExpired("sudo", 10),
        ):
            _restart_fava()  # Should not raise

    def test_append_to_ledger_triggers_restart(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        from istota.skills.accounting import _append_to_ledger

        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        ledger = tmp_path / "main.beancount"
        ledger.write_text("2026-01-01 open Assets:Bank USD\n")

        with patch("istota.skills.accounting._restart_fava") as mock_restart:
            _append_to_ledger(ledger, ["2026-01-15 * \"Test\" \"\"\n  Expenses:Test  1 USD\n  Assets:Bank"])
            mock_restart.assert_called_once()


class TestBackupLedger:
    def test_creates_backup(self, tmp_path):
        from istota.skills.accounting import _backup_ledger

        ledger = tmp_path / "main.beancount"
        ledger.write_text("original content")

        backup_path = _backup_ledger(ledger)

        assert backup_path is not None
        assert backup_path.exists()
        assert backup_path.read_text() == "original content"
        assert backup_path.parent == tmp_path / "backups"

    def test_rotates_old_backups(self, tmp_path):
        from istota.skills.accounting import _backup_ledger
        import time

        ledger = tmp_path / "main.beancount"
        ledger.write_text("content")
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()

        # Create 10 existing backups
        for i in range(10):
            (backups_dir / f"main.beancount.20260101_0000{i:02d}").write_text(f"old_{i}")
            time.sleep(0.01)

        _backup_ledger(ledger, max_backups=5)

        remaining = list(backups_dir.glob("main.beancount.*"))
        assert len(remaining) == 5

    def test_returns_none_for_missing_ledger(self, tmp_path):
        from istota.skills.accounting import _backup_ledger

        result = _backup_ledger(tmp_path / "nonexistent.beancount")
        assert result is None


class TestDeferredTracking:
    """Tests for _write_deferred_tracking() helper."""

    def test_creates_file_with_env_vars(self, tmp_path, monkeypatch):
        from istota.skills.accounting import _write_deferred_tracking
        deferred_dir = tmp_path / "deferred"
        deferred_dir.mkdir()
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred_dir))
        monkeypatch.setenv("ISTOTA_TASK_ID", "42")

        result = _write_deferred_tracking(
            monarch_synced=[{"id": "txn_1", "amount": 10.0, "merchant": "Shop"}],
        )
        assert result is True

        path = deferred_dir / "task_42_tracked_transactions.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data["monarch_synced"]) == 1
        assert data["monarch_synced"][0]["id"] == "txn_1"

    def test_appends_to_existing_file(self, tmp_path, monkeypatch):
        from istota.skills.accounting import _write_deferred_tracking
        deferred_dir = tmp_path / "deferred"
        deferred_dir.mkdir()
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred_dir))
        monkeypatch.setenv("ISTOTA_TASK_ID", "42")

        # First call
        _write_deferred_tracking(
            monarch_synced=[{"id": "txn_1", "amount": 10.0, "merchant": "A"}],
        )
        # Second call
        _write_deferred_tracking(
            csv_imported=[{"content_hash": "h1", "source_file": "f.csv"}],
        )

        data = json.loads((deferred_dir / "task_42_tracked_transactions.json").read_text())
        assert len(data["monarch_synced"]) == 1
        assert len(data["csv_imported"]) == 1

    def test_returns_none_without_env(self, monkeypatch):
        from istota.skills.accounting import _write_deferred_tracking
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)

        result = _write_deferred_tracking(
            monarch_synced=[{"id": "txn_1"}],
        )
        assert result is None

    def test_deferred_tracking_category_updates(self, monkeypatch, tmp_path):
        from istota.skills.accounting import _write_deferred_tracking

        deferred_dir = tmp_path / "deferred"
        deferred_dir.mkdir()
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred_dir))
        monkeypatch.setenv("ISTOTA_TASK_ID", "99")

        result = _write_deferred_tracking(
            monarch_category_updates=[
                {"monarch_transaction_id": "txn-123", "posted_account": "Expenses:Entertainment"},
            ],
        )

        assert result is True
        data = json.loads((deferred_dir / "task_99_tracked_transactions.json").read_text())
        assert len(data["monarch_category_updates"]) == 1
        assert data["monarch_category_updates"][0]["monarch_transaction_id"] == "txn-123"
        assert data["monarch_category_updates"][0]["posted_account"] == "Expenses:Entertainment"


class TestMonarchCategoryUpdateTracking:
    def test_update_monarch_transaction_posted_account(self, tmp_path):
        from istota.db import (
            get_db,
            init_db,
            track_monarch_transaction,
            get_active_monarch_synced_transactions,
            update_monarch_transaction_posted_account,
        )

        db_path = tmp_path / "test.db"
        init_db(db_path)

        with get_db(db_path) as conn:
            track_monarch_transaction(
                conn, "user1", "txn-123",
                tags_json='["Business"]',
                amount=504.0,
                merchant="PayPal",
                posted_account="Expenses:Office-Supplies",
                txn_date="2026-02-10",
            )

        # Update the posted account
        with get_db(db_path) as conn:
            result = update_monarch_transaction_posted_account(
                conn, "user1", "txn-123", "Expenses:Software:Subscriptions",
            )
            assert result is True

        # Verify the update
        with get_db(db_path) as conn:
            active = get_active_monarch_synced_transactions(conn, "user1")
            assert len(active) == 1
            assert active[0].posted_account == "Expenses:Software:Subscriptions"

    def test_update_does_not_affect_recategorized(self, tmp_path):
        from istota.db import (
            get_db,
            init_db,
            track_monarch_transaction,
            mark_monarch_transaction_recategorized,
            update_monarch_transaction_posted_account,
        )

        db_path = tmp_path / "test.db"
        init_db(db_path)

        with get_db(db_path) as conn:
            track_monarch_transaction(
                conn, "user1", "txn-123",
                tags_json='["Business"]',
                amount=50.0,
                merchant="Store",
                posted_account="Expenses:Shopping",
                txn_date="2026-02-10",
            )
            mark_monarch_transaction_recategorized(conn, "user1", "txn-123")

        # Updating a recategorized transaction should fail
        with get_db(db_path) as conn:
            result = update_monarch_transaction_posted_account(
                conn, "user1", "txn-123", "Expenses:Food",
            )
            assert result is False

    def test_update_wrong_user(self, tmp_path):
        from istota.db import (
            get_db,
            init_db,
            track_monarch_transaction,
            update_monarch_transaction_posted_account,
        )

        db_path = tmp_path / "test.db"
        init_db(db_path)

        with get_db(db_path) as conn:
            track_monarch_transaction(
                conn, "user1", "txn-123",
                tags_json='["Business"]',
                amount=50.0,
                merchant="Store",
                posted_account="Expenses:Shopping",
                txn_date="2026-02-10",
            )

        # Wrong user should not update
        with get_db(db_path) as conn:
            result = update_monarch_transaction_posted_account(
                conn, "user2", "txn-123", "Expenses:Food",
            )
            assert result is False
