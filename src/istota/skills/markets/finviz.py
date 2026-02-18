"""FinViz market data scraping via headless browser.

Fetches the FinViz homepage and parses structured market data:
- Market headlines
- Major movers (tickers in the news)
- Futures (commodities, index futures)
- Forex & bonds (currencies, treasuries)
- Upcoming earnings
- Economic calendar releases
"""

import logging
import os
import re
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("istota.skills.markets.finviz")

FINVIZ_URL = "https://finviz.com"
DEFAULT_API_URL = "http://localhost:9223"
BROWSE_TIMEOUT = 120.0


@dataclass
class Headline:
    time: str
    text: str


@dataclass
class MajorMover:
    ticker: str
    change_percent: float


@dataclass
class FuturesQuote:
    name: str
    last: float
    change: float
    change_percent: float


@dataclass
class ForexBondQuote:
    name: str
    last: float
    change: float
    change_percent: float


@dataclass
class EarningsEntry:
    date: str
    tickers: list[str] = field(default_factory=list)


@dataclass
class EconomicRelease:
    release: str
    period: str
    actual: str
    expected: str
    prior: str


@dataclass
class FinVizData:
    headlines: list[Headline] = field(default_factory=list)
    major_movers: list[MajorMover] = field(default_factory=list)
    futures: list[FuturesQuote] = field(default_factory=list)
    forex_bonds: list[ForexBondQuote] = field(default_factory=list)
    earnings: list[EarningsEntry] = field(default_factory=list)
    economic_releases: list[EconomicRelease] = field(default_factory=list)
    market_banner: str | None = None


def _parse_float(s: str) -> float:
    """Parse a float from a string, stripping +, %, commas, $."""
    s = s.strip().replace(",", "").replace("%", "").replace("$", "").replace("+", "")
    return float(s)


def parse_finviz_page(text: str) -> FinVizData:
    """Parse FinViz homepage text content into structured data.

    Args:
        text: Plain text content from the browse skill's page render.

    Returns:
        FinVizData with all parsed sections.
    """
    data = FinVizData()
    if not text or not text.strip():
        return data

    lines = text.split("\n")

    # Parse market banner (first headline-like line with time + message)
    _parse_banner(lines, data)

    # Parse headlines section
    _parse_headlines(lines, data)

    # Parse major movers
    _parse_major_movers(lines, data)

    # Parse futures
    _parse_futures(lines, data)

    # Parse forex & bonds
    _parse_forex_bonds(lines, data)

    # Parse earnings
    _parse_earnings(lines, data)

    # Parse economic calendar
    _parse_economic_releases(lines, data)

    return data


def _parse_banner(lines: list[str], data: FinVizData) -> None:
    """Extract the market banner headline (e.g., 'Today, 4:00 PMDow closes...')."""
    for line in lines:
        # Banner format: "Today, HH:MMAM/PM<headline>" or similar
        m = re.match(r"Today,\s+\d+:\d+\s*[AP]M(.+)", line.strip())
        if m:
            data.market_banner = m.group(1).strip()
            return


def _parse_headlines(lines: list[str], data: FinVizData) -> None:
    """Parse the Headlines section — lines like '4:00PM\\tHeadline text'."""
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == "Headlines":
            in_section = True
            continue
        if in_section:
            # Headlines end at "Major News" or another section
            if stripped in ("Major News", "Recent Quotes", "Date\tEarnings Release"):
                break
            # Match "HH:MMAM/PM\tHeadline text"
            m = re.match(r"(\d{1,2}:\d{2}[AP]M)\t(.+)", stripped)
            if m:
                data.headlines.append(Headline(time=m.group(1), text=m.group(2)))


def _parse_major_movers(lines: list[str], data: FinVizData) -> None:
    """Parse Major News ticker/change pairs."""
    in_section = False
    current_ticker = None
    for line in lines:
        stripped = line.strip()
        if stripped == "Major News":
            in_section = True
            continue
        if in_section:
            if stripped == "Recent Quotes":
                break
            # Ticker line: all-caps, 1-5 chars
            if re.match(r"^[A-Z]{1,5}$", stripped) and current_ticker is None:
                current_ticker = stripped
                continue
            # Change line: +/-NN.NN%
            if current_ticker and re.match(r"^[+-]?\d+\.?\d*%$", stripped):
                try:
                    pct = _parse_float(stripped)
                    data.major_movers.append(MajorMover(ticker=current_ticker, change_percent=pct))
                except ValueError:
                    pass
                current_ticker = None
                continue
            # If we see a non-matching line, reset
            current_ticker = None


