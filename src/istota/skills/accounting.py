"""Beancount accounting operations.

Provides a CLI for ledger operations from Claude Code:
    python -m istota.skills.accounting check
    python -m istota.skills.accounting balances [--account PATTERN]
    python -m istota.skills.accounting query "BQL"
    python -m istota.skills.accounting report TYPE [--year YYYY]
    python -m istota.skills.accounting lots SYMBOL
    python -m istota.skills.accounting wash-sales [--year YYYY]
    python -m istota.skills.accounting import-monarch FILE --account ACCT [--tag TAG] [--exclude-tag TAG]
    python -m istota.skills.accounting sync-monarch [--dry-run]
    python -m istota.skills.accounting add-transaction --date DATE --payee PAYEE ...
    python -m istota.skills.accounting invoice generate [--period YYYY-MM] [--client CLIENT]
    python -m istota.skills.accounting invoice list [--client CLIENT] [--overdue]
    python -m istota.skills.accounting invoice paid INV-XXX --date YYYY-MM-DD
    python -m istota.skills.accounting invoice create CLIENT --service SVC --qty N
"""

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import tomli
except ImportError:
    tomli = None  # type: ignore


# =============================================================================
# Monarch Money Configuration Dataclasses
# =============================================================================


@dataclass
class MonarchCredentials:
    """Credentials for Monarch Money API authentication."""
    email: str | None = None
    password: str | None = None
    session_token: str | None = None


@dataclass
class MonarchSyncSettings:
    """Settings for Monarch Money sync behavior."""
    lookback_days: int = 30
    default_account: str = "Assets:Bank:Checking"
    recategorize_account: str = "Expenses:Personal-Expense"


@dataclass
class MonarchTagFilters:
    """Tag-based transaction filtering."""
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class MonarchConfig:
    """Complete Monarch Money configuration from ACCOUNTING.md."""
    credentials: MonarchCredentials
    sync: MonarchSyncSettings
    accounts: dict[str, str]  # Monarch account name -> beancount account
    categories: dict[str, str]  # Monarch category -> beancount account (overrides)
    tags: MonarchTagFilters


def _extract_toml_from_markdown(text: str) -> str:
    """Extract TOML content from markdown code blocks."""
    pattern = r"```toml\s*\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    return "\n".join(matches)


def _get_accounting_config_path() -> Path:
    """Get accounting config path from environment variable."""
    path = os.environ.get("ACCOUNTING_CONFIG", "")
    if not path:
        raise ValueError("ACCOUNTING_CONFIG environment variable is required")
    return Path(path)


def parse_accounting_config(config_path: Path) -> MonarchConfig:
    """Parse ACCOUNTING.md config file into MonarchConfig.

    Args:
        config_path: Path to ACCOUNTING.md file

    Returns:
        MonarchConfig with parsed settings
    """
    if tomli is None:
        raise ValueError("tomli is required for config parsing")

    content = config_path.read_text()
    toml_content = _extract_toml_from_markdown(content)

    if not toml_content.strip():
        raise ValueError("No TOML content found in ACCOUNTING.md")

    data = tomli.loads(toml_content)

    # Parse monarch section
    monarch = data.get("monarch", {})

    credentials = MonarchCredentials(
        email=monarch.get("email"),
        password=monarch.get("password"),
        session_token=monarch.get("session_token"),
    )

    sync_data = monarch.get("sync", {})
    sync = MonarchSyncSettings(
        lookback_days=sync_data.get("lookback_days", 30),
        default_account=sync_data.get("default_account", "Assets:Bank:Checking"),
        recategorize_account=sync_data.get("recategorize_account", "Expenses:Personal-Expense"),
    )

    accounts = monarch.get("accounts", {})
    categories = monarch.get("categories", {})

    tags_data = monarch.get("tags", {})
    tags = MonarchTagFilters(
        include=tags_data.get("include", []),
        exclude=tags_data.get("exclude", []),
    )

    return MonarchConfig(
        credentials=credentials,
        sync=sync,
        accounts=accounts,
        categories=categories,
        tags=tags,
    )


# =============================================================================
# Ledger Path Helpers
# =============================================================================


def _get_ledger_path(ledger_name: str | None = None) -> Path:
    """Get the ledger path from environment variable.

    Args:
        ledger_name: Optional name to select from multiple ledgers (via LEDGER_PATHS).
                     If None, uses LEDGER_PATH (first/default ledger).

    Returns:
        Path to the ledger file.
    """
    # If a specific ledger name is requested, look it up in LEDGER_PATHS
    if ledger_name:
        ledger_paths_json = os.environ.get("LEDGER_PATHS", "")
        if ledger_paths_json:
            try:
                ledgers = json.loads(ledger_paths_json)
                for ledger in ledgers:
                    if ledger.get("name", "").lower() == ledger_name.lower():
                        return Path(ledger["path"])
                # List available ledgers in error message
                available = [l.get("name", "unnamed") for l in ledgers]
                raise ValueError(f"Ledger '{ledger_name}' not found. Available: {', '.join(available)}")
            except json.JSONDecodeError:
                raise ValueError("Invalid LEDGER_PATHS JSON")

    # Default: use LEDGER_PATH
    path = os.environ.get("LEDGER_PATH", "")
    if not path:
        raise ValueError("LEDGER_PATH environment variable is required")
    return Path(path)


