# Briefings

Scheduled summaries delivered to Talk and/or email. A morning briefing might include today's calendar events, market futures, headline news, and pending todos.

## Content model: blocks

Blocks are the sole content model. A briefing is an ordered list of **blocks**, each with a title, an optional synthesis directive, a render mode (`synthesis` / `structured`), and 1..N **sources**. Each block is gathered and synthesized into one section at generation time.

Source kinds (`kind = …`):

| Kind | What it gathers |
|---|---|
| `rss` | A Feeds subscription or category (needs the Feeds module) |
| `email` | Shared-pool newsletters, windowed and owner-filtered |
| `browse` | A bundled preset frontpage or an arbitrary URL (needs the browser) |
| `markets` | Futures / indices via yfinance |
| `calendar` | Today's / tomorrow's events |
| `todos` | Pending todo items from a workspace file, grouped by section |
| `reminders` | A random reminder |
| `notes` | Recent notes |
| `shared_block` | Pre-made curated content from the shared KV store (see [Shared curated content](#shared-curated-content)) |

> The legacy boolean-`components` content model (and its `[briefing_defaults]` expansion) is retired. Existing component-based briefings migrate once into blocks on first touch of the briefings module DB.

## Configuration sources

Schedule + delivery are loaded from (higher precedence wins):

1. `briefing_configs` DB table — provisioned via `istota briefings schedule ensure` (Ansible) or the web UI (briefings tab → settings); `enabled=0` mutes a row without scheduling.
2. `[[users.alice.briefings]]` in main `config/config.toml` (name/cron/title/output; DB rows win by name).

Content **blocks** are seeded once into the per-user briefings module DB from config-authored `[[users.X.briefings.blocks]]` and are then edited in the web block editor.

## Default briefings

A canonical shared set defined once in the top-level `[[default_briefings]]` section is seeded by name into each opted-in user (per-user `default_briefings` flag, default on; an explicit user briefing of the same name wins). Seeding is one-time — later web edits win and re-runs never clobber them. Opt a user out with `istota user ensure --no-default-briefings`.

## Memory isolation

Briefing prompts intentionally exclude USER.md and dated memories. This prevents private context from leaking into what may be shared or newsletter-style output.

## Example config (TOML)

```toml
[[users.alice.briefings]]
name = "morning"
cron = "0 6 * * *"               # 6am in user's timezone
conversation_token = "room123"    # Talk room to post to
title = "Morning Briefing"        # optional; blank derives it from the name
output = "both"                   # "talk", "email", or "both"

  [[users.alice.briefings.blocks]]
  title = "Today"
  render_mode = "structured"

    [[users.alice.briefings.blocks.sources]]
    kind = "calendar"
    config = {}

    [[users.alice.briefings.blocks.sources]]
    kind = "todos"
    config = {}

  [[users.alice.briefings.blocks]]
  title = "World News"
  render_mode = "synthesis"

    [[users.alice.briefings.blocks.sources]]
    kind = "browse"
    config = { preset = "ap" }
```

## Shared curated content

Some content is the same for everyone — world headlines, a markets summary, a newsletter digest. Without sharing, every user's briefing fetches the frontpage and pays a synthesis pass again, so at 7am N users each independently do the same expensive work. The **shared curated content** plane decouples that: expensive shared generation runs a handful of times total, and each per-user briefing reads the pre-made artifact.

It has two layers over one storage substrate — the **shared KV store** (`shared_kv`: cross-user, namespaced JSON; reads open to all, writes admin-only).

### Module-owned shared blocks (batteries-included — the default)

The briefings module ships **shared blocks**: one-block briefings it schedules and generates *once globally*, writing the rendered section into `shared_kv`. A fresh deploy ships a canonical set (`world-headlines` via browse presets, `markets-summary` via the markets source) and the default morning briefing references `world-headlines` — so the N-way frontpage fetch + N-way synthesis collapses to **one generation total**, with no operator scripting.

A shared block is defined at the instance level (top-level `[[briefing_shared_blocks]]`, Ansible `istota_briefing_shared_blocks`):

```toml
[[briefing_shared_blocks]]
name = "world-headlines"          # → shared_kv key in namespace "briefing_shared_blocks"
cron = "0 6 * * *"                # generated before the 07:00 briefing window; UTC (global)
title = "🌍 World headlines"
directive = "Synthesize the frontpages into ~8 top world stories."
render_mode = "synthesis"
enabled = true

  [[briefing_shared_blocks.sources]]
  kind = "browse"
  config = { preset = "ap" }
  [[briefing_shared_blocks.sources]]
  kind = "browse"
  config = { preset = "reuters" }
```

- Generation runs off the dispatch thread (sleep-cycle-style: one non-streaming, no-sandbox model call) under the reserved `__system__` identity, so it needs **no admin configured** and never blocks task processing.
- Only **user-agnostic** source kinds are allowed: `browse`, `markets`, and `email` (the shared/unowned pool). `rss` (needs a real feeds user), the personal built-ins (`calendar`/`todos`/`reminders`/`notes`), and `shared_block` (no chaining) are dropped with a warning.
- If every source fails/empties (a transient AP/Reuters outage), the write is **skipped** and the prior value kept (last-known-good) rather than blanking the section.
- Omit or set `enabled = false` to disable one; an explicit empty `[[briefing_shared_blocks]]` list opts out entirely.

A briefing consumes a shared block with a `shared_block` source:

```toml
  [[users.alice.briefings.blocks]]
  title = "🌍 World headlines"
  render_mode = "structured"

    [[users.alice.briefings.blocks.sources]]
    kind = "shared_block"
    config = { name = "world-headlines", max_age_hours = 12 }
```

`max_age_hours` is a **freshness window** independent of the block's own generation cron: if the shared block hasn't regenerated in time, the consuming section is simply omitted (fail-soft), never blocked-on. Omit it for no freshness check.

### External-script curation (the escape hatch)

For content the module doesn't ship a generator for, write into the shared-block namespace (`briefing_shared_blocks`) directly from an admin CRON `command:` job (running as an admin user):

```bash
# CRON.md job body — fetch/build content, then publish to the shared store.
istota-skill kv set --shared briefing_shared_blocks my-digest '{"text": "…section text…"}'
# or, to share raw items for per-user synthesis:
istota-skill kv set --shared briefing_shared_blocks my-digest '{"items": [{"title": "…", "url": "…"}]}'
```

Then a briefing reads it via a `shared_block` source (the key becomes the block name, and it shows up in the web editor's shared-block picker as a "custom" entry):

```toml
    [[users.alice.briefings.blocks.sources]]
    kind = "shared_block"
    config = { name = "my-digest", max_age_hours = 12 }
```

A full copy-paste example lives at [`scripts/examples/shared_kv_curation.sh`](https://github.com/istota-project/istota/blob/main/scripts/examples/shared_kv_curation.sh).

### Value shape controls granularity

The **writer** chooses whether the fetch or the synthesis is shared, via the stored JSON shape:

- `{"items": [{"title", "summary", "url"}, …]}` (or a bare JSON list) → each reader's block **synthesizes** the items (share the fetch, not the prose).
- `{"text": "…"}` (or a bare JSON string) → the section text is **spliced** near-verbatim into a `structured` block (share the synthesis too).

### Authorization + trust

- **Writes are admin-only** (the `/etc/istota/admins` allowlist), **fail-closed**: on a deployment with a *blank* admins file, no user can write shared content (only module-owned generation, a trusted daemon write, still works). Reads are open to any user.
- Shared content is **wrapped untrusted by default** (a trusted identity typically relayed web-sourced content), matching the `browse`/`email` posture. Trust is a property of the stored value — set `"trusted": true` in the published JSON to skip the wrap for content you control. A consuming user can never flip it.

## Reading briefings (web)

The briefings tab is a reader for generated briefings, with an archive sidebar for past results (filterable by briefing name from the header, each result deletable from its kebab menu) and a settings page behind the cog for editing blocks, sources, schedule and delivery. Source paths use a searching file picker with an advisory existence check — advisory rather than blocking, because the path resolver is fail-soft, so a not-yet-created file must not trap the editor. Admins additionally get a "Shared blocks" card for the module-owned blocks.

Each generated briefing is archived on delivery and pruned by `[briefings] archive_retention_days` (default 90) on insert, alongside the manual per-result delete.

## Output format

The model returns structured JSON — `{"body": "..."}`, body only. The scheduler parses it and handles delivery deterministically. The **title is not the model's to choose**: it is composed from the briefing's configured `title` (blank derives it from the name, so `morning` becomes "Morning Briefing") plus the run date in the user's timezone, and the archive entry, the email subject and the ntfy push title all call the same function, so they cannot disagree about the same run. A stray `subject` from an in-flight run is tolerated and ignored. The email skill is excluded from briefing tasks via `exclude_skills` to prevent the model from sending emails directly.

## Reading a todo file

The `todos` source keeps each item's place in the file rather than flattening it. That is what makes a directive like "only cover what's under NOW" followable instead of merely ignorable — previously the headings were stripped before the briefing was written, so every item arrived as one undifferentiated list.

Sections are detected in **one dialect per file**, chosen from what the file actually uses, in the order ATX (`### NOW`) → setext (an underlined title) → bold (`**NOW**`) → label (`NOW:`). One dialect rather than all at once, because a stray `Blockers:` line in an ATX file would otherwise steal the items sitting under `### NOW`. Detection is strict and labelling lenient: `#tags` is not enough to establish an ATX file, but inside one, `#NOW` still labels. Items are matched before headings, so a bullet reading `- NOW:` stays a bullet. A leading YAML frontmatter block is dropped, but only when it really is one — a note that opens with a `---` rule keeps its content, whereas a notes-app header block was previously read as todos and led the list with the file's own tags.

Checkboxes (`- [ ]`), bullets and numbered items all count; `- [x]` is done and excluded. Section membership is flat — the nearest heading above the item, at any level, not a breadcrumb. The rendered block labels each group with a plain `Section:` line rather than a markdown heading, since the block prompt forbids headings in output.

The size cap is `[briefings] max_source_chars`, spent item by item: an item that doesn't fit is dropped whole along with everything after it, and the provenance line says how many were left out. Never mid-item — half a todo reads as a todo saying something your file doesn't. The first item is always kept.

## Scheduling

Cron expressions are evaluated in each user's timezone. The scheduler checks for due briefings every `briefing_check_interval` (60s) and tracks the last run per briefing in the `briefing_state` table.