def _parse_futures(lines: list[str], data: FinVizData) -> None:
    """Parse the Futures table."""
    in_section = False
    for line in lines:
        stripped = line.strip()
        # Detect section start
        if stripped.startswith("Futures\t") and "Last" in stripped:
            in_section = True
            continue
        if in_section:
            # End at next section header or empty gap
            if stripped.startswith("Forex & Bonds\t") or stripped.startswith("First Time"):
                break
            # Parse "Name\tLast\tChange\tChange %"
            parts = stripped.split("\t")
            if len(parts) >= 4:
                try:
                    name = parts[0].strip()
                    last = _parse_float(parts[1])
                    change = _parse_float(parts[2])
                    change_pct = _parse_float(parts[3])
                    data.futures.append(FuturesQuote(
                        name=name, last=last, change=change, change_percent=change_pct,
                    ))
                except (ValueError, IndexError):
                    continue


def _parse_forex_bonds(lines: list[str], data: FinVizData) -> None:
    """Parse the Forex & Bonds table."""
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Forex & Bonds\t") and "Last" in stripped:
            in_section = True
            continue
        if in_section:
            # End at ad/footer content
            if stripped.startswith("Smarter") or stripped.startswith("Quotes delayed"):
                break
            parts = stripped.split("\t")
            if len(parts) >= 4:
                try:
                    name = parts[0].strip()
                    last = _parse_float(parts[1])
                    change = _parse_float(parts[2])
                    change_pct = _parse_float(parts[3])
                    data.forex_bonds.append(ForexBondQuote(
                        name=name, last=last, change=change, change_percent=change_pct,
                    ))
                except (ValueError, IndexError):
                    continue


def _parse_earnings(lines: list[str], data: FinVizData) -> None:
    """Parse the earnings calendar table."""
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == "Date\tEarnings Release":
            in_section = True
            continue
        if in_section:
            # End at next section (Insider Trading table)
            if stripped.startswith("Ticker\t") and "Insider" in stripped:
                break
            # Parse "Feb 10/b\tKO\tAZN\tSPGI..."
            parts = stripped.split("\t")
            if len(parts) >= 2:
                date_str = parts[0].strip()
                # Validate it looks like a date: "Feb DD" with optional /a or /b
                if re.match(r"^[A-Z][a-z]{2}\s+\d{1,2}(/[ab])?$", date_str):
                    tickers = [p.strip() for p in parts[1:] if p.strip()]
                    data.earnings.append(EarningsEntry(date=date_str, tickers=tickers))


def _parse_economic_releases(lines: list[str], data: FinVizData) -> None:
    """Parse the economic calendar table."""
    in_section = False
    for line in lines:
        stripped = line.strip()
        # Header: "Date\tTime\tImpact\tRelease\tFor\tActual\tExpected\tPrior"
        if stripped.startswith("Date\tTime\t") and "Release" in stripped:
            in_section = True
            continue
        if in_section:
            # End at earnings or other section
            if stripped == "Date\tEarnings Release" or stripped.startswith("Ticker\t"):
                break
            # Parse data rows — skip "Today\tHH:MM AM" time-only rows
            parts = stripped.split("\t")
            if len(parts) >= 7:
                # Find the Release column — it's after Date, Time, Impact
                # Format: Date\tTime\t[Impact]\tRelease\tFor\tActual\tExpected\tPrior
                # Impact may be empty
                release_name = parts[3].strip() if len(parts) > 3 else ""
                period = parts[4].strip() if len(parts) > 4 else ""
                actual = parts[5].strip() if len(parts) > 5 else ""
                expected = parts[6].strip() if len(parts) > 6 else ""
                prior = parts[7].strip() if len(parts) > 7 else ""

                if release_name and actual and actual != "-":
                    data.economic_releases.append(EconomicRelease(
                        release=release_name,
                        period=period,
                        actual=actual,
                        expected=expected,
                        prior=prior,
                    ))


