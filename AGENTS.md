# Istota - Claude Code Bot

Claude Code-powered assistant bot with Nextcloud Talk interface.

**Production server**: `your-server` (accessible via SSH, installed at `/srv/app/istota`)

## Project Structure

```
istota/
├── src/istota/
│   ├── cron_loader.py       # CRON.md loading + DB sync for scheduled jobs
│   ├── cli.py               # CLI for local testing and administration
│   ├── config.py            # TOML configuration loading
│   ├── context.py           # Conversation context selection (Sonnet-based)
│   ├── db.py                # SQLite operations (all tables)
│   ├── email_poller.py      # Email polling and task creation
│   ├── executor.py          # Per-task orchestration (memory, skills, sandbox); delegates model invocation to a Brain
│   ├── brain/               # Brain abstraction — model invocation behind a single protocol
│   │   ├── __init__.py        # Brain protocol + factory (`make_brain`)
│   │   ├── _types.py          # BrainRequest, BrainResult, BrainConfig
│   │   ├── _events.py         # StreamEvent types + Claude Code stream-json parser
│   │   └── claude_code.py     # ClaudeCodeBrain — wraps `claude` CLI subprocess
│   ├── skill_proxy.py       # Unix socket proxy for credential-isolated skill CLI calls
│   ├── skill_client.py      # istota-skill console script (proxy client + direct fallback)
│   ├── network_proxy.py     # CONNECT proxy for network isolation (domain allowlist)
│   ├── knowledge_graph.py   # Temporal entity-relationship triples
│   ├── heartbeat.py         # Heartbeat monitoring system
│   ├── web_app.py            # Authenticated web interface (Nextcloud OIDC)
│   ├── webhook_receiver.py   # FastAPI webhook receiver (Overland GPS, etc.)
│   ├── logging_setup.py     # Central logging configuration
│   ├── nextcloud_api.py     # Nextcloud API user metadata hydration
│   ├── nextcloud_client.py  # Shared Nextcloud HTTP plumbing (OCS + WebDAV)
│   ├── notifications.py     # Central notification dispatcher (Talk, Email, ntfy)
│   ├── feeds.py             # Miniflux API client + HTML feed page generation
│   ├── location_logic.py    # Place stats, cluster discovery, dismiss-zone helpers (shared web ⇄ skill)
│   ├── scheduler.py         # Task processor, briefing scheduler, all polling
│   ├── shared_file_organizer.py # Auto-organize files shared with bot
│   ├── sleep_cycle.py       # Nightly memory extraction
│   ├── storage.py           # Bot-managed Nextcloud storage
│   ├── stream_parser.py     # Backward-compat shim (re-exports from brain/_events.py)
│   ├── commands.py          # !command dispatch (help, stop, status, memory, cron, skills, check, export, more, search)
│   ├── talk.py              # Nextcloud Talk API client (user API)
│   ├── talk_poller.py       # Talk conversation polling
│   ├── tasks_file_poller.py # TASKS.md file monitoring
│   ├── memory_search.py     # Hybrid BM25 + vector search over conversations/memories
│   └── skills/              # Self-contained skill directories (skill.md with YAML frontmatter + optional Python)
│       ├── _types.py        # SkillMeta, EnvSpec dataclasses
│       ├── _loader.py       # Skill discovery, manifest loading, doc resolution
│       ├── _env.py          # Declarative env var resolver + setup_env() hook dispatch
│       ├── bookmarks/       # Karakeep bookmark management
│       ├── briefing/        # Briefing prompt builder, config loader, post-processing
│       ├── briefings_config/ # User briefing schedule config (doc-only)
│       ├── browse/          # Web browsing CLI (Docker container API)
│       ├── calendar/        # CalDAV operations CLI
│       ├── developer/       # Git/GitLab/GitHub workflows (doc-only)
│       ├── feeds/           # Miniflux RSS feed management CLI
│       ├── email/           # Native IMAP/SMTP operations
│       ├── files/           # Nextcloud file ops (mount-aware, rclone fallback)
│       ├── heartbeat/       # Heartbeat monitoring reference (doc-only)
│       ├── markets/         # yfinance + FinViz scraping CLI
│       ├── memory/          # Memory file reference (doc-only)
│       ├── memory_search/   # Memory search CLI (search, index, reindex, stats)
│       ├── nextcloud/       # Nextcloud sharing + OCS API CLI
│       ├── notes/           # Markdown note-saving conventions (doc-only)
│       ├── reminders/       # Time-based reminders via CRON.md (doc-only)
│       ├── schedules/       # CRON.md job management reference (doc-only)
│       ├── scripts/         # User scripts reference (doc-only)
│       ├── sensitive_actions/ # Confirmation rules (doc-only)
│       ├── tasks/           # Subtask/queue reference (doc-only)
│       ├── todos/           # Todo list reference (doc-only)
│       ├── transcribe/      # OCR transcription via Tesseract
│       ├── untrusted_input/  # How to read content from outside the trust boundary (doc-only, companion)
│       ├── website/         # Website management reference (doc-only)
│       ├── google_workspace/ # Google Workspace CLI wrapper (Drive, Gmail, Calendar, Sheets, Docs)
│       ├── location/       # GPS location tracking + calendar attendance (Overland)
│       ├── money/         # Accounting (ledger, invoicing, work log) — in-process facade over `istota.money.cli`
│       └── whisper/         # Audio transcription via faster-whisper
├── config/
│   ├── config.toml          # Active configuration (gitignored)
│   ├── config.example.toml  # Example configuration
│   ├── users/               # Per-user config files (override [users] section)
│   ├── emissaries.md        # Constitutional principles (global only, not user-overridable)
│   ├── persona.md           # Default personality (user workspace PERSONA.md overrides)
│   ├── system-prompt.md     # Custom Claude CLI system prompt (used when custom_system_prompt = true)
│   ├── guidelines/          # Channel-specific formatting (talk.md, email.md, briefing.md)
│   └── skills/              # Operator override directory (empty by default)
├── deploy/
│   ├── ansible/             # Ansible role (defaults, tasks, handlers, templates)
│   ├── install.sh           # Bootstrap: ensures Ansible, runs wizard, delegates to role
│   ├── wizard.sh            # Interactive setup wizard (writes settings.toml)
│   ├── settings_to_vars.py  # Converts settings.toml to Ansible vars YAML
│   ├── local-playbook.yml   # Playbook for local-mode deployment
│   └── README.md            # Deployment documentation
├── docker/
│   ├── docker-compose.yml   # Full stack: postgres + redis + nextcloud + istota
│   ├── istota/              # Dockerfile, entrypoint, provisioning scripts
│   ├── browser/             # Playwright browser container (Flask API)
│   └── .env.example         # Environment variables template
├── web/                     # SvelteKit frontend (adapter-static, base /istota)
│   ├── src/
│   │   ├── routes/          # Pages: dashboard, feeds, location, money
│   │   └── lib/
│   │       ├── api.ts       # Backend API client
│   │       └── components/
│   │           ├── ui/      # Shared primitives: AppShell, ShellHeader, Sidebar,
│   │           │            #   SidebarToggle, CategoryGroup, NavLink, Chip,
│   │           │            #   Button, Select (bits-ui), Modal (bits-ui Dialog)
│   │           └── location/ # Domain-specific (PlaceForm, LocationMap, etc.)
│   ├── svelte.config.js     # adapter-static, base path /istota
│   ├── vite.config.ts       # Dev proxy to FastAPI; mock plugin behind VITE_MOCK_API=1
│   └── vite-mock-api.ts     # In-process mock backend for `npm run dev` UI iteration
├── scripts/                 # setup.sh, scheduler.sh
├── tests/                   # pytest + pytest-asyncio (~3450 tests, 65 files)
├── schema.sql
└── pyproject.toml
```

