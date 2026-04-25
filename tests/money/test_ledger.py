"""Tests for money.core.ledger module."""

import subprocess
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from money.core.ledger import (
    run_bean_check,
    run_bean_query,
    check,
    balances,
    query,
    report,
    lots,
    detect_wash_sales,
    wash_sales,
)
from money.core.models import PurchaseTransaction, SaleTransaction


class TestBeanCheck:
    @patch("money.core.ledger.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        success, errors = run_bean_check(Path("/test/ledger.beancount"))
        assert success is True
        assert errors == []
        assert "bean-check" in mock_run.call_args[0][0][0]

    @patch("money.core.ledger.subprocess.run")
    def test_errors(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="ledger.beancount:10: Invalid account 'Foo'\nledger.beancount:20: Missing narration"
        )
        success, errors = run_bean_check(Path("/test/ledger.beancount"))
        assert success is False
        assert len(errors) == 2
        assert "Invalid account" in errors[0]

    @patch("money.core.ledger.subprocess.run")
    def test_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        with pytest.raises(ValueError, match="bean-check not found"):
            run_bean_check(Path("/test/ledger.beancount"))

    @patch("money.core.ledger.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="bean-check", timeout=60)
        with pytest.raises(ValueError, match="timed out"):
            run_bean_check(Path("/test/ledger.beancount"))


class TestBeanQuery:
    @patch("money.core.ledger.subprocess.run")
    def test_success(self, mock_run):
        csv_output = "account,sum(position)\nAssets:Bank,1000 USD\nExpenses:Food,500 USD\n"
        mock_run.return_value = MagicMock(returncode=0, stdout=csv_output, stderr="")
        result = run_bean_query(Path("/test/ledger.beancount"), "SELECT account, sum(position)")
        assert len(result) == 2
        assert result[0]["account"] == "Assets:Bank"

    @patch("money.core.ledger.subprocess.run")
    def test_empty_result(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = run_bean_query(Path("/test/ledger.beancount"), "SELECT * WHERE 1=0")
        assert result == []

    @patch("money.core.ledger.subprocess.run")
    def test_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Invalid query syntax")
        with pytest.raises(ValueError, match="Invalid query syntax"):
            run_bean_query(Path("/test/ledger.beancount"), "INVALID QUERY")


class TestCheck:
    @patch("money.core.ledger.run_bean_check")
    def test_success(self, mock_check, tmp_path):
        ledger = tmp_path / "ledger.beancount"
        ledger.write_text("")
        mock_check.return_value = (True, [])
        result = check(ledger)
        assert result["status"] == "ok"
        assert result["error_count"] == 0

    @patch("money.core.ledger.run_bean_check")
    def test_with_errors(self, mock_check, tmp_path):
        ledger = tmp_path / "ledger.beancount"
        ledger.write_text("")
        mock_check.return_value = (False, ["Error 1", "Error 2"])
        result = check(ledger)
        assert result["status"] == "error"
        assert result["error_count"] == 2

    def test_file_not_found(self, tmp_path):
        result = check(tmp_path / "nonexistent.beancount")
        assert result["status"] == "error"
        assert "not found" in result["error"]


class TestBalances:
    @patch("money.core.ledger.run_bean_query")
    def test_all_accounts(self, mock_query):
        mock_query.return_value = [
            {"account": "Assets:Bank", "sum(position)": "1000 USD"},
            {"account": "Expenses:Food", "sum(position)": "500 USD"},
        ]
        result = balances(Path("/test/ledger.beancount"))
        assert result["status"] == "ok"
        assert result["account_count"] == 2

    @patch("money.core.ledger.run_bean_query")
    def test_with_filter(self, mock_query):
        mock_query.return_value = [{"account": "Assets:Bank", "sum(position)": "1000 USD"}]
        result = balances(Path("/test/ledger.beancount"), account="Assets:Bank")
        assert result["status"] == "ok"
        assert "Assets:Bank" in mock_query.call_args[0][1]


class TestQuery:
    @patch("money.core.ledger.run_bean_query")
    def test_success(self, mock_query):
        mock_query.return_value = [{"date": "2026-01-15", "payee": "Store"}]
        result = query(Path("/test/ledger.beancount"), "SELECT date, payee LIMIT 1")
        assert result["status"] == "ok"
        assert result["row_count"] == 1


class TestReport:
    @patch("money.core.ledger.run_bean_query")
    def test_income_statement(self, mock_query):
        mock_query.return_value = [
            {"account": "Income:Salary", "sum(position)": "-60000 USD"},
        ]
        result = report(Path("/test/ledger.beancount"), "income-statement", 2026)
        assert result["status"] == "ok"
        assert result["report_type"] == "income-statement"
        assert result["year"] == 2026

    def test_unknown_type(self):
        result = report(Path("/test/ledger.beancount"), "unknown-type")
        assert result["status"] == "error"


class TestLots:
    @patch("money.core.ledger.run_bean_query")
    def test_success(self, mock_query):
        mock_query.return_value = [
            {"account": "Assets:Investment", "units(position)": "10 AAPL"}
        ]
        result = lots(Path("/test/ledger.beancount"), "aapl")
        assert result["status"] == "ok"
        assert result["symbol"] == "AAPL"
        assert result["lot_count"] == 1


class TestWashSales:
    def test_detect_within_30_days(self):
        sales = [SaleTransaction(
            date=date(2026, 6, 15), account="Assets:Investment",
            symbol="AAPL", units=10, proceeds=1400.0, cost_basis=1500.0, gain_loss=-100.0,
        )]
        purchases = [PurchaseTransaction(
            date=date(2026, 6, 20), account="Assets:Investment",
            symbol="AAPL", units=5, cost=700.0,
        )]
        violations = detect_wash_sales(sales, purchases, 2026)
        assert len(violations) == 1
        assert violations[0]["symbol"] == "AAPL"
        assert violations[0]["loss_amount"] == -100.0
        assert violations[0]["triggering_purchases"][0]["days_from_sale"] == 5

    def test_no_wash_sale_outside_30_days(self):
        sales = [SaleTransaction(
            date=date(2026, 6, 15), account="Assets:Investment",
            symbol="AAPL", units=10, proceeds=1400.0, cost_basis=1500.0, gain_loss=-100.0,
        )]
        purchases = [PurchaseTransaction(
            date=date(2026, 7, 20), account="Assets:Investment",
            symbol="AAPL", units=5, cost=700.0,
        )]
        violations = detect_wash_sales(sales, purchases, 2026)
        assert len(violations) == 0

    def test_no_wash_sale_different_symbol(self):
        sales = [SaleTransaction(
            date=date(2026, 6, 15), account="Assets:Investment",
            symbol="AAPL", units=10, proceeds=1400.0, cost_basis=1500.0, gain_loss=-100.0,
        )]
        purchases = [PurchaseTransaction(
            date=date(2026, 6, 20), account="Assets:Investment",
            symbol="GOOGL", units=5, cost=700.0,
        )]
        violations = detect_wash_sales(sales, purchases, 2026)
        assert len(violations) == 0

    def test_wash_sale_before_sale(self):
        sales = [SaleTransaction(
            date=date(2026, 6, 15), account="Assets:Investment",
            symbol="AAPL", units=10, proceeds=1400.0, cost_basis=1500.0, gain_loss=-100.0,
        )]
        purchases = [PurchaseTransaction(
            date=date(2026, 6, 1), account="Assets:Investment",
            symbol="AAPL", units=5, cost=700.0,
        )]
        violations = detect_wash_sales(sales, purchases, 2026)
        assert len(violations) == 1
        assert violations[0]["triggering_purchases"][0]["days_from_sale"] == -14

    def test_wrong_year_ignored(self):
        sales = [SaleTransaction(
            date=date(2026, 6, 15), account="Assets:Investment",
            symbol="AAPL", units=10, proceeds=1600.0, cost_basis=1500.0, gain_loss=100.0,
        )]
        purchases = [PurchaseTransaction(
            date=date(2026, 6, 20), account="Assets:Investment",
            symbol="AAPL", units=5, cost=700.0,
        )]
        violations = detect_wash_sales(sales, purchases, 2025)
        assert len(violations) == 0
