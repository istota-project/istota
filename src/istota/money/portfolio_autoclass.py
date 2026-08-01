"""Automatic symbol classification for the portfolio module.

New symbols are tickers, and ticker metadata is public — so a new position
should classify itself instead of landing in the settings page as homework.
Two tiers, both writing ``source='auto'`` rows that never overwrite an
existing classification (user edits stay authoritative):

1. **Online lookup** (:func:`classify_from_lookup` over yfinance metadata) —
   quote type, fund category, equity sector and country. Primary tier; needs
   ``yfinance`` (shipped in both the ``money`` and ``markets`` extras) and
   network, both guarded, both fail-soft.
2. **Offline description heuristics** (:func:`classify_from_description`) —
   the export row's own description ("ISHARES 20 PLUS YR TREASURY BD ETF"
   says Fixed Income with no network). Deliberately conservative: a bare
   company name yields ``None`` rather than a guess, because a silent wrong
   classification is worse than a visible Unclassified.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sqlite3
import threading
import time

from istota.money import portfolio
from istota.money.portfolio import CASH_CLASS, UNCLASSIFIED, normalize_symbol

logger = logging.getLogger("istota.money.portfolio_autoclass")

# Two independent bounds on one run's network work. The count cap alone
# bounds nothing in time — a lookup's duration is the remote host's decision
# — and both an import and the backfill button run inside a request the
# reverse proxy will give up on. Past either bound the offline heuristic
# still runs, and the next run picks up whatever remains.
MAX_LOOKUPS_PER_RUN = 40
LOOKUP_BUDGET_SECONDS = 25.0
LOOKUP_TIMEOUT_SECONDS = 5.0

# Below this, "every lookup came back empty" is more likely one unknown
# ticker than an outage, and warning about it would be noise.
_OUTAGE_WARNING_MIN_ATTEMPTS = 3

# Morningstar-style equity categories → the seed map's sub-class vocabulary,
# matched compositionally rather than by exact key: the real names combine a
# region with a size and a style ("Global Large-Stock Blend"), so an exact
# table only ever covers the handful of spellings someone thought to write
# down. First match wins, so region rules precede size/style ones.
_EQUITY_SUB_RULES = (
    (re.compile(r"\bemerging\b", re.IGNORECASE), "Emerging Markets"),
    (re.compile(r"\bforeign\b", re.IGNORECASE), "Developed Markets"),
    (re.compile(r"\blarge\b.*\bgrowth\b", re.IGNORECASE), "Large Cap Growth"),
    (re.compile(r"\blarge\b.*\bvalue\b", re.IGNORECASE), "Large Cap Value"),
    (re.compile(r"\blarge\b", re.IGNORECASE), "Large Cap"),
    (re.compile(r"\bmid(-|\s)?cap\b.*\bgrowth\b", re.IGNORECASE), "Mid Cap Growth"),
    (re.compile(r"\bmid(-|\s)?cap\b.*\bvalue\b", re.IGNORECASE), "Mid Cap Value"),
    (re.compile(r"\bmid(-|\s)?cap\b", re.IGNORECASE), "Mid Cap"),
    (re.compile(r"\bsmall\b.*\bgrowth\b", re.IGNORECASE), "Small Cap Growth"),
    (re.compile(r"\bsmall\b.*\bvalue\b", re.IGNORECASE), "Small Cap Value"),
    (re.compile(r"\bsmall\b", re.IGNORECASE), "Small Cap"),
)

_BOND_RE = re.compile(
    r"\b(bonds?|bd|treasur(y|ies)|treas|tips|notes?|nts|"
    r"fixed income|municipal|muni|govt|government securit\w*)\b",
    re.IGNORECASE,
)
# A direct bond carries a coupon rate and a maturity date; a company name
# carries neither. This is what lets a Treasury note past the fund-marker
# gate below without letting "TREASURE GLOBAL INC" through with it.
_BOND_STRUCTURE_RE = re.compile(
    r"\d+(\.\d+)?\s*%|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
)
_BOND_SECURITY_TYPE_RE = re.compile(
    r"\b(bond|note|treasur(y|ies)|fixed income|municipal)\w*\b", re.IGNORECASE
)
_MONEY_MARKET_RE = re.compile(
    # "GOVT RESERVES FD" is a Fidelity MMF spelling that carries neither
    # "money market" nor "mmkt", and `\bgovt\b` in _BOND_RE would otherwise
    # file it as Fixed Income.
    r"\bmoney\s*(market|mkt)\b|\bmmkt\b|\b(govt|government)\s+reserves\b",
    re.IGNORECASE,
)
_CRYPTO_RE = re.compile(
    r"\bbitcoin\b|\bethereum\b|\bcrypto\w*\b|\bdigital assets?\b", re.IGNORECASE
)
# Morningstar's government-bond categories ("Short Government",
# "Intermediate Government", "Long Government") carry no word _BOND_RE
# matches. Applied to the structured category only — a bare "government" in a
# free-text description belongs to companies like GOVERNMENT PROPERTIES
# INCOME TRUST.
_BOND_CATEGORY_RE = re.compile(r"\bgovernment\b", re.IGNORECASE)
# Categories that are emphatically *not* plain equity. Writing one as
# `Stocks` would be a durable wrong row, and because a non-None lookup
# short-circuits the heuristic, the offline tier would never get a say.
_NOT_PLAIN_EQUITY_RE = re.compile(
    r"\btrading\b|\binverse\b|\bleveraged\b|\bbear\b|\bpreferred\b|"
    r"\bconvertible\b|\bderivative income\b|\boptions?\b|\bmiscellaneous\b",
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

# Word-anchored: an unanchored "gold"/"silver"/"platinum" substring matched
# GOLDMAN SACHS GROUP INC, SILVERGATE CAPITAL CORP and PLATINUM EQUITY
# HOLDINGS, each of which was then written as a durable Commodities row.
_COMMODITY_METALS = (
    (re.compile(r"\bgold\b", re.IGNORECASE), "Gold"),
    (re.compile(r"\bsilver\b", re.IGNORECASE), "Silver"),
    (re.compile(r"\bplatinum\b", re.IGNORECASE), "Platinum"),
    (re.compile(r"\bpalladium\b", re.IGNORECASE), "Palladium"),
)
_BROAD_BASKET_RE = re.compile(r"\bbroad basket\b", re.IGNORECASE)
_COMMODITY_RE = re.compile(r"\bcommodit(y|ies)\b", re.IGNORECASE)

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


def _bond_sub_class(text: str) -> str:
    if _TIPS_RE.search(text):
        return "TIPS"
    if _SHORT_TERM_RE.search(text):
        return "Short-Term"
    if _LONG_TERM_RE.search(text):
        return "Long-Term"
    if _INTERMEDIATE_RE.search(text):
        return "Intermediate"
    return ""


def _commodity_metal(text: str) -> str:
    for pattern, sub in _COMMODITY_METALS:
        if pattern.search(text):
            return sub
    return ""


def _equity_sub_class(text: str) -> str:
    if _TOTAL_MARKET_RE.search(text):
        return "Total Market"
    for pattern, sub in _EQUITY_SUB_RULES:
        if pattern.search(text):
            return sub
    return ""


def _classify_fund_text(text: str) -> tuple[str, str, str] | None:
    """Fund rules over a free-text export description.

    Ungated — :func:`classify_from_description` decides whether the text is
    trustworthy enough to act on. The lookup tier has its own composition
    (:func:`_classify_fund`), because a structured category and a fund's
    marketing name deserve different trust.
    """
    if not text.strip():
        return None
    if _MONEY_MARKET_RE.search(text):
        return (CASH_CLASS, "Money Market", "US")
    if _CRYPTO_RE.search(text):
        return ("Alternative", "Cryptocurrency", "Global")
    metal = _commodity_metal(text)
    if metal:
        return ("Commodities", metal, "Global")
    if _COMMODITY_RE.search(text):
        sub = "Broad Basket" if _BROAD_BASKET_RE.search(text) else ""
        return ("Commodities", sub, "Global")
    if _BOND_RE.search(text):
        return ("Fixed Income", _bond_sub_class(text), _fund_geography(text))
    if _REAL_ESTATE_RE.search(text):
        # The canonical vocabulary has Real Estate as an asset class with
        # sub-class REIT; emitting Stocks/Real Estate would put a
        # hand-classified REIT and an auto-classified one in different slices.
        return ("Real Estate", "REIT", _fund_geography(text))
    return ("Stocks", _equity_sub_class(text), _fund_geography(text))


def classify_from_lookup(info: dict | None) -> tuple[str, str, str] | None:
    """Map a yfinance-style info dict to (asset_class, sub_class, geography).

    Returns ``None`` when the metadata carries no usable signal.
    """
    if not info:
        return None
    quote_type = (info.get("quoteType") or "").upper()
    if quote_type == "MONEYMARKET":
        # Yahoo's enum carries this, though a Fidelity MMF reports
        # MUTUALFUND with a "Money Market-…" category (which _classify_fund
        # catches). Kept because it costs a line and covers an MMF whose
        # description says neither.
        return (CASH_CLASS, "Money Market", "US")
    if quote_type == "CRYPTOCURRENCY":
        return ("Alternative", "Cryptocurrency", "Global")
    if quote_type in ("ETF", "MUTUALFUND"):
        return _classify_fund(
            (info.get("category") or "").strip(),
            (info.get("longName") or info.get("shortName") or "").strip(),
        )
    if quote_type == "EQUITY":
        sector = (info.get("sector") or "").strip() or "Individual Stock"
        country = (info.get("country") or "").strip()
        geography = "US" if country in ("", "United States") else country
        return ("Stocks", sector, geography)
    return None


def _classify_fund(category: str, name: str) -> tuple[str, str, str] | None:
    """A fund's classification from its category *and* its name.

    The two carry different signals and deserve different trust, which is why
    this is not simply :func:`_classify_fund_text` over the pair:

    - **Which asset class it is** is the category's job alone for commodities.
      A fund name is marketing text that routinely carries a metal word —
      "Goldman Sachs ActiveBeta US Large Cap Equity ETF" is a large-cap equity
      fund, "Sprott Gold Miners" is equity too. The name only breaks the tie a
      "Commodities Focused" category can't (it doesn't say which commodity).
    - **Everything else reads both**, because the detail lives in the name:
      Morningstar says "Long Government" and the name says "20+ Year
      Treasury"; it says "Large Blend" and the name says "Total Stock Market".
      Reading the category alone dropped TLT/SHY/IEF out of the tier entirely
      and demoted VTI from Total Market to Large Cap.
    """
    combined = f"{category} {name}".strip()
    if not combined:
        return None
    # An MMF reports MUTUALFUND with a "Money Market-Taxable" category.
    if _MONEY_MARKET_RE.search(combined):
        return (CASH_CLASS, "Money Market", "US")
    if _CRYPTO_RE.search(combined):
        return ("Alternative", "Cryptocurrency", "Global")
    commodity_text = category or name
    metal = _commodity_metal(commodity_text)
    if metal or _COMMODITY_RE.search(commodity_text):
        sub = metal or _commodity_metal(name)
        if not sub and _BROAD_BASKET_RE.search(combined):
            sub = "Broad Basket"
        return ("Commodities", sub, "Global")
    if _BOND_RE.search(combined) or (category and _BOND_CATEGORY_RE.search(category)):
        return ("Fixed Income", _bond_sub_class(combined), _fund_geography(combined))
    if _REAL_ESTATE_RE.search(combined):
        return ("Real Estate", "REIT", _fund_geography(combined))
    if category and _NOT_PLAIN_EQUITY_RE.search(category):
        # "Trading--Inverse Equity" and "Preferred Stock" are not plain
        # equity, and writing them as Stocks short-circuits the heuristic
        # tier that would otherwise get a say. Fall through instead. An
        # equity category we merely don't have a sub-class for ("Technology",
        # "Europe Stock") is still equity — dropping those cost the tier
        # every sector, region, allocation and target-date fund.
        return None
    return ("Stocks", _equity_sub_class(combined), _fund_geography(combined))


def classify_from_description(
    symbol_norm: str, description: str, security_type: str
) -> tuple[str, str, str] | None:
    """Offline classification from the export row itself.

    Conservative by design: funds, bonds and commodities announce themselves
    in a Fidelity description; a bare company name does not, and guessing
    from one would silently misfile it. Each branch therefore carries a gate
    naming what a company description cannot have — a fund marker for the
    fund-shaped classes, and for a direct bond that, a coupon and maturity,
    or a bond-shaped ``security_type``. Without them BARRICK GOLD CORP is a
    Commodities/Gold row and TREASURE GLOBAL INC is Fixed Income.

    The coupon/maturity branch is the one that fires in production: neither
    shipped importer populates ``security_type`` with a security type
    (Fidelity's Type column is the account registration — Cash/Margin), so
    the third alternative is forward compatibility for an importer that does.
    """
    desc = (description or "").strip()
    if not desc:
        return None
    result = _classify_fund_text(desc)
    if result is None:
        return None
    asset_class, sub, geography = result
    if asset_class == CASH_CLASS:
        # No company is named "money market"; nothing to gate against.
        return (asset_class, sub, geography)
    is_fund = bool(_FUND_MARKER_RE.search(desc))
    if asset_class == "Fixed Income":
        if not (
            is_fund
            or _BOND_STRUCTURE_RE.search(desc)
            or _BOND_SECURITY_TYPE_RE.search(security_type or "")
        ):
            return None
    elif not is_fund:
        return None
    return (asset_class, sub, geography)


def lookup_backend_available() -> bool:
    """Is the ticker-metadata backend installed at all?

    yfinance is an optional dependency, so a lean install has the primary
    tier permanently dead. That used to be indistinguishable from "nothing
    needed looking up": the import failure returned ``None`` from a bare
    ``except ImportError`` and every fetch failure logged below the default
    level, so a total outage was silent while the docs advertised lookup.

    Broad ``except`` because this is purely a reporting signal — a module
    without a ``__spec__`` (a lazy-import shim, a test double) makes
    ``find_spec`` raise ``ValueError``, and no caller wants classification to
    fail over the answer to "is it installed".
    """
    try:
        return importlib.util.find_spec("yfinance") is not None
    except Exception:
        return False


def _call_yfinance(symbol: str) -> dict | None:
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


def fetch_symbol_info(
    symbol: str, *, timeout: float = LOOKUP_TIMEOUT_SECONDS,
) -> dict | None:
    """One ticker metadata lookup; ``None`` on any failure (no yfinance,
    no network, unknown symbol, or slower than ``timeout``).

    The timeout is enforced here rather than passed down because yfinance
    exposes none: ``Ticker.get_info()`` takes no argument and its internal
    default is 30 seconds per HTTP call, several calls deep — so a count cap
    alone bounds a run at tens of minutes, well past the reverse proxy's own
    read timeout. A daemon worker thread lets us stop waiting; the abandoned
    call finishes on its own and its result is discarded.
    """
    if timeout <= 0:
        return _call_yfinance(symbol)
    box: list[dict | None] = []

    def run() -> None:
        box.append(_call_yfinance(symbol))

    worker = threading.Thread(
        target=run, name=f"autoclass-lookup-{symbol}", daemon=True,
    )
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        logger.debug("autoclass lookup for %s exceeded %.1fs", symbol, timeout)
        return None
    return box[0] if box else None


def auto_classify_symbols(
    conn: sqlite3.Connection,
    candidates: list[tuple[str, str, str]],
    *,
    fetch=None,
    max_lookups: int = MAX_LOOKUPS_PER_RUN,
    budget_seconds: float = LOOKUP_BUDGET_SECONDS,
    allow_lookups: bool = True,
) -> dict:
    """Classify every candidate symbol that has no classification row.

    ``candidates`` is ``(symbol, description, security_type)`` per position
    row (raw symbol fine — normalized here, deduped by the normalized key).
    Writes ``source='auto'`` rows through
    :func:`portfolio.insert_classification_if_absent`, so an existing row of
    any source is never touched — including a deliberate ``Unclassified``,
    and including one written while this run's lookup was in flight.

    Network work is bounded twice over: ``max_lookups`` caps the count and
    ``budget_seconds`` the wall clock (a count alone bounds neither, since a
    lookup's duration is the remote host's decision). Past either bound the
    offline heuristic still runs and the next run picks up what remains.

    Returns ``{"classified": [...], "unresolved": [...], "lookups_attempted":
    int, "lookups_failed": int, "lookups_available": bool}``; each classified
    entry carries the written triple plus its ``method`` (``lookup`` |
    ``heuristic``).
    """
    using_default_fetch = fetch is None
    if using_default_fetch:
        fetch = fetch_symbol_info
    backend_ok = True
    if allow_lookups and using_default_fetch:
        backend_ok = lookup_backend_available()
        if not backend_ok:
            logger.warning(
                "portfolio autoclass: yfinance is not installed — ticker "
                "lookup is unavailable, falling back to description "
                "heuristics only"
            )
    lookups_on = allow_lookups and backend_ok

    existing = portfolio._classification_map(conn)
    deadline = time.monotonic() + budget_seconds if budget_seconds > 0 else None
    seen: set[str] = set()
    classified: list[dict] = []
    unresolved: list[str] = []
    attempted = 0
    failed = 0
    for symbol, description, security_type in candidates:
        norm = normalize_symbol(symbol)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        try:
            if norm in existing:
                # Row existence, not resolved value: "Unclassified" is an
                # offered value everywhere, so a user can hold that opinion.
                continue
            resolved = portfolio._resolve(
                existing, norm, description, security_type, "position",
            )
            if resolved[0] != UNCLASSIFIED:
                continue  # cash / options — the fallback chain has it
            info = None
            if (
                lookups_on
                and attempted < max_lookups
                and (deadline is None or time.monotonic() < deadline)
            ):
                attempted += 1
                try:
                    info = fetch(norm)
                except Exception:
                    logger.debug(
                        "autoclass fetch raised for %s", norm, exc_info=True
                    )
                if info is None:
                    failed += 1
            result = classify_from_lookup(info)
            method = "lookup"
            if result is None:
                result = classify_from_description(norm, description, security_type)
                method = "heuristic"
            if result is None:
                unresolved.append(norm)
                continue
            asset_class, sub_class, geography = result
            written = portfolio.insert_classification_if_absent(
                conn, norm,
                asset_class=asset_class, sub_class=sub_class,
                geography=geography, source="auto",
            )
            if not written:
                # Someone classified it while we were on the network. Their
                # row stands, and this run doesn't claim what it didn't write.
                unresolved.append(norm)
                continue
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
    if attempted >= _OUTAGE_WARNING_MIN_ATTEMPTS and failed == attempted:
        # A count, not a verdict: "returned no metadata" is also the honest
        # answer for a delisted or private holding, so several in a row is a
        # hint for the operator's log rather than a fact for the UI.
        logger.warning(
            "portfolio autoclass: all %d ticker lookups came back empty — "
            "a lookup outage would look like this", attempted,
        )
    return {
        "classified": classified,
        "unresolved": sorted(unresolved),
        "lookups_attempted": attempted,
        "lookups_failed": failed,
        # Whether the tier was *usable*, not whether it found anything. One
        # unknown ticker is not an outage, and reporting it as one told the
        # user their lookup was unavailable and the agent the operator might
        # have switched it off — both false.
        "lookups_available": lookups_on,
    }


def _snapshot_candidates(parsed) -> list[tuple[str, str, str]]:
    return [
        (row.symbol, row.description, row.security_type)
        for row in parsed.rows
        if row.row_type == "position"
    ]


def auto_classify_snapshots(
    conn: sqlite3.Connection,
    snapshots,
    **kwargs,
) -> dict:
    """One classification pass over every snapshot an import produced.

    A fina history file parses into one snapshot per distinct export date, so
    classifying per snapshot spent the whole lookup budget again on each of
    them — and an unresolvable symbol writes no row, so it was re-fetched
    every time. The advertised one-time migration was the worst case: five
    years of monthly exports with twenty opaque tickers is ~1200 lookups for
    twenty distinct symbols. Union first; the dedup then does its job.
    """
    candidates: list[tuple[str, str, str]] = []
    for parsed in snapshots:
        candidates.extend(_snapshot_candidates(parsed))
    return auto_classify_symbols(conn, candidates, **kwargs)


def auto_classify_snapshot(conn: sqlite3.Connection, parsed, **kwargs) -> dict:
    """Auto-classify a single parsed snapshot's position rows."""
    return auto_classify_symbols(conn, _snapshot_candidates(parsed), **kwargs)


def summarize_auto_results(results: list[dict]) -> dict:
    """Deduped union of the per-snapshot classification keys.

    A multi-snapshot import answers with ``{status, imported, duplicates,
    results:[…]}``, so anything living only inside a result is invisible to a
    client reading the top level — which is exactly the fina migration, the
    case the one-pass classification was written for.
    """
    classified: dict[str, dict] = {}
    unresolved: set[str] = set()
    for result in results:
        if result.get("status") != "ok":
            continue
        for entry in result.get("auto_classified") or []:
            classified.setdefault(entry["symbol"], entry)
        unresolved.update(result.get("unclassified_symbols") or [])
    return {
        "auto_classified": [classified[s] for s in sorted(classified)],
        "unclassified_symbols": sorted(unresolved),
    }


def apply_auto_results(results: list[dict], auto: dict) -> None:
    """Distribute one shared classification pass back onto per-snapshot results.

    Each ok result keeps its own ``unclassified_symbols`` minus whatever the
    pass classified, and gains the ``auto_classified`` entries for symbols it
    actually held. Sorted, because ``insert_snapshot`` returns a sorted list
    and the same response field must not change order depending on whether
    classification ran.
    """
    classified_by_symbol = {c["symbol"]: c for c in auto.get("classified", [])}
    for result in results:
        if result.get("status") != "ok":
            continue
        original = result.get("unclassified_symbols") or []
        result["auto_classified"] = [
            classified_by_symbol[s] for s in sorted(original)
            if s in classified_by_symbol
        ]
        result["unclassified_symbols"] = sorted(
            s for s in original if s not in classified_by_symbol
        )


def candidates_from_positions(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Distinct position symbols across all snapshots, newest row each —
    the backfill path's candidate set.

    The description and security type come from one row, picked by the
    snapshot's ``exported_at`` rather than its insertion order (importing an
    older export after a newer one would otherwise hand the backfill a stale
    description) and tie-broken by row id, since one symbol can appear in
    several accounts within a snapshot. Independent ``MAX()`` aggregates —
    the earlier shape — can draw the two columns from different rows.
    """
    rows = conn.execute(
        "SELECT p.symbol_norm, p.description, p.security_type "
        "FROM portfolio_positions p "
        "WHERE p.id = ("
        "  SELECT q.id FROM portfolio_positions q "
        "  JOIN portfolio_snapshots s ON s.id = q.snapshot_id "
        "  WHERE q.symbol_norm = p.symbol_norm "
        "    AND q.row_type = 'position' AND q.symbol_norm != '' "
        "  ORDER BY s.exported_at DESC, q.id DESC LIMIT 1"
        ") "
        "AND p.row_type = 'position' AND p.symbol_norm != '' "
        "ORDER BY p.symbol_norm"
    ).fetchall()
    return [(r[0], r[1] or "", r[2] or "") for r in rows]
