"""Tests for automatic symbol classification (lookup + offline heuristics)."""

from __future__ import annotations

import sqlite3
import sys
import time
import types

import pytest

from istota.money import portfolio, portfolio_autoclass
from istota.money.portfolio_autoclass import (
    auto_classify_symbols,
    classify_from_description,
    classify_from_lookup,
)

# Captured before the package-wide autouse fixture swaps the module attribute
# out: TestFetchSymbolInfo is the one place that must exercise the real thing.
_real_fetch_symbol_info = portfolio_autoclass.fetch_symbol_info


def _row(symbol, description, row_type="position", security_type="", value=100.0):
    from istota.money.core.importers.positions_base import PositionRow

    return PositionRow(
        account_number="X1", account_name="A", symbol=symbol,
        description=description, row_type=row_type, quantity=1.0,
        price=value, value=value, cost_basis=None, avg_cost_basis=None,
        day_gain=None, day_gain_pct=None, total_gain=None,
        total_gain_pct=None, pct_of_account=None, security_type=security_type,
    )


def _snapshot(exported_at, rows):
    from datetime import datetime

    from istota.money.core.importers.positions_base import ParsedSnapshot

    return ParsedSnapshot(
        exported_at=datetime.fromisoformat(exported_at),
        exported_at_estimated=False, rows=rows,
        source="fidelity-positions-csv", warnings=[],
    )


@pytest.fixture
def conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "money.db"))
    conn.row_factory = sqlite3.Row
    portfolio.ensure_schema(conn)
    yield conn
    conn.close()


