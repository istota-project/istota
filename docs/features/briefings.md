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

1. `briefing_configs` DB table — provisioned via `istota briefings schedule ensure` or the web UI (briefings tab → settings); `enabled=0` mutes a row without scheduling.
2. `[[users.alice.briefings]]` in main `config/config.toml` (name/cron/title/output; DB rows win by name).

Those two are the whole surface. A user's workspace `{bot_dir}/config/BRIEFINGS.md` was a third source, applied over both at read time; it is retired, and on first start after the upgrade whatever it held is imported into `briefing_configs`, once per user. An existing file is left on disk and is inert.

Content **blocks** are seeded once into the per-user briefings module DB from config-authored `[[users.X.briefings.blocks]]` and are then edited in the web block editor.

Because a name can come from either source, or from blocks with no schedule at all, `istota briefings list` (and `istota-skill briefings list`) is what answers "which briefings does this user actually have" — every effective name after defaults and disabled rows are applied, with each one's block count and its latest archived generation. Asking for a name that is not on that list now returns the valid options rather than an empty briefing: a zero-row block query used to read as "configured, no content", so a typo or a case mismatch produced a confident and wrong success (ISSUE-328).

## Building a briefing

Open Briefings → Settings and choose a briefing. Its schedule, destinations and ordered blocks appear together. Disabled schedules remain available for editing, including briefings with no blocks yet. Use Edit schedule to change when and where it runs; operator-managed schedules remain read-only.

Add block opens a searchable preset list for feeds, websites, newsletters, calendar, markets, tasks, notes, reminders, shared content or a custom block. Presets use the existing sources. Feed presets need Feeds enabled with a subscription or category; shared content needs an available shared block. Choose the source's feed, website, file or shared-block name before saving.

The block editor keeps a local draft of the title, source list and width. Automatic uses one column, Wide uses two where space allows, and Full width spans the reader pane. All widths become one column on a narrow pane. Advanced contains the directive, generation mode, story count and tone. Save block writes the block and all its sources together. Cancel discards the draft after confirmation when it has changes; a failed save keeps it open. The app bar's Save continues to control the separate HTML email preference.

Each block's actions include Edit, Move up, Move down, Duplicate and Delete. Moves work with the keyboard or touch and keep reading order. Duplication copies every source, including disabled ones. Deleting or changing a block affects future editions; existing editions remain available. Admins can still manage and run shared blocks in their own card.

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
output = "talk,email"             # one destination or a comma-separated list

  [[users.alice.briefings.blocks]]
  title = "Today"
  render_mode = "structured"
  options = { presentation = { width = "wide" } }

    [[users.alice.briefings.blocks.sources]]
    kind = "calendar"
    config = {}

    [[users.alice.briefings.blocks.sources]]
    kind = "todos"
    config = { path = "shared/tasks.md" }

  [[users.alice.briefings.blocks]]
  title = "World News"
  render_mode = "synthesis"
  options = { story_count = 5, tone = "neutral", presentation = { width = "full" } }

    [[users.alice.briefings.blocks.sources]]
    kind = "browse"
    config = { preset = "ap" }