def format_finviz_briefing(data: FinVizData) -> str:
    """Format FinViz data into a structured text block for the evening briefing.

    Output is plain text suitable for Nextcloud Talk chat messages.
    Uses bold labels (not markdown headings) for section headers.

    Args:
        data: Parsed FinViz data.

    Returns:
        Formatted briefing text.
    """
    sections = []

    # Market headlines
    if data.headlines:
        lines = ["**MARKET HEADLINES**"]
        for h in data.headlines[:6]:
            lines.append(f"- {h.time} — {h.text}")
        sections.append("\n".join(lines))

    # Major movers — sorted by absolute change magnitude
    if data.major_movers:
        sorted_movers = sorted(data.major_movers, key=lambda m: abs(m.change_percent), reverse=True)
        lines = ["**MOVERS**"]
        for m in sorted_movers[:8]:
            indicator = "🟢" if m.change_percent > 0 else "🔴" if m.change_percent < 0 else "⚪"
            sign = "+" if m.change_percent > 0 else ""
            lines.append(f"{indicator} **{m.ticker}** {sign}{m.change_percent:.2f}%")
        sections.append("\n".join(lines))

    # Futures
    if data.futures:
        lines = ["**FUTURES**"]
        for f in data.futures:
            indicator = "🟢" if f.change >= 0 else "🔴"
            sign = "+" if f.change >= 0 else ""
            lines.append(
                f"{indicator} **{f.name}**: {f.last:,.2f} ({sign}{f.change:,.2f}, {sign}{f.change_percent:.2f}%)"
            )
        sections.append("\n".join(lines))

    # Forex & Bonds
    if data.forex_bonds:
        lines = ["**FOREX & BONDS**"]
        for fb in data.forex_bonds:
            indicator = "🟢" if fb.change >= 0 else "🔴"
            sign = "+" if fb.change >= 0 else ""
            # Different precision for different instruments
            if fb.last > 100:
                price_fmt = f"{fb.last:,.2f}"
            elif fb.last > 10:
                price_fmt = f"{fb.last:.3f}"
            else:
                price_fmt = f"{fb.last:.4f}"
            lines.append(
                f"{indicator} **{fb.name}**: {price_fmt} ({sign}{fb.change:.4f}, {sign}{fb.change_percent:.2f}%)"
            )
        sections.append("\n".join(lines))

    # Earnings calendar
    if data.earnings:
        lines = ["**EARNINGS**"]
        for e in data.earnings:
            marker = ""
            if "/b" in e.date:
                marker = " (before open)"
            elif "/a" in e.date:
                marker = " (after close)"
            clean_date = e.date.replace("/b", "").replace("/a", "")
            tickers_str = ", ".join(e.tickers[:8])
            if len(e.tickers) > 8:
                tickers_str += f" +{len(e.tickers) - 8} more"
            lines.append(f"- **{clean_date}**{marker}: {tickers_str}")
        sections.append("\n".join(lines))

    # Economic data releases
    if data.economic_releases:
        lines = ["**ECONOMIC DATA**"]
        for r in data.economic_releases:
            surprise = _format_surprise(r)
            lines.append(f"- **{r.release}** ({r.period}): {r.actual}{surprise}")
        sections.append("\n".join(lines))

    if not sections:
        return "FinViz market data unavailable"

    return "\n\n".join(sections)


def _format_surprise(release: EconomicRelease) -> str:
    """Format surprise indicator comparing actual vs expected."""
    if release.expected == "-" or not release.expected:
        return f" (prior: {release.prior})"

    # Try to compare numeric values
    try:
        actual_val = _parse_float(release.actual.replace("T", "e12").replace("B", "e9").replace("M", "e6").replace("K", "e3"))
        expected_val = _parse_float(release.expected.replace("T", "e12").replace("B", "e9").replace("M", "e6").replace("K", "e3"))
        if actual_val > expected_val:
            return f" vs exp. {release.expected} 🟢 beat"
        elif actual_val < expected_val:
            return f" vs exp. {release.expected} 🔴 miss"
        else:
            return f" vs exp. {release.expected} (inline)"
    except (ValueError, TypeError):
        return f" (exp. {release.expected}, prior: {release.prior})"


def fetch_finviz_data(api_url: str | None = None) -> FinVizData | None:
    """Fetch and parse FinViz homepage data via the headless browser API.

    Args:
        api_url: Browser API URL. Defaults to BROWSER_API_URL env var or localhost:9223.

    Returns:
        Parsed FinVizData, or None on failure.
    """
    if api_url is None:
        api_url = os.environ.get("BROWSER_API_URL", DEFAULT_API_URL)

    try:
        resp = httpx.post(
            f"{api_url}/browse",
            json={"url": FINVIZ_URL, "timeout": 30},
            timeout=BROWSE_TIMEOUT,
        )
        result = resp.json()

        if result.get("status") != "ok":
            logger.warning("FinViz fetch failed: %s", result.get("error", result.get("status")))
            return None

        text = result.get("text", "")
        if not text:
            logger.warning("FinViz returned empty page text")
            return None

        return parse_finviz_page(text)

    except Exception as e:
        logger.warning("FinViz fetch error: %s", e)
        return None