class TestClassifyFromLookup:
    def test_us_equity_uses_sector_and_country(self):
        cls = classify_from_lookup({
            "quoteType": "EQUITY",
            "sector": "Technology",
            "country": "United States",
        })
        assert cls == ("Stocks", "Technology", "US")

    def test_foreign_equity_keeps_country(self):
        cls = classify_from_lookup({
            "quoteType": "EQUITY",
            "sector": "Consumer Cyclical",
            "country": "Germany",
        })
        assert cls == ("Stocks", "Consumer Cyclical", "Germany")

    def test_equity_without_sector_is_individual_stock(self):
        cls = classify_from_lookup({"quoteType": "EQUITY"})
        assert cls == ("Stocks", "Individual Stock", "US")

    def test_bond_fund_category(self):
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Intermediate Core Bond",
        })
        assert cls[0] == "Fixed Income"
        assert cls[1] == "Intermediate"
        assert cls[2] == "US"

    def test_short_term_treasury_fund(self):
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Ultrashort Bond",
        })
        assert cls[:2] == ("Fixed Income", "Short-Term")

    def test_tips_fund(self):
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Inflation-Protected Bond",
        })
        assert cls[:2] == ("Fixed Income", "TIPS")

    def test_gold_fund(self):
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Commodities Focused",
            "longName": "iShares Gold Trust",
        })
        assert cls == ("Commodities", "Gold", "Global")

    def test_broad_commodity_fund(self):
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Commodities Broad Basket",
        })
        assert cls == ("Commodities", "Broad Basket", "Global")

    def test_emerging_markets_fund(self):
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Diversified Emerging Mkts",
        })
        assert cls == ("Stocks", "Emerging Markets", "International")

    def test_foreign_blend_maps_to_developed_markets(self):
        cls = classify_from_lookup({
            "quoteType": "MUTUALFUND",
            "category": "Foreign Large Blend",
        })
        assert cls == ("Stocks", "Developed Markets", "International")

    def test_category_sub_class_survives_appended_fund_name(self):
        # The real VXUS shape: category plus a long fund name; the exact
        # sub-class map must still key on the category alone.
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Foreign Large Blend",
            "longName": "Vanguard Total International Stock Index Fund ETF Shares",
        })
        assert cls == ("Stocks", "Developed Markets", "International")

    def test_us_large_blend_normalizes_sub_class(self):
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Large Blend",
        })
        assert cls == ("Stocks", "Large Cap", "US")

    def test_world_fund_is_global(self):
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Global Large-Stock Blend",
        })
        assert cls[0] == "Stocks"
        assert cls[2] == "Global"

    def test_money_market_fund(self):
        """yfinance has no MONEYMARKET quote type — an MMF reports
        MUTUALFUND with a "Money Market-…" category, and used to fall
        through to the plain-Stocks catch-all, landing cash in the equity
        slice."""
        cls = classify_from_lookup({
            "quoteType": "MUTUALFUND",
            "category": "Money Market-Taxable",
            "longName": "Fidelity Government Money Market Fund",
        })
        assert cls == ("Cash & Equivalents", "Money Market", "US")

    def test_money_market_by_abbreviation(self):
        cls = classify_from_lookup({
            "quoteType": "MUTUALFUND",
            "category": "",
            "longName": "FIDELITY GOVT MMKT RESERVES FD",
        })
        assert cls == ("Cash & Equivalents", "Money Market", "US")

    def test_commodity_named_fund_with_equity_category(self):
        """The fund *name* must not drive the commodity branch: the real
        GSLC shape is an equity category under a Goldman Sachs name."""
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Large Blend",
            "longName": "Goldman Sachs ActiveBeta US Large Cap Equity ETF",
        })
        assert cls == ("Stocks", "Large Cap", "US")

    def test_unrecognized_fund_category_returns_none(self):
        """An inverse-equity fund is not plain Stocks. With no sub-class to
        offer, fall through rather than write a durable wrong row that the
        heuristic tier never gets to disagree with."""
        assert classify_from_lookup({
            "quoteType": "ETF",
            "category": "Trading--Inverse Equity",
        }) is None
        assert classify_from_lookup({
            "quoteType": "MUTUALFUND",
            "category": "Preferred Stock",
        }) is None

    def test_reit_fund_uses_the_canonical_vocabulary(self):
        """Real Estate is an asset class with sub-class REIT — a
        hand-classified REIT and an auto-classified one must land in the
        same allocation slice."""
        cls = classify_from_lookup({"quoteType": "ETF", "category": "Real Estate"})
        assert cls[:2] == ("Real Estate", "REIT")

    @pytest.mark.parametrize(
        "category,name,expected",
        [
            # Maturity and breadth live in the *name*; the category names
            # only the shape. Reading the category alone dropped the
            # government funds out of the tier entirely and demoted VTI.
            ("Long Government", "iShares 20+ Year Treasury Bond ETF",
             ("Fixed Income", "Long-Term", "US")),
            ("Short Government", "iShares 1-3 Year Treasury Bond ETF",
             ("Fixed Income", "Short-Term", "US")),
            ("Intermediate Government", "iShares 7-10 Year Treasury Bond ETF",
             ("Fixed Income", "Intermediate", "US")),
            ("Corporate Bond", "Vanguard Intermediate-Term Corporate Bond ETF",
             ("Fixed Income", "Intermediate", "US")),
            ("Large Blend", "Vanguard Total Stock Market Index Fund ETF Shares",
             ("Stocks", "Total Market", "US")),
        ],
    )
    def test_the_fund_name_still_carries_detail_the_category_lacks(
        self, category, name, expected,
    ):
        cls = classify_from_lookup({
            "quoteType": "ETF", "category": category, "longName": name,
        })
        assert cls == expected

    @pytest.mark.parametrize(
        "category,name",
        [
            ("Technology", "Technology Select Sector SPDR Fund"),
            ("Equity Energy", "Energy Select Sector SPDR Fund"),
            ("Europe Stock", "Vanguard FTSE Europe ETF"),
            ("Moderate Allocation", "Vanguard Balanced Index Fund"),
            ("Target-Date 2050", "Fidelity Freedom 2050 Fund"),
        ],
    )
    def test_an_equity_category_without_a_sub_class_is_still_equity(
        self, category, name,
    ):
        """Falling through for *any* unrecognized equity category cost the
        tier every sector, region, allocation and target-date fund — a much
        larger set than the two the fall-through exists for."""
        cls = classify_from_lookup({
            "quoteType": "ETF", "category": category, "longName": name,
        })
        assert cls is not None
        assert cls[0] == "Stocks"

    def test_crypto_fund(self):
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Digital Assets",
            "longName": "iShares Bitcoin Trust",
        })
        assert cls == ("Alternative", "Cryptocurrency", "Global")

    def test_money_market_quote_type(self):
        """Yahoo's enum carries MONEYMARKET even though a Fidelity MMF
        reports MUTUALFUND with a money-market category."""
        cls = classify_from_lookup({"quoteType": "MONEYMARKET"})
        assert cls == ("Cash & Equivalents", "Money Market", "US")

    def test_gold_miner_name_does_not_make_an_equity_fund_a_commodity(self):
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Equity Precious Metals",
            "longName": "Sprott Gold Miners ETF",
        })
        assert cls is not None
        assert cls[0] == "Stocks"

    def test_crypto(self):
        cls = classify_from_lookup({"quoteType": "CRYPTOCURRENCY"})
        assert cls == ("Alternative", "Cryptocurrency", "Global")

    def test_unknown_quote_type_returns_none(self):
        assert classify_from_lookup({"quoteType": "INDEX"}) is None
        assert classify_from_lookup({}) is None
        assert classify_from_lookup(None) is None

    def test_etf_without_category_uses_name(self):
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "longName": "iShares 20+ Year Treasury Bond ETF",
        })
        assert cls[0] == "Fixed Income"
        assert cls[1] == "Long-Term"


