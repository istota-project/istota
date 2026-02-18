"""Tests for skills/markets module."""

import json
import sys
from datetime import datetime
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.markets import (
    DEFAULT_FUTURES,
    DEFAULT_INDICES,
    SUMMARY_SYMBOLS,
    SYMBOL_NAMES,
    MarketQuote,
    build_parser,
    cmd_finviz,
    cmd_quote,
    cmd_summary,
    format_market_summary,
    format_quote,
)


def _make_mock_yf(prices: dict[str, tuple[float, float]]):
    """Create a mock yfinance module.

    Args:
        prices: mapping of symbol -> (last_price, previous_close)
    """
    mock_yf = MagicMock()

    def make_ticker(symbol):
        ticker = MagicMock()
        if symbol in prices:
            last, prev = prices[symbol]
            ticker.fast_info.last_price = last
            ticker.fast_info.previous_close = prev
        else:
            ticker.fast_info.last_price = None
            ticker.fast_info.previous_close = None
        return ticker

    mock_yf.Ticker.side_effect = make_ticker
    return mock_yf


# --- get_quotes tests ---


class TestGetQuotes:
    def test_valid_symbols(self):
        mock_yf = _make_mock_yf({"ES=F": (5000.0, 4950.0)})
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            # Re-import to pick up the mock
            from istota.skills.markets import get_quotes
            result = get_quotes(["ES=F"])

        assert len(result) == 1
        assert result[0].symbol == "ES=F"
        assert result[0].price == 5000.0
        assert result[0].change == pytest.approx(50.0)
        assert result[0].change_percent == pytest.approx(1.0101, rel=1e-3)

    def test_failed_symbol_skipped(self):
        mock_yf = MagicMock()
        mock_yf.Ticker.side_effect = Exception("API error")
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            from istota.skills.markets import get_quotes
            result = get_quotes(["BAD"])

        assert result == []

    def test_missing_yfinance(self):
        """When yfinance is not installed, get_quotes returns empty list."""
        # Remove yfinance from sys.modules so the import fails
        saved = sys.modules.pop("yfinance", None)
        try:
            with patch.dict(sys.modules, {"yfinance": None}):
                # Force re-evaluation by calling the function
                # The import inside get_quotes will raise ImportError when module is None
                from istota.skills.markets import get_quotes
                # Actually, patch.dict with None doesn't raise ImportError.
                # We need to simulate import failure differently.
                pass
        finally:
            if saved is not None:
                sys.modules["yfinance"] = saved

        # Use a more direct approach: mock builtins.__import__
        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def failing_import(name, *args, **kwargs):
            if name == "yfinance":
                raise ImportError("No module named 'yfinance'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=failing_import):
            from istota.skills.markets import get_quotes
            result = get_quotes(["ES=F"])

        assert result == []

    def test_none_price_skipped(self):
        mock_yf = _make_mock_yf({})  # all symbols return None prices
        mock_yf.Ticker.side_effect = None
        ticker = MagicMock()
        ticker.fast_info.last_price = None
        ticker.fast_info.previous_close = 100.0
        mock_yf.Ticker.return_value = ticker

        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            from istota.skills.markets import get_quotes
            result = get_quotes(["ES=F"])

        assert result == []

    def test_multiple_symbols(self):
        mock_yf = _make_mock_yf({
            "ES=F": (5000.0, 4950.0),
            "NQ=F": (18000.0, 17900.0),
        })
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            from istota.skills.markets import get_quotes
            result = get_quotes(["ES=F", "NQ=F"])

        assert len(result) == 2
        assert result[0].symbol == "ES=F"
        assert result[1].symbol == "NQ=F"


# --- format_quote tests ---


class TestFormatQuote:
    def test_positive_change(self):
        quote = MarketQuote(
            symbol="ES=F", name="S&P 500 E-mini",
            price=5000.0, change=50.0, change_percent=1.01,
        )
        formatted = format_quote(quote)
        assert "+50.00" in formatted
        assert "+1.01%" in formatted
        assert "5,000.00" in formatted

    def test_negative_change(self):
        quote = MarketQuote(
            symbol="ES=F", name="S&P 500 E-mini",
            price=4900.0, change=-50.0, change_percent=-1.01,
        )
        formatted = format_quote(quote)
        assert "-50.00" in formatted
        assert "-1.01%" in formatted
        assert "+" not in formatted

    def test_zero_change(self):
        quote = MarketQuote(
            symbol="ES=F", name="S&P 500 E-mini",
            price=5000.0, change=0.0, change_percent=0.0,
        )
        formatted = format_quote(quote)
        assert "+0.00" in formatted
        assert "+0.00%" in formatted


# --- format_market_summary tests ---


class TestFormatMarketSummary:
    def test_morning_header(self):
        quotes = [
            MarketQuote(
                symbol="ES=F", name="S&P 500 E-mini",
                price=5000.0, change=50.0, change_percent=1.01,
                timestamp=datetime(2025, 1, 27, 8, 30),
            )
        ]
        result = format_market_summary(quotes, mode="morning")
        assert "Pre-market Futures" in result

    def test_evening_header(self):
        quotes = [
            MarketQuote(
                symbol="^GSPC", name="S&P 500",
                price=5000.0, change=-20.0, change_percent=-0.4,
                timestamp=datetime(2025, 1, 27, 16, 0),
            )
        ]
        result = format_market_summary(quotes, mode="evening")
        assert "Market Close" in result

    def test_empty_quotes(self):
        result = format_market_summary([], mode="morning")
        assert result == "Market data unavailable"

    def test_includes_timestamp(self):
        quotes = [
            MarketQuote(
                symbol="ES=F", name="S&P 500 E-mini",
                price=5000.0, change=50.0, change_percent=1.01,
                timestamp=datetime(2025, 1, 27, 14, 45),
            )
        ]
        result = format_market_summary(quotes)
        assert "14:45" in result

    def test_formats_all_quotes(self):
        quotes = [
            MarketQuote(
                symbol="ES=F", name="S&P 500 E-mini",
                price=5000.0, change=50.0, change_percent=1.01,
                timestamp=datetime(2025, 1, 27, 8, 0),
            ),
            MarketQuote(
                symbol="NQ=F", name="Nasdaq 100 E-mini",
                price=18000.0, change=100.0, change_percent=0.56,
                timestamp=datetime(2025, 1, 27, 8, 0),
            ),
        ]
        result = format_market_summary(quotes)
        assert "S&P 500 E-mini" in result
        assert "Nasdaq 100 E-mini" in result

    def test_no_timestamp(self):
        quotes = [
            MarketQuote(
                symbol="ES=F", name="S&P 500 E-mini",
                price=5000.0, change=50.0, change_percent=1.01,
                timestamp=None,
            )
        ]
        result = format_market_summary(quotes)
        assert "As of:" not in result


class TestDefaults:
    def test_default_futures_defined(self):
        assert len(DEFAULT_FUTURES) > 0
        assert "ES=F" in DEFAULT_FUTURES

    def test_default_indices_defined(self):
        assert len(DEFAULT_INDICES) > 0
        assert "^GSPC" in DEFAULT_INDICES

    def test_symbol_names_covers_defaults(self):
        for sym in DEFAULT_FUTURES + DEFAULT_INDICES:
            assert sym in SYMBOL_NAMES

    def test_summary_symbols_defined(self):
        assert "^GSPC" in SUMMARY_SYMBOLS
        assert "^VIX" in SUMMARY_SYMBOLS
        assert "GC=F" in SUMMARY_SYMBOLS


# --- CLI tests ---


class TestBuildParser:
    def test_quote_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["quote", "AAPL", "MSFT"])
        assert args.command == "quote"
        assert args.symbols == ["AAPL", "MSFT"]

    def test_summary_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["summary"])
        assert args.command == "summary"

    def test_finviz_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["finviz"])
        assert args.command == "finviz"

    def test_no_subcommand(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None


class TestCmdQuote:
    def test_outputs_json(self, capsys):
        mock_yf = _make_mock_yf({"AAPL": (195.0, 190.0)})
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            from istota.skills.markets import get_quotes  # noqa: F811
            parser = build_parser()
            args = parser.parse_args(["quote", "AAPL"])
            cmd_quote(args)

        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        assert output[0]["symbol"] == "AAPL"
        assert output[0]["price"] == 195.0
        assert isinstance(output[0]["change"], float)
        assert isinstance(output[0]["change_percent"], float)
        assert output[0]["timestamp"] is not None

    def test_empty_result(self, capsys):
        mock_yf = _make_mock_yf({})
        mock_yf.Ticker.side_effect = None
        ticker = MagicMock()
        ticker.fast_info.last_price = None
        ticker.fast_info.previous_close = None
        mock_yf.Ticker.return_value = ticker
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            parser = build_parser()
            args = parser.parse_args(["quote", "INVALID"])
            cmd_quote(args)

        output = json.loads(capsys.readouterr().out)
        assert output == []


class TestCmdSummary:
    def test_fetches_summary_symbols(self, capsys):
        prices = {sym: (100.0, 99.0) for sym in SUMMARY_SYMBOLS}
        mock_yf = _make_mock_yf(prices)
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            parser = build_parser()
            args = parser.parse_args(["summary"])
            cmd_summary(args)

        output = json.loads(capsys.readouterr().out)
        assert len(output) == len(SUMMARY_SYMBOLS)
        symbols = [q["symbol"] for q in output]
        for sym in SUMMARY_SYMBOLS:
            assert sym in symbols


class TestCmdFinviz:
    def test_success(self, capsys):
        mock_data = MagicMock()
        with patch(
            "istota.skills.markets.finviz.fetch_finviz_data", return_value=mock_data
        ) as mock_fetch, patch(
            "istota.skills.markets.finviz.format_finviz_briefing",
            return_value="formatted output",
        ):
            parser = build_parser()
            args = parser.parse_args(["finviz"])
            cmd_finviz(args)

        output = json.loads(capsys.readouterr().out)
        assert output["formatted"] == "formatted output"
        mock_fetch.assert_called_once()

    def test_failure_exits(self):
        with patch(
            "istota.skills.markets.finviz.fetch_finviz_data", return_value=None
        ):
            parser = build_parser()
            args = parser.parse_args(["finviz"])
            with pytest.raises(SystemExit, match="1"):
                cmd_finviz(args)
