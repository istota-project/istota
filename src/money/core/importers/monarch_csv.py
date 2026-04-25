"""Monarch Money CSV import source."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .base import NormalizedTransaction


MONARCH_CSV_COLUMNS = {"Date", "Merchant", "Category", "Account", "Amount"}


def detect_monarch_csv(file_path: Path) -> bool:
    """Check if a CSV file has Monarch Money column headers."""
    try:
        with open(file_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None:
                return False
            return MONARCH_CSV_COLUMNS.issubset(set(h.strip() for h in header))
    except Exception:
        return False


def parse_monarch_csv(
    file_path: Path,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
) -> list[NormalizedTransaction]:
    """Parse a Monarch Money CSV export into normalized transactions.

    Columns: Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner
    """
    from money.core.transactions import parse_tags, filter_by_tags

    transactions = []

    with open(file_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row.get("Date", "")
            try:
                if "/" in date_str:
                    txn_date = datetime.strptime(date_str, "%m/%d/%Y").date()
                else:
                    txn_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            tags = parse_tags(row.get("Tags", ""))

            if not filter_by_tags(tags, include_tags, exclude_tags):
                continue

            amount_str = row.get("Amount", "0").replace("$", "").replace(",", "")
            try:
                amount = float(amount_str)
            except ValueError:
                continue

            transactions.append(NormalizedTransaction(
                date=txn_date,
                amount=amount,
                payee=row.get("Merchant", "").strip(),
                category=row.get("Category", "").strip(),
                account_name=row.get("Account", "").strip(),
                notes=row.get("Notes", "").strip(),
                tags=tags,
                raw={
                    "original_statement": row.get("Original Statement", "").strip(),
                    "owner": row.get("Owner", "").strip(),
                },
            ))

    return transactions