class TestClassifyFromDescription:
    def test_treasury_etf(self):
        cls = classify_from_description("SGOV", "ISHARES 0-3 MONTH TREASURY BOND ETF", "")
        assert cls[0] == "Fixed Income"

    def test_fidelity_abbreviated_treasury(self):
        cls = classify_from_description("TLT", "ISHARES 20 PLUS YR TREASURY BD ETF", "")
        assert cls[:2] == ("Fixed Income", "Long-Term")

    def test_direct_treasury_note(self):
        cls = classify_from_description(
            "91282CJK8", "UNITED STATES TREAS NTS 4.875% 11/30/2025", ""
        )
        assert cls[0] == "Fixed Income"

    def test_gold_trust(self):
        cls = classify_from_description("IAU", "ISHARES GOLD TRUST", "")
        assert cls == ("Commodities", "Gold", "Global")

    def test_intl_fund(self):
        cls = classify_from_description("VXUS", "VANGUARD TOTAL INTL STOCK ETF", "")
        assert cls[0] == "Stocks"
        assert cls[2] == "International"

    def test_total_market_fund(self):
        cls = classify_from_description("VTI", "VANGUARD TOTAL STOCK MARKET ETF", "")
        assert cls == ("Stocks", "Total Market", "US")

    def test_emerging_markets_fund(self):
        cls = classify_from_description("VWO", "VANGUARD EMERGING MARKETS STOCK ETF", "")
        assert cls[:2] == ("Stocks", "Emerging Markets")

    def test_reit_fund_uses_the_canonical_vocabulary(self):
        cls = classify_from_description("VNQ", "VANGUARD REAL ESTATE ETF", "")
        assert cls[:2] == ("Real Estate", "REIT")

    def test_plain_company_name_returns_none(self):
        """A bare equity description carries no signal — leave it to the
        online lookup rather than guess."""
        assert classify_from_description("ZZZQ", "ACME ROCKETRY CO", "") is None

    def test_empty_description_returns_none(self):
        assert classify_from_description("ZZZQ", "", "") is None

    @pytest.mark.parametrize(
        "symbol,description",
        [
            # The commodity needles were substring matches with no word
            # boundary and no fund-marker gate, so every one of these was
            # written as a durable Commodities row.
            ("GS", "GOLDMAN SACHS GROUP INC"),
            ("SI", "SILVERGATE CAPITAL CORP CL A"),
            ("PEH", "PLATINUM EQUITY HOLDINGS"),
            ("GOLD", "BARRICK GOLD CORP"),
            # "treasur\\w*" matched TREASURE.
            ("TGL", "TREASURE GLOBAL INC"),
            # A bare company name that happens to carry a bond-ish word.
            ("NOTE", "FISCALNOTE HOLDINGS INC"),
        ],
    )
    def test_company_names_are_never_classified(self, symbol, description):
        assert classify_from_description(symbol, description, "") is None

    def test_commodity_fund_still_classifies(self):
        """The gate is the fund marker, not the commodity word — a real
        commodity fund announces itself as a fund."""
        assert classify_from_description("GLD", "SPDR GOLD SHARES", "") == (
            "Commodities", "Gold", "Global",
        )
        assert classify_from_description(
            "SIVR", "ABRDN PHYSICAL SILVER SHARES ETF", "",
        )[:2] == ("Commodities", "Silver")

    def test_direct_bond_classifies_on_its_coupon_and_maturity(self):
        """A direct Treasury carries no fund marker; the coupon rate and
        maturity date are what a company name cannot have. This is the
        branch that fires in production — neither shipped importer puts a
        security type in `security_type` (Fidelity's Type column is
        Cash/Margin), so the security-type gate below is forward
        compatibility, not the live path."""
        cls = classify_from_description(
            "91282CJK8", "UNITED STATES TREAS NTS 4.875% 11/30/2025", "",
        )
        assert cls[0] == "Fixed Income"

    def test_security_type_alone_admits_a_direct_bond(self):
        """Isolated: no fund marker, no coupon, no maturity date — only the
        security type says bond. Passing a description that also carries a
        coupon would short-circuit before this gate is consulted."""
        assert classify_from_description("X", "UNITED STATES TREAS NTS", "") is None
        cls = classify_from_description("X", "UNITED STATES TREAS NTS", "Bond")
        assert cls[0] == "Fixed Income"

    def test_crypto_fund(self):
        cls = classify_from_description("IBIT", "ISHARES BITCOIN TRUST ETF", "")
        assert cls == ("Alternative", "Cryptocurrency", "Global")

    def test_govt_reserves_fund_is_cash_not_a_bond(self):
        """`\\bgovt\\b` is a bond needle, so a Fidelity MMF spelled
        "GOVT RESERVES FD" landed in Fixed Income."""
        cls = classify_from_description("X", "FIDELITY GOVT RESERVES FD", "")
        assert cls == ("Cash & Equivalents", "Money Market", "US")