## Architecture

```
Talk Poll ──►┐
Email Poll ──►├─► SQLite Queue → Scheduler → Claude Code → Talk/Email Response
TASKS.md ────►│
CLI ─────────►┘

GPS Webhook ──► Location DB → Place detection → Notifications (ntfy/Talk)

Web App ──► Nextcloud OIDC → Session → Dashboard / Feed pages
```

- **Talk poller**: Background daemon thread, long-polling per conversation, WAL mode for concurrent DB access
- **Email poller**: Polls INBOX via imap-tools. Routing precedence: (1) recipient plus-address (`bot+user_id@domain`), (2) sender match against user `email_addresses`, (3) thread match against `sent_emails`. Plus-addressing enables external contacts to email a specific user's agent directly. Outbound `SMTP_FROM` uses plus-addressed `bot+user_id@domain` so replies route back correctly. `routing_method` column in `processed_emails` tracks how each email was routed. **Confirmation gate**: plus-addressed emails from untrusted senders (not in `trusted_email_senders` or user's own `email_addresses`) are held in `pending_confirmation` — deterministic, non-LLM gate. Confirmation prompt posted to user's Talk channel; user replies yes/no.
- **Task queue** (`db.py`): Atomic locking with `user_id` filter, retry logic (exponential backoff: 1, 4, 16 min)
- **Scheduler**: Per-user threaded worker pool. Three-tier concurrency: instance-level fg/bg caps, per-user limits. Workers keyed by `(user_id, queue_type, slot)`.
- **Executor**: Builds prompts (resources + skills + context + memory), composes a `BrainRequest`, and hands it to a `Brain` for model invocation. Validates output for malformed model responses (leaked tool-call XML), composes the final result from streaming events (CM-aware), and processes deferred files. The brain owns the subprocess/HTTP call, stream parsing, and transient-error retries.
- **Brain**: Pluggable model-invocation backend in `src/istota/brain/`. Selected via `[brain] kind = "..."` in config. Phase 1 ships `claude_code` (wraps the `claude` CLI subprocess with `--output-format stream-json`, auto-retries 5xx/429 up to 3 times). Future brains (`openrouter`, `anthropic`) will share the same `Brain` protocol so swapping them touches no executor code.
- **Context** (`context.py`): Hybrid triage — recent N messages always included, older messages selected by LLM
- **Storage** (`storage.py`): Bot-owned Nextcloud directories and user memory files

