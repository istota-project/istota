# Skills index

All built-in skills shipped with Istota. Skills marked "always" are loaded for every task. Skills marked "doc-only" provide reference documentation without a CLI module.

## Always included

| Skill | Description |
|---|---|
| `files` | Nextcloud file operations (mount-aware, rclone fallback) |
| `sensitive_actions` | Confirmation rules for destructive operations |
| `memory` | Memory file reference (USER.md, CHANNEL.md, dated memories) |
| `scripts` | User's reusable Python scripts |
| `memory_search` | Memory search CLI (search, index, reindex, stats, facts, timeline, add-fact, invalidate, delete-fact) |
| `kv` | Key-value store for persistent runtime state |

## Communication

| Skill | Keywords | CLI |
|---|---|---|
| `email` | email, mail, send, inbox, reply, message | yes -- send, output |
| `nextcloud` | share, sharing, nextcloud, permission, access | yes -- share list/create/delete/search |

## Productivity

| Skill | Keywords | CLI |
|---|---|---|
| `calendar` | calendar, event, meeting, schedule, appointment | yes -- list, create, update, delete |
| `todos` | todo, task, checklist, reminder, done, complete | doc-only |
| `reminders` | remind, reminder, alert me, notify me | doc-only |
| `schedules` | schedule, recurring, cron, daily, weekly | doc-only |
| `tasks` | subtask, queue, background, later | doc-only (admin-only) |
| `bookmarks` | bookmark, karakeep, save, read later | yes -- search, list, add, tags, etc. |

## Information

| Skill | Keywords | CLI |
|---|---|---|
| `briefing` | (auto-selected for briefing source type) | doc-only |
| `briefings_config` | briefing config, briefing schedule | doc-only |
| `markets` | market, stock, ticker, index, futures | yes -- quote, summary, finviz |
| `feeds` | feed, rss, subscribe, unsubscribe | yes -- list, add, remove, entries |
| `browse` | browse, website, scrape, screenshot, url | yes -- get, screenshot, extract, interact |

## Media

| Skill | Keywords | CLI |
|---|---|---|
| `transcribe` | transcribe, ocr, screenshot, scan | yes -- OCR via Tesseract |
| `whisper` | transcribe, whisper, audio, voice, speech | yes -- audio transcription via faster-whisper |

## Development

| Skill | Keywords | CLI |
|---|---|---|
| `developer` | git, gitlab, repo, commit, branch, MR, PR | doc-only (env setup via hook) |

## Accounting

| Skill | Keywords | CLI | Notes |
|---|---|---|---|
| `moneyman` | accounting, ledger, beancount, invoice, expense | yes -- Moneyman API client | Requires `moneyman` resource |

## Google Workspace

| Skill | Keywords | CLI |
|---|---|---|
| `google_workspace` | google drive, google docs, google sheets, google calendar, google chat, spreadsheet, gws | doc-only (uses `gws` CLI via Bash) |

Requires OAuth connection via the [web dashboard](../features/google-workspace.md). Token injected via `setup_env()` hook.

## Location

| Skill | Keywords | CLI |
|---|---|---|
| `location` | location, gps, where, place, tracking | yes -- current, history, places, learn, etc. |

## Monitoring

| Skill | Keywords | CLI |
|---|---|---|
| `heartbeat` | heartbeat, monitoring, health check, alert | doc-only |

## Web

| Skill | Keywords | CLI |
|---|---|---|
| `website` | website, site, publish, blog | doc-only |

## Selection triggers

Skills are selected through a two-pass system:

1. **Pass 1** (keyword matching): `keywords` in prompt, `resource_types` match, `source_types` match, `file_types` match, `always_include`, `companion_skills`
2. **Pass 2** (semantic routing): LLM-based classification catches implied needs that keywords miss

See [skills](../features/skills.md) for details on the selection system.

## Checking availability

In Talk, use `!skills` to see which skills are available, unavailable (missing dependencies), or disabled for your user. Use `!skills <name>` for details on a specific skill.
