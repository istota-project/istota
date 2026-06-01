"""Stable transaction id generation.

Lives in its own module so every ledger writer (``transactions``,
``invoicing``, ``importers``) can stamp an ``id:`` without importing
``core.edit`` (which imports ``transactions`` and would cycle).
"""

from __future__ import annotations

import uuid


def new_txn_id() -> str:
    """Return a fresh stable transaction id (uuid4 hex, 32 chars)."""
    return uuid.uuid4().hex
