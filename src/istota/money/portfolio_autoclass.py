"""Automatic symbol classification for the portfolio module.

New symbols are tickers, and ticker metadata is public — so a new position
should classify itself instead of landing in the settings page as homework.
Two tiers, both writing ``source='auto'`` rows that never overwrite an
existing classification (user edits stay authoritative):

1. **Online lookup** (:func:`classify_from_lookup` over yfinance metadata) —
   quote type, fund category, equity sector and country. Primary tier; needs
   the ``markets`` extra and network, both guarded, both fail-soft.
2. **Offline description heuristics** (:func:`classify_from_description`) —
   the export row's own description ("ISHARES 20 PLUS YR TREASURY BD ETF"
   says Fixed Income with no network). Deliberately conservative: a bare
   company name yields ``None`` rather than a guess, because a silent wrong
   classification is worse than a visible Unclassified.
"""

from __future__ import annotations

import logging
import re
import sqlite3

from istota.money import portfolio
from istota.money.portfolio import CASH_CLASS, UNCLASSIFIED, normalize_symbol

logger = logging.getLogger("istota.money.portfolio_autoclass")

# Bound the network time one run can spend; the offline heuristic still runs
# for symbols past the cap, and the next run picks up whatever remains.
MAX_LOOKUPS_PER_RUN = 40

# Morningstar-style category names → the seed map's sub-class vocabulary.
_SUB_CLASS_MAP = {
    "large blend": "Large Cap",
    "large growth": "Large Cap Growth",
    "large value": "Large Cap Value",
    "mid-cap blend": "Mid Cap",
    "mid-cap growth": "Mid Cap Growth",
    "mid-cap value": "Mid Cap Value",
    "small blend": "Small Cap",
    "small growth": "Small Cap Growth",
    "small value": "Small Cap Value",
    "foreign large blend": "Developed Markets",
    "foreign large growth": "Developed Markets",
    "foreign large value": "Developed Markets",
    "diversified emerging mkts": "Emerging Markets",
}

_BOND_RE = re.compile(
    r"\b(bonds?|bd|treasur\w*|treas|tips|notes?|nts|"
    r"fixed income|municipal|muni|govt|government securit\w*)\b",
    re.IGNORECASE,
)
_SHORT_TERM_RE = re.compile(
    r"\b(ultra\s*short|short[- ]term|0-3|1-3)\b", re.IGNORECASE
)
_LONG_TERM_RE = re.compile(
    r"\b(?:long[- ]term|20 plus|extended duration)\b|\b20\+", re.IGNORECASE
)
_TIPS_RE = re.compile(r"\b(tips|inflation)\b", re.IGNORECASE)
_INTERMEDIATE_RE = re.compile(r"\b(intermediate|intmdt)\b", re.IGNORECASE)

_COMMODITY_SUBS = (
    ("gold", "Gold"),
    ("silver", "Silver"),
    ("platinum", "Platinum"),
    ("palladium", "Palladium"),
    ("commodit", "Broad Basket"),
)

_EMERGING_RE = re.compile(r"\bemerging\b", re.IGNORECASE)
_GLOBAL_RE = re.compile(r"\b(global|world)\b", re.IGNORECASE)
_INTERNATIONAL_RE = re.compile(
    r"\b(intl|international|foreign|ex[- ]us|europe|china|japan|india|"
    r"pacific|latin america)\b",
    re.IGNORECASE,
)
_REAL_ESTATE_RE = re.compile(r"\b(real estate|reit)\b", re.IGNORECASE)
_TOTAL_MARKET_RE = re.compile(
    r"\btotal (stock |stk )?(market|mkt)\b|\btotal (stock|stk)\b", re.IGNORECASE
)
_FUND_MARKER_RE = re.compile(
    r"\b(etf|fund|fd|index|trust|tr|shares|ishares|spdr|vanguard|fidelity)\b",
    re.IGNORECASE,
)


def _fund_geography(text: str) -> str:
    if _GLOBAL_RE.search(text):
        return "Global"
    if _EMERGING_RE.search(text) or _INTERNATIONAL_RE.search(text):
        return "International"
    return "US"


def _classify_fund_text(text: str) -> tuple[str, str, str] | None:
    """Shared fund-text rules for a lookup category and an export description."""
    if not text.strip():
        return None
    lowered = text.lower()
    for needle, sub in _COMMODITY_SUBS:
        if needle in lowered:
            return ("Commodities", sub, "Global")
    if _BOND_RE.search(text):
        if _TIPS_RE.search(text):
            sub = "TIPS"
        elif _SHORT_TERM_RE.search(text):
            sub = "Short-Term"
        elif _LONG_TERM_RE.search(text):
            sub = "Long-Term"
        elif _INTERMEDIATE_RE.search(text):
            sub = "Intermediate"
        else:
            sub = ""
        return ("Fixed Income", sub, _fund_geography(text))
    if _REAL_ESTATE_RE.search(text):
        return ("Stocks", "Real Estate", _fund_geography(text))
    geography = _fund_geography(text)
    if _EMERGING_RE.search(text):
        sub = "Emerging Markets"
    elif _TOTAL_MARKET_RE.search(text):
        sub = "Total Market"
    else:
        sub = _SUB_CLASS_MAP.get(lowered.strip(), "")
    return ("Stocks", sub, geography)


