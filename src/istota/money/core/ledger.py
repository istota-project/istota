"""Beancount ledger operations: validation, queries, reports, lots, wash sales."""

from __future__ import annotations

import csv
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import PurchaseTransaction, SaleTransaction


def _bean_cmd(name: str) -> str:
    """Resolve a beancount CLI tool, preferring the current venv's bin directory."""
    venv_bin = Path(sys.executable).parent / name
    if venv_bin.exists():
        return str(venv_bin)
    return name


def run_bean_check(ledger_path: Path) -> tuple[bool, list[str]]:
    """Run bean-check on the ledger file.

    Returns:
        Tuple of (success, list of error messages)
    """
    try:
        result = subprocess.run(
            [_bean_cmd("bean-check"), str(ledger_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return True, []

        errors = [line.strip() for line in result.stderr.strip().split("\n") if line.strip()]
        return False, errors
    except FileNotFoundError:
        raise ValueError("bean-check not found. Is beancount installed?")
    except subprocess.TimeoutExpired:
        raise ValueError("bean-check timed out")


def run_bean_query(ledger_path: Path, query: str) -> list[dict]:
    """Run bean-query and return results as list of dicts."""
    try:
        result = subprocess.run(
            [_bean_cmd("bean-query"), str(ledger_path), query, "-f", "csv"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or "Query failed"
            raise ValueError(error_msg)

        output = result.stdout.strip()
        if not output:
            return []

        rows = []
        reader = csv.DictReader(output.split("\n"))
        for row in reader:
            rows.append(dict(row))

        return rows
    except FileNotFoundError:
        raise ValueError("bean-query not found. Is beancount installed?")
    except subprocess.TimeoutExpired:
        raise ValueError("bean-query timed out")


def check(ledger_path: Path) -> dict:
    """Validate the ledger file."""
    if not ledger_path.exists():
        return {"status": "error", "error": "Ledger file not found"}

    success, errors = run_bean_check(ledger_path)

    if success:
        return {"status": "ok", "message": "Ledger is valid", "error_count": 0}
    else:
        return {
            "status": "error",
            "message": "Ledger has errors",
            "error_count": len(errors),
            "errors": errors[:20],
        }


def _sanitize_bql_string(value: str) -> str:
    """Escape single quotes in a value interpolated into a BQL query string."""
    return value.replace("'", "''")


def balances(ledger_path: Path, account: str | None = None) -> dict:
    """Show account balances."""
    if account:
        safe_account = _sanitize_bql_string(account)
        query = f"SELECT account, sum(position) WHERE account ~ '{safe_account}' GROUP BY account ORDER BY account"
    else:
        query = "SELECT account, sum(position) GROUP BY account ORDER BY account"

    try:
        rows = run_bean_query(ledger_path, query)
        return {
            "status": "ok",
            "account_count": len(rows),
            "balances": rows,
        }
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def query(ledger_path: Path, bql: str) -> dict:
    """Run a BQL query."""
    try:
        rows = run_bean_query(ledger_path, bql)
        return {
            "status": "ok",
            "row_count": len(rows),
            "results": rows,
        }
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def report(ledger_path: Path, report_type: str, year: int | None = None) -> dict:
    """Generate a financial report."""
    if report_type == "income-statement":
        year_filter = f"\n            AND year = {year}" if year else ""
        bql = f"""
            SELECT account, sum(position)
            WHERE account ~ '^(Income|Expenses):'{year_filter}
            GROUP BY account
            ORDER BY account
        """
    elif report_type == "cash-flow":
        year_filter = f"\n            AND year = {year}" if year else ""
        bql = f"""
            SELECT year, month, account, sum(position)
            WHERE account ~ '^(Income|Expenses):'{year_filter}
            GROUP BY year, month, account
            ORDER BY year, month, account
        """
    elif report_type == "balance-sheet":
        bql = f"""
            SELECT account, sum(position)
            WHERE account ~ '^(Assets|Liabilities|Equity):'
            GROUP BY account
            ORDER BY account
        """
    else:
        return {"status": "error", "error": f"Unknown report type: {report_type}"}

    try:
        rows = run_bean_query(ledger_path, bql)
        return {
            "status": "ok",
            "report_type": report_type,
            "year": year,
            "row_count": len(rows),
            "results": rows,
        }
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def lots(ledger_path: Path, symbol: str) -> dict:
    """Show open lots for a security symbol."""
    symbol = symbol.upper()
    safe_symbol = _sanitize_bql_string(symbol)

    bql = f"""
        SELECT account, units(position), cost(position), cost_date
        WHERE currency = '{safe_symbol}'
        ORDER BY account, cost_date
    """

    try:
        rows = run_bean_query(ledger_path, bql)
        return {
            "status": "ok",
            "symbol": symbol,
            "lot_count": len(rows),
            "lots": rows,
        }
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def parse_transactions_for_wash_sales(
    ledger_path: Path, year: int
) -> tuple[list[SaleTransaction], list[PurchaseTransaction]]:
    """Parse ledger for sales with losses and purchases for wash sale detection."""
    start_date = date(year, 1, 1) - timedelta(days=30)
    end_date = date(year, 12, 31) + timedelta(days=30)

    sales_query = f"""
        SELECT date, account, currency, units(position), cost(position), value(position)
        WHERE units(position) < 0
        AND date >= {start_date.isoformat()}
        AND date <= {end_date.isoformat()}
        AND account ~ '^Assets:'
        ORDER BY date
    """

    purchases_query = f"""
        SELECT date, account, currency, units(position), cost(position)
        WHERE units(position) > 0
        AND date >= {start_date.isoformat()}
        AND date <= {end_date.isoformat()}
        AND account ~ '^Assets:'
        ORDER BY date
    """

    sales = []
    try:
        sale_rows = run_bean_query(ledger_path, sales_query)
        for row in sale_rows:
            try:
                txn_date = datetime.strptime(row.get("date", ""), "%Y-%m-%d").date()
                units_str = row.get("units(position)", "0")
                units = abs(float(re.sub(r"[^\d.-]", "", units_str.split()[0])))
                cost_str = row.get("cost(position)", "0")
                cost_match = re.search(r"([\d.]+)", cost_str)
                cost_basis = float(cost_match.group(1)) if cost_match else 0.0
                value_str = row.get("value(position)", "0")
                value_match = re.search(r"([\d.]+)", value_str)
                proceeds = float(value_match.group(1)) if value_match else 0.0
                gain_loss = proceeds - cost_basis

                if gain_loss < 0:
                    sales.append(SaleTransaction(
                        date=txn_date, account=row.get("account", ""),
                        symbol=row.get("currency", ""), units=units,
                        proceeds=proceeds, cost_basis=cost_basis, gain_loss=gain_loss,
                    ))
            except (ValueError, TypeError):
                continue
    except ValueError:
        pass

    purchases = []
    try:
        purchase_rows = run_bean_query(ledger_path, purchases_query)
        for row in purchase_rows:
            try:
                txn_date = datetime.strptime(row.get("date", ""), "%Y-%m-%d").date()
                units_str = row.get("units(position)", "0")
                units = float(re.sub(r"[^\d.-]", "", units_str.split()[0]))
                cost_str = row.get("cost(position)", "0")
                cost_match = re.search(r"([\d.]+)", cost_str)
                cost = float(cost_match.group(1)) if cost_match else 0.0
                purchases.append(PurchaseTransaction(
                    date=txn_date, account=row.get("account", ""),
                    symbol=row.get("currency", ""), units=units, cost=cost,
                ))
            except (ValueError, TypeError):
                continue
    except ValueError:
        pass

    return sales, purchases


def detect_wash_sales(
    sales: list[SaleTransaction],
    purchases: list[PurchaseTransaction],
    year: int,
) -> list[dict]:
    """Detect wash sale violations.

    A wash sale occurs when you sell a security at a loss and purchase
    substantially identical securities within 30 days before or after the sale.
    """
    violations = []

    for sale in sales:
        if sale.date.year != year:
            continue

        wash_window_start = sale.date - timedelta(days=30)
        wash_window_end = sale.date + timedelta(days=30)

        matching_purchases = [
            p for p in purchases
            if p.symbol == sale.symbol
            and wash_window_start <= p.date <= wash_window_end
            and p.date != sale.date
        ]

        if matching_purchases:
            violations.append({
                "sale_date": sale.date.isoformat(),
                "symbol": sale.symbol,
                "units_sold": sale.units,
                "loss_amount": round(sale.gain_loss, 2),
                "triggering_purchases": [
                    {
                        "date": p.date.isoformat(),
                        "units": p.units,
                        "days_from_sale": (p.date - sale.date).days,
                    }
                    for p in matching_purchases
                ],
            })

    return violations


def wash_sales(ledger_path: Path, year: int | None = None) -> dict:
    """Detect potential wash sale violations."""
    year = year or date.today().year

    try:
        sales, purchases = parse_transactions_for_wash_sales(ledger_path, year)
        violations = detect_wash_sales(sales, purchases, year)

        return {
            "status": "ok",
            "year": year,
            "sales_with_losses": len(sales),
            "violation_count": len(violations),
            "violations": violations,
        }
    except ValueError as e:
        return {"status": "error", "error": str(e)}
