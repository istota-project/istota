"""Modular import system for transaction sources.

Each source provides a parser that produces NormalizedTransaction objects.
The shared import_transactions() pipeline handles dedup, beancount formatting,
staging files, and ledger writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from money.core.dedup import compute_transaction_hash, parse_ledger_transactions
from money.core.transactions import (
    format_beancount_transaction,
    map_monarch_category,
    append_to_ledger,
)

from .base import NormalizedTransaction


@dataclass
class ImportSource:
    """Registry entry for an import source."""
    name: str
    source_type: str  # "csv" or "api"
    detect: Callable[[Path], bool] | None = None


IMPORT_SOURCES: dict[str, ImportSource] = {}


def register_source(source: ImportSource) -> None:
    IMPORT_SOURCES[source.name] = source


def _register_builtin_sources() -> None:
    from .monarch_csv import detect_monarch_csv

    register_source(ImportSource(
        name="monarch-csv",
        source_type="csv",
        detect=detect_monarch_csv,
    ))
    register_source(ImportSource(
        name="monarch-api",
        source_type="api",
        detect=None,
    ))


_register_builtin_sources()


def detect_source(file_path: Path) -> str | None:
    """Auto-detect import source from file headers.

    Returns source name or None if no match.
    """
    for name, source in IMPORT_SOURCES.items():
        if source.detect is not None and source.detect(file_path):
            return name
    return None


def import_transactions(
    ledger_path: Path,
    transactions: list[NormalizedTransaction],
    source_name: str,
    contra_account: str,
    category_map: dict[str, str] | None = None,
    db_conn=None,
    source_file: str | None = None,
) -> dict:
    """Shared import pipeline for all file-based sources.

    Steps:
    1. Content-based dedup against ledger and DB
    2. Map categories to beancount accounts
    3. Format beancount entries
    4. Write staging file
    5. Append to ledger
    6. Track hashes in DB
    """
    if not transactions:
        return {"status": "error", "error": "No transactions to import"}

    ledger_hashes = parse_ledger_transactions(ledger_path)

    entries = []
    content_hashes = []
    content_skipped_count = 0

    for txn in transactions:
        content_hash = compute_transaction_hash(
            txn.date.isoformat(), abs(txn.amount), txn.payee,
        )

        if content_hash in ledger_hashes:
            content_skipped_count += 1
            continue

        if db_conn is not None:
            from money.db import is_content_hash_synced
            if is_content_hash_synced(db_conn, content_hash):
                content_skipped_count += 1
                continue

        if category_map and txn.category:
            posting_account = _map_category(txn.category, category_map)
        elif txn.category:
            posting_account = map_monarch_category(txn.category)
        else:
            posting_account = "Expenses:Uncategorized"

        entry = format_beancount_transaction(
            txn_date=txn.date,
            payee=txn.payee,
            narration=txn.notes or txn.category or "Imported transaction",
            posting_account=posting_account,
            contra_account=contra_account,
            amount=txn.amount,
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

    # Write staging file
    ledger_dir = ledger_path.parent
    imports_dir = ledger_dir / "imports"
    imports_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging_file = imports_dir / f"{source_name}_import_{timestamp}.beancount"

    header = f"; Imported via {source_name} on {datetime.now().isoformat()}\n"
    if source_file:
        header += f"; Source: {source_file}\n"
    header += f"; Transaction count: {len(entries)}\n"
    if content_skipped_count > 0:
        header += f"; Skipped (already in ledger): {content_skipped_count}\n"
    header += "; Auto-appended to main ledger. Staging file kept for audit trail.\n\n"

    staging_file.write_text(header + "\n\n".join(entries) + "\n")

    append_to_ledger(ledger_path, entries)

    # Track imported hashes in DB
    if content_hashes and db_conn is not None:
        from money.db import track_csv_transactions_batch
        track_csv_transactions_batch(db_conn, content_hashes, source_file)

    return {
        "status": "ok",
        "transaction_count": len(entries),
        "content_skipped_count": content_skipped_count,
        "staging_file": str(staging_file),
        "message": f"Imported {len(entries)} transactions to ledger",
    }


def _map_category(category: str, category_map: dict[str, str]) -> str:
    """Look up category in a map with case-insensitive fallback."""
    if category in category_map:
        return category_map[category]
    for key, value in category_map.items():
        if key.lower() == category.lower():
            return value
    return f"Expenses:Uncategorized:{category.replace(' ', '')}"
