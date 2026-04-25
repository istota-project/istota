"""Content hashing and cross-source deduplication."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


def compute_transaction_hash(
    txn_date: str,
    amount: float,
    merchant: str,
    account: str = "",
) -> str:
    """Compute SHA-256 hash for transaction deduplication.

    Args:
        txn_date: Transaction date in YYYY-MM-DD format
        amount: Transaction amount
        merchant: Merchant/payee name
        account: Account name (optional, omit for cross-source matching)

    Returns:
        Hex-encoded SHA-256 hash
    """
    content = f"{txn_date}|{amount:.2f}|{merchant.strip().lower()}"
    if account:
        content += f"|{account.strip().lower()}"
    return hashlib.sha256(content.encode()).hexdigest()


def parse_ledger_transactions(ledger_path: Path) -> set[str]:
    """Parse beancount ledger and return content hashes of existing transactions.

    Extracts (date, amount, payee) from each transaction for cross-source dedup.
    Returns a set of SHA-256 hashes.
    """
    if not ledger_path.exists():
        return set()

    text = ledger_path.read_text()
    hashes: set[str] = set()

    # Also scan import staging files in the imports/ directory
    imports_dir = ledger_path.parent / "imports"
    texts = [text]
    if imports_dir.is_dir():
        for f in imports_dir.glob("*.beancount"):
            texts.append(f.read_text())

    # Match transaction header: YYYY-MM-DD * "payee" "narration"
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

            rest = content[match.end():]
            amount_match = amount_pattern.match(rest) or amount_pattern.search(
                rest.split("\n\n")[0]
            )
            if not amount_match:
                continue

            amount = abs(float(amount_match.group(1).replace(",", "")))
            content_hash = compute_transaction_hash(txn_date, amount, payee)
            hashes.add(content_hash)

    return hashes
