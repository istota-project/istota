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
| `feeds` | feed, rss, subscribe, unsubscribe, opml | yes -- list, categories, entries, add, remove, refresh, poll, run-scheduled, import-opml, export-opml, star, starred, mark-read |
| `browse` | browse, website, scrape, screenshot, url | yes -- get, screenshot, extract, interact |

## Media

| Skill | Keywords | CLI |
|---|---|---|
| `transcribe` | transcribe, ocr, screenshot, scan, image | yes -- OCR via Tesseract |
| `whisper` | transcribe, whisper, audio, voice, speech | yes -- audio transcription via faster-whisper |
| `notes` | note, save, write, markdown | doc-only (companion to transcribe) |

## Development

| Skill | Keywords | CLI |
|---|---|---|
| `developer` | git, gitlab, repo, commit, branch, MR, PR | doc-only (env setup via hook) |

## Accounting

| Skill | Keywords | CLI | Notes |
|---|---|---|---|
| `money` | accounting, ledger, beancount, invoice, expense, money | yes -- in-process accounting (ledger, invoicing, transactions, work log) | Requires `money` resource |

## Google Workspace

| Skill | Keywords | CLI |
|---|---|---|
| `google_workspace` | google drive, google docs, google sheets, google calendar, google chat, spreadsheet, gws | doc-only (uses `gws` CLI via Bash) |

Requires OAuth connection via the [web dashboard](../features/google-workspace.md). Token injected via `setup_env()` hook.

## Location

| Skill | Keywords | CLI |
|---|---|---|
| `location` | location, gps, where, place, tracking | yes -- current, history, places, learn, etc. |

## Health

| Skill | Keywords | CLI |
|---|---|---|
| `health` | health, weight, bloodwork, labs, biomarker, panel, blood pressure | yes -- log, stats, latest, panels, add-panel, add-biomarker, trend, upload, import-csv, export-csv, summary, settings, set, encounters, add-encounter, diagnoses, add-diagnosis, history-summary, immunizations, add-immunization, vaccine-refs, coverage, garmin-status, garmin-sync, garmin-disconnect |

Requires the `health` module to be enabled (on by default).

## Infrastructure

| Skill | Keywords | CLI |
|---|---|---|
| `devbox` | devbox, install package, pip install, compile, dig, nslookup, traceroute, network diagnostic | yes -- exec, cp-in, cp-out, status, rebuild |

## Specs

| Skill | Keywords | CLI |
|---|---|---|
| `spec` | spec, draft spec, design doc, implementation plan | doc-only |

Codifies a spec-driven development workflow. Specs live in `{notes_folder}/Specs/{Drafts,Active,Done}/` by default, or in a named project's folder. Supports drafting, starting, marking done, listing, showing, and editing specs. See the skill body for the full lifecycle and conventions.

## Monitoring

| Skill | Keywords | CLI |
|---|---|---|
| `heartbeat` | heartbeat, monitoring, health check, alert | doc-only |

## Web

| Skill | Keywords | CLI |
|---|---|---|
| `website` | website, site, publish, blog | doc-only |

## Safety

| Skill | Keywords | CLI |
|---|---|---|
| `untrusted_input` | (none — never selected directly) | doc-only |

`untrusted_input` is a doc-only companion skill with no triggers. It loads via `companion_skills` declarations on the seven ingest-shaped skills (`email`, `browse`, `calendar`, `transcribe`, `whisper`, `feeds`, `bookmarks`), so its inbound-content security rules ride along whenever a task processes content from outside the trust boundary. It pairs with `sensitive_actions` (outbound rules there, inbound-reading rules here).

## Selection triggers

Skills are selected by a single deterministic pass: `keywords` in prompt, `resource_types` match, `source_types` match, `file_types` match, `always_include`, `companion_skills`. Progressive disclosure then widens the on-demand index to the full eligible catalogue so the model can load any relevant skill that keyword matching didn't surface.

See [skills](../features/skills.md) for details on the selection system.

## Checking availability

Use `!skills` (in Talk or web chat) to see which skills are available, unavailable (missing dependencies), or disabled for your user. Use `!skills <name>` for details on a specific skill.