## Key Architecture Notes

- **Technical identifiers** (package, env vars, DB tables, CLI): always `istota`
- **User-facing identity** (Nextcloud folders, chat persona, email signatures): configurable via `bot_name` config field (default: "Istota")
- `config.bot_dir_name` sanitizes `bot_name` for filesystem use (ASCII lowercase, spaces→underscores, non-alphanumeric stripped)
- All storage path functions require explicit `bot_dir` parameter — no hidden defaults
- Skill docs, persona, and guidelines use `{BOT_NAME}`, `{BOT_DIR}`, and `{user_id}` placeholders, substituted at load time
- **Emissaries** (`config/emissaries.md`): constitutional principles — global only, not user-overridable, no `{BOT_NAME}` substitution. Injected before persona in every prompt. Controlled by `emissaries_enabled` (default true).
- **Persona** (`config/persona.md`): character layer — user workspace `PERSONA.md` overrides global (seeded from global on first run). Uses `{BOT_NAME}` placeholders.
- **Custom system prompt** (`config/system-prompt.md`): when `custom_system_prompt = true`, replaces Claude Code's default system prompt with a minimal one (~2,600 tokens) focused on tool usage and working practices. Eliminates identity conflicts with persona/emissaries and removes irrelevant interactive/git/IDE instructions. Toggle via config — disabled by default.

## Key Design Decisions

### Admin/Non-Admin User Isolation
Admin users listed in `/etc/istota/admins`. Empty file = all users are admin (backward compat). Override path via `ISTOTA_ADMINS_FILE`.

Non-admin restrictions: scoped mount path, no DB access, no subtask creation, `admin_only` skills filtered out.

### Multi-user Resources
Resources defined in per-user config or DB, merged at task time. Types: `calendar`, `folder`, `todo_file`, `email_folder`, `shared_file`, `reminders_file`, `notes_folder`, `ledger`, `karakeep`, `monarch`, `miniflux`, `money` (legacy alias `moneyman` accepted). CalDAV calendars auto-discovered from Nextcloud. Service credentials (Monarch, Karakeep, Miniflux) are configured as `[[resources]]` entries with type-specific fields in `extra`. The `money` resource has two modes: workspace (preferred) where the loader synthesizes a `UserContext` rooted at the user's workspace and reads `INVOICING.md`/`TAX.md`/`MONARCH.md` (with `.toml` fallback) from `{data_dir}/config/` first, then `{workspace}/config/`; and legacy where `config_path` points at a money config TOML with `[users.X]` sections.

### Nextcloud Directory Structure

```
/Users/{user_id}/
├── {bot_name}/      # Shared with user via OCS
│   ├── config/      # USER.md, TASKS.md, BRIEFINGS.md, PERSONA.md, etc.
│   ├── exports/     # Bot-generated files
│   ├── scripts/     # User's reusable Python scripts
│   └── examples/    # Documentation and config reference
├── inbox/           # Files user wants bot to process
├── memories/        # Dated memories (sleep cycle): YYYY-MM-DD.md
└── shared/          # Auto-organized files shared by user

/Channels/{conversation_token}/
├── CHANNEL.md       # Persistent channel memory
└── memories/        # Channel sleep cycle memories
```