def classify_from_lookup(info: dict | None) -> tuple[str, str, str] | None:
    """Map a yfinance-style info dict to (asset_class, sub_class, geography).

    Returns ``None`` when the metadata carries no usable signal.
    """
    if not info:
        return None
    quote_type = (info.get("quoteType") or "").upper()
    if quote_type == "MONEYMARKET":
        return (CASH_CLASS, "Money Market", "US")
    if quote_type == "CRYPTOCURRENCY":
        return ("Alternative", "Cryptocurrency", "Global")
    if quote_type in ("ETF", "MUTUALFUND"):
        category = (info.get("category") or "").strip()
        name = (info.get("longName") or info.get("shortName") or "").strip()
        # Category is the structured signal; the name breaks ties it can't
        # (a "Commodities Focused" category doesn't say which commodity).
        result = _classify_fund_text(f"{category} {name}".strip())
        if result is not None and result[0] == "Stocks" and not result[1]:
            # The sub-class table keys on the exact category, which the
            # appended fund name would otherwise mask.
            sub = _SUB_CLASS_MAP.get(category.lower())
            if sub:
                result = (result[0], sub, result[2])
        return result
    if quote_type == "EQUITY":
        sector = (info.get("sector") or "").strip() or "Individual Stock"
        country = (info.get("country") or "").strip()
        if not country or country == "United States":
            geography = "US"
        else:
            geography = country
        return ("Stocks", sector, geography)
    return None


def classify_from_description(
    symbol_norm: str, description: str, security_type: str
) -> tuple[str, str, str] | None:
    """Offline classification from the export row itself.

    Conservative by design: funds, bonds and commodities announce themselves
    in a Fidelity description; a bare company name does not, and guessing
    "Stocks/US" for it would silently misfile foreign listings and direct
    bonds. Those wait for the online lookup.
    """
    desc = (description or "").strip()
    if not desc:
        return None
    result = _classify_fund_text(desc)
    if result is None:
        return None
    asset_class, sub, geography = result
    if asset_class == "Stocks" and not _FUND_MARKER_RE.search(desc):
        # No fund marker and no bond/commodity signal: a plain security
        # description we refuse to guess from.
        return None
    return (asset_class, sub, geography)


def fetch_symbol_info(symbol: str) -> dict | None:
    """One ticker metadata lookup; ``None`` on any failure (no yfinance,
    no network, unknown symbol)."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        info = yf.Ticker(symbol).get_info()
    except Exception:
        logger.debug("autoclass lookup failed for %s", symbol, exc_info=True)
        return None
    return info if isinstance(info, dict) else None


def auto_classify_symbols(
    conn: sqlite3.Connection,
    candidates: list[tuple[str, str, str]],
    *,
    fetch=None,
    max_lookups: int = MAX_LOOKUPS_PER_RUN,
) -> dict:
    """Classify every candidate that currently resolves to Unclassified.

    ``candidates`` is ``(symbol, description, security_type)`` per position
    row (raw symbol fine — normalized here, deduped by the normalized key).
    Writes ``source='auto'`` rows; an existing row of any source is never
    touched. Returns ``{"classified": [...], "unresolved": [...]}`` where
    each classified entry carries the written triple plus its ``method``
    (``lookup`` | ``heuristic``).
    """
    if fetch is None:
        fetch = fetch_symbol_info
    seen: set[str] = set()
    classified: list[dict] = []
    unresolved: list[str] = []
    lookups = 0
    for symbol, description, security_type in candidates:
        norm = normalize_symbol(symbol)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        try:
            resolved = portfolio.resolve_classification(
                conn, norm, description, security_type
            )
            if resolved[0] != UNCLASSIFIED:
                continue
            info = None
            if lookups < max_lookups:
                lookups += 1
                try:
                    info = fetch(norm)
                except Exception:
                    logger.debug(
                        "autoclass fetch raised for %s", norm, exc_info=True
                    )
            result = classify_from_lookup(info)
            method = "lookup"
            if result is None:
                result = classify_from_description(norm, description, security_type)
                method = "heuristic"
            if result is None:
                unresolved.append(norm)
                continue
            asset_class, sub_class, geography = result
            portfolio.set_classification(
                conn, norm,
                asset_class=asset_class, sub_class=sub_class,
                geography=geography, source="auto",
            )
            classified.append({
                "symbol": norm,
                "asset_class": asset_class,
                "sub_class": sub_class,
                "geography": geography,
                "method": method,
            })
        except Exception:
            logger.warning("autoclass failed for %s", norm, exc_info=True)
            unresolved.append(norm)
    return {"classified": classified, "unresolved": unresolved}


def auto_classify_snapshot(
    conn: sqlite3.Connection,
    parsed,
    *,
    fetch=None,
    max_lookups: int = MAX_LOOKUPS_PER_RUN,
) -> dict:
    """Auto-classify a just-imported snapshot's position rows — the import
    call sites' one-liner. ``parsed`` is a ``ParsedSnapshot``."""
    candidates = [
        (row.symbol, row.description, row.security_type)
        for row in parsed.rows
        if row.row_type == "position"
    ]
    return auto_classify_symbols(conn, candidates, fetch=fetch, max_lookups=max_lookups)


def candidates_from_positions(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Distinct position symbols across all snapshots, one sample row each —
    the backfill path's candidate set."""
    rows = conn.execute(
        "SELECT symbol_norm, MAX(description), MAX(security_type) "
        "FROM portfolio_positions "
        "WHERE row_type = 'position' AND symbol_norm != '' "
        "GROUP BY symbol_norm ORDER BY symbol_norm"
    ).fetchall()
    return [(r[0], r[1] or "", r[2] or "") for r in rows]
