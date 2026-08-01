"""Tests for automatic symbol classification (lookup + offline heuristics)."""

from __future__ import annotations

import sqlite3

import pytest

from istota.money import portfolio, portfolio_autoclass
from istota.money.portfolio_autoclass import (
    auto_classify_symbols,
    classify_from_description,
    classify_from_lookup,
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

    def test_real_estate_fund(self):
        cls = classify_from_lookup({
            "quoteType": "ETF",
            "category": "Real Estate",
        })
        assert cls[:2] == ("Stocks", "Real Estate")

    def test_money_market(self):
        cls = classify_from_lookup({"quoteType": "MONEYMARKET"})
        assert cls == ("Cash & Equivalents", "Money Market", "US")

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

    def test_plain_company_name_returns_none(self):
        """A bare equity description carries no signal — leave it to the
        online lookup rather than guess."""
        assert classify_from_description("ZZZQ", "ACME ROCKETRY CO", "") is None

    def test_empty_description_returns_none(self):
        assert classify_from_description("ZZZQ", "", "") is None


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


class TestCandidatesFromPositions:
    def test_distinct_position_symbols(self, conn):
        from datetime import datetime

        from istota.money.core.importers.positions_base import (
            ParsedSnapshot, PositionRow,
        )

        def row(symbol, description, value=100.0):
            return PositionRow(
                account_number="X1", account_name="A", symbol=symbol,
                description=description, row_type="position", quantity=1.0,
                price=value, value=value, cost_basis=None, avg_cost_basis=None,
                day_gain=None, day_gain_pct=None, total_gain=None,
                total_gain_pct=None, pct_of_account=None, security_type="",
            )

        parsed = ParsedSnapshot(
            exported_at=datetime(2026, 1, 15), exported_at_estimated=False,
            rows=[row("ZZZQ", "ACME CO"), row("ZZZQ", "ACME CO"),
                  row("VTI", "VANGUARD TOTAL")],
            source="fidelity-positions-csv", warnings=[],
        )
        portfolio.insert_snapshot(conn, parsed)
        candidates = portfolio_autoclass.candidates_from_positions(conn)
        symbols = [c[0] for c in candidates]
        assert symbols.count("ZZZQ") == 1
        assert "VTI" in symbols


class TestAutoClassifySnapshot:
    def test_classifies_position_rows_only(self, conn):
        from datetime import datetime

        from istota.money.core.importers.positions_base import (
            ParsedSnapshot, PositionRow,
        )

        def row(symbol, description, row_type="position"):
            return PositionRow(
                account_number="X1", account_name="A", symbol=symbol,
                description=description, row_type=row_type, quantity=1.0,
                price=100.0, value=100.0, cost_basis=None, avg_cost_basis=None,
                day_gain=None, day_gain_pct=None, total_gain=None,
                total_gain_pct=None, pct_of_account=None, security_type="",
            )

        parsed = ParsedSnapshot(
            exported_at=datetime(2026, 1, 15), exported_at_estimated=False,
            rows=[row("ZZZB", "ACME GOLD TRUST"),
                  row("USD***", "US DOLLARS", row_type="cash")],
            source="fidelity-positions-csv", warnings=[],
        )
        result = portfolio_autoclass.auto_classify_snapshot(
            conn, parsed, fetch=lambda s: None
        )
        assert [c["symbol"] for c in result["classified"]] == ["ZZZB"]