### Memory System
- **User memory** (`USER.md`): Auto-loaded into prompts (except briefings). Optional nightly curation via `curate_user_memory` (sleep cycle promotes durable facts from dated memories; curation prompt includes KG facts to avoid duplication).
- **Channel memory** (`CHANNEL.md`): Loaded when `conversation_token` set
- **Dated memories** (`memories/YYYY-MM-DD.md`): Auto-loaded into prompts (last N days via `auto_load_dated_days`, default 3). Includes task provenance references (`ref:TASK_ID`).
- **Knowledge graph** (`knowledge_facts` table): Temporal entity-relationship triples (subject, predicate, object) with validity windows (`valid_from`, `valid_until`). Predicates are freeform — any snake_case verb accepted. Single-valued predicates (`works_at`, `lives_in`, `has_role`, `has_status`) auto-supersede on new value; all others are multi-valued. Temporary predicates (`staying_in`, `visiting`) coexist with permanent facts. Fuzzy dedup via word-level Jaccard similarity (threshold 0.7) catches near-duplicate facts. Extraction prompt lists annotated suggested predicates with usage hints; the model may use unlisted predicates when needed. Relevance-filtered facts loaded into prompts as "Known facts" section: user's own identity facts (subject matches user_id) always included, other facts included only when their subject or object is mentioned in the prompt. Capped via `max_knowledge_facts` (default 0 = unlimited). User-facing: `!memory facts [entity]`. CLI: `istota-skill memory_search facts/timeline/add-fact/invalidate/delete-fact`.
- **Memory recall** (BM25): Auto-recall via `auto_recall` config — searches indexed memories/conversations using task prompt as query, independent of context triage. Search supports `--topic` and `--entity` filters (NULL-topic chunks always included).
- **Memory cap** (`max_memory_chars`): Limits total memory in prompts. Truncation order: recalled → knowledge facts → dated → warn about user/channel. Default 0 (unlimited).
- **Chunk metadata**: Memory chunks have optional `topic` (work, tech, personal, finance, admin, learning, meta) and `entities` (JSON array) columns. Populated during sleep cycle extraction. Entity search uses `json_each()` for exact matching.
- Briefings exclude all personal memory to prevent leaking into newsletter-style output

### Talk Integration
Polling-based (user API, not bot API). Istota runs as a regular Nextcloud user.

- Long-polling per conversation, message cache in `talk_messages` table
- Progress updates: random ack before execution, streaming progress (rate-limited: min 8s, max 5/task). `progress_style`: `replace` (edit ack in-place with elapsed time, default), `full` (accumulated tool descriptions), `legacy` (post individual progress messages), `none` (silent). Optional `progress_show_text` for intermediate assistant text.
- Per-user log channel (`log_channel` config): verbose tool-by-tool execution logs posted to a dedicated Talk room
- Per-user alerts channel (`alerts_channel` config): Talk room for confirmations and security alerts. Falls back to briefing token, then auto-detected 1:1 DM with the bot (from talk poller conversation list cache)
- Multi-user rooms: only responds when @mentioned; 2-person rooms behave like DMs
- `!commands`: intercepted in poller before task creation — `!help`, `!stop`, `!status`, `!memory`, `!cron`, `!check`, `!export` (conversation history export), `!skills` (list available skills), `!more #<task_id>` (show execution trace), `!search <query>` (search conversation history via memory index + Talk API)
- Confirmation flow: regex-detected → `pending_confirmation` → user replies yes/no. Three-path lookup: reply-to-specific message (by `talk_response_id`) → same-conversation → cross-conversation fallback by `user_id`. Supports email confirmation gates where the task's `conversation_token` differs from the Talk channel where the user replies.

### Skills
Self-contained directories under `src/istota/skills/`, each with a `skill.md` file containing YAML frontmatter for all metadata and markdown body for instructions. Frontmatter fields: `name`, `triggers`, `description`, `always_include`, `admin_only`, `cli`, `resource_types`, `source_types`, `file_types`, `companion_skills`, `exclude_skills`, `dependencies`, `exclude_memory`, `exclude_persona`, `exclude_resources`, `env` (JSON-encoded array of env specs). Operator overrides in `config/skills/` can use `skill.toml` for backward compatibility.

**Two-pass selection:** Pass 1 is deterministic (zero-cost): `always_include`, `source_types`, `file_types`, `triggers`/`keywords` (a keyword match requires `user_resource_types ∩ resource_types` if the skill declares `resource_types`), then sticky-skill injection from recent conversation tasks, then `companion_skills`, then `exclude_skills` removal. Pass 2 is LLM-based semantic routing (Haiku, ~500ms, ~$0.0003/task): sees the task prompt + a manifest of unselected skills (filtered for admin_only / disabled / unmet deps) and returns additional skills to load. Results are unioned and `exclude_skills` is re-applied. Pass 2 is additive — on timeout/error, falls back to Pass 1 only. Config: `[skills]` section.

Skills can exclude other skills via `exclude_skills` (e.g., briefing excludes email to prevent delivery interference). Skills can also be excluded via `disabled_skills` at instance level (top-level config) and per-user level (user config), both merged at selection time.

Sticky skills (talk/email tasks only): for follow-up turns the executor adds skills from the last 2 conversation tasks within the past 30 minutes plus the explicit `reply_to_talk_id` parent, so a follow-up like "and the next one" doesn't lose the relevant skill set. The resolved set is persisted via `db.save_task_selected_skills()` after each task.

