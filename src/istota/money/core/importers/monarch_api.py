"""Monarch Money API fetch operations.

Low-level API calls to Monarch Money. Sync orchestration (dedup, reconciliation,
ledger writes) lives in core.transactions.
"""

from __future__ import annotations

from datetime import date, timedelta

from istota.money.core.models import MonarchConfig


async def fetch_monarch_transactions(
    config: MonarchConfig,
    lookback_days: int,
) -> list[dict]:
    """Fetch transactions from Monarch Money API."""
    try:
        from monarchmoney import MonarchMoney
    except ImportError:
        raise ValueError("monarchmoneycommunity package is required for API sync")

    if config.credentials.session_token:
        mm = MonarchMoney(token=config.credentials.session_token)
    elif config.credentials.email and config.credentials.password:
        mm = MonarchMoney()
        await mm.login(config.credentials.email, config.credentials.password)
    else:
        raise ValueError("No Monarch credentials configured (need email+password or session_token)")

    start_date = date.today() - timedelta(days=lookback_days)
    end_date = date.today()
    transactions = await mm.get_transactions(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )

    return transactions.get("allTransactions", {}).get("results", [])


async def fetch_transactions_by_ids(
    config: MonarchConfig,
    transaction_ids: list[str],
) -> dict[str, dict]:
    """Fetch specific transactions from Monarch by ID."""
    try:
        from monarchmoney import MonarchMoney
    except ImportError:
        raise ValueError("monarchmoneycommunity package is required for API sync")

    if config.credentials.session_token:
        mm = MonarchMoney(token=config.credentials.session_token)
    elif config.credentials.email and config.credentials.password:
        mm = MonarchMoney()
        await mm.login(config.credentials.email, config.credentials.password)
    else:
        raise ValueError("No Monarch credentials configured")

    start_date = date.today() - timedelta(days=365)
    end_date = date.today()
    all_txns = await mm.get_transactions(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )

    results = all_txns.get("allTransactions", {}).get("results", [])
    return {txn.get("id"): txn for txn in results if txn.get("id") in transaction_ids}
