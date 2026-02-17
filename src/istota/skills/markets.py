"""Market data via yfinance."""

from dataclasses import dataclass
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
