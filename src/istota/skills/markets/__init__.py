"""Market data via yfinance.

Also provides a CLI for interactive market queries:
    python -m istota.skills.markets quote AAPL MSFT
    python -m istota.skills.markets summary
    python -m istota.skills.markets finviz
"""

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass
class MarketQuote:
    symbol: str
    name: str
    price: float
    change: float
    change_percent: float
    timestamp: datetime | None = None


# Default symbols for market overview
DEFAULT_FUTURES = ["ES=F", "NQ=F", "YM=F"]  # S&P 500, Nasdaq 100, Dow Jones E-mini futures
DEFAULT_INDICES = ["^GSPC", "^IXIC", "^DJI"]  # S&P 500, Nasdaq Composite, Dow Jones

# Human-readable names for common symbols
SYMBOL_NAMES = {
    "ES=F": "S&P 500 E-mini",
    "NQ=F": "Nasdaq 100 E-mini",
    "YM=F": "Dow E-mini",
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq Composite",
    "^DJI": "Dow Jones",
    "^VIX": "VIX",
    "GC=F": "Gold",
    "CL=F": "Crude Oil",
    "^TNX": "10-Year Treasury",
}


def get_quotes(symbols: list[str]) -> list[MarketQuote]:
    """
    Fetch current quotes for given symbols.

    Returns list of MarketQuote objects. Failed fetches are silently skipped.
    """
    try:
        import yfinance as yf
    except ImportError:
        return []

    quotes = []
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info

            # Get current price and previous close
            price = info.last_price
            prev_close = info.previous_close

            if price is None or prev_close is None:
                continue

            change = price - prev_close
            change_pct = (change / prev_close) * 100 if prev_close else 0

            quotes.append(MarketQuote(
                symbol=symbol,
                name=SYMBOL_NAMES.get(symbol, symbol),
                price=price,
                change=change,
                change_percent=change_pct,
                timestamp=datetime.now(),
            ))
        except Exception:
            # Skip symbols that fail to fetch
            continue

    return quotes


def get_futures_quotes(symbols: list[str] | None = None) -> list[MarketQuote]:
    """
    Fetch futures quotes.

    Args:
        symbols: List of futures symbols, defaults to major index futures

    Returns:
        List of MarketQuote objects
    """
    if symbols is None:
        symbols = DEFAULT_FUTURES
    return get_quotes(symbols)


def get_index_quotes(symbols: list[str] | None = None) -> list[MarketQuote]:
    """
    Fetch index quotes.

    Args:
        symbols: List of index symbols, defaults to major US indices

    Returns:
        List of MarketQuote objects
    """
    if symbols is None:
        symbols = DEFAULT_INDICES
    return get_quotes(symbols)


def format_quote(quote: MarketQuote) -> str:
    """Format a single quote for display."""
    sign = "+" if quote.change >= 0 else ""
    if quote.change > 0:
        dot = "🟢"
    elif quote.change < 0:
        dot = "🔴"
    else:
        dot = "⚪"
    return (
        f"{dot} {quote.name}: "
        f"{quote.price:,.2f} ({sign}{quote.change:,.2f}, {sign}{quote.change_percent:.2f}%)"
    )


def format_market_summary(quotes: list[MarketQuote], mode: str = "morning") -> str:
    """
    Format market quotes for display in briefing.

    Args:
        quotes: List of MarketQuote objects
        mode: "morning" for pre-market futures, "evening" for day summary

    Returns:
        Formatted string for display
    """
    if not quotes:
        return "Market data unavailable"

    header = "Pre-market Futures" if mode == "morning" else "Market Close"
    lines = [f"## {header}:"]

    for quote in quotes:
        lines.append(f"  {format_quote(quote)}")

    # Add timestamp from first quote if available
    if quotes[0].timestamp:
        time_str = quotes[0].timestamp.strftime("%H:%M")
        lines.append(f"  As of: {time_str}")

    return "\n".join(lines)


# --- CLI ---

SUMMARY_SYMBOLS = ["^GSPC", "^IXIC", "^DJI", "^VIX", "GC=F", "CL=F", "^TNX"]


def _quote_to_dict(q: MarketQuote) -> dict:
    d = asdict(q)
    d["timestamp"] = q.timestamp.isoformat() if q.timestamp else None
    return d


def cmd_quote(args: argparse.Namespace) -> None:
    quotes = get_quotes(args.symbols)
    print(json.dumps([_quote_to_dict(q) for q in quotes], indent=2))


def cmd_summary(args: argparse.Namespace) -> None:
    quotes = get_quotes(SUMMARY_SYMBOLS)
    print(json.dumps([_quote_to_dict(q) for q in quotes], indent=2))


def cmd_finviz(args: argparse.Namespace) -> None:
    from .finviz import fetch_finviz_data, format_finviz_briefing

    api_url = os.environ.get("BROWSER_API_URL")
    data = fetch_finviz_data(api_url=api_url)
    if data is None:
        print(json.dumps({"error": "Failed to fetch FinViz data"}))
        sys.exit(1)
    print(json.dumps({"formatted": format_finviz_briefing(data)}))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Market data CLI")
    sub = parser.add_subparsers(dest="command")

    quote_cmd = sub.add_parser("quote", help="Fetch quotes for specific symbols")
    quote_cmd.add_argument("symbols", nargs="+", help="Ticker symbols (e.g. AAPL MSFT)")

    sub.add_parser("summary", help="Broad market overview")
    sub.add_parser("finviz", help="FinViz market data (requires BROWSER_API_URL)")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "quote":
        cmd_quote(args)
    elif args.command == "summary":
        cmd_summary(args)
    elif args.command == "finviz":
        cmd_finviz(args)
    else:
        parser.print_help()
        sys.exit(1)