Audio attachments pre-transcribed before skill selection so keyword matching works on voice memos.

Env var wiring is declarative via the `env` field in skill.md frontmatter (JSON-encoded array of env specs). Action skills expose `python -m istota.skills.<name>` CLI with JSON output.

### Conversation Context
Talk tasks use a poller-fed local cache (`talk_messages` table, bounded by `talk_cache_max_per_conversation`). Email tasks use DB-based context. Both paths use hybrid selection: recent N messages always included, older messages triaged by LLM. Recency window (`context_recency_hours`, default 0 = disabled) filters out old messages while guaranteeing at least `context_min_messages` (10). Config in `[conversation]` section.

### Input Channels
- **Talk**: Long-polling, message cache, referenceId tagging for ack/progress/result messages
- **Email**: IMAP polling, attachments to `/Users/{user_id}/inbox/`, threaded replies. Output via `istota-skill email output` (deferred file pattern). Emissary thread matching: unknown sender emails checked against `sent_emails` table via References header — replies to bot-initiated threads route to originating user via Talk. Plus-addressed emails from untrusted senders are held for user confirmation (see email poller confirmation gate above).
- **TASKS.md**: Polls user config file (30s). Status markers: `[ ]` `[~]` `[x]` `[!]`. Identity via SHA-256 hash.

