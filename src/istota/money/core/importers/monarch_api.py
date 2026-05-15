"""Monarch Money API fetch operations.

Low-level API calls to Monarch Money. Sync orchestration (dedup, reconciliation,
ledger writes) lives in core.transactions.

Auth: Monarch's web API now enforces Django CSRF on /graphql, so we use
session cookies (session_id + csrftoken) plus Origin/Referer headers via the
vendored client at ``istota.money._vendor.monarch_client``. The user pastes
the cookies once from browser DevTools — they last months on a trusted-device
login.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from istota.money._vendor.monarch_client import (
    MonarchAuthError,
    MonarchClient,
    MonarchCookieAuth,
)
from istota.money.core.models import MonarchConfig


logger = logging.getLogger(__name__)


def _build_client(config: MonarchConfig) -> MonarchClient:
    creds = config.credentials
    if not (creds.session_id and creds.csrftoken):
        raise ValueError(
            "Monarch credentials missing: need session_id and csrftoken cookies. "
            "Open app.monarch.com in your browser, copy session_id and csrftoken "
            "from DevTools > Application > Cookies, and store them via the "
            "money settings page or `istota secret ensure -u USER -s monarch "
            "-k session_id -v ...` / `... -k csrftoken -v ...`."
        )
    return MonarchClient(
        MonarchCookieAuth(
            session_id=creds.session_id,
            csrftoken=creds.csrftoken,
        )
    )


async def fetch_monarch_transactions(
    config: MonarchConfig,
    lookback_days: int,
) -> list[dict]:
    """Fetch transactions from Monarch Money API."""
    client = _build_client(config)
    start_date = date.today() - timedelta(days=lookback_days)
    end_date = date.today()
    try:
        data = await client.get_transactions(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            limit=500,
        )
    except MonarchAuthError:
        logger.exception("monarch_fetch_failed_auth lookback_days=%s", lookback_days)
        raise
    except Exception:
        logger.exception("monarch_fetch_failed lookback_days=%s", lookback_days)
        raise
    return data.get("allTransactions", {}).get("results", [])


async def fetch_transactions_by_ids(
    config: MonarchConfig,
    transaction_ids: list[str],
) -> dict[str, dict]:
    """Fetch specific transactions from Monarch by ID.

    The Monarch API has no by-id batch endpoint, so we pull a wide window
    and filter locally — same approach as the prior implementation.
    """
    client = _build_client(config)
    start_date = date.today() - timedelta(days=365)
    end_date = date.today()
    try:
        data = await client.get_transactions(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            limit=2000,
        )
    except MonarchAuthError:
        logger.exception("monarch_fetch_by_ids_failed_auth count=%s", len(transaction_ids))
        raise
    except Exception:
        logger.exception("monarch_fetch_by_ids_failed count=%s", len(transaction_ids))
        raise
    results = data.get("allTransactions", {}).get("results", [])
    wanted = set(transaction_ids)
    return {txn.get("id"): txn for txn in results if txn.get("id") in wanted}
