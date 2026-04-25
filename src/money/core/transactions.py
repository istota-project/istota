"""Transaction operations: category mapping, beancount formatting, ledger writes, config parsing.

Import/sync logic lives in core.importers.* — this module re-exports the public
functions for backward compatibility with CLI, API, and tests.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import tomli

from .dedup import compute_transaction_hash, parse_ledger_transactions
from .models import (
    MonarchConfig,
    MonarchCredentials,
    MonarchProfile,
    MonarchSyncSettings,
    MonarchTagFilters,
)


# =============================================================================
# Monarch category to beancount account mapping
# =============================================================================

MONARCH_CATEGORY_MAP = {
    # Income
    "Income": "Income:Salary",
    "Paycheck": "Income:Salary",
    "Interest": "Income:Interest",
    "Dividends": "Income:Dividends",
    "Investment Income": "Income:Investment",
    "Refund": "Income:Refunds",
    # Food
    "Groceries": "Expenses:Food:Groceries",
    "Restaurants": "Expenses:Food:Restaurants",
    "Food & Drink": "Expenses:Food:Other",
    "Coffee Shops": "Expenses:Food:Coffee",
    # Transport
    "Gas": "Expenses:Transport:Gas",
    "Parking": "Expenses:Transport:Parking",
    "Auto Insurance": "Expenses:Transport:Insurance",
    "Auto Payment": "Expenses:Transport:CarPayment",
    "Public Transit": "Expenses:Transport:Transit",
    "Rideshare": "Expenses:Transport:Rideshare",
    "Transportation": "Expenses:Transport:Other",
    # Housing
    "Rent": "Expenses:Housing:Rent",
    "Mortgage": "Expenses:Housing:Mortgage",
    "Utilities": "Expenses:Housing:Utilities",
    "Internet": "Expenses:Housing:Internet",
    "Phone": "Expenses:Housing:Phone",
    "Home Improvement": "Expenses:Housing:Improvement",
    "Home Insurance": "Expenses:Housing:Insurance",
    # Shopping
    "Shopping": "Expenses:Shopping",
    "Clothing": "Expenses:Shopping:Clothing",
    "Electronics": "Expenses:Shopping:Electronics",
    "Amazon": "Expenses:Shopping:Amazon",
    # Entertainment
    "Entertainment": "Expenses:Entertainment",
    "Streaming": "Expenses:Entertainment:Streaming",
    "Movies": "Expenses:Entertainment:Movies",
    "Games": "Expenses:Entertainment:Games",
    # Health
    "Health": "Expenses:Health",
    "Doctor": "Expenses:Health:Doctor",
    "Pharmacy": "Expenses:Health:Pharmacy",
    "Health Insurance": "Expenses:Health:Insurance",
    # Travel
    "Travel": "Expenses:Travel",
    "Hotels": "Expenses:Travel:Hotels",
    "Flights": "Expenses:Travel:Flights",
    # Other
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


def map_monarch_category(category: str) -> str:
    """Map a Monarch category to a beancount account."""
    if category in MONARCH_CATEGORY_MAP:
        return MONARCH_CATEGORY_MAP[category]

    for key, value in MONARCH_CATEGORY_MAP.items():
        if key.lower() == category.lower():
            return value

    return f"Expenses:Uncategorized:{category.replace(' ', '')}"


def map_monarch_category_with_config(category: str, config: MonarchConfig) -> str:
    """Map a Monarch category, checking config overrides first."""
    if category in config.categories:
        return config.categories[category]

    for key, value in config.categories.items():
        if key.lower() == category.lower():
            return value

    return map_monarch_category(category)


def map_monarch_account(account_name: str, config: MonarchConfig) -> str:
    """Map a Monarch account name to a beancount account."""
    if account_name in config.accounts:
        return config.accounts[account_name]

    for key, value in config.accounts.items():
        if key.lower() == account_name.lower():
            return value

    return config.sync.default_account


# =============================================================================
# Tag filtering
# =============================================================================


def parse_tags(tags_str: str) -> list[str]:
    """Parse comma-separated tags from Monarch CSV Tags column."""
    if not tags_str or not tags_str.strip():
        return []
    return [t.strip() for t in tags_str.split(",") if t.strip()]


def filter_by_tags(
    tags: list[str],
    include_tags: list[str] | None,
    exclude_tags: list[str] | None,
) -> bool:
    """Check if transaction passes tag filters.

    Returns True if transaction passes filters.
    """
    if include_tags:
        if not any(t in include_tags for t in tags):
            return False

    if exclude_tags:
        if any(t in exclude_tags for t in tags):
            return False

    return True


# =============================================================================
# TOML/Markdown config parsing
# =============================================================================


def parse_monarch_config(
    config_path: Path,
    secrets: dict | None = None,
) -> MonarchConfig:
    """Parse Monarch Money config file (TOML or MONARCH.md) into MonarchConfig.

    Args:
        config_path: Path to monarch.toml or MONARCH.md
        secrets: Optional dict from secrets file. If present, secrets["monarch"]
                 fields override credentials from the config file.
    """
    from money._config_io import read_toml_config
    data = read_toml_config(config_path)

    monarch = data.get("monarch", {})

    # Merge secrets overlay onto credentials
    secret_creds = (secrets or {}).get("monarch", {})
    credentials = MonarchCredentials(
        email=secret_creds.get("email") or monarch.get("email"),
        password=secret_creds.get("password") or monarch.get("password"),
        session_token=secret_creds.get("session_token") or monarch.get("session_token"),
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

    # Parse per-ledger profiles
    profiles = []
    for profile_name, profile_data in monarch.get("profiles", {}).items():
        profile_sync_data = profile_data.get("sync", {})
        profile_sync = MonarchSyncSettings(
            lookback_days=profile_sync_data.get("lookback_days", sync.lookback_days),
            default_account=profile_data.get("default_account", profile_sync_data.get("default_account", sync.default_account)),
            recategorize_account=profile_data.get("recategorize_account", profile_sync_data.get("recategorize_account", sync.recategorize_account)),
        )

        profile_tags_data = profile_data.get("tags", {})
        profile_tags = MonarchTagFilters(
            include=profile_tags_data.get("include", []),
            exclude=profile_tags_data.get("exclude", []),
        )

        # Profile accounts/categories inherit from top-level if not set
        profile_accounts = profile_data.get("accounts", None)
        if profile_accounts is None:
            profile_accounts = dict(accounts)
        profile_categories = profile_data.get("categories", None)
        if profile_categories is None:
            profile_categories = dict(categories)

        profiles.append(MonarchProfile(
            name=profile_name,
            ledger=profile_data.get("ledger", profile_name),
            sync=profile_sync,
            accounts=profile_accounts,
            categories=profile_categories,
            tags=profile_tags,
        ))

    return MonarchConfig(
        credentials=credentials,
        sync=sync,
        accounts=accounts,
        categories=categories,
        tags=tags,
        profiles=profiles,
    )


# =============================================================================
# Beancount formatting
# =============================================================================


def format_beancount_transaction(
    txn_date: date,
    payee: str,
    narration: str,
    posting_account: str,
    contra_account: str,
    amount: float,
    currency: str = "USD",
) -> str:
    """Format a single beancount transaction."""
    payee = payee.replace('"', '\\"')
    narration = narration.replace('"', '\\"')

    lines = [f'{txn_date.isoformat()} * "{payee}" "{narration}"']

    if amount < 0:
        lines.append(f'  {posting_account}  {abs(amount):.2f} {currency}')
        lines.append(f'  {contra_account}')
    else:
        lines.append(f'  {contra_account}  {amount:.2f} {currency}')
        lines.append(f'  {posting_account}')

    return "\n".join(lines)


def format_recategorization_entry(
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
    lines.append(f'  {recategorize_account}  {abs(amount):.2f} {currency}')
    lines.append(f'  {original_account}  -{abs(amount):.2f} {currency}')
    return "\n".join(lines)


def format_category_change_entry(
    txn_date: date,
    merchant: str,
    old_account: str,
    new_account: str,
    amount: float,
    currency: str = "USD",
) -> str:
    """Format a ledger entry that moves a transaction from one category to another."""
    merchant = merchant.replace('"', '\\"')
    lines = [f'{txn_date.isoformat()} * "{merchant}" "Recategorized in Monarch"']
    lines.append(f'  {new_account}  {abs(amount):.2f} {currency}')
    lines.append(f'  {old_account}  -{abs(amount):.2f} {currency}')
    return "\n".join(lines)


# =============================================================================
# Ledger file operations
# =============================================================================


def backup_ledger(ledger_path: Path, max_backups: int = 10) -> Path | None:
    """Create a timestamped backup of the ledger file before modification."""
    if not ledger_path.exists():
        return None

    backups_dir = ledger_path.parent / "backups"
    backups_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backups_dir / f"{ledger_path.name}.{timestamp}"

    shutil.copy2(ledger_path, backup_path)

    existing = sorted(backups_dir.glob(f"{ledger_path.name}.*"), reverse=True)
    for old_backup in existing[max_backups:]:
        old_backup.unlink()

    return backup_path


def append_to_ledger(ledger_path: Path, entries: list[str]) -> None:
    """Append beancount entries to the main ledger file with backup."""
    if not entries:
        return
    backup_ledger(ledger_path)
    with open(ledger_path, "a") as f:
        for entry in entries:
            f.write(f"\n{entry}\n")


# =============================================================================
# CSV import — delegates to core.importers
# =============================================================================


def parse_monarch_csv(
    file_path: Path,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
) -> list[dict]:
    """Parse a Monarch Money CSV export.

    Backward-compatible wrapper: returns list[dict] (not NormalizedTransaction).
    """
    from .importers.monarch_csv import parse_monarch_csv as _parse

    normalized = _parse(file_path, include_tags, exclude_tags)
    return [
        {
            "date": txn.date,
            "merchant": txn.payee,
            "category": txn.category,
            "account_name": txn.account_name,
            "original_statement": txn.raw.get("original_statement", ""),
            "amount": txn.amount,
            "notes": txn.notes,
            "tags": txn.tags,
            "owner": txn.raw.get("owner", ""),
        }
        for txn in normalized
    ]


def import_csv(
    ledger_path: Path,
    file_path: Path,
    account: str,
    db_conn=None,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
) -> dict:
    """Import transactions from Monarch Money CSV export.

    Delegates to the modular import pipeline via core.importers.
    """
    from .importers import import_transactions
    from .importers.monarch_csv import parse_monarch_csv as _parse

    if not file_path.exists():
        return {"status": "error", "error": f"File not found: {file_path}"}

    try:
        transactions = _parse(file_path, include_tags, exclude_tags)
    except Exception as e:
        return {"status": "error", "error": f"Failed to parse CSV: {e}"}

    if not transactions:
        filter_msg = ""
        if include_tags or exclude_tags:
            filter_msg = " (after applying tag filters)"
        return {"status": "error", "error": f"No valid transactions found in CSV{filter_msg}"}

    return import_transactions(
        ledger_path=ledger_path,
        transactions=transactions,
        source_name="monarch-csv",
        contra_account=account,
        category_map=MONARCH_CATEGORY_MAP,
        db_conn=db_conn,
        source_file=file_path.name,
    )


# =============================================================================
# Monarch API sync
# =============================================================================


async def fetch_monarch_transactions(
    config: MonarchConfig,
    lookback_days: int,
) -> list[dict]:
    """Fetch transactions from Monarch Money API."""
    from .importers.monarch_api import fetch_monarch_transactions as _fetch
    return await _fetch(config, lookback_days)


async def fetch_transactions_by_ids(
    config: MonarchConfig,
    transaction_ids: list[str],
) -> dict[str, dict]:
    """Fetch specific transactions from Monarch by ID."""
    from .importers.monarch_api import fetch_transactions_by_ids as _fetch
    return await _fetch(config, transaction_ids)


def sync_monarch(
    ledger_path: Path,
    config: MonarchConfig,
    db_conn=None,
    dry_run: bool = False,
    transactions: list[dict] | None = None,
    profile: str = "",
) -> dict:
    """Sync transactions from Monarch Money API and reconcile tag changes.

    Args:
        ledger_path: Path to ledger file
        config: Monarch configuration
        db_conn: Optional database connection for dedup tracking
        dry_run: If True, preview without writing files or tracking
        transactions: Pre-fetched transactions list. If None, fetches from API.
        profile: Profile name for DB tracking (empty string for no profile).
    """
    import asyncio

    # Fetch transactions from API if not pre-fetched
    if transactions is None:
        try:
            transactions = asyncio.run(fetch_monarch_transactions(
                config, config.sync.lookback_days,
            ))
        except Exception as e:
            return {"status": "error", "error": f"Failed to fetch transactions: {e}"}

    # Build lookup of all fetched transactions by ID for reconciliation
    all_txn_by_id = {txn.get("id"): txn for txn in transactions if txn.get("id")}

    # Filter by tags if configured
    filtered_transactions = []
    for txn in transactions:
        txn_tags = [t.get("name", "") for t in txn.get("tags", [])]
        if not filter_by_tags(
            txn_tags,
            config.tags.include if config.tags.include else None,
            config.tags.exclude if config.tags.exclude else None,
        ):
            continue
        filtered_transactions.append(txn)

    # Deduplicate against previously synced transactions and ledger content
    new_transactions = []
    skipped_count = 0
    content_skipped_count = 0

    ledger_hashes = parse_ledger_transactions(ledger_path)

    if db_conn is not None:
        from money.db import (
            is_content_hash_synced,
            is_monarch_transaction_synced,
            get_active_monarch_synced_transactions,
        )

        for txn in filtered_transactions:
            txn_id = txn.get("id", "")
            if txn_id and is_monarch_transaction_synced(db_conn, txn_id, profile=profile):
                skipped_count += 1
                continue

            merchant = txn.get("merchant", {}).get("name", "") or txn.get("name", "Unknown")
            amount = float(txn.get("amount", 0))
            txn_date_str = txn.get("date", "")[:10]
            content_hash = compute_transaction_hash(txn_date_str, abs(amount), merchant)

            if content_hash in ledger_hashes or is_content_hash_synced(db_conn, content_hash):
                content_skipped_count += 1
                continue

            new_transactions.append(txn)
    else:
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
    synced_data = []

    for txn in new_transactions:
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

        contra_account = map_monarch_account(account_name, config)
        posting_account = map_monarch_category_with_config(category, config)

        entry = format_beancount_transaction(
            txn_date=txn_date,
            payee=merchant,
            narration=notes or category,
            posting_account=posting_account,
            contra_account=contra_account,
            amount=amount,
        )
        entries.append(entry)

        if txn_id:
            synced_data.append({
                "id": txn_id,
                "tags_json": json.dumps(txn_tags),
                "amount": amount,
                "merchant": merchant,
                "posted_account": posting_account,
                "txn_date": txn_date.isoformat(),
                "content_hash": compute_transaction_hash(txn_date.isoformat(), abs(amount), merchant),
            })

    # Reconciliation: check for tag/category changes on previously synced transactions
    recategorized_entries = []
    recategorized_ids = []
    category_change_entries = []
    category_change_updates = []

    if db_conn is not None:
        active_synced = get_active_monarch_synced_transactions(db_conn, profile=profile)

        if active_synced:
            for synced_txn in active_synced:
                current_txn = all_txn_by_id.get(synced_txn.monarch_transaction_id)
                if current_txn is None:
                    continue

                current_tags = [t.get("name", "") for t in current_txn.get("tags", [])]
                still_has_business_tag = filter_by_tags(
                    current_tags,
                    config.tags.include if config.tags.include else None,
                    config.tags.exclude if config.tags.exclude else None,
                )

                if not still_has_business_tag:
                    if (
                        synced_txn.amount is not None
                        and synced_txn.posted_account
                        and synced_txn.merchant
                        and synced_txn.txn_date
                    ):
                        recat_entry = format_recategorization_entry(
                            txn_date=date.today(),
                            merchant=synced_txn.merchant,
                            original_account=synced_txn.posted_account,
                            recategorize_account=config.sync.recategorize_account,
                            amount=synced_txn.amount,
                        )
                        recategorized_entries.append(recat_entry)
                        recategorized_ids.append(synced_txn.monarch_transaction_id)
                    continue

                if (
                    synced_txn.amount is not None
                    and synced_txn.posted_account
                    and synced_txn.merchant
                    and synced_txn.txn_date
                ):
                    current_category = current_txn.get("category", {}).get("name", "") or "Uncategorized"
                    new_posted_account = map_monarch_category_with_config(current_category, config)

                    if new_posted_account != synced_txn.posted_account:
                        cat_entry = format_category_change_entry(
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
    if category_change_entries:
        cat_change_file = imports_dir / f"monarch_category_change_{timestamp}.beancount"
        header = f"; Category changes from Monarch Money on {datetime.now().isoformat()}\n"
        header += f"; These transactions were recategorized in Monarch\n"
        header += f"; Category change count: {len(category_change_entries)}\n"
        header += "; Auto-appended to main ledger. Staging file kept for audit trail.\n\n"
        cat_change_file.write_text(header + "\n\n".join(category_change_entries) + "\n")
        result["category_change_file"] = str(cat_change_file)

    # Append to main ledger
    append_to_ledger(ledger_path, entries + recategorized_entries + category_change_entries)

    # Track in DB
    if db_conn is not None:
        from money.db import (
            track_monarch_transactions_batch,
            mark_monarch_transaction_recategorized,
            update_monarch_transaction_posted_account,
        )
        if synced_data:
            track_monarch_transactions_batch(db_conn, synced_data, profile=profile)
        for txn_id in recategorized_ids:
            mark_monarch_transaction_recategorized(db_conn, txn_id)
        for update in category_change_updates:
            update_monarch_transaction_posted_account(
                db_conn,
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


def sync_all_profiles(
    config: MonarchConfig,
    ledgers: list[dict],
    db_conn=None,
    dry_run: bool = False,
) -> dict:
    """Sync Monarch transactions across all configured profiles.

    If no profiles are defined, falls back to syncing with the first ledger
    using the flat config (backward compatible).

    Fetches transactions from Monarch once and passes them to each profile's sync.
    """
    import asyncio

    if not config.profiles:
        # Backward compatible: no profiles, sync to default ledger
        if not ledgers:
            return {"status": "error", "error": "No ledgers configured"}
        return sync_monarch(
            ledgers[0]["path"], config, db_conn=db_conn, dry_run=dry_run,
        )

    # Fetch transactions once for all profiles
    lookback = max(p.sync.lookback_days for p in config.profiles)
    try:
        all_transactions = asyncio.run(fetch_monarch_transactions(config, lookback))
    except Exception as e:
        return {"status": "error", "error": f"Failed to fetch transactions: {e}"}

    # Build ledger lookup
    ledger_by_name = {entry["name"].lower(): entry["path"] for entry in ledgers}

    profile_results = []
    for profile in config.profiles:
        ledger_path = ledger_by_name.get(profile.ledger.lower())
        if ledger_path is None:
            profile_results.append({
                "name": profile.name,
                "ledger": profile.ledger,
                "status": "error",
                "error": f"Ledger '{profile.ledger}' not found",
            })
            continue

        # Build a profile-scoped config with the profile's settings
        # but sharing credentials from the top-level config
        profile_config = MonarchConfig(
            credentials=config.credentials,
            sync=profile.sync,
            accounts=profile.accounts,
            categories=profile.categories,
            tags=profile.tags,
        )

        result = sync_monarch(
            ledger_path, profile_config,
            db_conn=db_conn, dry_run=dry_run,
            transactions=all_transactions,
            profile=profile.name,
        )
        result["name"] = profile.name
        result["ledger"] = profile.ledger
        profile_results.append(result)

    return {"status": "ok", "profiles": profile_results}


# =============================================================================
# Add transaction
# =============================================================================


def add_transaction(
    ledger_path: Path,
    txn_date: date,
    payee: str,
    narration: str,
    debit: str,
    credit: str,
    amount: float,
    currency: str = "USD",
) -> dict:
    """Add a transaction to the ledger."""
    if amount <= 0:
        return {"status": "error", "error": "Amount must be positive"}

    payee_escaped = payee.replace('"', '\\"')
    narration_escaped = narration.replace('"', '\\"')

    txn = f'{txn_date} * "{payee_escaped}" "{narration_escaped}"\n'
    txn += f'  {debit}  {amount:.2f} {currency}\n'
    txn += f'  {credit}\n'

    txn_dir = ledger_path.parent / "transactions"
    txn_dir.mkdir(exist_ok=True)
    txn_file = txn_dir / f"{txn_date.year}.beancount"

    with open(txn_file, "a") as f:
        f.write(f"\n{txn}")

    from .ledger import run_bean_check
    success, errors = run_bean_check(ledger_path)
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
        "payee": payee,
        "amount": amount,
        "currency": currency,
        "debit": debit,
        "credit": credit,
        "file": str(ledger_path),
    }
