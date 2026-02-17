For briefings, market data is pre-fetched and included directly in your prompt. You don't need to fetch it yourself.

### Morning Briefings (before noon)
Pre-market futures are provided:
- ES=F: S&P 500 E-mini futures
- NQ=F: Nasdaq 100 E-mini futures
- YM=F: Dow Jones E-mini futures

### Evening Briefings (noon and after)
Index closing prices are provided via yfinance:
- ^GSPC: S&P 500
- ^IXIC: Nasdaq Composite
- ^DJI: Dow Jones Industrial Average

Additionally, FinViz data is scraped and pre-formatted, providing:
- Market headlines (top stories with timestamps)
- Major movers (tickers in the news, sorted by magnitude)
- Futures (crude oil, gold, index futures)
- Forex & bonds (EUR/USD, USD/JPY, treasuries, BTC)
- Economic data releases (with beat/miss indicators vs consensus)
- Upcoming earnings calendar (2-3 days ahead)

Include the pre-fetched market data in your briefing. Copy FinViz data as-is — it is pre-formatted.