class TestAutoClassifySymbols:
    def test_lookup_writes_auto_row(self, conn):
        def fetch(symbol):
            assert symbol == "ZZZQ"
            return {"quoteType": "EQUITY", "sector": "Technology",
                    "country": "United States"}

        result = auto_classify_symbols(
            conn, [("ZZZQ", "ACME ROCKETRY CO", "")], fetch=fetch
        )
        assert result["classified"] == [{
            "symbol": "ZZZQ",
            "asset_class": "Stocks",
            "sub_class": "Technology",
            "geography": "US",
            "method": "lookup",
        }]
        assert result["unresolved"] == []
        by_symbol = {c.symbol_norm: c for c in portfolio.list_classifications(conn)}
        assert by_symbol["ZZZQ"].source == "auto"
        assert by_symbol["ZZZQ"].asset_class == "Stocks"

    def test_falls_back_to_description_heuristic(self, conn):
        result = auto_classify_symbols(
            conn,
            [("ZZZB", "ACME 20 PLUS YR TREASURY BD ETF", "")],
            fetch=lambda s: None,
        )
        assert result["classified"][0]["method"] == "heuristic"
        assert result["classified"][0]["asset_class"] == "Fixed Income"

    def test_fetch_exception_falls_back(self, conn):
        def fetch(symbol):
            raise RuntimeError("network down")

        result = auto_classify_symbols(
            conn, [("ZZZB", "ACME GOLD TRUST", "")], fetch=fetch
        )
        assert result["classified"][0]["method"] == "heuristic"

    def test_unresolvable_symbol_reported(self, conn):
        result = auto_classify_symbols(
            conn, [("ZZZQ", "ACME ROCKETRY CO", "")], fetch=lambda s: None
        )
        assert result["classified"] == []
        assert result["unresolved"] == ["ZZZQ"]
        assert "ZZZQ" not in {
            c.symbol_norm for c in portfolio.list_classifications(conn)
        }

    def test_never_overwrites_existing_row(self, conn):
        portfolio.set_classification(
            conn, "ZZZQ", asset_class="Alternative", sub_class="SPAC"
        )
        result = auto_classify_symbols(
            conn,
            [("ZZZQ", "ACME ROCKETRY CO", "")],
            fetch=lambda s: {"quoteType": "EQUITY", "sector": "Technology"},
        )
        assert result["classified"] == []
        by_symbol = {c.symbol_norm: c for c in portfolio.list_classifications(conn)}
        assert by_symbol["ZZZQ"].asset_class == "Alternative"
        assert by_symbol["ZZZQ"].source == "user"

    def test_explicit_unclassified_row_survives(self, conn):
        """"Unclassified" is an offered value on every surface — a user who
        picked it deliberately is stating a classification, not asking for a
        guess. Gating on the *resolved value* rather than row existence let
        the auto tier overwrite exactly that row."""
        portfolio.set_classification(conn, "ZZZQ", asset_class="Unclassified")
        calls = []

        def fetch(symbol):
            calls.append(symbol)
            return {"quoteType": "EQUITY", "sector": "Technology"}

        result = auto_classify_symbols(
            conn, [("ZZZQ", "ACME ROCKETRY CO", "")], fetch=fetch
        )
        assert result["classified"] == []
        assert calls == []  # an existing row needs no lookup either
        by_symbol = {c.symbol_norm: c for c in portfolio.list_classifications(conn)}
        assert by_symbol["ZZZQ"].asset_class == "Unclassified"
        assert by_symbol["ZZZQ"].source == "user"

    def test_row_written_during_the_lookup_window_survives(self, conn):
        """The network fetch sits between the read and the write, so a user
        edit landing in that window must not be clobbered — the write is
        INSERT OR IGNORE, not a read-then-replace."""
        def fetch(symbol):
            # Stand in for the user saving on /money/settings mid-backfill.
            portfolio.set_classification(
                conn, symbol, asset_class="Alternative", sub_class="SPAC"
            )
            return {"quoteType": "EQUITY", "sector": "Technology"}

        result = auto_classify_symbols(
            conn, [("ZZZQ", "ACME ROCKETRY CO", "")], fetch=fetch
        )
        by_symbol = {c.symbol_norm: c for c in portfolio.list_classifications(conn)}
        assert by_symbol["ZZZQ"].asset_class == "Alternative"
        assert by_symbol["ZZZQ"].source == "user"
        # and the run doesn't claim a classification it didn't write
        assert result["classified"] == []
        assert result["unresolved"] == ["ZZZQ"]

    def test_skips_rows_the_fallback_chain_already_resolves(self, conn):
        """Cash and options rows resolve without an explicit row — no lookup."""
        calls = []

        def fetch(symbol):
            calls.append(symbol)
            return None

        result = auto_classify_symbols(
            conn,
            [("FDRXX", "GOVT MONEY MARKET", ""),
             ("MU 18JUL25 120 C", "MU 18JUL25 120 C", "Option")],
            fetch=fetch,
        )
        assert calls == []
        assert result["classified"] == []
        assert result["unresolved"] == []

    def test_dedupes_candidates(self, conn):
        calls = []

        def fetch(symbol):
            calls.append(symbol)
            return {"quoteType": "EQUITY", "sector": "Energy",
                    "country": "United States"}

        result = auto_classify_symbols(
            conn,
            [("ZZZQ", "ACME CO", ""), ("zzzq**", "ACME CO", "")],
            fetch=fetch,
        )
        assert calls == ["ZZZQ"]
        assert len(result["classified"]) == 1

    def test_lookup_cap_still_runs_heuristics(self, conn):
        calls = []

        def fetch(symbol):
            calls.append(symbol)
            return None

        candidates = [
            (f"ZZZ{i}", f"ACME {i} GOLD TRUST", "") for i in range(5)
        ]
        result = auto_classify_symbols(
            conn, candidates, fetch=fetch, max_lookups=2
        )
        assert len(calls) == 2
        # every symbol still classified via the offline heuristic
        assert len(result["classified"]) == 5

    def test_default_fetch_used_when_none(self, conn, monkeypatch):
        monkeypatch.setattr(
            portfolio_autoclass, "fetch_symbol_info",
            lambda s: {"quoteType": "EQUITY", "sector": "Utilities",
                       "country": "United States"},
        )
        result = auto_classify_symbols(conn, [("ZZZQ", "ACME CO", "")])
        assert result["classified"][0]["sub_class"] == "Utilities"


