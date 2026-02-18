"""Tests for skills/finviz.py — FinViz market data scraping."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.markets.finviz import (
    FinVizData,
    Headline,
    MajorMover,
    FuturesQuote,
    ForexBondQuote,
    EarningsEntry,
    EconomicRelease,
    parse_finviz_page,
    format_finviz_briefing,
    fetch_finviz_data,
)


# --- Sample FinViz page text (simulates browse skill output) ---

SAMPLE_PAGE_TEXT = """\
Home
News
Screener
Maps
Tue FEB 10 2026 4:15 PM ET
Theme
Help
Login
Register
Today, 4:00 PMDow closes at record as rate-cut hopes lift stocks
More
Advancing
62.4% (3471)
Declining
(1911) 34.4%
New High
74.8% (294)
New Low
(99) 25.2%
Above
54.9% (3044)
SMA50
Below
(2504) 45.1%
Above
54.7% (3037)
SMA200
Below
(2511) 45.3%
BULL
BEAR
Ticker\tLast\tChange\tVolume
Signal
Daily
QNCX\t0.32\t146.69%\t816.09M\t\tTop Gainers
JZXN\t2.46\t81.25%\t60.45M\t\tTop Gainers
NKTR\t53.00\t42.97%\t5.92M\t\tTop Gainers
PHIO\t1.32\t46.11%\t141.14M\t\tTop Gainers
ICHR\t46.28\t35.67%\t3.60M\t\tNew High
UCTT\t57.98\t14.18%\t1.43M\t\tNew High
PKST\t20.77\t0.07%\t110.12K\t\tOverbought
BALL\t67.32\t1.01%\t551.81K\t\tOverbought
Ticker\tLast\tChange\tVolume
Signal
Daily
UOKA\t0.76\t-66.87%\t18.09M\t\tTop Losers
AZI\t0.74\t-57.22%\t43.46M\t\tTop Losers
BRTX\t0.70\t-33.01%\t280.14K\t\tTop Losers
VRSK\t173.37\t-2.71%\t1.03M\t\tOversold
QNCX\t0.32\t146.69%\t816.09M\t\tMost Active
SOXS\t1.79\t-1.10%\t299.17M\t\tMost Active
BKNG\t4331.70\t2.23%\t264.78K\t\tUpgrades
ACRE\t5.78\t13.21%\t1.31M\t\tEarnings Before
Headlines
4:00PM\tDow closes at record as rate-cut hopes lift stocks
3:45PM\tS&P 500 gains as tech rallies on AI optimism
3:30PM\tRetail sales data disappoints, fueling rate-cut bets
2:15PM\tTreasury yields tumble on weak economic data
1:00PM\tGold holds above $5,000 as inflation fears persist
Major News
GOOGL
-1.54%
KO
-2.03%
NVDA
+0.50%
SPOT
+14.36%
TSLA
+1.87%
META
-0.50%
AAPL
-0.53%
DDOG
+16.07%
Recent Quotes
IBIT
-1.36%
Date\tTime\tImpact\tRelease\tFor\tActual\tExpected\tPrior
Today\t10:00 AM\t\tBusiness Inventories MoM\tNov\t0.1%\t0.2%\t0.3%
Today\t10:00 AM\t\tRetail Inventories Ex Autos MoM\tNov\t0.2%\t-\t0.3%
Today\t11:00 AM\t\tTotal Household Debt\tQ4\t$18.8T\t-\t$18.59T
Date\tEarnings Release
Feb 10/b\tKO\tAZN\tSPGI\tSPOT\tMAR\tCVS\tDUK\tBP
Feb 10/a\tGILD\tWELL\tHOOD\tNET\tF\tEW\tHMC\tAIG
Feb 11\tCSCO\tMCD\tTMUS\tSHOP\tAPP\tEQIX\tNTES\tVRT
Feb 12\tAMAT\tANET\tBUD\tVRTX\tBN\tAEM\tHWM\tABNB
Ticker\tLatest Insider Trading\tRelationship\tDate\tTransaction\tCost\t#Shares\tValue($)
NVST\tReis Mischa\tOfficer\tFeb 10\tProposed Sale\t30.00\t9,675\t290,250
Futures\tLast\tChange\tChange %
Crude Oil\t64.09\t-0.27\t-0.42%
Natural Gas\t3.1840\t+0.0460\t+1.47%
Gold\t5058.70\t-20.70\t-0.41%
Dow\t50483.00\t+264.00\t+0.53%
S&P 500\t6994.25\t+11.00\t+0.16%
Nasdaq 100\t25377.25\t+23.25\t+0.09%
Russell 2000\t2708.20\t+12.10\t+0.45%
Forex & Bonds\tLast\tChange\tChange %
EUR/USD\t1.1899\t-0.0015\t-0.13%
USD/JPY\t154.32\t-1.55\t-0.99%
GBP/USD\t1.3657\t-0.0033\t-0.24%
BTC/USD\t69877.40\t+117.90\t+0.17%
5-Year Treasury\t3.699\t-0.042\t-1.12%
10-Year Treasury\t4.147\t-0.051\t-1.21%
30-Year Treasury\t4.788\t-0.06\t-1.24%
Quotes delayed 15 minutes.
"""


# --- Dataclass construction tests ---


class TestDataclasses:
    def test_headline(self):
        h = Headline(time="4:00PM", text="Dow closes at record")
        assert h.time == "4:00PM"
        assert h.text == "Dow closes at record"

    def test_major_mover(self):
        m = MajorMover(ticker="GOOGL", change_percent=-1.54)
        assert m.ticker == "GOOGL"
        assert m.change_percent == -1.54

    def test_futures_quote(self):
        f = FuturesQuote(name="Gold", last=5058.70, change=-20.70, change_percent=-0.41)
        assert f.name == "Gold"
        assert f.last == 5058.70
        assert f.change_percent == -0.41

    def test_forex_bond_quote(self):
        fb = ForexBondQuote(name="EUR/USD", last=1.1899, change=-0.0015, change_percent=-0.13)
        assert fb.name == "EUR/USD"

    def test_earnings_entry(self):
        e = EarningsEntry(date="Feb 10/b", tickers=["KO", "AZN", "SPGI"])
        assert e.date == "Feb 10/b"
        assert len(e.tickers) == 3

    def test_economic_release(self):
        r = EconomicRelease(
            release="Business Inventories MoM", period="Nov",
            actual="0.1%", expected="0.2%", prior="0.3%",
        )
        assert r.release == "Business Inventories MoM"
        assert r.actual == "0.1%"

    def test_finviz_data(self):
        data = FinVizData(
            headlines=[],
            major_movers=[],
            futures=[],
            forex_bonds=[],
            earnings=[],
            economic_releases=[],
            market_banner=None,
        )
        assert data.headlines == []


# --- Parsing tests ---


class TestParseFinvizPage:
    def test_parses_headlines(self):
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        assert len(data.headlines) == 5
        assert data.headlines[0].time == "4:00PM"
        assert "Dow closes at record" in data.headlines[0].text

    def test_parses_major_movers(self):
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        assert len(data.major_movers) > 0
        tickers = [m.ticker for m in data.major_movers]
        assert "GOOGL" in tickers
        assert "NVDA" in tickers
        assert "SPOT" in tickers

    def test_major_mover_change_sign(self):
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        movers_by_ticker = {m.ticker: m for m in data.major_movers}
        assert movers_by_ticker["GOOGL"].change_percent < 0
        assert movers_by_ticker["SPOT"].change_percent > 0

    def test_parses_futures(self):
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        assert len(data.futures) >= 4
        names = [f.name for f in data.futures]
        assert "Gold" in names
        assert "S&P 500" in names
        assert "Crude Oil" in names

    def test_futures_values(self):
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        gold = next(f for f in data.futures if f.name == "Gold")
        assert gold.last == pytest.approx(5058.70)
        assert gold.change == pytest.approx(-20.70)
        assert gold.change_percent == pytest.approx(-0.41)

    def test_parses_forex_bonds(self):
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        assert len(data.forex_bonds) >= 4
        names = [fb.name for fb in data.forex_bonds]
        assert "EUR/USD" in names
        assert "10-Year Treasury" in names
        assert "BTC/USD" in names

    def test_forex_values(self):
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        eur = next(fb for fb in data.forex_bonds if fb.name == "EUR/USD")
        assert eur.last == pytest.approx(1.1899)
        assert eur.change == pytest.approx(-0.0015)

    def test_parses_earnings(self):
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        assert len(data.earnings) >= 2
        # Should find today's earnings
        today_earnings = [e for e in data.earnings if "Feb 10" in e.date]
        assert len(today_earnings) >= 1

    def test_earnings_tickers(self):
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        all_tickers = []
        for e in data.earnings:
            all_tickers.extend(e.tickers)
        assert "KO" in all_tickers
        assert "CSCO" in all_tickers

    def test_parses_economic_releases(self):
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        assert len(data.economic_releases) >= 2
        releases = [r.release for r in data.economic_releases]
        assert "Business Inventories MoM" in releases

    def test_economic_release_values(self):
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        biz_inv = next(r for r in data.economic_releases if "Business Inventories" in r.release)
        assert biz_inv.actual == "0.1%"
        assert biz_inv.expected == "0.2%"
        assert biz_inv.prior == "0.3%"

    def test_parses_market_banner(self):
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        assert data.market_banner is not None
        assert "Dow closes at record" in data.market_banner

    def test_empty_page(self):
        data = parse_finviz_page("")
        assert data.headlines == []
        assert data.major_movers == []
        assert data.futures == []
        assert data.forex_bonds == []
        assert data.earnings == []
        assert data.economic_releases == []
        assert data.market_banner is None

    def test_partial_page_no_crash(self):
        """Parser handles partial/malformed input without crashing."""
        data = parse_finviz_page("Just some random text\nno structure here\n")
        assert isinstance(data, FinVizData)

    def test_does_not_include_recent_quotes_in_movers(self):
        """Recent Quotes section tickers should not be parsed as major movers."""
        data = parse_finviz_page(SAMPLE_PAGE_TEXT)
        tickers = [m.ticker for m in data.major_movers]
        assert "IBIT" not in tickers


# --- Formatting tests ---


class TestFormatFinvizBriefing:
    def _make_sample_data(self) -> FinVizData:
        return FinVizData(
            headlines=[
                Headline(time="4:00PM", text="Dow closes at record as rate-cut hopes lift stocks"),
                Headline(time="3:45PM", text="S&P 500 gains as tech rallies on AI optimism"),
                Headline(time="3:30PM", text="Retail sales data disappoints, fueling rate-cut bets"),
            ],
            major_movers=[
                MajorMover(ticker="SPOT", change_percent=14.36),
                MajorMover(ticker="DDOG", change_percent=16.07),
                MajorMover(ticker="GOOGL", change_percent=-1.54),
                MajorMover(ticker="KO", change_percent=-2.03),
                MajorMover(ticker="BP", change_percent=-5.97),
            ],
            futures=[
                FuturesQuote(name="Crude Oil", last=64.09, change=-0.27, change_percent=-0.42),
                FuturesQuote(name="Gold", last=5058.70, change=-20.70, change_percent=-0.41),
                FuturesQuote(name="S&P 500", last=6994.25, change=11.00, change_percent=0.16),
                FuturesQuote(name="Dow", last=50483.00, change=264.00, change_percent=0.53),
            ],
            forex_bonds=[
                ForexBondQuote(name="EUR/USD", last=1.1899, change=-0.0015, change_percent=-0.13),
                ForexBondQuote(name="10-Year Treasury", last=4.147, change=-0.051, change_percent=-1.21),
            ],
            earnings=[
                EarningsEntry(date="Feb 10/a", tickers=["GILD", "WELL", "HOOD", "NET"]),
                EarningsEntry(date="Feb 11", tickers=["CSCO", "MCD", "TMUS", "SHOP"]),
            ],
            economic_releases=[
                EconomicRelease(
                    release="Business Inventories MoM", period="Nov",
                    actual="0.1%", expected="0.2%", prior="0.3%",
                ),
                EconomicRelease(
                    release="Total Household Debt", period="Q4",
                    actual="$18.8T", expected="-", prior="$18.59T",
                ),
            ],
            market_banner="Dow closes at record as rate-cut hopes lift stocks",
        )

    def test_contains_headlines_section(self):
        data = self._make_sample_data()
        result = format_finviz_briefing(data)
        assert "**MARKET HEADLINES**" in result
        assert "Dow closes at record" in result

    def test_contains_movers_section(self):
        data = self._make_sample_data()
        result = format_finviz_briefing(data)
        assert "**MOVERS**" in result
        assert "SPOT" in result
        assert "DDOG" in result

    def test_movers_sorted_by_magnitude(self):
        data = self._make_sample_data()
        result = format_finviz_briefing(data)
        # DDOG (+16.07%) should appear before SPOT (+14.36%) since sorted by abs value
        ddog_pos = result.index("DDOG")
        spot_pos = result.index("SPOT")
        assert ddog_pos < spot_pos

    def test_movers_have_indicators(self):
        data = self._make_sample_data()
        result = format_finviz_briefing(data)
        # Positive movers get green, negative get red
        assert "🟢" in result
        assert "🔴" in result

    def test_contains_futures_section(self):
        data = self._make_sample_data()
        result = format_finviz_briefing(data)
        assert "**FUTURES**" in result
        assert "Gold" in result
        assert "Crude Oil" in result

    def test_contains_forex_bonds_section(self):
        data = self._make_sample_data()
        result = format_finviz_briefing(data)
        assert "**FOREX & BONDS**" in result
        assert "EUR/USD" in result
        assert "10-Year Treasury" in result

    def test_contains_earnings_section(self):
        data = self._make_sample_data()
        result = format_finviz_briefing(data)
        assert "**EARNINGS**" in result
        assert "GILD" in result
        assert "CSCO" in result

    def test_contains_economic_data_section(self):
        data = self._make_sample_data()
        result = format_finviz_briefing(data)
        assert "**ECONOMIC DATA**" in result
        assert "Business Inventories" in result

    def test_economic_surprise_indicator(self):
        data = self._make_sample_data()
        result = format_finviz_briefing(data)
        # Business Inventories: actual 0.1% < expected 0.2% → miss
        assert "miss" in result.lower() or "⬇" in result or "🔴" in result

    def test_omits_empty_sections(self):
        data = FinVizData(
            headlines=[], major_movers=[], futures=[],
            forex_bonds=[], earnings=[], economic_releases=[],
            market_banner=None,
        )
        result = format_finviz_briefing(data)
        assert "HEADLINES" not in result
        assert "MOVERS" not in result

    def test_empty_data_returns_fallback(self):
        data = FinVizData(
            headlines=[], major_movers=[], futures=[],
            forex_bonds=[], earnings=[], economic_releases=[],
            market_banner=None,
        )
        result = format_finviz_briefing(data)
        assert "unavailable" in result.lower() or result.strip() == ""

    def test_format_is_plain_text(self):
        """Output should be plain text suitable for chat, no markdown headings."""
        data = self._make_sample_data()
        result = format_finviz_briefing(data)
        assert "##" not in result
        assert "```" not in result


# --- Fetch integration tests (mocked browser API) ---


class TestFetchFinvizData:
    def test_successful_fetch(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "text": SAMPLE_PAGE_TEXT,
            "title": "FinViz",
            "url": "https://finviz.com",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_response) as mock_post:
            data = fetch_finviz_data()

        assert data is not None
        assert isinstance(data, FinVizData)
        assert len(data.headlines) > 0
        assert len(data.futures) > 0
        mock_post.assert_called_once()

    def test_fetch_uses_browse_api(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "text": SAMPLE_PAGE_TEXT,
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_response) as mock_post:
            fetch_finviz_data()

        call_args = mock_post.call_args
        assert "/browse" in call_args[0][0]
        payload = call_args[1].get("json", call_args[0][1] if len(call_args[0]) > 1 else {})
        assert "finviz.com" in payload.get("url", "")

    def test_fetch_returns_none_on_error(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "error",
            "error": "timed out",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_response):
            data = fetch_finviz_data()

        assert data is None

    def test_fetch_returns_none_on_connection_error(self):
        with patch("httpx.post", side_effect=Exception("Connection refused")):
            data = fetch_finviz_data()

        assert data is None

    def test_fetch_returns_none_on_captcha(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "captcha",
            "session_id": "abc123",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_response):
            data = fetch_finviz_data()

        assert data is None

    def test_custom_api_url(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "text": SAMPLE_PAGE_TEXT,
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_response) as mock_post:
            fetch_finviz_data(api_url="http://custom:9999")

        call_url = mock_post.call_args[0][0]
        assert call_url.startswith("http://custom:9999")


# --- Edge case parsing tests ---


class TestParsingEdgeCases:
    def test_headline_with_special_characters(self):
        text = "Headlines\n4:00PM\tS&P 500's 'worst day' in months — analysts weigh in\n"
        data = parse_finviz_page(text)
        assert len(data.headlines) == 1
        assert "S&P 500" in data.headlines[0].text

    def test_mover_with_zero_change(self):
        text = "Major News\nFLAT\n0.00%\n"
        data = parse_finviz_page(text)
        if data.major_movers:
            flat = data.major_movers[0]
            assert flat.change_percent == 0.0

    def test_futures_negative_values(self):
        text = "Futures\tLast\tChange\tChange %\nCrude Oil\t64.09\t-0.27\t-0.42%\n"
        data = parse_finviz_page(text)
        assert len(data.futures) == 1
        assert data.futures[0].change < 0
        assert data.futures[0].change_percent < 0

    def test_economic_release_dash_expected(self):
        """Some releases have '-' for expected — should be stored as-is."""
        text = (
            "Date\tTime\tImpact\tRelease\tFor\tActual\tExpected\tPrior\n"
            "Today\t11:00 AM\t\tTotal Household Debt\tQ4\t$18.8T\t-\t$18.59T\n"
        )
        data = parse_finviz_page(text)
        assert len(data.economic_releases) == 1
        assert data.economic_releases[0].expected == "-"

    def test_earnings_before_after_markers(self):
        """Earnings dates with /b (before) and /a (after) markers."""
        text = (
            "Date\tEarnings Release\n"
            "Feb 10/b\tKO\tAZN\tSPGI\n"
            "Feb 10/a\tGILD\tWELL\n"
        )
        data = parse_finviz_page(text)
        assert len(data.earnings) == 2
        before = next(e for e in data.earnings if "/b" in e.date)
        assert "KO" in before.tickers
        after = next(e for e in data.earnings if "/a" in e.date)
        assert "GILD" in after.tickers
