"""Shared types for positions-kind import sources.

Positions imports (point-in-time portfolio snapshots) share no pipeline with
transaction imports downstream of parsing — a parser produces
:class:`ParsedSnapshot` objects that :func:`istota.money.portfolio.insert_snapshot`
stores. One snapshot per Fidelity export; many for the fina history file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime


class PositionParseError(Exception):
    """A positions file could not be parsed. Message is user-facing."""


@dataclass
class PositionRow:
    account_number: str          # raw, may be "" (options rows)
    account_name: str
    symbol: str                  # raw as exported, e.g. "SPAXX**"
    description: str
    row_type: str                # "position" | "cash" | "pending"
    quantity: float | None
    price: float | None
    value: float | None          # Current Value
    cost_basis: float | None     # Cost Basis Total
    avg_cost_basis: float | None # cleaned to float (fina stored the raw string)
    day_gain: float | None
    day_gain_pct: float | None   # stored as fraction (0.0169 = 1.69%)
    total_gain: float | None
    total_gain_pct: float | None
    pct_of_account: float | None
    security_type: str           # Fidelity "Type" column
    raw: dict = field(default_factory=dict)


@dataclass
class ParsedSnapshot:
    exported_at: datetime        # from the export footer, or fallback (flagged)
    exported_at_estimated: bool  # True when the footer date was missing/unparseable
    rows: list[PositionRow]
    source: str                  # registry source name
    warnings: list[str]          # non-fatal parse notes surfaced to the caller
    group_hints: dict[str, str] = field(default_factory=dict)  # account_name -> group label


_CASH_SYMBOL_PREFIXES = ("SPAXX", "FZDXX", "CORE", "USD")
_CASH_DESCRIPTION_RE = re.compile(r"money market|cash|fdic", re.IGNORECASE)

OPTION_DESCRIPTION_RE = re.compile(r"\d+ [CP]$")


def is_cash_row(symbol: str, description: str) -> bool:
    """Fidelity cash/core-position detection, shared by both parsers.

    Matches the symbol shapes (``SPAXX**``, ``USD***``, ``CORE**``, bare
    ``**``) and the money-market/FDIC description patterns fina's fallback
    regex cast, minus the dead exact-match list.
    """
    sym = (symbol or "").strip()
    if sym:
        if sym.upper().startswith(_CASH_SYMBOL_PREFIXES):
            return True
        if sym.endswith("**") or sym == "**":
            return True
    if description and _CASH_DESCRIPTION_RE.search(description):
        return True
    return False


def clean_money(raw: str | None) -> float | None:
    """``"$1,234.56 "`` / ``"($130.52)"`` / ``"+$2.00"`` → float; ``--``/blank → None."""
    if raw is None:
        return None
    s = raw.strip()
    if not s or s == "--":
        return None
    s = s.replace("$", "").replace(",", "").replace(")", "").replace("+", "")
    s = s.replace("(", "-").strip()
    if not s or s == "--":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean_percent(raw: str | None) -> float | None:
    """``"-0.44%"`` / ``"+9.27%"`` → fraction; ``--``/blank → None."""
    if raw is None:
        return None
    s = raw.strip()
    if not s or s == "--":
        return None
    s = s.replace("%", "").replace("+", "").replace(")", "")
    s = s.replace("(", "-").strip()
    if not s:
        return None
    try:
        return float(s) / 100.0
    except ValueError:
        return None


def clean_quantity(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = raw.strip().replace(",", "")
    if not s or s == "--":
        return None
    try:
        return float(s)
    except ValueError:
        return None