class TestNetworkBounds:
    def test_wall_clock_budget_stops_issuing_lookups(self, conn):
        """The count cap bounds how many lookups a run makes, not how long
        they take — and a lookup's duration is the remote host's decision."""
        calls = []

        def fetch(symbol):
            calls.append(symbol)
            time.sleep(0.02)
            return None

        candidates = [(f"ZZZ{i}", "ACME ROCKETRY CO", "") for i in range(40)]
        auto_classify_symbols(
            conn, candidates, fetch=fetch, budget_seconds=0.05,
        )
        assert 0 < len(calls) < 40

    def test_lookups_disabled_skips_the_network_entirely(self, conn):
        calls = []

        def fetch(symbol):
            calls.append(symbol)
            return {"quoteType": "EQUITY", "sector": "Technology"}

        result = auto_classify_symbols(
            conn,
            [("ZZZB", "ACME GOLD TRUST", "")],
            fetch=fetch,
            allow_lookups=False,
        )
        assert calls == []
        assert result["lookups_available"] is False
        # the offline tier still classifies what it can
        assert result["classified"][0]["method"] == "heuristic"

    def test_one_unknown_ticker_is_not_an_outage(self, conn):
        """"Returned no metadata" is the honest answer for a delisted or
        private holding. Reporting it as an unavailable tier told the user
        their lookup was down and the agent the operator might have switched
        it off — both false."""
        result = auto_classify_symbols(
            conn, [("ZZZQ", "ACME ROCKETRY CO", "")], fetch=lambda s: None,
        )
        assert result["lookups_attempted"] == 1
        assert result["lookups_failed"] == 1
        assert result["lookups_available"] is True

    def test_missing_backend_reports_unavailable(self, conn, monkeypatch):
        monkeypatch.setattr(
            portfolio_autoclass, "lookup_backend_available", lambda: False
        )
        calls = []
        monkeypatch.setattr(
            portfolio_autoclass, "fetch_symbol_info",
            lambda s, **kw: calls.append(s) or None,
        )
        result = auto_classify_symbols(conn, [("ZZZQ", "ACME ROCKETRY CO", "")])
        assert calls == []
        assert result["lookups_available"] is False

    def test_reports_a_working_lookup_tier(self, conn):
        result = auto_classify_symbols(
            conn,
            [("ZZZQ", "ACME CO", "")],
            fetch=lambda s: {"quoteType": "EQUITY", "sector": "Energy"},
        )
        assert result["lookups_available"] is True
        assert result["lookups_failed"] == 0

    def test_unresolved_is_sorted(self, conn):
        result = auto_classify_symbols(
            conn,
            [("ZZZC", "ACME CO", ""), ("ZZZA", "BETA CO", ""), ("ZZZB", "GAMMA CO", "")],
            fetch=lambda s: None,
        )
        assert result["unresolved"] == ["ZZZA", "ZZZB", "ZZZC"]


