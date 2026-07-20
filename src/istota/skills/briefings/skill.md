---
name: briefings
triggers: [briefing, briefings, block, digest, daily briefing, morning briefing, briefing block, briefing source]
description: Manage briefing content blocks and sources (in-tree module)
cli: true
companion_skills: [untrusted_input]
env: [{"var":"BRIEFINGS_USER","from":"user_id"}]
---
# Briefings (content management)

Manage the content model of the user's briefings through the in-tree briefings module. A briefing is an ordered list of **blocks**; each block has a title, an optional synthesis directive, and 1..N **sources** that are gathered and synthesized into one coherent section at generation time. Per-user SQLite; block/source/archive data live there. Scheduling and delivery (cron, target room) are managed separately with `istota briefings schedule …` (operator) or the web settings page — this skill manages *content*.

## CLI

Run `istota-skill briefings --help` for the live list. Output is JSON.

```bash
# Blocks
istota-skill briefings blocks list --briefing Morning
istota-skill briefings blocks add --briefing Morning --title "World News" [--directive "3-5 stories, neutral"] [--render-mode synthesis|structured]
istota-skill briefings blocks set --id 3 [--title …] [--directive …] [--render-mode …] [--options '{"tone":"brief"}']
istota-skill briefings blocks reorder --briefing Morning --ids 3,1,2
istota-skill briefings blocks remove --id 3

# Sources (kind: rss|email|browse|markets|calendar|todos|reminders|notes)
istota-skill briefings sources list --block 3
istota-skill briefings sources add --block 3 --kind email --config '{"mode":"shared","lookback_hours":12}'
istota-skill briefings sources add --block 3 --kind browse --config '{"preset":"ap"}'
istota-skill briefings sources add --block 3 --kind rss --config '{"feed_ref":{"kind":"category","value":4},"limit":10}'
istota-skill briefings sources remove --id 7

# Archive (past rendered briefings)
istota-skill briefings archive list [--briefing Morning] [--limit 20]
istota-skill briefings archive show --id 42
```

## Source kinds

- **email** — shared/unowned newsletter pool (`{"mode":"shared"}`) or a sender allowlist (`{"mode":"senders","senders":["*@semafor.com"]}`).
- **rss** — recent entries from a Feeds subscription/category (`feed_ref`).
- **browse** — a frontpage URL, by preset (`{"preset":"ap"}`) or custom (`{"url":"https://…"}`).
- **markets** / **calendar** — structured built-ins (rendered verbatim; use `--render-mode structured`).
- **todos** / **reminders** / **notes** — workspace convention files (override with `{"path":"…"}`).

A briefing with no blocks falls back to the legacy component-based generation; adding blocks switches it to the synthesized-block path.
