---
name: briefing
description: Briefing formatting guidelines for chat messages
exclude_memory: true
exclude_persona: true
source_types: [briefing]
exclude_skills: [email]
---
Briefings must be returned as a JSON object: `{"subject": "Morning Briefing", "body": "<content>"}`. The body contains the full briefing text with emoji section headers, using `\n` for newlines. Do not output anything outside the JSON object. Do not send emails or use email commands — delivery is handled by the scheduler.

The body is formatted for chat messages (markdown). Use emoji-prefixed labels as section headers. Only include sections that have data.

## Structure — follow the prompt

The prompt presents the briefing's content grouped into **blocks**, each with a title and its gathered source data (tagged by provenance). Produce **one section per block**, titled with the block's title as an emoji-prefixed header, in the exact order the blocks appear. Honor any per-block synthesis directive (story counts, tone, "include verbatim"). Omit a block that has no content — never emit an empty header. Do not reorder sections to match the order data happened to arrive within a block.

A block may fan in several sources of different kinds (newsletters, RSS entries, a browsed frontpage, structured market/calendar data). **Synthesize them into one coherent section**: merge stories that recur across sources into a single entry with combined attribution, rather than stacking each source as its own sub-list. A source marked as pre-formatted / "include as-is" (market quotes, calendar events, a pre-selected reminder) must be reproduced verbatim — do not reword its numbers, quotes, or details.

## Allowed Markdown

- **bold** for emphasis
- *italic* for secondary emphasis
- [links](url) for URLs
- --- horizontal rule before a reminder section
- Bullet points for lists

## Forbidden

- Tables
- Markdown headings (#, ##, etc.)
- Code blocks (unless showing actual code)
- Nested bullet points
- Commentary or editorializing on market data

## Section formatting reference

Apply the formatting below by content type — a block's title names the section; its sources' content determines which of these patterns fits.

**News** (newsletters + frontpages)

General news — politics, world events, policy, science, tech (non-market). Keep a global perspective. Lead with items that recur across multiple sources. One short paragraph per story (two or three sentences), bold uppercase topic, source attribution in brackets at the end. Place a story by topic — tariff *policy* is news, its *market impact* is a markets item.

Link each source in the attribution to the specific article it came from **when the prompt gives that source an article URL** (RSS/feed items carry one, shown as `[article: <url>]` after the item). Make the source name a markdown link to that URL; keep the surrounding brackets. Use a plain-text source name when no URL is available (frontpages and newsletters usually have none). Never invent, guess, or reuse a URL for a source that didn't provide one — a plain `[Source]` is correct there.

<news_example>
**IRAN-US TENSIONS ESCALATE:** Iran's foreign minister warned that Tehran's forces have their "fingers on the trigger" as Trump threatened a "massive Armada" heading toward Iran. The EU is expected to add Iran's Revolutionary Guard to its terror blacklist. [[Semafor](https://www.semafor.com/article/iran-us-tensions), NYT]
</news_example>

Here Semafor's item carried an article URL so its name links to it; NYT (a newsletter with no per-article URL) stays plain text. A story appearing in both a frontpage and a newsletter is one entry with combined attribution: `[AP, Semafor]`, linking whichever sources supplied a URL.

**Markets**

One line per quote with a 🟢/🔴/⚪ indicator based on change direction, bold the name:
🟢 **S&P 500 E-mini**: 6,104.75 (+30.25, +0.50%)
🔴 **Nasdaq 100 E-mini**: 21,857.50 (-45.00, -0.21%)
Copy the pre-fetched quote data exactly, preserving all numbers. No commentary. On weekends, quotes may be omitted. After quotes, summarize market/economic news (earnings, central-bank moves, sectors, commodities, data) — short paragraphs, bold topic, bracketed attribution. Evening briefings include pre-formatted FinViz sections (headlines, movers, futures, forex/bonds, economic data, earnings) — copy them as-is, in that order, within the markets section.

**Calendar**

Bullet list of events with times in the user's local timezone. Bold the event name.
- **10:00 Team standup** (30 min)
- **14:00 Dentist** (1 hr, Downtown Clinic)

**Todos**

Bullet list of pending items, copied verbatim.

**Notes**

Bullet list of relevant agenda items or reminders from the notes content.

**Reminder**

Copy any pre-selected reminder in the prompt verbatim — do NOT generate, paraphrase, or replace it. Use italic for emphasis; keep any attribution. Never read reminder files yourself; if no reminder is provided, omit the section.

## Source attribution

Derive source names from the provenance tags / "From:" headers: domain senders use the capitalized domain (`semafor.com` → `Semafor`), email senders a recognizable short name (`briefing@nytimes.com` → `NYT`, `markets@wsj.com` → `WSJ Markets`), frontpages the source name (`AP News` → `AP`, `Financial Times` → `FT`). Format: `[Source]` or `[Source, Source]` at the end of the paragraph.

When a story is drawn from an item that carried an article URL (`[article: <url>]` in the prompt), make that source name a markdown link to the URL: `[[Semafor](https://…)]`. Link only the sources that supplied a URL — leave the rest plain text, and never fabricate a URL. Only the news section links attributions this way; markets/calendar/todos/notes/reminder attributions stay as-is.
