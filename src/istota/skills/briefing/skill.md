Briefings must be returned as a JSON object: `{"subject": "Morning Briefing", "body": "<content>"}`. The body contains the full briefing text with emoji section headers, using `\n` for newlines. Do not output anything outside the JSON object. Do not send emails or use email commands — delivery is handled by the scheduler.

The body is formatted for Nextcloud Talk chat messages. Use emoji-prefixed labels as section headers. Only include sections that have data.

## Allowed Markdown

- **bold** for emphasis
- *italic* for secondary emphasis
- [links](url) for URLs
- --- horizontal rule before the reminder section
- Bullet points for lists

## Forbidden

- Tables
- Markdown headings (#, ##, etc.)
- Code blocks (unless showing actual code)
- Nested bullet points
- Commentary or editorializing on market data

## Section Format

Sections appear in this order. Omit any section with no data.

**📰 NEWS**

General news — politics, world events, policy, science, tech (non-market). Focus on US and world events and keep a global perspective. Lead with items that recur across multiple sources. Target 5 stories. One short paragraph per story, bold uppercase topic. Keep each paragraph to two or three sentences. Add source attribution at the end of each paragraph in brackets.

Newsletters often mix general and market news. Place each story in the right section by topic — a story about tariff policy goes in NEWS, its market impact goes in MARKETS.

<news_example>
**IRAN-US TENSIONS ESCALATE:** Iran's foreign minister warned that Tehran's forces have their "fingers on the trigger" as Trump threatened a "massive Armada" heading toward Iran, saying "time is running out" for a nuclear deal. The EU is expected to add Iran's Revolutionary Guard to its terror blacklist. [Semafor, NYT]
</news_example>

When both headlines (web frontpages) and news (email newsletters) are present, merge stories from both sources. A story that appears in both AP frontpage and a Semafor newsletter should be one entry with combined attribution: `[AP, Semafor]`.

**📈 MARKETS**

One line per quote with 🟢/🔴 indicator based on change direction, bold the name:
🟢 **S&P 500 E-mini**: 6,104.75 (+30.25, +0.50%)
🔴 **Nasdaq 100 E-mini**: 21,857.50 (-45.00, -0.21%)
Use 🟢 for positive change, 🔴 for negative, ⚪ for zero. Apply to ALL tickers — futures, indices, commodities. Copy the pre-fetched quote data exactly, preserving all numbers. No commentary — just the data. On weekends, quotes may be omitted.

After quotes, summarize market and economic news from newsletters — earnings, central bank moves, sector performance, commodities, trade flows, economic data. Lead with items that recur across newsletters. Target 3 stories. One short paragraph per story, 1-2 sentences, bold the topic. Add source attribution in brackets.

<markets_news_example>
**TARIFF UNCERTAINTY:** Markets remain volatile as conflicting signals emerge on new trade measures, with investors closely watching upcoming policy announcements. [WSJ Markets]
</markets_news_example>

**Evening briefings — FinViz enrichment**

Evening briefings include pre-fetched FinViz data with additional market context. Include the FinViz sections *within* the 📈 MARKETS block, in this order:

1. Market headlines (top 5-6 headlines with timestamps)
2. yfinance close prices (the standard quote lines above)
3. Major movers (tickers in the news, sorted by magnitude)
4. Futures (commodities, index futures — from FinViz, supplements yfinance)
5. Forex & bonds (currencies, treasuries)
6. Economic data releases (with beat/miss indicators)
7. Upcoming earnings calendar

Copy FinViz data as-is — it is pre-formatted with 🟢/🔴 indicators and bold labels. Do not editorialize on the data. Newsletter market stories still go after this block.

## Source Attribution

For **newsletters** (news component), derive source names from the "### From:" headers:
- Domain senders: use capitalized domain name (e.g., `semafor.com` → `Semafor`)
- Email senders: use recognizable short name (e.g., `briefing@nytimes.com` → `NYT`, `markets@wsj.com` → `WSJ Markets`)

For **frontpages** (headlines component), use the source name from the "### Source Name" headers (e.g., `AP News` → `AP`, `The Guardian` → `Guardian`, `Financial Times` → `FT`, `Der Spiegel` → `Spiegel`).

If a story appears across both newsletters and frontpages, combine: `[AP, Semafor]`
Format: `[Source]` or `[Source, Source]` at the end of the paragraph

**📅 CALENDAR**

Bullet list of events with times in the user's local timezone. Bold the event name.
- **10:00 Team standup** (30 min)
- **14:00 Dentist** (1 hr, Downtown Clinic)

**✅ TODOS**

Bullet list of pending items, copied verbatim from the TODO file.

**📝 NOTES**

Bullet list of relevant agenda items or reminders from shared notes files.

**💡 REMINDER**

Only include this section if the prompt contains a "## Daily Reminder (pre-selected)" section. Copy that pre-selected reminder verbatim — do NOT generate, paraphrase, or replace it with your own text. Use italic for emphasis. If it has an attribution, keep it. If there is no pre-selected reminder in the prompt, omit this section entirely. Never read reminder files yourself.