```

Width lives in `options.presentation.width` and accepts `auto`, `wide` or `full`, with `auto` as the default. The editor preserves other option fields. Config-authored blocks seed once; later config edits do not overwrite a user's saved block changes.

## Shared curated content

Some content is the same for everyone — world headlines, a markets summary, a newsletter digest. Without sharing, every user's briefing fetches the frontpage and pays a synthesis pass again, so at 7am N users each independently do the same expensive work. The **shared curated content** plane decouples that: expensive shared generation runs a handful of times total, and each per-user briefing reads the pre-made artifact.

It has two layers over one storage substrate — the **shared KV store** (`shared_kv`: cross-user, namespaced JSON; reads open to all, writes admin-only).

### Module-owned shared blocks (batteries-included — the default)

The briefings module ships **shared blocks**: one-block briefings it schedules and generates *once globally*, writing the rendered section into `shared_kv`. A fresh deploy ships a canonical set (`world-headlines` via browse presets, `markets-summary` via the markets source) and the default morning briefing references `world-headlines` — so the N-way frontpage fetch + N-way synthesis collapses to **one generation total**, with no operator scripting.

A shared block is defined at the instance level (top-level `[[briefing_shared_blocks]]`):

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

- **Writes are admin-only** (the admins file), **fail-closed**: on a deployment with a *blank* admins file, no user can write shared content (only module-owned generation, a trusted daemon write, still works). Reads are open to any user.
- Shared content is **wrapped untrusted by default** (a trusted identity typically relayed web-sourced content), matching the `browse`/`email` posture. Trust is a property of the stored value — set `"trusted": true` in the published JSON to skip the wrap for content you control. A consuming user can never flip it.

## Reading briefings (web)

The briefings tab is a reader for generated briefings, with an archive sidebar for past results (filterable by briefing name from the header, each result deletable from its kebab menu) and a settings page behind the cog for editing blocks, sources, schedule and delivery. Source paths use a searching file picker with an advisory existence check — advisory rather than blocking, because the path resolver is fail-soft, so a not-yet-created file must not trap the editor. Admins additionally get a "Shared blocks" card for the module-owned blocks.

Selecting an archived briefing or changing the briefing-name filter adds a browser history entry. Back and Forward restore the selection, and reloading or sharing the URL opens the same briefing and filter. Returning from settings keeps the selection. If a briefing or filter is no longer available, the reader falls back to the first available briefing or All and updates the current URL without adding an entry.

A generated result is an edition. New editions show one card per populated block, in the saved order, with the edition title and generation date above them. Cards use one, two or three columns according to the space available beside the archive. Automatic blocks take one column, Wide blocks take two where space allows, and Full width blocks take the whole row. At narrow widths every block takes one column. Layout and title changes affect future editions; an archived edition keeps its saved content and layout.

Cards preview the first three complete Markdown blocks, with at most five top-level items in each list. Paragraphs, links and nested items remain whole. When content is omitted, **Read full block** opens the complete block in a viewer. Article links work directly from the card. **Some sources unavailable** means the block includes useful content despite a source failure; internal source errors are never shown in the card.

Older editions without a structured document stay readable in a single full-width card. Unsupported or damaged documents also use their complete stored Markdown body. Opening an edition reads the archive without fetching sources or generating content. Changing the selection shows a loading state until that edition arrives, so the previous edition cannot appear under the new selection.

Each generated briefing is archived on delivery and pruned by `[briefings] archive_retention_days` (default 90) on insert, alongside the manual per-result delete.

## Output format

Generation makes one source gather and one model call for the edition. Before the call, the worker saves a protected manifest with the edition title, ordered numeric block IDs, block titles, render modes, presentation widths and source status. Duplicate titles remain distinct. Blocks with no gathered content are omitted from the request; failed sources keep their provenance outside the reader-facing document.

The model returns `{"version": 2, "blocks": [{"block_id": 42, "body_md": "..."}]}`. Each requested ID must appear exactly once. An empty string explicitly omits a block whose gathered content produced nothing useful. The model supplies only block bodies; titles, order, presentation and source status come from the manifest. The edition title uses the configured title (blank derives it from the briefing name) and the task's creation date in the user's timezone.

Before marking the task completed, indexing its answer or delivering it, the scheduler validates the response against that manifest. Missing, duplicate or unknown IDs, wrong types and unsupported versions fail through the normal quiet briefing retry path. An edition with no useful blocks fails with `briefing_no_content` rather than publishing an empty edition. Useful blocks with failed sources retain a partial-source status.

The completed task stores a canonical envelope with `body` and `briefing_document`. The archive stores the same immutable document and its deterministic linear Markdown projection. Editing or deleting a definition during generation cannot change that edition's titles, content, order or layout. Chat, digest and existing text destinations use the projection; email and ntfy subjects use the saved edition title even after archive cleanup removes the manifest file. An archive write failure is logged and does not repeat generation or block delivery.

Email uses the same complete ordered blocks as the archive. HTML mail stacks sections under escaped headings with no added styling. Plain mail uses section labels and preserves article destinations as `Label (URL)`, or one URL when it is already the label. A partial-source block includes a short status note in both formats. The existing HTML preference selects multipart alternatives or a single plain part; an HTML rendering failure also sends the full plain edition. Delivery uses the document retained at completion, so archive cleanup cannot remove it.

Old body-only tasks retain their linear path without inferred block boundaries. The parser still tolerates JSON fences and wrappers. The email skill stays excluded from briefing tasks so delivery remains scheduler-owned.

## Reading a todo file

The `todos` source keeps each item's place in the file rather than flattening it. That is what makes a directive like "only cover what's under NOW" followable instead of merely ignorable — previously the headings were stripped before the briefing was written, so every item arrived as one undifferentiated list.

Sections are detected in **one dialect per file**, chosen from what the file actually uses, in the order ATX (`### NOW`) → setext (an underlined title) → bold (`**NOW**`) → label (`NOW:`). One dialect rather than all at once, because a stray `Blockers:` line in an ATX file would otherwise steal the items sitting under `### NOW`. Detection is strict and labelling lenient: `#tags` is not enough to establish an ATX file, but inside one, `#NOW` still labels. Items are matched before headings, so a bullet reading `- NOW:` stays a bullet. A leading YAML frontmatter block is dropped, but only when it really is one — a note that opens with a `---` rule keeps its content, whereas a notes-app header block was previously read as todos and led the list with the file's own tags.

Checkboxes (`- [ ]`), bullets and numbered items all count; `- [x]` is done and excluded. Section membership is flat — the nearest heading above the item, at any level, not a breadcrumb. The rendered block labels each group with a plain `Section:` line rather than a markdown heading, since the block prompt forbids headings in output.

The size cap is `[briefings] max_source_chars`, spent item by item: an item that doesn't fit is dropped whole along with everything after it, and the provenance line says how many were left out. Never mid-item — half a todo reads as a todo saying something your file doesn't. The first item is always kept.

## Scheduling

Cron expressions are evaluated in each user's timezone. The scheduler checks for due briefings every `briefing_check_interval` (60s) and tracks the last run per briefing in the `briefing_state` table.