def _run_bean_check(ledger_path: Path) -> tuple[bool, list[str]]:
    """Run bean-check on the ledger file.

    Returns:
        Tuple of (success, list of error messages)
    """
    try:
        result = subprocess.run(
            ["bean-check", str(ledger_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return True, []

        # Parse errors from stderr
        errors = [line.strip() for line in result.stderr.strip().split("\n") if line.strip()]
        return False, errors
    except FileNotFoundError:
        raise ValueError("bean-check not found. Is beancount installed?")
    except subprocess.TimeoutExpired:
        raise ValueError("bean-check timed out")


def _run_bean_query(ledger_path: Path, query: str) -> list[dict]:
    """Run bean-query and return results as list of dicts.

    Args:
        ledger_path: Path to ledger file
        query: BQL query string

    Returns:
        List of result rows as dictionaries
    """
    try:
        result = subprocess.run(
            ["bean-query", str(ledger_path), query, "-f", "csv"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or "Query failed"
            raise ValueError(error_msg)

        # Parse CSV output
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


def cmd_check(args) -> dict:
    """Validate the ledger file."""
    ledger_path = _get_ledger_path(getattr(args, 'ledger', None))

    if not ledger_path.exists():
        return {"status": "error", "error": f"Ledger file not found: {ledger_path}"}

    success, errors = _run_bean_check(ledger_path)

    if success:
        return {"status": "ok", "message": "Ledger is valid", "error_count": 0}
    else:
        return {
            "status": "error",
            "message": "Ledger has errors",
            "error_count": len(errors),
            "errors": errors[:20],  # Limit to first 20 errors
        }


def cmd_balances(args) -> dict:
    """Show account balances."""
    ledger_path = _get_ledger_path(getattr(args, 'ledger', None))

    # Build query
    if args.account:
        query = f"SELECT account, sum(position) WHERE account ~ '{args.account}' GROUP BY account ORDER BY account"
    else:
        query = "SELECT account, sum(position) GROUP BY account ORDER BY account"

    try:
        rows = _run_bean_query(ledger_path, query)
        return {
            "status": "ok",
            "account_count": len(rows),
            "balances": rows,
        }
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def cmd_query(args) -> dict:
    """Run a BQL query."""
    ledger_path = _get_ledger_path(getattr(args, 'ledger', None))

    try:
        rows = _run_bean_query(ledger_path, args.query)
        return {
            "status": "ok",
            "row_count": len(rows),
            "results": rows,
        }
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def cmd_report(args) -> dict:
    """Generate a financial report."""
    ledger_path = _get_ledger_path(getattr(args, 'ledger', None))
    year = args.year or date.today().year

    if args.type == "income-statement":
        # Income statement: Revenue - Expenses
        query = f"""
            SELECT account, sum(position)
            WHERE account ~ '^(Income|Expenses):'
            AND year = {year}
            GROUP BY account
            ORDER BY account
        """
    elif args.type == "balance-sheet":
        # Balance sheet: Assets, Liabilities, Equity as of end of year
        query = f"""
            SELECT account, sum(position)
            WHERE account ~ '^(Assets|Liabilities|Equity):'
            GROUP BY account
            ORDER BY account
        """
    else:
        return {"status": "error", "error": f"Unknown report type: {args.type}"}

    try:
        rows = _run_bean_query(ledger_path, query)
        return {
            "status": "ok",
            "report_type": args.type,
            "year": year,
            "row_count": len(rows),
            "results": rows,
        }
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def cmd_lots(args) -> dict:
    """Show open lots for a security symbol."""
    ledger_path = _get_ledger_path(getattr(args, 'ledger', None))
    symbol = args.symbol.upper()

    # Query for open positions with cost basis
    query = f"""
        SELECT account, units(position), cost(position), cost_date
        WHERE currency = '{symbol}'
        ORDER BY account, cost_date
    """

    try:
        rows = _run_bean_query(ledger_path, query)
        return {
            "status": "ok",
            "symbol": symbol,
            "lot_count": len(rows),
            "lots": rows,
        }
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@dataclass
class SaleTransaction:
    """Represents a sale transaction for wash sale analysis."""
    date: date
    account: str
    symbol: str
    units: float
    proceeds: float
    cost_basis: float
    gain_loss: float


@dataclass
class PurchaseTransaction:
    """Represents a purchase transaction for wash sale analysis."""
    date: date
    account: str
    symbol: str
    units: float
    cost: float


def _parse_transactions_for_wash_sales(
    ledger_path: Path, year: int
) -> tuple[list[SaleTransaction], list[PurchaseTransaction]]:
    """Parse ledger for sales with losses and purchases for wash sale detection.

    Returns:
        Tuple of (sales with losses, all purchases in wash sale window)
    """
    # Get sales (negative units indicates a sale)
    # We need to look at the year and 30 days before/after
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
        sale_rows = _run_bean_query(ledger_path, sales_query)
        for row in sale_rows:
            try:
                # Parse the date
                txn_date = datetime.strptime(row.get("date", ""), "%Y-%m-%d").date()

                # Parse units (negative for sales)
                units_str = row.get("units(position)", "0")
                units = abs(float(re.sub(r"[^\d.-]", "", units_str.split()[0])))

                # Parse cost basis
                cost_str = row.get("cost(position)", "0")
                cost_match = re.search(r"([\d.]+)", cost_str)
                cost_basis = float(cost_match.group(1)) if cost_match else 0.0

                # Parse proceeds (value)
                value_str = row.get("value(position)", "0")
                value_match = re.search(r"([\d.]+)", value_str)
                proceeds = float(value_match.group(1)) if value_match else 0.0

                # Calculate gain/loss
                gain_loss = proceeds - cost_basis

                # Only include losses
                if gain_loss < 0:
                    sales.append(SaleTransaction(
                        date=txn_date,
                        account=row.get("account", ""),
                        symbol=row.get("currency", ""),
                        units=units,
                        proceeds=proceeds,
                        cost_basis=cost_basis,
                        gain_loss=gain_loss,
                    ))
            except (ValueError, TypeError):
                continue
    except ValueError:
        pass

    purchases = []
    try:
        purchase_rows = _run_bean_query(ledger_path, purchases_query)
        for row in purchase_rows:
            try:
                txn_date = datetime.strptime(row.get("date", ""), "%Y-%m-%d").date()

                units_str = row.get("units(position)", "0")
                units = float(re.sub(r"[^\d.-]", "", units_str.split()[0]))

                cost_str = row.get("cost(position)", "0")
                cost_match = re.search(r"([\d.]+)", cost_str)
                cost = float(cost_match.group(1)) if cost_match else 0.0

                purchases.append(PurchaseTransaction(
                    date=txn_date,
                    account=row.get("account", ""),
                    symbol=row.get("currency", ""),
                    units=units,
                    cost=cost,
                ))
            except (ValueError, TypeError):
                continue
    except ValueError:
        pass

    return sales, purchases


def _detect_wash_sales(
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
        # Only check sales in the target year
        if sale.date.year != year:
            continue

        # Find purchases of same symbol within 30 days
        wash_window_start = sale.date - timedelta(days=30)
        wash_window_end = sale.date + timedelta(days=30)

        matching_purchases = [
            p for p in purchases
            if p.symbol == sale.symbol
            and wash_window_start <= p.date <= wash_window_end
            and p.date != sale.date  # Exclude same-day transactions
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


def cmd_wash_sales(args) -> dict:
    """Detect potential wash sale violations."""
    ledger_path = _get_ledger_path(getattr(args, 'ledger', None))
    year = args.year or date.today().year

    try:
        sales, purchases = _parse_transactions_for_wash_sales(ledger_path, year)
        violations = _detect_wash_sales(sales, purchases, year)

        return {
            "status": "ok",
            "year": year,
            "sales_with_losses": len(sales),
            "violation_count": len(violations),
            "violations": violations,
        }
    except ValueError as e:
        return {"status": "error", "error": str(e)}


# Monarch category to beancount account mapping
MONARCH_CATEGORY_MAP = {
    # Income
    "Income": "Income:Salary",
    "Paycheck": "Income:Salary",
    "Interest": "Income:Interest",
    "Dividends": "Income:Dividends",
    "Investment Income": "Income:Investment",
    "Refund": "Income:Refunds",

    # Expenses
    "Groceries": "Expenses:Food:Groceries",
    "Restaurants": "Expenses:Food:Restaurants",
    "Food & Drink": "Expenses:Food:Other",
    "Coffee Shops": "Expenses:Food:Coffee",

    "Gas": "Expenses:Transport:Gas",
    "Parking": "Expenses:Transport:Parking",
    "Auto Insurance": "Expenses:Transport:Insurance",
    "Auto Payment": "Expenses:Transport:CarPayment",
    "Public Transit": "Expenses:Transport:Transit",
    "Rideshare": "Expenses:Transport:Rideshare",
    "Transportation": "Expenses:Transport:Other",

    "Rent": "Expenses:Housing:Rent",
    "Mortgage": "Expenses:Housing:Mortgage",
    "Utilities": "Expenses:Housing:Utilities",
    "Internet": "Expenses:Housing:Internet",
    "Phone": "Expenses:Housing:Phone",
    "Home Improvement": "Expenses:Housing:Improvement",
    "Home Insurance": "Expenses:Housing:Insurance",

    "Shopping": "Expenses:Shopping",
    "Clothing": "Expenses:Shopping:Clothing",
    "Electronics": "Expenses:Shopping:Electronics",
    "Amazon": "Expenses:Shopping:Amazon",

    "Entertainment": "Expenses:Entertainment",
    "Streaming": "Expenses:Entertainment:Streaming",
    "Movies": "Expenses:Entertainment:Movies",
    "Games": "Expenses:Entertainment:Games",

    "Health": "Expenses:Health",
    "Doctor": "Expenses:Health:Doctor",
    "Pharmacy": "Expenses:Health:Pharmacy",
    "Health Insurance": "Expenses:Health:Insurance",

    "Travel": "Expenses:Travel",
    "Hotels": "Expenses:Travel:Hotels",
    "Flights": "Expenses:Travel:Flights",

    "Education": "Expenses:Education",
    "Books": "Expenses:Education:Books",
    "Subscriptions": "Expenses:Subscriptions",

    "Gifts": "Expenses:Gifts",
    "Charity": "Expenses:Charity",
    "Fees": "Expenses:Fees",
    "Bank Fee": "Expenses:Fees:Bank",
    "ATM Fee": "Expenses:Fees:ATM",

    "Transfer": "Equity:Transfers",
    "Credit Card Payment": "Liabilities:CreditCard",
}


def _map_monarch_category(category: str) -> str:
    """Map a Monarch category to a beancount account."""
    # Try exact match first
    if category in MONARCH_CATEGORY_MAP:
        return MONARCH_CATEGORY_MAP[category]

    # Try case-insensitive match
    for key, value in MONARCH_CATEGORY_MAP.items():
        if key.lower() == category.lower():
            return value

    # Default to Expenses:Uncategorized
    return f"Expenses:Uncategorized:{category.replace(' ', '')}"


def _parse_tags(tags_str: str) -> list[str]:
    """Parse comma-separated tags from Monarch CSV Tags column."""
    if not tags_str or not tags_str.strip():
        return []
    return [t.strip() for t in tags_str.split(",") if t.strip()]


def _filter_by_tags(
    tags: list[str],
    include_tags: list[str] | None,
    exclude_tags: list[str] | None,
) -> bool:
    """Check if transaction passes tag filters.

    Args:
        tags: Transaction tags
        include_tags: If set, transaction must have at least one of these tags
        exclude_tags: If set, transaction must not have any of these tags

    Returns:
        True if transaction passes filters
    """
    # Apply include filter first (if set)
    if include_tags:
        if not any(t in include_tags for t in tags):
            return False

    # Apply exclude filter
    if exclude_tags:
        if any(t in exclude_tags for t in tags):
            return False

    return True


def _parse_monarch_csv(
    file_path: Path,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
) -> list[dict]:
    """Parse a Monarch Money CSV export.

    Actual columns: Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner

    Args:
        file_path: Path to CSV file
        include_tags: If set, only include transactions with these tags
        exclude_tags: If set, exclude transactions with these tags

    Returns:
        List of parsed transaction dicts
    """
    transactions = []

    with open(file_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Parse date (expected format: YYYY-MM-DD or MM/DD/YYYY)
            date_str = row.get("Date", "")
            try:
                if "/" in date_str:
                    txn_date = datetime.strptime(date_str, "%m/%d/%Y").date()
                else:
                    txn_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue  # Skip invalid dates

            # Parse tags
            tags = _parse_tags(row.get("Tags", ""))

            # Apply tag filters
            if not _filter_by_tags(tags, include_tags, exclude_tags):
                continue

            # Parse amount (negative = expense, positive = income)
            amount_str = row.get("Amount", "0").replace("$", "").replace(",", "")
            try:
                amount = float(amount_str)
            except ValueError:
                continue

            transactions.append({
                "date": txn_date,
                "merchant": row.get("Merchant", "").strip(),
                "category": row.get("Category", "").strip(),
                "account_name": row.get("Account", "").strip(),
                "original_statement": row.get("Original Statement", "").strip(),
                "amount": amount,
                "notes": row.get("Notes", "").strip(),
                "tags": tags,
                "owner": row.get("Owner", "").strip(),
            })

    return transactions


def _format_beancount_transaction(
    txn_date: date,
    payee: str,
    narration: str,
    posting_account: str,
    contra_account: str,
    amount: float,
    currency: str = "USD",
) -> str:
    """Format a single beancount transaction."""
    # Escape quotes in payee/narration
    payee = payee.replace('"', '\\"')
    narration = narration.replace('"', '\\"')

    lines = [f'{txn_date.isoformat()} * "{payee}" "{narration}"']

    if amount < 0:
        # Expense: money leaves the bank account
        lines.append(f'  {posting_account}  {abs(amount):.2f} {currency}')
        lines.append(f'  {contra_account}')
    else:
        # Income: money enters the bank account
        lines.append(f'  {contra_account}  {amount:.2f} {currency}')
        lines.append(f'  {posting_account}')

    return "\n".join(lines)


def _write_deferred_tracking(
    monarch_synced: list[dict] | None = None,
    csv_imported: list[dict] | None = None,
    monarch_recategorized: list[str] | None = None,
    monarch_category_updates: list[dict] | None = None,
) -> bool | None:
    """Write deferred transaction tracking to JSON file for scheduler processing.

    Returns True if written, None if env vars not set (caller should use direct DB).
    """
    deferred_dir = os.environ.get("ISTOTA_DEFERRED_DIR")
    task_id = os.environ.get("ISTOTA_TASK_ID")
    if not deferred_dir or not task_id:
        return None

    path = Path(deferred_dir) / f"task_{task_id}_tracked_transactions.json"

    # Merge with existing file if present
    existing = {
        "monarch_synced": [],
        "csv_imported": [],
        "monarch_recategorized": [],
        "monarch_category_updates": [],
    }
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if monarch_synced:
        existing["monarch_synced"].extend(monarch_synced)
    if csv_imported:
        existing["csv_imported"].extend(csv_imported)
    if monarch_recategorized:
        existing["monarch_recategorized"].extend(monarch_recategorized)
    if monarch_category_updates:
        existing.setdefault("monarch_category_updates", []).extend(monarch_category_updates)

    path.write_text(json.dumps(existing))
    return True


def cmd_import_monarch(args) -> dict:
    """Import transactions from Monarch Money CSV export."""
    from istota.db import compute_transaction_hash

    ledger_path = _get_ledger_path(getattr(args, 'ledger', None))
    file_path = Path(args.file)

    if not file_path.exists():
        return {"status": "error", "error": f"File not found: {file_path}"}

    # Get tag filters from args
    include_tags = getattr(args, 'tag', None) or None
    exclude_tags = getattr(args, 'exclude_tag', None) or None

    try:
        transactions = _parse_monarch_csv(file_path, include_tags, exclude_tags)
    except Exception as e:
        return {"status": "error", "error": f"Failed to parse CSV: {e}"}

    if not transactions:
        filter_msg = ""
        if include_tags or exclude_tags:
            filter_msg = " (after applying tag filters)"
        return {"status": "error", "error": f"No valid transactions found in CSV{filter_msg}"}

    # Content-based dedup against ledger and DB
    ledger_hashes = _parse_ledger_transactions(ledger_path)

    db_path_str = os.environ.get("ISTOTA_DB_PATH")
    db_path = Path(db_path_str) if db_path_str else None
    user_id = os.environ.get("ISTOTA_USER_ID", "default")

    db_conn = None
    if db_path:
        from istota.db import get_db, is_content_hash_synced

    # Build beancount entries, skipping duplicates
    entries = []
    content_hashes = []
    content_skipped_count = 0

    for txn in transactions:
        content_hash = compute_transaction_hash(
            txn["date"].isoformat(), abs(txn["amount"]), txn["merchant"],
        )

        # Check ledger
        if content_hash in ledger_hashes:
            content_skipped_count += 1
            continue

        # Check DB
        if db_path:
            with get_db(db_path) as conn:
                if is_content_hash_synced(conn, user_id, content_hash):
                    content_skipped_count += 1
                    continue

        posting_account = _map_monarch_category(txn["category"])

        entry = _format_beancount_transaction(
            txn_date=txn["date"],
            payee=txn["merchant"],
            narration=txn["notes"] or txn["category"],
            posting_account=posting_account,
            contra_account=args.account,
            amount=txn["amount"],
        )
        entries.append(entry)
        content_hashes.append(content_hash)

    if not entries:
        return {
            "status": "ok",
            "transaction_count": 0,
            "content_skipped_count": content_skipped_count,
            "message": f"No new transactions to import ({content_skipped_count} already in ledger)",
        }

    # Write to staging file
    ledger_dir = ledger_path.parent
    imports_dir = ledger_dir / "imports"
    imports_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging_file = imports_dir / f"monarch_import_{timestamp}.beancount"

    header = f"; Imported from Monarch Money on {datetime.now().isoformat()}\n"
    header += f"; Source: {file_path.name}\n"
    header += f"; Transaction count: {len(entries)}\n"
    if content_skipped_count > 0:
        header += f"; Skipped (already in ledger): {content_skipped_count}\n"
    header += "; Auto-appended to main ledger. Staging file kept for audit trail.\n\n"

    staging_file.write_text(header + "\n\n".join(entries) + "\n")

    # Append to main ledger
    _append_to_ledger(ledger_path, entries)

    # Track imported hashes in DB for future dedup
    if content_hashes:
        csv_entries = [{"content_hash": h, "source_file": file_path.name} for h in content_hashes]
        if _write_deferred_tracking(csv_imported=csv_entries) is None and db_path:
            from istota.db import track_csv_transactions_batch
            with get_db(db_path) as conn:
                track_csv_transactions_batch(conn, user_id, content_hashes, file_path.name)

    return {
        "status": "ok",
        "transaction_count": len(entries),
        "content_skipped_count": content_skipped_count,
        "staging_file": str(staging_file),
        "message": f"Imported {len(entries)} transactions to ledger",
    }


def _map_monarch_account(
    account_name: str,
    config: MonarchConfig,
) -> str:
    """Map a Monarch account name to a beancount account.

    Args:
        account_name: Account name from Monarch
        config: MonarchConfig with account mappings

    Returns:
        Beancount account path
    """
    # Check explicit mapping first
    if account_name in config.accounts:
        return config.accounts[account_name]

    # Try case-insensitive match
    for key, value in config.accounts.items():
        if key.lower() == account_name.lower():
            return value

    # Fall back to default
    return config.sync.default_account


def _map_monarch_category_with_config(
    category: str,
    config: MonarchConfig,
) -> str:
    """Map a Monarch category to a beancount account, checking config overrides first.

    Args:
        category: Category name from Monarch
        config: MonarchConfig with category overrides

    Returns:
        Beancount account path
    """
    # Check config overrides first
    if category in config.categories:
        return config.categories[category]

    # Try case-insensitive match in config
    for key, value in config.categories.items():
        if key.lower() == category.lower():
            return value

    # Fall back to built-in mapping
    return _map_monarch_category(category)


async def _fetch_monarch_transactions(
    config: MonarchConfig,
    lookback_days: int,
) -> list[dict]:
    """Fetch transactions from Monarch Money API.

    Args:
        config: MonarchConfig with credentials
        lookback_days: Number of days to look back

    Returns:
        List of transaction dicts from Monarch API
    """
    try:
        from monarchmoney import MonarchMoney
    except ImportError:
        raise ValueError("monarchmoneycommunity package is required for API sync")

    # Authenticate - pass token to constructor so headers are set correctly
    if config.credentials.session_token:
        mm = MonarchMoney(token=config.credentials.session_token)
    elif config.credentials.email and config.credentials.password:
        mm = MonarchMoney()
        await mm.login(config.credentials.email, config.credentials.password)
    else:
        raise ValueError("No Monarch credentials configured (need email+password or session_token)")

    # Fetch transactions
    start_date = date.today() - timedelta(days=lookback_days)
    end_date = date.today()
    transactions = await mm.get_transactions(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )

    return transactions.get("allTransactions", {}).get("results", [])


def _format_recategorization_entry(
    txn_date: date,
    merchant: str,
    original_account: str,
    recategorize_account: str,
    amount: float,
    currency: str = "USD",
) -> str:
    """Format a recategorization entry that moves an expense to personal."""
    merchant = merchant.replace('"', '\\"')
    lines = [f'{txn_date.isoformat()} * "{merchant}" "Recategorized: business tag removed in Monarch"']
    # Move from original business expense to personal
    lines.append(f'  {recategorize_account}  {abs(amount):.2f} {currency}')
    lines.append(f'  {original_account}  -{abs(amount):.2f} {currency}')
    return "\n".join(lines)


def _format_category_change_entry(
    txn_date: date,
    merchant: str,
    old_account: str,
    new_account: str,
    amount: float,
    currency: str = "USD",
) -> str:
    """Format a ledger entry that moves a transaction from one category to another.

    Used when a transaction's category is changed in Monarch after initial sync.
    """
    merchant = merchant.replace('"', '\\"')
    lines = [f'{txn_date.isoformat()} * "{merchant}" "Recategorized in Monarch"']
    # Move amount from old account to new account
    lines.append(f'  {new_account}  {abs(amount):.2f} {currency}')
    lines.append(f'  {old_account}  -{abs(amount):.2f} {currency}')
    return "\n".join(lines)


async def _fetch_transactions_by_ids(
    config: MonarchConfig,
    transaction_ids: list[str],
) -> dict[str, dict]:
    """Fetch specific transactions from Monarch by ID.

    Returns a dict mapping transaction_id -> transaction data.
    """
    try:
        from monarchmoney import MonarchMoney
    except ImportError:
        raise ValueError("monarchmoneycommunity package is required for API sync")

    # Authenticate
    if config.credentials.session_token:
        mm = MonarchMoney(token=config.credentials.session_token)
    elif config.credentials.email and config.credentials.password:
        mm = MonarchMoney()
        await mm.login(config.credentials.email, config.credentials.password)
    else:
        raise ValueError("No Monarch credentials configured")

    # Fetch all transactions in a wide range to find the ones we need
    # Use a large lookback to cover historical transactions
    start_date = date.today() - timedelta(days=365)
    end_date = date.today()
    all_txns = await mm.get_transactions(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )

    results = all_txns.get("allTransactions", {}).get("results", [])
    return {txn.get("id"): txn for txn in results if txn.get("id") in transaction_ids}


def _backup_ledger(ledger_path: Path, max_backups: int = 10) -> Path | None:
    """Create a timestamped backup of the ledger file before modification.

    Rotates old backups, keeping at most max_backups files.
    Returns the backup path, or None if the ledger doesn't exist.
    """
    if not ledger_path.exists():
        return None

    backups_dir = ledger_path.parent / "backups"
    backups_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backups_dir / f"{ledger_path.name}.{timestamp}"

    import shutil
    shutil.copy2(ledger_path, backup_path)

    # Prune old backups beyond max_backups
    existing = sorted(backups_dir.glob(f"{ledger_path.name}.*"), reverse=True)
    for old_backup in existing[max_backups:]:
        old_backup.unlink()

    return backup_path


def _restart_fava() -> None:
    """Restart the user's Fava service to pick up ledger changes."""
    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        return
    service = f"istota-fava-{user_id}.service"
    try:
        subprocess.run(
            ["sudo", "--non-interactive", "systemctl", "restart", service],
            capture_output=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _append_to_ledger(ledger_path: Path, entries: list[str]) -> None:
    """Append beancount entries to the main ledger file with backup."""
    if not entries:
        return
    _backup_ledger(ledger_path)
    with open(ledger_path, "a") as f:
        for entry in entries:
            f.write(f"\n{entry}\n")
    _restart_fava()


def _parse_ledger_transactions(ledger_path: Path) -> set[str]:
    """Parse beancount ledger and return content hashes of existing transactions.

    Extracts (date, amount, payee) from each transaction for cross-source dedup.
    Returns a set of SHA-256 hashes.
    """
    from istota.db import compute_transaction_hash

    if not ledger_path.exists():
        return set()

    text = ledger_path.read_text()
    hashes = set()

    # Also scan import staging files in the imports/ directory
    imports_dir = ledger_path.parent / "imports"
    texts = [text]
    if imports_dir.is_dir():
        for f in imports_dir.glob("*.beancount"):
            texts.append(f.read_text())

    # Match transaction header: YYYY-MM-DD * "payee" "narration"
    # Then extract amount from the first posting line
    txn_pattern = re.compile(
        r'^(\d{4}-\d{2}-\d{2})\s+[*!]\s+"([^"]*)"',
        re.MULTILINE,
    )
    # Match posting line with amount: e.g. "  Expenses:Food  50.00 USD"
    amount_pattern = re.compile(
        r'^\s+\S+\s+(-?[\d,]+\.?\d*)\s+[A-Z]{3}',
        re.MULTILINE,
    )

    for content in texts:
        for match in txn_pattern.finditer(content):
            txn_date = match.group(1)
            payee = match.group(2)

            # Find the first posting with an amount after this header
            rest = content[match.end():]
            amount_match = amount_pattern.match(rest) or amount_pattern.search(
                rest.split("\n\n")[0]  # Only look within the same transaction block
            )
            if not amount_match:
                continue

            amount = abs(float(amount_match.group(1).replace(",", "")))
            content_hash = compute_transaction_hash(txn_date, amount, payee)
            hashes.add(content_hash)

    return hashes


def cmd_sync_monarch(args) -> dict:
    """Sync transactions from Monarch Money API and reconcile tag changes."""
    import asyncio

    ledger_path = _get_ledger_path(getattr(args, 'ledger', None))
    dry_run = getattr(args, 'dry_run', False)

    # Load config
    try:
        config_path = _get_accounting_config_path()
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    if not config_path.exists():
        return {"status": "error", "error": f"Config not found: {config_path}"}

    try:
        config = parse_accounting_config(config_path)
    except Exception as e:
        return {"status": "error", "error": f"Failed to parse config: {e}"}

    # Fetch transactions from API
    try:
        transactions = asyncio.run(_fetch_monarch_transactions(
            config,
            config.sync.lookback_days,
        ))
    except Exception as e:
        return {"status": "error", "error": f"Failed to fetch transactions: {e}"}

    # Get user_id from env for deduplication (set by executor)
    user_id = os.environ.get("ISTOTA_USER_ID", "default")

    # Build lookup of all fetched transactions by ID for reconciliation
    all_txn_by_id = {txn.get("id"): txn for txn in transactions if txn.get("id")}

    # Filter by tags if configured
    filtered_transactions = []
    for txn in transactions:
        txn_tags = [t.get("name", "") for t in txn.get("tags", [])]

        if not _filter_by_tags(
            txn_tags,
            config.tags.include if config.tags.include else None,
            config.tags.exclude if config.tags.exclude else None,
        ):
            continue

        filtered_transactions.append(txn)

    # Import DB functions only when needed (avoid circular imports)
    db_path_str = os.environ.get("ISTOTA_DB_PATH")
    db_path = Path(db_path_str) if db_path_str else None

    # Deduplicate against previously synced transactions and ledger content
    new_transactions = []
    skipped_count = 0
    content_skipped_count = 0

    # Parse existing ledger for content-based dedup
    ledger_hashes = _parse_ledger_transactions(ledger_path)

    if db_path:
        from istota.db import (
            compute_transaction_hash,
            get_db,
            is_content_hash_synced,
            is_monarch_transaction_synced,
            track_monarch_transactions_batch,
            get_active_monarch_synced_transactions,
            mark_monarch_transaction_recategorized,
        )

        with get_db(db_path) as conn:
            for txn in filtered_transactions:
                txn_id = txn.get("id", "")
                if txn_id and is_monarch_transaction_synced(conn, user_id, txn_id):
                    skipped_count += 1
                    continue

                # Content-based dedup: check ledger + DB
                merchant = txn.get("merchant", {}).get("name", "") or txn.get("name", "Unknown")
                amount = float(txn.get("amount", 0))
                txn_date_str = txn.get("date", "")[:10]
                content_hash = compute_transaction_hash(txn_date_str, abs(amount), merchant)

                if content_hash in ledger_hashes or is_content_hash_synced(conn, user_id, content_hash):
                    content_skipped_count += 1
                    continue

                new_transactions.append(txn)
    else:
        # No DB available, still check ledger content hashes
        from istota.db import compute_transaction_hash

        for txn in filtered_transactions:
            merchant = txn.get("merchant", {}).get("name", "") or txn.get("name", "Unknown")
            amount = float(txn.get("amount", 0))
            txn_date_str = txn.get("date", "")[:10]
            content_hash = compute_transaction_hash(txn_date_str, abs(amount), merchant)

            if content_hash in ledger_hashes:
                content_skipped_count += 1
            else:
                new_transactions.append(txn)

    # Build beancount entries for new transactions
    entries = []
    synced_data = []  # Store full metadata for DB tracking

    for txn in new_transactions:
        # Parse transaction data
        txn_date_str = txn.get("date", "")
        try:
            txn_date = datetime.strptime(txn_date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue

        merchant = txn.get("merchant", {}).get("name", "") or txn.get("name", "Unknown")
        category = txn.get("category", {}).get("name", "") or "Uncategorized"
        account_name = txn.get("account", {}).get("displayName", "")
        amount = float(txn.get("amount", 0))
        notes = txn.get("notes", "") or ""
        txn_id = txn.get("id", "")
        txn_tags = [t.get("name", "") for t in txn.get("tags", [])]

        # Map accounts
        contra_account = _map_monarch_account(account_name, config)
        posting_account = _map_monarch_category_with_config(category, config)

        entry = _format_beancount_transaction(
            txn_date=txn_date,
            payee=merchant,
            narration=notes or category,
            posting_account=posting_account,
            contra_account=contra_account,
            amount=amount,
        )
        entries.append(entry)

        if txn_id:
            from istota.db import compute_transaction_hash as _cth
            synced_data.append({
                "id": txn_id,
                "tags_json": json.dumps(txn_tags),
                "amount": amount,
                "merchant": merchant,
                "posted_account": posting_account,
                "txn_date": txn_date.isoformat(),
                "content_hash": _cth(txn_date.isoformat(), abs(amount), merchant),
            })

    # === RECONCILIATION: Check for tag/category changes on previously synced transactions ===
    recategorized_entries = []
    recategorized_ids = []
    category_change_entries = []
    category_change_updates = []  # dicts with monarch_transaction_id + new posted_account

    if db_path:
        with get_db(db_path) as conn:
            active_synced = get_active_monarch_synced_transactions(conn, user_id)

        if active_synced:
            # Check each previously synced transaction against current Monarch state
            for synced_txn in active_synced:
                current_txn = all_txn_by_id.get(synced_txn.monarch_transaction_id)

                if current_txn is None:
                    # Transaction not in current fetch window - skip (might be too old)
                    continue

                # Get current tags from Monarch
                current_tags = [t.get("name", "") for t in current_txn.get("tags", [])]

                # Check if it still passes the include filter
                still_has_business_tag = _filter_by_tags(
                    current_tags,
                    config.tags.include if config.tags.include else None,
                    config.tags.exclude if config.tags.exclude else None,
                )

                if not still_has_business_tag:
                    # Business tag was removed - create recategorization entry
                    if (
                        synced_txn.amount is not None
                        and synced_txn.posted_account
                        and synced_txn.merchant
                        and synced_txn.txn_date
                    ):
                        try:
                            original_date = datetime.strptime(synced_txn.txn_date, "%Y-%m-%d").date()
                        except ValueError:
                            original_date = date.today()

                        recat_entry = _format_recategorization_entry(
                            txn_date=date.today(),  # Recategorization happens today
                            merchant=synced_txn.merchant,
                            original_account=synced_txn.posted_account,
                            recategorize_account=config.sync.recategorize_account,
                            amount=synced_txn.amount,
                        )
                        recategorized_entries.append(recat_entry)
                        recategorized_ids.append(synced_txn.monarch_transaction_id)
                    continue  # Tag removal takes priority over category change

                # Check if category changed in Monarch
                if (
                    synced_txn.amount is not None
                    and synced_txn.posted_account
                    and synced_txn.merchant
                    and synced_txn.txn_date
                ):
                    current_category = current_txn.get("category", {}).get("name", "") or "Uncategorized"
                    new_posted_account = _map_monarch_category_with_config(current_category, config)

                    if new_posted_account != synced_txn.posted_account:
                        cat_entry = _format_category_change_entry(
                            txn_date=date.today(),
                            merchant=synced_txn.merchant,
                            old_account=synced_txn.posted_account,
                            new_account=new_posted_account,
                            amount=synced_txn.amount,
                        )
                        category_change_entries.append(cat_entry)
                        category_change_updates.append({
                            "monarch_transaction_id": synced_txn.monarch_transaction_id,
                            "posted_account": new_posted_account,
                        })

    # Prepare result
    ledger_dir = ledger_path.parent
    imports_dir = ledger_dir / "imports"
    imports_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    result = {
        "status": "ok",
        "transaction_count": len(entries),
        "skipped_count": skipped_count,
        "content_skipped_count": content_skipped_count,
        "recategorized_count": len(recategorized_entries),
        "category_changed_count": len(category_change_entries),
        "dry_run": dry_run,
    }

    if dry_run:
        result["message"] = f"Would import {len(entries)} transactions"
        if entries:
            result["sample_entries"] = entries[:3]
        if recategorized_entries:
            result["sample_recategorizations"] = recategorized_entries[:3]
        if category_change_entries:
            result["sample_category_changes"] = category_change_entries[:3]
        return result

    # Write new transactions to staging file
    staging_file = None
    if entries:
        staging_file = imports_dir / f"monarch_sync_{timestamp}.beancount"
        header = f"; Synced from Monarch Money API on {datetime.now().isoformat()}\n"
        header += f"; Lookback days: {config.sync.lookback_days}\n"
        header += f"; Transaction count: {len(entries)}\n"
        if skipped_count > 0:
            header += f"; Skipped (already synced): {skipped_count}\n"
        if content_skipped_count > 0:
            header += f"; Skipped (already in ledger): {content_skipped_count}\n"
        header += "; Auto-appended to main ledger. Staging file kept for audit trail.\n\n"
        staging_file.write_text(header + "\n\n".join(entries) + "\n")
        result["staging_file"] = str(staging_file)

    # Write recategorizations to separate staging file
    recat_file = None
    if recategorized_entries:
        recat_file = imports_dir / f"monarch_recategorize_{timestamp}.beancount"
        header = f"; Recategorizations from Monarch Money on {datetime.now().isoformat()}\n"
        header += f"; These transactions had their business tag removed in Monarch\n"
        header += f"; Recategorization count: {len(recategorized_entries)}\n"
        header += f"; Target account: {config.sync.recategorize_account}\n"
        header += "; Auto-appended to main ledger. Staging file kept for audit trail.\n\n"
        recat_file.write_text(header + "\n\n".join(recategorized_entries) + "\n")
        result["recategorize_file"] = str(recat_file)

    # Write category changes to separate staging file
    cat_change_file = None
    if category_change_entries:
        cat_change_file = imports_dir / f"monarch_category_change_{timestamp}.beancount"
        header = f"; Category changes from Monarch Money on {datetime.now().isoformat()}\n"
        header += f"; These transactions were recategorized in Monarch\n"
        header += f"; Category change count: {len(category_change_entries)}\n"
        header += "; Auto-appended to main ledger. Staging file kept for audit trail.\n\n"
        cat_change_file.write_text(header + "\n\n".join(category_change_entries) + "\n")
        result["category_change_file"] = str(cat_change_file)

    # Append to main ledger
    _append_to_ledger(ledger_path, entries + recategorized_entries + category_change_entries)

    # Track synced transactions, recategorizations, and category changes in DB
    deferred = _write_deferred_tracking(
        monarch_synced=synced_data or None,
        monarch_recategorized=recategorized_ids or None,
        monarch_category_updates=category_change_updates or None,
    )
    if deferred is None:
        # No deferred dir — fall back to direct DB writes
        if db_path and synced_data:
            with get_db(db_path) as conn:
                track_monarch_transactions_batch(conn, user_id, synced_data)
        if db_path and recategorized_ids:
            with get_db(db_path) as conn:
                for txn_id in recategorized_ids:
                    mark_monarch_transaction_recategorized(conn, user_id, txn_id)
        if db_path and category_change_updates:
            from istota.db import update_monarch_transaction_posted_account
            with get_db(db_path) as conn:
                for update in category_change_updates:
                    update_monarch_transaction_posted_account(
                        conn, user_id,
                        update["monarch_transaction_id"],
                        update["posted_account"],
                    )

    # Build message
    messages = []
    if entries:
        messages.append(f"Synced {len(entries)} new transactions")
    if recategorized_entries:
        messages.append(f"Created {len(recategorized_entries)} recategorization entries")
    if category_change_entries:
        messages.append(f"Updated {len(category_change_entries)} categories")
    if not entries and not recategorized_entries and not category_change_entries:
        messages.append("No changes")

    result["message"] = ". ".join(messages)
    return result


def _generate_invoice_html(
    client: str,
    items: list[dict],
    invoice_number: str,
    invoice_date: date,
    due_date: date,
    notes: str = "",
    from_name: str = "",
    from_address: str = "",
) -> str:
    """Generate an HTML invoice."""
    # Calculate totals
    subtotal = sum(item.get("amount", 0) * item.get("quantity", 1) for item in items)

    items_html = ""
    for item in items:
        qty = item.get("quantity", 1)
        amount = item.get("amount", 0)
        total = qty * amount
        items_html += f"""
        <tr>
            <td>{item.get('description', '')}</td>
            <td style="text-align: center;">{qty}</td>
            <td style="text-align: right;">${amount:.2f}</td>
            <td style="text-align: right;">${total:.2f}</td>
        </tr>
        """

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Invoice {invoice_number}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; color: #333; }}
        .header {{ display: flex; justify-content: space-between; margin-bottom: 40px; }}
        .invoice-title {{ font-size: 32px; font-weight: bold; color: #2c3e50; }}
        .invoice-info {{ text-align: right; }}
        .addresses {{ display: flex; justify-content: space-between; margin-bottom: 40px; }}
        .address-block {{ width: 45%; }}
        .address-label {{ font-weight: bold; color: #7f8c8d; margin-bottom: 8px; }}
        table {{ width: 100%; border-collapse: collapse; margin-bottom: 30px; }}
        th {{ background: #2c3e50; color: white; padding: 12px; text-align: left; }}
        td {{ padding: 12px; border-bottom: 1px solid #ddd; }}
        .totals {{ text-align: right; }}
        .total-row {{ font-size: 18px; font-weight: bold; }}
        .notes {{ margin-top: 40px; padding: 20px; background: #f8f9fa; border-radius: 8px; }}
        .notes-label {{ font-weight: bold; margin-bottom: 8px; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="invoice-title">INVOICE</div>
        <div class="invoice-info">
            <div><strong>Invoice #:</strong> {invoice_number}</div>
            <div><strong>Date:</strong> {invoice_date.isoformat()}</div>
            <div><strong>Due:</strong> {due_date.isoformat()}</div>
        </div>
    </div>

    <div class="addresses">
        <div class="address-block">
            <div class="address-label">FROM</div>
            <div>{from_name}</div>
            <div style="white-space: pre-line;">{from_address}</div>
        </div>
        <div class="address-block">
            <div class="address-label">TO</div>
            <div>{client}</div>
        </div>
    </div>

    <table>
        <thead>
            <tr>
                <th>Description</th>
                <th style="text-align: center;">Qty</th>
                <th style="text-align: right;">Rate</th>
                <th style="text-align: right;">Amount</th>
            </tr>
        </thead>
        <tbody>
            {items_html}
        </tbody>
    </table>

    <div class="totals">
        <div class="total-row">Total: ${subtotal:.2f}</div>
    </div>

    {f'<div class="notes"><div class="notes-label">Notes</div>{notes}</div>' if notes else ''}
</body>
</html>"""

    return html


def _get_invoicing_config_path() -> Path:
    """Get invoicing config path from environment variable."""
    path = os.environ.get("INVOICING_CONFIG", "")
    if not path:
        raise ValueError("INVOICING_CONFIG environment variable is required")
    return Path(path)


def cmd_invoice_generate(args) -> dict:
    """Generate invoices for a billing period."""
    from istota.skills.invoicing import (
        generate_invoices_for_period,
        parse_invoicing_config,
    )

    try:
        config_path = _get_invoicing_config_path()
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    if not config_path.exists():
        return {"status": "error", "error": f"Config not found: {config_path}"}

    try:
        config = parse_invoicing_config(config_path)
    except Exception as e:
        return {"status": "error", "error": f"Failed to parse config: {e}"}

    try:
        results = generate_invoices_for_period(
            config=config,
            config_path=config_path,
            period=args.period,
            client_filter=args.client,
            entity_filter=getattr(args, "entity", None),
            dry_run=args.dry_run,
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}

    if not results:
        period_desc = f" for period {args.period}" if args.period else ""
        return {
            "status": "ok",
            "message": f"No uninvoiced entries found{period_desc}",
            "invoices": [],
        }

    total = sum(r["total"] for r in results)
    result = {
        "status": "ok",
        "invoice_count": len(results),
        "total": round(total, 2),
        "dry_run": args.dry_run,
        "invoices": results,
    }
    if args.period:
        result["period"] = args.period
    return result


def cmd_invoice_list(args) -> dict:
    """List invoices from work log (outstanding by default, --all for all)."""
    from istota.skills.invoicing import (
        build_line_items,
        parse_invoicing_config,
        parse_work_log,
        _resolve_nc_path,
    )

    # Load invoicing config
    try:
        config_path = _get_invoicing_config_path()
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    if not config_path.exists():
        return {"status": "error", "error": f"Config not found: {config_path}"}

    try:
        config = parse_invoicing_config(config_path)
    except Exception as e:
        return {"status": "error", "error": f"Failed to parse config: {e}"}

    work_log_path = _resolve_nc_path(config.work_log)
    if not work_log_path.exists():
        return {"status": "ok", "invoice_count": 0, "invoices": []}

    entries = parse_work_log(work_log_path)

    # Group entries by invoice number
    invoice_groups: dict[str, list] = {}
    for entry in entries:
        if not entry.invoice:
            continue
        if entry.invoice not in invoice_groups:
            invoice_groups[entry.invoice] = []
        invoice_groups[entry.invoice].append(entry)

    # Apply client filter
    client_filter = getattr(args, 'client', None)
    show_all = getattr(args, 'all', False)

    invoices = []
    for inv_num, inv_entries in sorted(invoice_groups.items()):
        # Filter by client if specified
        if client_filter and not any(e.client == client_filter for e in inv_entries):
            continue

        # Compute total from line items
        items = build_line_items(inv_entries, config.services)
        total = sum(item.amount for item in items)

        # Determine paid status
        is_paid = all(e.paid_date is not None for e in inv_entries)
        paid_date = inv_entries[0].paid_date.isoformat() if is_paid and inv_entries[0].paid_date else None

        # Skip paid invoices unless --all
        if is_paid and not show_all:
            continue

        client_key = inv_entries[0].client
        client_config = config.clients.get(client_key)
        client_name = client_config.name if client_config else client_key

        inv_date = min(e.date for e in inv_entries)

        invoice_info = {
            "invoice_number": inv_num,
            "client": client_name,
            "date": inv_date.isoformat(),
            "total": round(total, 2),
            "status": "paid" if is_paid else "outstanding",
        }
        if is_paid and paid_date:
            invoice_info["paid_date"] = paid_date

        invoices.append(invoice_info)

    outstanding = [i for i in invoices if i["status"] == "outstanding"]

    return {
        "status": "ok",
        "invoice_count": len(invoices),
        "outstanding_count": len(outstanding),
        "invoices": invoices,
    }


def cmd_invoice_paid(args) -> dict:
    """Record payment for an invoice (cash-basis: creates income posting)."""
    from istota.skills.invoicing import (
        compute_income_lines,
        create_income_posting,
        parse_invoicing_config,
        parse_work_log,
        resolve_bank_account,
        resolve_currency,
        resolve_entity,
        stamp_work_log_paid_dates,
        _resolve_nc_path,
    )

    # Parse payment date
    try:
        payment_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    except ValueError:
        return {"status": "error", "error": "Invalid date format. Use YYYY-MM-DD"}

    # Load invoicing config
    try:
        config_path = _get_invoicing_config_path()
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    if not config_path.exists():
        return {"status": "error", "error": f"Config not found: {config_path}"}

    try:
        config = parse_invoicing_config(config_path)
    except Exception as e:
        return {"status": "error", "error": f"Failed to parse config: {e}"}

    # Parse work log and find entries for this invoice
    invoice_number = args.invoice_number
    work_log_path = _resolve_nc_path(config.work_log)
    if not work_log_path.exists():
        return {"status": "error", "error": f"Work log not found: {work_log_path}"}

    entries = parse_work_log(work_log_path)
    matching = [(idx, e) for idx, e in enumerate(entries) if e.invoice == invoice_number]

    if not matching:
        return {"status": "error", "error": f"Invoice {invoice_number} not found in work log"}

    # Check if already paid
    if all(e.paid_date is not None for _, e in matching):
        return {"status": "error", "error": f"Invoice {invoice_number} is already marked as paid"}

    # Get client info from first matching entry
    first_entry = matching[0][1]
    client_config = config.clients.get(first_entry.client)
    if not client_config:
        return {"status": "error", "error": f"Client '{first_entry.client}' not found in config"}

    # Resolve entity, bank account, currency
    entity = resolve_entity(config, entry=first_entry, client_config=client_config)
    bank_account = args.bank or resolve_bank_account(entity, config)
    currency = resolve_currency(entity, config)

    # Compute income lines from matching entries
    matched_entries = [e for _, e in matching]
    income_lines = compute_income_lines(matched_entries, config.services)

    if not income_lines:
        return {"status": "error", "error": f"No billable items found for {invoice_number}"}

    total = sum(income_lines.values())
    no_post = getattr(args, 'no_post', False)

    if not no_post:
        # Create and append income posting
        posting = create_income_posting(
            invoice_number=invoice_number,
            client_name=client_config.name,
            income_lines=income_lines,
            payment_date=payment_date,
            bank_account=bank_account,
            currency=currency,
        )

        ledger_path = _get_ledger_path(getattr(args, 'ledger', None))
        _append_to_ledger(ledger_path, [posting])

        # Validate ledger
        success, errors = _run_bean_check(ledger_path)
        if not success:
            return {
                "status": "error",
                "error": "Payment recorded but ledger validation failed",
                "validation_errors": errors[:5],
                "file": str(ledger_path),
            }

    # Stamp paid_date on matching entries
    paid_stamps = {idx: payment_date.isoformat() for idx, _ in matching}
    stamp_work_log_paid_dates(work_log_path, paid_stamps)

    result = {
        "status": "ok",
        "invoice_number": invoice_number,
        "client": client_config.name,
        "amount": round(total, 2),
        "payment_date": payment_date.isoformat(),
        "bank_account": bank_account,
    }
    if not no_post:
        result["file"] = str(ledger_path)
    if no_post:
        result["no_post"] = True

    return result


def cmd_invoice_create(args) -> dict:
    """Create a manual single invoice."""
    from istota.skills.invoicing import (
        InvoiceLineItem,
        generate_invoice,
        generate_invoice_html,
        generate_invoice_pdf,
        format_invoice_number,
        parse_invoicing_config,
        update_invoice_number,
    )

    try:
        config_path = _get_invoicing_config_path()
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    if not config_path.exists():
        return {"status": "error", "error": f"Config not found: {config_path}"}

    try:
        config = parse_invoicing_config(config_path)
    except Exception as e:
        return {"status": "error", "error": f"Failed to parse config: {e}"}

    client_config = config.clients.get(args.client)
    if not client_config:
        available = list(config.clients.keys())
        return {"status": "error", "error": f"Client '{args.client}' not found. Available: {', '.join(available)}"}

    # Resolve entity
    from istota.skills.invoicing import resolve_entity, resolve_currency
    entity_key = getattr(args, "entity", None)
    if entity_key:
        if entity_key not in config.companies:
            available = list(config.companies.keys())
            return {"status": "error", "error": f"Entity '{entity_key}' not found. Available: {', '.join(available)}"}
        entity = config.companies[entity_key]
    else:
        entity = resolve_entity(config, client_config=client_config)

    # Build line items from --service/--qty and --item arguments
    from istota.skills.invoicing import WorkEntry, build_line_items

    entries = []
    if args.service:
        if args.service not in config.services:
            available = list(config.services.keys())
            return {"status": "error", "error": f"Service '{args.service}' not found. Available: {', '.join(available)}"}
        entries.append(WorkEntry(
            date=date.today(),
            client=args.client,
            service=args.service,
            qty=args.qty,
            description=args.description or "",
        ))

    items = build_line_items(entries, config.services)

    # Add manual --item entries
    if args.items:
        for item_str in args.items:
            # Format: "description" amount
            parts = item_str.rsplit(" ", 1)
            if len(parts) != 2:
                return {"status": "error", "error": f"Invalid item format: {item_str}. Use: \"description\" amount"}
            desc = parts[0].strip('"').strip("'")
            try:
                amount = float(parts[1])
            except ValueError:
                return {"status": "error", "error": f"Invalid amount in item: {parts[1]}"}
            items.append(InvoiceLineItem(
                display_name=desc,
                description="",
                quantity=1,
                rate=amount,
                discount=0,
                amount=amount,
            ))

    if not items:
        return {"status": "error", "error": "No line items specified. Use --service/--qty or --item"}

    invoice_number = config.next_invoice_number
    invoice_date = date.today()
    total = sum(item.amount for item in items)
    from datetime import timedelta as td
    due_date = invoice_date + td(days=client_config.terms)

    from istota.skills.invoicing import Invoice
    invoice = Invoice(
        number=format_invoice_number(invoice_number),
        date=invoice_date,
        due_date=due_date,
        client=client_config,
        company=entity,
        items=items,
        total=total,
        group_name="",
    )

    # Generate PDF — resolve logo per entity
    from istota.skills.invoicing import _resolve_nc_path
    accounting_path = _resolve_nc_path(config.accounting_path)
    logo_path = None
    if entity.logo:
        logo_path = accounting_path / entity.logo
        if not logo_path.exists():
            logo_path = None
    html = generate_invoice_html(invoice, logo_path=logo_path)
    year = str(invoice_date.year)
    output_dir = accounting_path / config.invoice_output / year
    pdf_filename = f"Invoice-{invoice_number:06d}-{invoice_date.strftime('%m_%d_%Y')}.pdf"
    pdf_path = output_dir / pdf_filename
    generate_invoice_pdf(html, pdf_path)

    result = {
        "status": "ok",
        "invoice_number": invoice.number,
        "client": client_config.name,
        "total": round(total, 2),
        "due_date": due_date.isoformat(),
        "file": str(pdf_path),
    }

    # Update invoice number
    update_invoice_number(config_path, invoice_number + 1)

    return result


def cmd_add_transaction(args) -> dict:
    """Add a transaction to the ledger."""
    ledger_path = _get_ledger_path(getattr(args, 'ledger', None))

    # Parse and validate date
    try:
        txn_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    except ValueError:
        return {"status": "error", "error": "Invalid date format. Use YYYY-MM-DD"}

    # Parse and validate amount
    try:
        amount = float(args.amount)
        if amount <= 0:
            return {"status": "error", "error": "Amount must be positive"}
    except ValueError:
        return {"status": "error", "error": "Invalid amount"}

    currency = args.currency or "USD"

    # Escape quotes in payee/narration
    payee = args.payee.replace('"', '\\"')
    narration = args.narration.replace('"', '\\"')

    # Format transaction
    txn = f'{txn_date} * "{payee}" "{narration}"\n'
    txn += f'  {args.debit}  {amount:.2f} {currency}\n'
    txn += f'  {args.credit}\n'

    # Determine target file
    txn_dir = ledger_path.parent / "transactions"
    txn_dir.mkdir(exist_ok=True)
    txn_file = txn_dir / f"{txn_date.year}.beancount"

    # Append transaction
    with open(txn_file, "a") as f:
        f.write(f"\n{txn}")
    _restart_fava()

    # Validate ledger after adding
    success, errors = _run_bean_check(ledger_path)
    if not success:
        return {
            "status": "error",
            "error": "Transaction added but ledger validation failed",
            "validation_errors": errors[:5],
            "file": str(ledger_path),
        }

    return {
        "status": "ok",
        "date": txn_date.isoformat(),
        "payee": args.payee,
        "amount": amount,
        "currency": currency,
        "debit": args.debit,
        "credit": args.credit,
        "file": str(ledger_path),
    }


def cmd_list_ledgers(args) -> dict:
    """List available ledgers."""
    ledger_paths_json = os.environ.get("LEDGER_PATHS", "")
    ledger_path = os.environ.get("LEDGER_PATH", "")

    if ledger_paths_json:
        try:
            ledgers = json.loads(ledger_paths_json)
            return {
                "status": "ok",
                "ledger_count": len(ledgers),
                "ledgers": [
                    {"name": l.get("name", "unnamed"), "path": l.get("path", "")}
                    for l in ledgers
                ],
            }
        except json.JSONDecodeError:
            pass

    if ledger_path:
        return {
            "status": "ok",
            "ledger_count": 1,
            "ledgers": [{"name": "default", "path": ledger_path}],
        }

    return {"status": "error", "error": "No ledgers configured"}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.accounting",
        description="Beancount ledger operations CLI",
    )
    # Global --ledger flag for selecting which ledger to use
    parser.add_argument(
        "--ledger", "-l",
        help="Ledger name to use (from LEDGER_PATHS). Defaults to first ledger."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list (show available ledgers)
    sub.add_parser("list", help="List available ledgers")

    # check
    sub.add_parser("check", help="Validate ledger file")

    # balances
    p_bal = sub.add_parser("balances", help="Show account balances")
    p_bal.add_argument("--account", "-a", help="Filter by account pattern (regex)")

    # query
    p_query = sub.add_parser("query", help="Run a BQL query")
    p_query.add_argument("query", help="BQL query string")

    # report
    p_report = sub.add_parser("report", help="Generate financial report")
    p_report.add_argument("type", choices=["income-statement", "balance-sheet"], help="Report type")
    p_report.add_argument("--year", "-y", type=int, help="Year for report (default: current year)")

    # lots
    p_lots = sub.add_parser("lots", help="Show open lots for a security")
    p_lots.add_argument("symbol", help="Security symbol (e.g., AAPL, VTI)")

    # wash-sales
    p_wash = sub.add_parser("wash-sales", help="Detect wash sale violations")
    p_wash.add_argument("--year", "-y", type=int, help="Year to analyze (default: current year)")

    # import-monarch
    p_import = sub.add_parser("import-monarch", help="Import from Monarch Money CSV")
    p_import.add_argument("file", help="Path to Monarch CSV export file")
    p_import.add_argument("--account", "-a", required=True, help="Bank/credit card account (e.g., Assets:Bank:Checking)")
    p_import.add_argument("--tag", "-t", action="append", help="Only include transactions with this tag (can specify multiple)")
    p_import.add_argument("--exclude-tag", "-x", action="append", help="Exclude transactions with this tag (can specify multiple)")

    # sync-monarch
    p_sync = sub.add_parser("sync-monarch", help="Sync transactions from Monarch Money API")
    p_sync.add_argument("--dry-run", action="store_true", help="Preview without writing files or tracking")

    # add-transaction
    p_add = sub.add_parser("add-transaction", help="Add a single transaction to the ledger")
    p_add.add_argument("--date", "-d", required=True, help="Transaction date (YYYY-MM-DD)")
    p_add.add_argument("--payee", "-p", required=True, help="Payee name")
    p_add.add_argument("--narration", "-n", required=True, help="Transaction description")
    p_add.add_argument("--debit", required=True, help="Debit account (e.g., Expenses:Food:Groceries)")
    p_add.add_argument("--credit", required=True, help="Credit account (e.g., Assets:Bank:Checking)")
    p_add.add_argument("--amount", "-a", required=True, help="Transaction amount (positive number)")
    p_add.add_argument("--currency", "-c", default="USD", help="Currency (default: USD)")

    # invoice (with subcommands)
    p_inv = sub.add_parser("invoice", help="Invoice management")
    inv_sub = p_inv.add_subparsers(dest="invoice_command", required=True)

    # invoice generate
    p_inv_gen = inv_sub.add_parser("generate", help="Generate invoices for a billing period")
    p_inv_gen.add_argument("--period", "-p", default=None, help="Billing period upper bound (YYYY-MM). If omitted, all uninvoiced entries are selected.")
    p_inv_gen.add_argument("--client", "-c", help="Filter by client key")
    p_inv_gen.add_argument("--entity", "-e", help="Filter by entity key")
    p_inv_gen.add_argument("--dry-run", action="store_true", help="Preview without generating files")

    # invoice list
    p_inv_list = inv_sub.add_parser("list", help="List invoices (outstanding by default)")
    p_inv_list.add_argument("--client", "-c", help="Filter by client")
    p_inv_list.add_argument("--all", "-a", action="store_true", help="Show all invoices (including paid)")

    # invoice paid
    p_inv_paid = inv_sub.add_parser("paid", help="Record invoice payment")
    p_inv_paid.add_argument("invoice_number", help="Invoice number (e.g., INV-000001)")
    p_inv_paid.add_argument("--date", "-d", required=True, help="Payment date (YYYY-MM-DD)")
    p_inv_paid.add_argument("--bank", "-b", help="Bank account (default: from entity config)")
    p_inv_paid.add_argument("--no-post", action="store_true", help="Skip ledger posting (e.g., when bank transaction already imported via Monarch)")

    # invoice create
    p_inv_create = inv_sub.add_parser("create", help="Create a manual single invoice")
    p_inv_create.add_argument("client", help="Client key (from INVOICING.md config)")
    p_inv_create.add_argument("--service", "-s", help="Service key")
    p_inv_create.add_argument("--qty", "-q", type=float, help="Quantity (hours, days, etc.)")
    p_inv_create.add_argument("--description", help="Line item description")
    p_inv_create.add_argument("--item", dest="items", action="append", help='Manual item: "description" amount')
    p_inv_create.add_argument("--entity", "-e", help="Entity key for the invoice")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    invoice_commands = {
        "generate": cmd_invoice_generate,
        "list": cmd_invoice_list,
        "paid": cmd_invoice_paid,
        "create": cmd_invoice_create,
    }

    commands = {
        "list": cmd_list_ledgers,
        "check": cmd_check,
        "balances": cmd_balances,
        "query": cmd_query,
        "report": cmd_report,
        "lots": cmd_lots,
        "wash-sales": cmd_wash_sales,
        "import-monarch": cmd_import_monarch,
        "sync-monarch": cmd_sync_monarch,
        "add-transaction": cmd_add_transaction,
    }

    try:
        if args.command == "invoice":
            result = invoice_commands[args.invoice_command](args)
        else:
            result = commands[args.command](args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result.get("status") == "error":
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
