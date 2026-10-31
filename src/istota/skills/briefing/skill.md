---
name: briefing
description: Briefing formatting guidelines for chat messages
exclude_memory: true
exclude_persona: true
source_types: [briefing]
exclude_skills: [email]
egress: local
---
Briefings must be returned as a JSON object: `{"version": 2, "blocks": [{"block_id": 42, "body_md": "<content>"}]}`. Return each Block ID from the prompt exactly once, as an integer, with its complete Markdown content in `body_md`. Use `\n` for newlines inside strings. Do not output anything outside the JSON object. Do not write block titles, an edition title or a subject line; the delivery layer supplies them. Do not send emails or use email commands. Delivery is handled by the scheduler.

## Structure — follow the prompt

The prompt groups the briefing's content into blocks, each with a numeric Block ID, a title and gathered source data tagged by provenance. Produce one body per requested ID. Keep bodies in prompt order and honor each block's synthesis directive, including story counts, tone and verbatim content. When a requested block has nothing useful to include, return its ID with an empty `body_md` string. Do not omit requested IDs or invent new ones.

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

Apply the formatting below by content type — the delivery layer supplies the block's title; its sources' content determines which of these patterns fits.

**News** (newsletters + frontpages)

General news — politics, world events, policy, science, tech (non-market). Keep a global perspective. Lead with items that recur across multiple sources. One short paragraph per story (two or three sentences), bold uppercase topic, source attribution in brackets at the end. Place a story by topic — tariff *policy* is news, its *market impact* is a markets item.

Link each source in the attribution to the specific article it came from **when the prompt gives that source an article URL**. Two shapes carry one: an RSS/feed item ends in `[article: <url>]`, and a newsletter body has its links inline as markdown `[text](url)`. Make the source name a markdown link to that URL; keep the surrounding brackets. In a newsletter, use the link on the story you're summarizing — not the newsletter's home page, web-version, or "read more from us" link; if a story has no link of its own, leave that source plain. Use a plain-text source name when no URL is available (browsed frontpages have none). Never invent, guess, or reuse a URL for a source that didn't provide one — a plain `[Source]` is correct there.

<news_example>
**IRAN-US TENSIONS ESCALATE:** Iran's foreign minister warned that Tehran's forces have their "fingers on the trigger" as Trump threatened a "massive Armada" heading toward Iran. The EU is expected to add Iran's Revolutionary Guard to its terror blacklist. [[Semafor](https://www.semafor.com/article/iran-us-tensions), NYT]
</news_example>

Here Semafor's item carried an article URL so its name links to it; NYT stays plain text because its newsletter carried no link for that story. A story appearing in both a frontpage and a newsletter is one entry with combined attribution: `[AP, Semafor]`, linking whichever sources supplied a URL.

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

When a story is drawn from an item that carried an article URL — `[article: <url>]` after an RSS/feed item, or an inline `[text](url)` link in a newsletter body — make that source name a markdown link to the URL: `[[Semafor](https://…)]`. For a newsletter, take the link belonging to that story; ignore its unsubscribe / web-version / social links. Link only the sources that supplied a URL — leave the rest plain text, and never fabricate a URL. Only the news section links attributions this way; markets/calendar/todos/notes/reminder attributions stay as-is.