### Emissary Email Threads
Outbound emails tracked in `sent_emails` table (Message-ID, recipient, user, conversation_token). When external contacts reply, the email poller matches References headers against sent emails and creates tasks with `output_target="talk"` routed to the originating Talk conversation. The bot drafts a response and asks for confirmation. On approval, the task re-executes with `confirmation_context` injected (the bot's previous output), instructing it to send the draft rather than re-draft. Pending confirmations are auto-cancelled when the user sends a new message in the same conversation.

### Briefings
Sources: user `BRIEFINGS.md` > per-user config > main config. Cron in user's timezone. Components: `calendar`, `todos`, `email`, `markets`, `news`, `headlines`, `notes`, `reminders`. Market data pre-fetched. Memory isolated from briefing prompts. Claude returns structured JSON (`{"subject": "...", "body": "..."}`); the scheduler parses it and handles delivery deterministically. The email skill is excluded from briefing tasks via `exclude_skills` to prevent the model from sending emails directly.

### Scheduled Jobs
Defined in user's `CRON.md` (markdown with TOML `[[jobs]]`). Job types: `prompt` (Claude Code), `prompt_file` (prompt loaded from external file), or `command` (shell). `prompt_file` paths are relative to the Nextcloud mount root and resolved at load time. One-time jobs (`once = true`) auto-deleted after success. Auto-disable after 5 consecutive failures. Results excluded from interactive context. Per-job `skip_log_channel = true` suppresses log channel output for frequent jobs. Per-job `model = "claude-sonnet-4-6"` and `effort = "low"` (or any of `low/medium/high/xhigh/max`) override `config.model` / `config.effort` for that one job — useful for downgrading high-volume retrieve-and-render jobs. CRON.md load warns (never rejects) on suspicious model strings (no `claude-` prefix, embedded whitespace) or effort values outside the known set. The log channel finalize header appends the resolved model+effort inline (`(claude-opus-4-7 high)`) for at-a-glance observability of which model produced each output.

### Sleep Cycle
Nightly memory extraction (direct subprocess). Gathers completed tasks → Claude extracts memories → writes dated memory files with task provenance (`ref:TASK_ID`). Extraction prompt requests structured MEMORIES/FACTS/TOPICS sections; parser falls back to plain text if structured output is missing or malformed. Personal attributes and relationships are routed to FACTS only (not duplicated as MEMORY bullets). Extracted facts inserted into knowledge graph (`knowledge_facts` table) with freeform predicates; annotated suggested predicates guide the model toward consistent naming. Temporal facts use `valid_from`/`valid_until` fields rather than baking dates into object strings. Dominant topic from TOPICS section passed to memory indexer for chunk metadata. Channel sleep cycle runs in parallel for shared context. Optional USER.md curation pass (`curate_user_memory`, includes KG facts to avoid duplication; creates new headings for unrelated topics). Task data uses tail-biased truncation (40% head + 60% tail) to preserve conclusions, with dynamic per-task budget allocation proportional to content length. Tasks sharing a conversation are grouped as threads. Config: `[sleep_cycle]`, `[channel_sleep_cycle]`.

### Heartbeat Monitoring
User-defined health checks in `HEARTBEAT.md`. Types: `file-watch`, `shell-command`, `url-health`, `calendar-conflicts`, `task-deadline`, `self-check`. Cooldown, check intervals, and quiet hours supported.

### Memory Search
Hybrid BM25 + vector search using sqlite-vec and sentence-transformers. Auto-indexes conversations and memory files. Channel support via `channel:{token}` namespace. Degrades to BM25-only if deps unavailable. Optional: `uv sync --extra memory-search`. Search supports `topics` and `entities` filter params; entity matching uses `json_each()` for exact JSON array element comparison. Knowledge graph queries (facts, timeline) available via same CLI skill.

### GPS Location Tracking
Overland GPS webhook receiver (`webhook_receiver.py`) ingests location pings and detects place transitions. Runs as a separate FastAPI service (`uvicorn istota.webhook_receiver:app`).

Config: `[location]` section — `enabled: bool = False`, `webhooks_port: int = 8765`.

Per-user config via `[[resources]]` with `type = "overland"`:
- `ingest_token`: shared secret for Overland endpoint
- `default_radius`: default geofence radius (meters)

Places (named geofences) stored in `places` DB table. Full CRUD via CLI (`learn`, `update`, `delete`) and web UI (create from discovered clusters, edit form, drag-to-reposition on map). Place detection uses asymmetric thresholds: opening a visit needs 2 consecutive pings at a place (hysteresis), closing one needs continuous "away" time to reach `visit_exit_minutes` (default 5) so brief GPS drift doesn't fragment a stay. Pings with `horizontal_accuracy > accuracy_threshold_m` (default 100 m) are stored but skipped for place matching and state-machine updates. A periodic reconciler (`reconcile_enabled`, default on) re-derives closed visits from pings in a recent window (`reconcile_lookback_hours` back, stopping `reconcile_buffer_minutes` before now) so historical visits recover from state-machine drift without touching the currently-open visit. Updating a place's location or radius triggers automatic ping reassignment (backfill nearby unassigned pings, unassign pings now outside radius).

Discovered clusters (computed on-the-fly from unassigned pings) can be dismissed instead of saved as places. Dismissals are stored in `dismissed_clusters` (per-user lat/lon/radius_meters); the discovery query filters out clusters whose center falls within any dismissed zone. Reversible via `DELETE /location/dismissed-clusters/{id}` from the places page (toggle "Show N dismissed" → click → confirm restore). The discovery query also computes a per-cluster `radius_meters` from the actual ping spread, so dismiss/save defaults match the discovered footprint instead of a fixed value.

DB tables: `location_pings`, `places`, `visits`, `location_state`, `dismissed_clusters`. Old pings cleaned after `location_ping_retention_days` (365).

### Authenticated Web Interface
SvelteKit frontend (`web/`) with FastAPI backend (`web_app.py`). Nextcloud OIDC for authentication. Runs as a separate service (`uvicorn istota.web_app:app`). Session-based auth via `SessionMiddleware`, 7-day cookie.

Backend routes: `/istota/login` (OIDC redirect), `/istota/callback` (token exchange), `/istota/logout`, `/istota/api/me` (user info + features), `/istota/api/feeds` (Miniflux proxy), `/istota/api/feeds/entries/{id}` (mark single entry read), `/istota/api/feeds/entries/batch` (batch mark read), `/istota/api/location/*` (places CRUD, pings, day summary, trips, discover, place stats, dismissed-clusters CRUD). SvelteKit build served as static files for all other `/istota/*` paths. Money pages live at `/istota/money/*`, served by the same web service; backend routers from `istota.money.routes` mount at `/istota/money/api/*`.

Frontend: SvelteKit with `adapter-static`, dark theme (matching feed page design). Dashboard shows available features. Feeds page has masonry card grid, image/text filter, sort by published/added, grid/list view, image lightbox, viewport-based read tracking. Location pages: today (current visit + day stats + trips/stops, all in a full-width bottom bar with collapsible details panel) and history (date picker, activity filter, heatmap, **Discover** chip that overlays unknown clusters + dismissed zones onto the same map). The standalone Places page was folded into history so cluster discovery happens in the same spatial context as the actual ping/track data. Sidebar (shared by both pages) hosts the place list, per-place visit stats, edit form, drag-to-reposition affordance, and a **+ New place** action that puts the active map into pick mode. Reads directly from Miniflux API via the backend proxy (no static file generation).

Read tracking: IntersectionObserver marks entries as read in Miniflux after 1.5s visible in viewport (half-visible threshold). Batch API calls debounced at 3s intervals. Read cards render at reduced opacity (85%, full on hover). "New" filter chip shows only unread entries. Status badge shows unread/total count.

User verification: `preferred_username` from OIDC must exist in `config.users`. Session rotated on login (cleared before writing user info). CSRF protection via Origin header validation on state-changing endpoints (PUT/POST/DELETE). Config: `[web]` section — `enabled`, `port`, `oidc_issuer`, `oidc_client_id`, `oidc_client_secret`, `session_secret_key`. Secrets via env vars: `ISTOTA_OIDC_CLIENT_SECRET`, `ISTOTA_WEB_SECRET_KEY`.

Deploy requires Node.js for `npm run build`. Ansible handles this when `istota_web_enabled` is set.

**Frontend primitives** (`web/src/lib/components/ui/`): `AppShell` (fullscreen flex shell with breakout margin + mobile breakpoint), `ShellHeader` (h1 + nav + tools), `Sidebar` (header + scrollable list, default 220px, mobile slide-in), `SidebarToggle`, `CategoryGroup` (uppercase label + optional count + optional `collapsible`), `NavLink` (pill-styled route link), `Chip` (toggleable pill button), `Button` (variants `primary`/`pill`/`ghost`/`subtle`/`danger-icon`), `Select` (bits-ui Select wrapper), `Modal` (bits-ui Dialog wrapper). All four route layouts (feeds, location, money, money/transactions) are built on these. Import via `import { AppShell, … } from '$lib/components/ui'`.

**Alignment system**: chip/nav-link horizontal padding lives in `--chip-padding-x` (`app.css`); chip-row gap lives in `--chip-gap`. The `.nav-hang` utility class applies `margin-inline-start: calc(-1 * var(--chip-padding-x))` so the *text* of a leftmost chip aligns with heading text on the row above (the bg pill hangs into the parent's left padding — standard hanging-pill pattern). Applied globally to `.money-section-nav` so all tertiary navs inherit it.

**Local UI dev**: `VITE_MOCK_API=1 npm run dev` runs the dev server with an in-process mock backend (`web/vite-mock-api.ts`) that intercepts `/istota/api/*` and `/istota/money/api/*`. Lets you iterate on UI tweaks with HMR without booting FastAPI / Nextcloud / Postgres. The mock holds places, dismissed clusters, and discovered clusters in memory and mutates them on POST/PUT/DELETE so you can exercise the full place-creation / dismiss / restore flows; state resets on dev-server restart. Without the env var, the original proxy-to-`localhost:8766` behavior is unchanged.

### Filesystem Sandbox (bubblewrap)
Per-user filesystem isolation via `bwrap`. Non-admins see only their Nextcloud subtree + system libs. Admins see full mount + DB (RO by default). **Linux + bubblewrap is the only supported deployment** — non-Linux / no-bwrap setups still run but provide no isolation guarantees and are dev-only. Scheduler logs `SECURITY UNSUPPORTED CONFIGURATION` at startup when sandbox is unavailable or disabled with multiple users configured.

### Network Isolation (CONNECT Proxy)
When `[security.network] enabled`, each task's sandbox gets `--unshare-net` (own network namespace, no external connectivity). Outbound traffic goes through a CONNECT proxy on a Unix socket (`network_proxy.py`) that only tunnels allowlisted `host:port` pairs. A TCP-to-Unix bridge script inside the sandbox listens on `127.0.0.1:18080` and forwards to the proxy socket. Claude Code sees `HTTPS_PROXY=http://127.0.0.1:18080`.

Default allowlist: `api.anthropic.com:443`, `mcp-proxy.anthropic.com:443` (Claude API), `pypi.org:443`, `files.pythonhosted.org:443` (package installs, configurable via `allow_pypi`). Per-user resource hosts (Miniflux, etc.) scoped to current task's user only. Git remote hosts added from `[developer]` config when the developer skill is selected. Operator extras via `extra_hosts`. No MITM — TLS is end-to-end. Config: `[security.network]` section.

### Credential Isolation (Skill Proxy)
When `skill_proxy_enabled`, secret env vars (CALDAV_PASSWORD, NC_PASS, SMTP_PASSWORD, IMAP_PASSWORD, KARAKEEP_API_KEY, MINIFLUX_API_KEY, GITLAB_TOKEN, GITHUB_TOKEN, MONARCH_SESSION_TOKEN, GOOGLE_WORKSPACE_CLI_TOKEN) are stripped from Claude's env. Skill CLI commands run through a Unix socket proxy (`skill_proxy.py`) in the executor thread, which injects credentials server-side. The proxy's allowed skill list is derived from the skill index (`cli: true` in metadata) — no hardcoded allowlist. **Credential authorization is decoupled from skill selection**: a skill's credentials are injectable if any of its mapped credentials is actually present in the user's env (i.e. the user has the corresponding resource or the instance has the relevant config). Skill selection still controls which docs are loaded, not credential access — see `_authorized_skills_from_credentials()` in `executor.py`. Every proxy rejection emits a structured WARNING (`proxy_rejected task_id=… type=skill|credential reason=…`) for observability. The `istota-skill` client connects to the socket or falls back to direct execution when the proxy is disabled. Config: `[security]` section, `skill_proxy_enabled`, `skill_proxy_timeout`.

### Deferred DB Operations
With sandbox, Claude writes JSON request files to temp dir (`ISTOTA_DEFERRED_DIR`). Scheduler processes after successful completion. Patterns: `task_{id}_subtasks.json`, `task_{id}_tracked_transactions.json`, `task_{id}_email_output.json`, `task_{id}_sent_emails.json`, `task_{id}_user_alerts.json`. Identity fields (`user_id`, `conversation_token`) always come from the task, not from deferred JSON — prevents spoofing via prompt injection. User alerts (`user_alerts.json`) are posted to the user's alerts channel (Talk) for suspicious inbound content (social engineering, prompt injection, exfil attempts).

Subtask creation is rate-limited per task to bound prompt-injection blast radius: `scheduler.max_subtasks_per_task` (default 10) caps fan-out per parent, `scheduler.max_subtask_depth` (default 3) refuses creation when the parent chain already at-or-past the cap, and `scheduler.max_subtask_prompt_chars` (default 8000) skips oversize prompts. Subtask creation is also admin-only.

### Scheduler Robustness
- Stale confirmations auto-cancelled after 120 min
- Stuck/ancient tasks auto-failed
- Old tasks/logs cleaned after `task_retention_days` (7)

## Testing

TDD with pytest + pytest-asyncio, class-based tests, `unittest.mock`. Real SQLite via `tmp_path`. Integration tests marked `@pytest.mark.integration`. Current: ~3450 tests across 65 files.

```bash
uv run pytest tests/ -v                              # Unit tests
uv run pytest -m integration -v                       # Integration tests
uv run pytest tests/ --cov=istota --cov-report=term-missing  # Coverage
```

## Development Commands

```bash
uv sync                                          # Install dependencies
uv run istota init                                 # Initialize database
uv run istota task "prompt" -u USER -x [--dry-run] # Execute task (--dry-run shows prompt)
uv run istota task "prompt" -u USER -t ROOM -x     # With conversation context
uv run istota resource add -u USER -t TYPE -p PATH # Add resource
uv run istota resource list -u USER                # List resources
uv run istota run [--once] [--briefings]           # Process pending tasks
uv run istota email list|poll|test                 # Email commands
uv run istota user list|lookup|init|status         # User management
uv run istota calendar discover|test               # Calendar commands
uv run istota tasks-file poll|status [-u USER]     # TASKS.md commands
uv run istota kv get|set|list|delete|namespaces    # Key-value store
uv run istota list [-s STATUS] [-u USER]           # List tasks
uv run istota show <task-id>                       # Task details
uv run istota-scheduler [-d] [-v] [--max-tasks N]  # Scheduler (daemon/single)
```

## Configuration

Config searched: `config/config.toml` → `~/src/config/config.toml` → `~/.config/istota/config.toml` → `/etc/istota/config.toml`. Override: `-c PATH`.

Per-user config: `config/users/{user_id}.toml` — takes precedence over `[users]` in main config.

CalDAV derived from Nextcloud settings. Logging via `[logging]` section; CLI `-v` overrides to DEBUG.

## Docker Deployment

Full stack in `docker/docker-compose.yml`: postgres (Nextcloud DB), redis (NC session cache), nextcloud (with auto-provisioning), istota (scheduler + Claude Code).

```bash
cd docker && cp .env.example .env  # Edit: set CLAUDE_CODE_OAUTH_TOKEN + passwords
docker compose up -d
```

File access uses a shared Docker volume (`shared_files`) mounted RW in both containers. Nextcloud External Storage app presents it to users. NC's native data volume is mounted RO in istota at `/mnt/nc-data` for Talk attachment fallback. Sandbox, skill proxy, and network proxy are disabled (container provides isolation).

Key env vars: `CLAUDE_CODE_OAUTH_TOKEN`, `ADMIN_PASSWORD`, `USER_NAME`, `USER_PASSWORD`, `BOT_PASSWORD`, `POSTGRES_PASSWORD`.

## Ansible Deployment

Role at `deploy/ansible/` (symlinked from `~/Repos/ansible-server/roles/istota/`). When adding config fields, update `defaults/main.yml` and `templates/config.toml.j2`.

## Nextcloud File Access

Mounted at `/srv/mount/nextcloud/content` via rclone. Setup via Ansible (`istota_use_nextcloud_mount: true`).

## Task Status Values

`pending` → `locked` → `running` → `completed`/`failed`/`pending_confirmation` → `cancelled`