class TestFetchSymbolInfo:
    """The one function that touches yfinance. The package-wide autouse
    fixture replaces it and every other test injects its own fetch, so
    without these a yfinance API rename would be swallowed by the broad
    ``except`` and surface only as "everything is heuristic"."""

    def _install(self, monkeypatch, module):
        monkeypatch.setitem(sys.modules, "yfinance", module)

    def test_returns_the_info_dict(self, monkeypatch):
        info = {"quoteType": "EQUITY", "sector": "Technology"}

        class Ticker:
            def __init__(self, symbol):
                self.symbol = symbol

            def get_info(self):
                return info

        self._install(monkeypatch, types.SimpleNamespace(Ticker=Ticker))
        assert _real_fetch_symbol_info("ZZZQ") == info

    def test_missing_yfinance_returns_none(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yfinance", None)
        # `import yfinance` against a None entry raises ImportError.
        assert _real_fetch_symbol_info("ZZZQ") is None

    def test_raising_get_info_returns_none(self, monkeypatch):
        class Ticker:
            def __init__(self, symbol):
                pass

            def get_info(self):
                raise RuntimeError("network down")

        self._install(monkeypatch, types.SimpleNamespace(Ticker=Ticker))
        assert _real_fetch_symbol_info("ZZZQ") is None

    def test_non_dict_info_returns_none(self, monkeypatch):
        class Ticker:
            def __init__(self, symbol):
                pass

            def get_info(self):
                return "not a dict"

        self._install(monkeypatch, types.SimpleNamespace(Ticker=Ticker))
        assert _real_fetch_symbol_info("ZZZQ") is None

    def test_slow_lookup_times_out(self, monkeypatch):
        """yfinance exposes no timeout of its own — get_info() takes no
        argument and defaults to 30s per HTTP call, several calls deep."""
        class Ticker:
            def __init__(self, symbol):
                pass

            def get_info(self):
                time.sleep(5)
                return {"quoteType": "EQUITY"}

        self._install(monkeypatch, types.SimpleNamespace(Ticker=Ticker))
        started = time.monotonic()
        assert _real_fetch_symbol_info("ZZZQ", timeout=0.05) is None
        assert time.monotonic() - started < 2


class TestCandidatesFromPositions:
    def test_distinct_position_symbols(self, conn):
        parsed = _snapshot(
            "2026-01-15",
            [_row("ZZZQ", "ACME CO"), _row("ZZZQ", "ACME CO"),
             _row("VTI", "VANGUARD TOTAL")],
        )
        portfolio.insert_snapshot(conn, parsed)
        candidates = portfolio_autoclass.candidates_from_positions(conn)
        symbols = [c[0] for c in candidates]
        assert symbols.count("ZZZQ") == 1
        assert "VTI" in symbols

    def test_description_and_type_come_from_one_row(self, conn):
        """Independent MAX() aggregates draw the two columns from whichever
        row is lexicographically largest in each — possibly different rows."""
        portfolio.insert_snapshot(conn, _snapshot(
            "2026-01-15",
            [_row("ZZZQ", "AAA OLD DESCRIPTION", security_type="Zeta", value=100.0)],
        ))
        portfolio.insert_snapshot(conn, _snapshot(
            "2026-02-15",
            [_row("ZZZQ", "BBB CURRENT DESCRIPTION", security_type="Alpha", value=200.0)],
        ))
        candidates = dict(
            (c[0], (c[1], c[2]))
            for c in portfolio_autoclass.candidates_from_positions(conn)
        )
        assert candidates["ZZZQ"] == ("BBB CURRENT DESCRIPTION", "Alpha")


class TestAutoClassifySnapshot:
    def test_classifies_position_rows_only(self, conn):
        parsed = _snapshot(
            "2026-01-15",
            [_row("ZZZB", "ACME GOLD TRUST"),
             _row("USD***", "US DOLLARS", row_type="cash")],
        )
        result = portfolio_autoclass.auto_classify_snapshot(
            conn, parsed, fetch=lambda s: None
        )
        assert [c["symbol"] for c in result["classified"]] == ["ZZZB"]


class TestAutoClassifySnapshots:
    def test_one_budget_across_every_snapshot(self, conn):
        """A fina history file parses into one snapshot per export date, and
        an unresolvable symbol writes no row — so classifying per snapshot
        re-fetched the same symbols on every one of them, with a fresh cap
        each time. The advertised one-time migration was the worst case."""
        calls = []

        def fetch(symbol):
            calls.append(symbol)
            return None

        snapshots = [
            _snapshot(
                f"2026-0{month}-15",
                [_row("ZZZQ", "ACME ROCKETRY CO"), _row("ZZZR", "BETA WORKS CO")],
            )
            for month in range(1, 7)
        ]
        result = portfolio_autoclass.auto_classify_snapshots(
            conn, snapshots, fetch=fetch,
        )
        assert calls == ["ZZZQ", "ZZZR"]
        assert result["unresolved"] == ["ZZZQ", "ZZZR"]

    def test_results_are_distributed_back_per_snapshot(self, conn):
        results = [
            {"status": "ok", "unclassified_symbols": ["ZZZB", "ZZZQ"]},
            {"status": "ok", "unclassified_symbols": ["ZZZQ"]},
            {"status": "duplicate"},
        ]
        auto = {
            "classified": [{
                "symbol": "ZZZB", "asset_class": "Commodities",
                "sub_class": "Gold", "geography": "Global", "method": "heuristic",
            }],
            "unresolved": ["ZZZQ"],
        }
        portfolio_autoclass.apply_auto_results(results, auto)
        assert [c["symbol"] for c in results[0]["auto_classified"]] == ["ZZZB"]
        assert results[0]["unclassified_symbols"] == ["ZZZQ"]
        # A snapshot that never held the symbol doesn't claim it.
        assert results[1]["auto_classified"] == []
        assert results[1]["unclassified_symbols"] == ["ZZZQ"]
        assert "auto_classified" not in results[2]

    def test_unclassified_symbols_stay_sorted(self, conn):
        """insert_snapshot returns a sorted list; the same response field
        must not change order depending on whether classification ran."""
        results = [{"status": "ok", "unclassified_symbols": ["ZZZC", "ZZZA", "ZZZB"]}]
        portfolio_autoclass.apply_auto_results(results, {"classified": []})
        assert results[0]["unclassified_symbols"] == ["ZZZA", "ZZZB", "ZZZC"]
