"""Shared types for the import system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class NormalizedTransaction:
    """A transaction normalized from any import source.

    All import sources produce these, which then feed into the shared
    import pipeline for dedup, beancount formatting, and ledger writes.
    """
    date: date
    amount: float
    payee: str
    category: str = ""
    account_name: str = ""
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    source_id: str = ""
    raw: dict = field(default_factory=dict)
