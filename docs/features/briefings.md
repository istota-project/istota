# Briefings

Briefings can be personal or group-owned. A personal briefing gathers content for one person. A group briefing generates one common edition and sends that saved edition to the group’s eligible members. Open Briefings and choose New briefing to select Just me or a group you can author for.

## Content model: blocks

Personal briefings use blocks. A group briefing can generate from a prompt or use an ordered list of blocks. Each block has a title, an optional synthesis directive, a render mode (`synthesis` / `structured`), and 1..N **sources**. Each block is gathered and synthesized into one section at generation time.

Source kinds (`kind = …`):

| Kind | What it gathers |
|---|---|
| `rss` | A Feeds subscription or category (needs the Feeds module) |
| `email` | Shared-pool newsletters, windowed and owner-filtered |
| `my_email` | Your own mail from one connected account: your bot plus-address, or Gmail through your Google connection. Personal briefings only |
| `browse` | A bundled preset frontpage or an arbitrary URL (needs the browser) |
| `markets` | Futures / indices via yfinance |
| `calendar` | Today's / tomorrow's events |
| `todos` | Pending todo items from a workspace file, grouped by section |
| `reminders` | A random reminder |
| `notes` | Recent notes |
| `shared_block` | Pre-made curated content from the shared KV store (see [Shared curated content](#shared-curated-content)) |

> The legacy boolean-`components` content model (and its `[briefing_defaults]` expansion) is retired. Existing component-based briefings migrate once into blocks on first touch of the briefings module DB.

## Personal configuration sources

Schedule and delivery come from one place, the `briefing_configs` table. Rows are written by the web UI (Briefings → Settings), by `istota briefings schedule ensure`, and by a settings plan (`istota apply`). `enabled=0` mutes a row without scheduling it. Each row belongs to its user, who can edit, disable and delete it.

`[[users.alice.briefings]]` in `config/config.toml` is retired. On the first start after the upgrade, each entry is imported into `briefing_configs` once per user: an entry whose name already has a row is skipped, and an entry with no `cron` is imported disabled. After that the file is not read for briefings again, so a briefing deleted in the settings page stays deleted. Content blocks written under an entry are copied into the briefing's content at the same start, so nothing is lost when the block is removed. Until it is removed from the file, loading the config reports it as a retired key. A user's workspace `{bot_dir}/config/BRIEFINGS.md` was retired the same way: what it held is imported once and an existing file is left on disk, inert.

Content **blocks** live in the per-user briefings module DB and are edited in the web block editor. Blocks authored under a retired `[[users.X.briefings.blocks]]` entry are seeded once into the row of the same name.

Because a name can come from a schedule, or from blocks with no schedule at all, `istota briefings list` (and `istota-skill briefings list`) is what answers "which briefings does this user actually have" — every effective name after defaults and disabled rows are applied, with each one's block count and its latest archived generation. Asking for a name that is not on that list now returns the valid options rather than an empty briefing: a zero-row block query used to read as "configured, no content", so a typo or a case mismatch produced a confident and wrong success (ISSUE-328).

## Building a briefing

Open Briefings → Settings and choose a briefing. Its schedule, destinations and ordered blocks appear together. Disabled schedules remain available for editing, including briefings with no blocks yet. Use Edit schedule to change when and where it runs.

Add block opens a searchable preset list for feeds, websites, newsletters, your own email, calendar, markets, tasks, notes, reminders, shared content or a custom block. Presets use the existing sources. Feed presets need Feeds enabled with a subscription or category; shared content needs an available shared block. Choose the source's feed, website, file or shared-block name before saving.

The My email preset reads one account per block, chosen from the accounts you have: your bot address (`bot+<you>@…`) is always listed, and Gmail is listed when Google Workspace is set up and becomes selectable once you allow Gmail access in Settings. Its default instructions ask for the five most pressing action items from the past 24 hours. From your bot address it reads only mail that is yours by the same rule the email skill uses: sent to your plus-address, sent from one of your own addresses, or a reply in one of your threads. From Gmail it reads your inbox. It reads at most 50 messages, treats them as untrusted text, and is refused in group briefings and shared blocks.

The block editor keeps a local draft of the title, source list and width. Automatic uses one column, Wide uses two where space allows, and Full width spans the reader pane. All widths become one column on a narrow pane. Advanced contains the directive, generation mode, story count and tone. Save block writes the block and all its sources together. Cancel discards the draft after confirmation when it has changes; a failed save keeps it open. The app bar's Save continues to control the separate HTML email preference.

Each block's actions include Edit, Move up, Move down, Duplicate and Delete. Moves work with the keyboard or touch and keep reading order. Duplication copies every source, including disabled ones. Add a source before duplicating a legacy block that has no sources. Deleting or changing a block affects future editions; existing editions remain available. Admins can still manage and run shared blocks in their own card.

## Previewing personal delivery

Preview in the block builder opens Web, HTML email and Plain text views. Preview in the block editor includes that unsaved block draft. The example comes from the selected briefing's latest saved edition: content follows the block's saved identity while the proposed title, order and width come from the builder. Deleted blocks disappear. New blocks, blocks without saved content and legacy editions use labelled placeholders. The preview does not gather sources, call a model, save changes or send mail.

Web uses the reader's cards and full-block viewer. HTML email shows the complete sections in an isolated frame with links disabled and external resources blocked. Plain text keeps complete sections and usable URLs. A preview error leaves the block draft available for editing or retry. Layout and source changes affect future editions; preview does not predict their content.

## Group briefings

Select a group, one delivery channel and an explicit timezone before choosing Generate from prompt or Build from blocks. The audience review shows deliverable members, mutes, exclusions and missing destinations without contact details. Email sends individual copies, Talk uses each member’s private destination, and ntfy splits the saved text into bounded messages. In app creates no external sends. Every published edition remains available in group history regardless of the selected channel.

Only current group owners manage ordinary group briefings. A new owner can manage the existing definitions and editions immediately; the creator has no special rights after losing ownership. Installation admins outside an ordinary group cannot author or read its content. Saves check the current revision and refuse stale edits. A saved target cannot change: Duplicate for group creates a disabled draft with a new source review.

Owners may select Excluded users from the current group’s members. Exclusions suppress outbound sends and in-app notices, while retained history remains readable. They persist across departure and rejoin. Removing an exclusion does not resend skipped work. Members can separately mute outbound delivery or in-app notices for a briefing, or set group-wide mutes. A member’s own mute always applies.

Preview gathers and generates through the same authorization checks as publication, but its result is visible only to authors and sends nothing. Generate now creates a fresh publishable occurrence; it does not publish an old preview. Delivery status is separate from generation status. Retry failed deliveries reuses the saved edition and confirmed message parts. Uncertain delivery means the provider may already have accepted it; retry requires acknowledgement that it could create a duplicate. An already-started send may complete after membership removal, exclusion or source revocation. Delivered copies cannot be recalled.

Current members can read and download retained editions, including editions from before they joined. Joining creates no backlog of sends or notices. Removing or disabling a member denies subsequent reads and pending deliveries. Group editions have no personal Delete action. The Groups page links to the same editor and reader; turning off a personal Briefings module does not remove membership or history access through Groups.

### Sources and research

Group sources are public browse/market inputs, the shared newsletter pool, selected group notebook or text files, shared blocks, and explicitly approved personal file or calendar contributions. The newsletter pool is the same unowned mail a personal Newsletters source reads; mail addressed to a configured user never enters it. Direct personal RSS, notes, tasks and calendar sources cannot be copied into a group definition. Each contributor approves their exact resource and the recurring disclosure to current and future group members. Calendar approval states the date window and disclosed fields. Changing the resource, projection or channel requires renewed approval; an author or admin cannot approve for another person.

Public research has its own prompt. It runs through the existing browse skill in an ordinary sandboxed service task with a dedicated Briefings browser profile. It does not inherit a human browser session, personal memory or group files. The research prompt is sent to the configured model provider. Mark prompts containing personal information and acknowledge the group audience before enabling them; a pasted path never grants file access. Final synthesis receives the saved research and approved private inputs without tools; put private generation instructions in that separate prompt. Browser networking keeps its normal behavior, with no additional domain or public-IP firewall promised by this feature.

A refused selected source blocks publication, including structured-only editions. External outages can yield an edition labelled Incomplete sources when usable content remains. An all-empty requested gather fails and leaves the prior edition unchanged. An intentionally source-free prompt with research off remains valid. Revoking a contribution stops future use and pending delivery, but retained published history is not rewritten.

### All users and former defaults

All users is the built-in group of enabled installation accounts. Membership is automatic, including for users whose personal Briefings module is off. It cannot be manually changed or archived. Installation admins author its briefings; joining the installation grants access to retained All users history. Its creation alone enables no mailing and injects no group memory into ordinary tasks.

Legacy `[[default_briefings]]` and the old per-user default toggle are migration inputs only. Upgrade preserves existing effective personal schedules and content once. New accounts receive no copied schedules. Operators import disabled All users drafts, review personal variants and opt-outs, and explicitly cut over each definition. Existing personal archives stay personal. See [Migrating group briefings](../deployment/migrating-group-briefings.md) for the report, import, cutover and rollback commands.

## Administrator pause and delete

Admin → Briefings lists every personal, group and All users briefing on the installation, with its owner or group, schedule, delivery channel, state and last run. A text filter narrows both lists by owner, group or title. The page shows definitions and state only, never blocks, sources, prompts or editions.

Pause places an administrator hold. A held briefing does not run, and pending runs are cancelled; a run already in progress finishes, and earlier runs keep their recorded outcome. Pause, resume and delete take effect without a restart, and a resumed briefing runs again at its next scheduled time. The hold is separate from the owner's own on/off setting. The owner sees "Paused by an administrator" in Briefings settings and cannot turn the briefing on until an administrator chooses Resume, which restores whatever setting the owner had. A group's owners can still edit a held definition and turn it off.

Delete asks for the briefing's name typed back and then a confirmation code or password, the same two checks the configuration editor uses. A personal briefing is removed the way its owner would remove it, and past editions stay in the owner's archive. A group briefing leaves the group's settings and stops running, its source approvals are revoked, and its name is free to reuse. Members keep its published editions in the reader. A definition created by a former default or publishing-job migration cannot be deleted until that migration is cut over or rolled back. Importing the same default again after a delete starts a new draft (see [Migrating group briefings](../deployment/migrating-group-briefings.md)).

The owner of a personal briefing, or the current owners of a group, get a notification when an administrator pauses, resumes or deletes it. The acting administrator is not notified, and nobody is notified about an All users briefing.

## Changing briefings from chat

The bot can make the same changes the settings page makes, through two skills. `briefings` edits a personal briefing's blocks and sources, and its archive. `briefings_config` reads and changes the schedule, lets an admin list, create, change, run and remove shared blocks, and works on group briefings. Each change goes through the same checks as the web page, and a refusal names a reason the bot passes on to you. Removing a schedule, a shared block, an archived edition or a group draft, and revoking a source approval, are shown to you first and need your yes.

Group briefings are reachable from your private chat only. In a group room, a shared web room, a WhatsApp group, an email thread or any room someone else has been in, the bot cannot read or change them, because a definition holds the owners' instructions that members do not see. From your private chat it applies the authority the web applies: owners write definitions, preview and publish, members read published editions and set their own mutes, and admins write All users briefings. Edition text reaches the bot marked as untrusted content.

Two things stay on the web. Approving a personal file or calendar for a group shares it with every current and future member, and retrying failed deliveries can send someone the same edition twice. In both cases the web asks you to acknowledge that yourself, so the bot points you at Briefings → Settings.

## Memory isolation

Briefing prompts intentionally exclude USER.md and dated memories. This prevents private context from leaking into what may be shared or newsletter-style output.

## Example settings plan

A plan applied with `istota apply -f plan.toml` writes the same rows the settings page does.

```toml
[meta]
version = 1

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

Width lives in `options.presentation.width` and accepts `auto`, `wide` or `full`, with `auto` as the default. The editor preserves other option fields.

## Shared curated content

Some content is the same for everyone — world headlines, a markets summary, a newsletter digest. Without sharing, every user's briefing fetches the frontpage and pays a synthesis pass again, so at 7am N users each independently do the same expensive work. The **shared curated content** plane decouples that: expensive shared generation runs a handful of times total, and each per-user briefing reads the pre-made artifact.

It has two layers over one storage substrate — the **shared KV store** (`shared_kv`: cross-user, namespaced JSON; reads open to all, writes admin-only).

### Module-owned shared blocks (batteries-included — the default)

The briefings module ships **shared blocks**: one-block briefings it schedules and generates *once globally*, writing the rendered section into `shared_kv`. A fresh deploy ships a canonical set (`world-headlines` via browse presets, `markets-summary` via the markets source) for existing consumers. New group briefings can generate directly, without a shared-block publishing job.

A shared block is defined at the instance level, as `[[briefings.shared_blocks]]` (the top-level `[[briefing_shared_blocks]]` it replaced still loads, with a warning):

```toml
[[briefings.shared_blocks]]
name = "world-headlines"          # → shared_kv key in namespace "briefing_shared_blocks"
cron = "0 6 * * *"                # generated before the 07:00 briefing window; UTC (global)
title = "🌍 World headlines"
directive = "Synthesize the frontpages into ~8 top world stories."
render_mode = "synthesis"
enabled = true

  [[briefings.shared_blocks.sources]]
  kind = "browse"
  config = { preset = "ap" }
  [[briefings.shared_blocks.sources]]
  kind = "browse"
  config = { preset = "reuters" }
```

- Generation runs off the dispatch thread (sleep-cycle-style: one non-streaming, no-sandbox model call) under the reserved `__system__` identity, so it needs **no admin configured** and never blocks task processing.
- Only **user-agnostic** source kinds are allowed: `browse`, `markets`, and `email` (the shared/unowned pool). `rss` (needs a real feeds user), the personal built-ins (`calendar`/`todos`/`reminders`/`notes`), and `shared_block` (no chaining) are dropped with a warning.
- If every source fails/empties (a transient AP/Reuters outage), the write is **skipped** and the prior value kept (last-known-good) rather than blanking the section.
- Omit or set `enabled = false` to disable one; an explicit empty list (`[briefings] shared_blocks = []`) opts out entirely.

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

### Agent blocks

A synthesis block makes one model call over text gathered beforehand. It cannot follow a link into an article, check a detail, or retry a page that failed. An agent block can. Its directive is a task prompt, and the block is whatever the task answers. Use one for a digest that today runs as a CRON.md prompt job publishing to `shared_kv`.

```toml
[[briefings.shared_blocks]]
name = "world-headlines-agent"
cron = "50 5,17 * * *"            # in [briefings] shared_block_timezone
title = "World headlines"
render_mode = "agent"
directive = """
Read the front pages listed below and write a linked digest of the top stories...
"""
```

An agent block has a non-empty directive, no sources and no `trusted` flag. Its content is always stored untrusted, because it is model text written over arbitrary web pages. An entry that breaks these rules is dropped when the config loads, and refused by the web editor, `istota briefings shared ensure` and `istota apply`.

In the web UI, choose Agent as the render mode in Briefings → Settings → Shared blocks. The editor hides the sources and the trusted toggle and shows a Task prompt field. Edits made there, or from chat, take effect at the next cron slot without a restart.

#### What the task has

The block's cron queues an ordinary background task under the reserved `__briefings__` identity, the same kind of sandboxed research task group briefings use. The task has:

- the browse skill, limited to `render`, `get`, `links`, `extract` and `close`, on the anonymous Briefings browser profile;
- its own temp directory for scratch files;
- the date and time in `[briefings] shared_block_timezone`, the clock the block's cron runs on, rather than UTC.

It does not have scroll or any other interactive browsing, a user's workspace, `kv`, memory, USER.md, room context or email. A prompt that needs any of those, such as one that reads a workspace file or deduplicates through `kv` across weeks, has to stay a cron job.

The task's answer is published verbatim as the block. The system prompt already tells the task this, and tells it to answer with the content only: no preamble, no JSON envelope, no closing remarks. A converted prompt does not need to repeat that.

#### Runs and failures

A finished task is picked up on the scheduler's next pass. A non-empty answer replaces the block. An empty answer, an answer with no final response, or a failed task keeps the prior content. If the block was deleted or switched away from agent mode while its task ran, nothing is written. A cron slot or Run now that arrives while the previous task is still running starts no second task.

After three runs in a row that produce nothing usable, the operator gets one alert. The block stays enabled, and a reader's `max_age_hours` drops a stale section in the meantime.

The Shared blocks card shows the task while it runs ("queued, task #N") and then its outcome and time. Run now answers at once that the task was queued, then keeps following the task until its outcome is in.

Every `__briefings__` task shares one background worker: group research, group generation and delivery, and every agent block. A news digest can take 10 to 20 minutes, and anything queued behind it waits. Space agent block crons apart by at least the expected run length.

#### Editing the prompt from chat

An admin can ask the bot to read or change a block's prompt, the way a prompt file for a cron job is tuned today. The bot uses two `briefings_config` verbs:

```bash
istota-skill briefings_config shared-block show world-headlines-agent
istota-skill briefings_config shared-block set-prompt world-headlines-agent --prompt-file prompt.md
```

`show` prints the definition, with the full directive and the last run state. `set-prompt` reads the new directive from a file the task wrote, validates it, and changes only the directive. Both are refused for a user who may not write shared content.

#### Converting a cron digest prompt

Paste the prompt file into the directive, then:

1. Delete any setup of a scratch directory under a user's tmp path. The task has its own temp directory.
2. Delete instructions about delivery ("the scheduler posts this to the right channel") and about the output envelope (JSON wrapping, no preamble). The system prompt covers both.
3. Delete fallbacks that use interactive browsing, such as `browse interact --scroll` or a CSS-selector fallback for `browse links`.
4. Copy into the prompt any USER.md guidance the job relied on, for example how to get past a paywall. The task does not read USER.md.

Run it once with Run now and compare the result with the value the cron job publishes before pointing briefings at the new block and retiring the job.

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

The briefings tab lists personal and group editions with target labels and filters. Personal archive results can be deleted from their kebab menu; group editions stay in group history. Personal briefings have an archive sidebar and a settings page behind the cog for editing blocks, sources, schedule and delivery. Source paths use a searching file picker with an advisory existence check — advisory rather than blocking, because the path resolver is fail-soft, so a not-yet-created file must not trap the editor. Admins additionally get a "Shared blocks" card for the module-owned blocks.

Selecting an archived briefing or changing the briefing-name filter adds a browser history entry. Back and Forward restore the selection, and reloading or sharing the URL opens the same briefing and filter. Returning from settings keeps the selection. If a briefing or filter is no longer available, the reader falls back to the first available briefing or All and updates the current URL without adding an entry.

A generated result is an edition. New editions show one card per populated block, in the saved order, with the edition title and generation date above them. Cards use one, two or three masonry columns according to the space available beside the archive. Card heights round up to a shared height rhythm, and nearby cards with similar heights match each other. The saved order remains the keyboard and email order; visual rows can differ. Automatic blocks take one column, Wide blocks take two where space allows, and Full width blocks take the whole row. At narrow widths every block takes one column. Layout and title changes affect future editions; an archived edition keeps its saved content and layout.

Every card shows its complete content, including all paragraphs and list items. The expand icon in each card header opens the complete block in a viewer. For user-managed briefings, the settings icon opens that block’s editor if the saved block still exists. Article links work directly from the card. **Some sources unavailable** means the block includes useful content despite a source failure; internal source errors are never shown in the card.

Older editions without a structured document stay readable in a single full-width card. Unsupported or damaged documents also use their complete stored Markdown body. Opening an edition reads the archive without fetching sources or generating content. Changing the selection shows a loading state until that edition arrives, so the previous edition cannot appear under the new selection.

Personal briefings are archived on delivery and pruned by `[briefings] archive_retention_days` (default 90) on insert, alongside manual per-result deletion. Group editions are saved before delivery and use the same retention duration; active or uncertain deliveries delay pruning.

## Personal output format

Generation makes one source gather and one model call for the edition. Before the call, the worker saves a protected manifest with the edition title, ordered numeric block IDs, block titles, render modes, presentation widths and source status. Duplicate titles remain distinct. Blocks with no gathered content are omitted from the request; failed sources keep their provenance outside the reader-facing document.

The model returns `{"version": 2, "blocks": [{"block_id": 42, "body_md": "..."}]}`. Each requested ID must appear exactly once. An empty string explicitly omits a block whose gathered content produced nothing useful. The model supplies only block bodies; titles, order, presentation and source status come from the manifest. The edition title uses the configured title (blank derives it from the briefing name) and the task's creation date in the user's timezone.

Before marking the task completed, indexing its answer or delivering it, the scheduler validates the response against that manifest. Missing, duplicate or unknown IDs, wrong types and unsupported versions fail through the normal quiet briefing retry path. An edition with no useful blocks fails with `briefing_no_content` rather than publishing an empty edition. Useful blocks with failed sources retain a partial-source status.

The completed task stores a canonical envelope with `body` and `briefing_document`. The archive stores the same immutable document and its deterministic linear Markdown projection. Editing or deleting a definition during generation cannot change that edition's titles, content, order or layout. Chat, digest and existing text destinations use the projection; email and ntfy subjects use the saved edition title even after archive cleanup removes the manifest file. An archive write failure is logged and does not repeat generation or block delivery.

Email uses the same complete ordered blocks as the archive. HTML mail stacks sections under escaped headings in the shared mail template: inline spacing styles and lists without a left indent, with the mail app's own fonts, colours and width. Plain mail uses section labels and preserves article destinations as `Label (URL)`, or one URL when it is already the label. A partial-source block includes a short status note in both formats. The existing HTML preference selects multipart alternatives or a single plain part; an HTML rendering failure also sends the full plain edition. Delivery uses the document retained at completion, so archive cleanup cannot remove it.

Old body-only tasks retain their linear path without inferred block boundaries. The parser still tolerates JSON fences and wrappers. The email skill stays excluded from briefing tasks so delivery remains scheduler-owned.

## Extending blocks

A preset is an ordinary default block/source definition in `BLOCK_TEMPLATES`, returned by the existing config endpoint. Add a new source kind to `SOURCE_KINDS`, the source resolver dispatch, source fields and tests before adding its preset. Availability and source choices continue to use the existing picker endpoints. A preset does not introduce executable configuration or change source authority.

Every document block retains its complete `body_md`, stable numeric ID, snapshot title, source status and `presentation.width`. Add a specialized block kind only with a validated versioned data shape, web rendering and complete HTML/plain semantics together. Unknown kinds use their Markdown body; unknown document versions use the archive's linear body. Keep a small explicit component switch rather than loading components from server strings. Width remains an option, separate from the generation mode.

`POST /briefings/preview` accepts `{ "document": <version-1-document> }` and returns `body_md`, `plain` and `html` from the production renderers. Authentication, origin checks and the live module gate run before rendering. The gate may read profile settings; preview content never reads a file, resolves an ID or writes the content database. Requests are limited to 2 MiB, 100 blocks and 64 KiB of UTF-8 body text per block. Malformed documents return 400; size limits return 413. Only the preview copy gains the restrictive CSP (inline styles allowed, so the template's look shows) and noninteractive links; outgoing mail is the template's HTML unchanged.

## Reading a todo file

The `todos` source keeps each item's place in the file rather than flattening it. That is what makes a directive like "only cover what's under NOW" followable instead of merely ignorable — previously the headings were stripped before the briefing was written, so every item arrived as one undifferentiated list.

Sections are detected in **one dialect per file**, chosen from what the file actually uses, in the order ATX (`### NOW`) → setext (an underlined title) → bold (`**NOW**`) → label (`NOW:`). One dialect rather than all at once, because a stray `Blockers:` line in an ATX file would otherwise steal the items sitting under `### NOW`. Detection is strict and labelling lenient: `#tags` is not enough to establish an ATX file, but inside one, `#NOW` still labels. Items are matched before headings, so a bullet reading `- NOW:` stays a bullet. A leading YAML frontmatter block is dropped, but only when it really is one — a note that opens with a `---` rule keeps its content, whereas a notes-app header block was previously read as todos and led the list with the file's own tags.

Checkboxes (`- [ ]`), bullets and numbered items all count; `- [x]` is done and excluded. Section membership is flat — the nearest heading above the item, at any level, not a breadcrumb. The rendered block labels each group with a plain `Section:` line rather than a markdown heading, since the block prompt forbids headings in output.

The size cap is `[briefings] max_source_chars`, spent item by item: an item that doesn't fit is dropped whole along with everything after it, and the provenance line says how many were left out. Never mid-item — half a todo reads as a todo saying something your file doesn't. The first item is always kept.

## Scheduling

Personal cron expressions are evaluated in each user's timezone. Group schedules use their definition’s stored timezone and a unique occurrence per due instant. The scheduler checks for due briefings every `briefing_check_interval` (60s). Personal schedules track their last run in `briefing_state`; group runs use the framework occurrence ledger.
