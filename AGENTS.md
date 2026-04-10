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
│   ├── executor.py          # Claude Code execution wrapper (Popen + stream-json)
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
│   ├── scheduler.py         # Task processor, briefing scheduler, all polling
│   ├── shared_file_organizer.py # Auto-organize files shared with bot
│   ├── sleep_cycle.py       # Nightly memory extraction
│   ├── storage.py           # Bot-managed Nextcloud storage
│   ├── stream_parser.py     # Parse stream-json events
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
│       ├── reminders/       # Time-based reminders via CRON.md (doc-only)
│       ├── schedules/       # CRON.md job management reference (doc-only)
│       ├── scripts/         # User scripts reference (doc-only)
│       ├── sensitive_actions/ # Confirmation rules (doc-only)
│       ├── tasks/           # Subtask/queue reference (doc-only)
│       ├── todos/           # Todo list reference (doc-only)
│       ├── transcribe/      # OCR transcription via Tesseract
│       ├── website/         # Website management reference (doc-only)
│       ├── google_workspace/ # Google Workspace CLI wrapper (Drive, Gmail, Calendar, Sheets, Docs)
│       ├── location/       # GPS location tracking + calendar attendance (Overland)
│       ├── moneyman/       # Moneyman accounting API client (ledger, invoicing, work log)
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
│   │   ├── routes/          # Pages: dashboard, feeds, ledgers
│   │   └── lib/             # API client, components (FeedCard, Lightbox)
│   ├── svelte.config.js     # adapter-static, base path /istota
│   └── vite.config.ts       # Dev proxy to FastAPI
├── scripts/                 # setup.sh, scheduler.sh
├── tests/                   # pytest + pytest-asyncio (~2760 tests, 56 files)
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
- **Executor**: Builds prompts (resources + skills + context + memory), invokes Claude Code via `Popen` with `--output-format stream-json`. Auto-retries transient API errors (5xx, 429) up to 3 times. Validates output for malformed model responses (leaked tool-call XML) and collects execution traces for post-hoc inspection.
- **Context** (`context.py`): Hybrid triage — recent N messages always included, older messages selected by LLM
- **Storage** (`storage.py`): Bot-owned Nextcloud directories and user memory files

## Key Design Decisions

### Admin/Non-Admin User Isolation
Admin users listed in `/etc/istota/admins`. Empty file = all users are admin (backward compat). Override path via `ISTOTA_ADMINS_FILE`.

Non-admin restrictions: scoped mount path, no DB access, no subtask creation, `admin_only` skills filtered out.

### Multi-user Resources
Resources defined in per-user config or DB, merged at task time. Types: `calendar`, `folder`, `todo_file`, `email_folder`, `shared_file`, `reminders_file`, `ledger`, `karakeep`, `monarch`, `miniflux`, `moneyman`. CalDAV calendars auto-discovered from Nextcloud. Service credentials (Monarch, Karakeep, Miniflux, Moneyman) are configured as `[[resources]]` entries with type-specific fields in `extra`.

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

**Two-pass selection:** Pass 1 is keyword matching (deterministic, zero-cost): `always_include`, `source_types`, `triggers`/`keywords` (if skill also has `resource_types`, requires both keyword match + user has resource), `file_types`, `companion_skills`. Pass 2 is LLM-based semantic routing (Haiku, ~500ms, ~$0.0003/task): sees the task prompt + a manifest of unselected skills, returns additional skills to load. Results are unioned. Pass 2 is additive — on timeout/error, falls back to Pass 1 only. Config: `[skills]` section.

Skills can exclude other skills via `exclude_skills` (e.g., briefing excludes email to prevent delivery interference). Skills can also be excluded via `disabled_skills` at instance level (top-level config) and per-user level (user config), both merged at selection time.

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
Defined in user's `CRON.md` (markdown with TOML `[[jobs]]`). Job types: `prompt` (Claude Code), `prompt_file` (prompt loaded from external file), or `command` (shell). `prompt_file` paths are relative to the Nextcloud mount root and resolved at load time. One-time jobs (`once = true`) auto-deleted after success. Auto-disable after 5 consecutive failures. Results excluded from interactive context. Per-job `skip_log_channel = true` suppresses log channel output for frequent jobs.

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

Places (named geofences) stored in `places` DB table. Full CRUD via CLI (`learn`, `update`, `delete`) and web UI (create from discovered clusters, edit form, drag-to-reposition on map). Place detection uses hysteresis (2 consecutive pings required) to avoid flapping. Updating a place's location or radius triggers automatic ping reassignment (backfill nearby unassigned pings, unassign pings now outside radius).

DB tables: `location_pings`, `places`, `visits`, `location_state`. Old pings cleaned after `location_ping_retention_days` (365).

### Authenticated Web Interface
SvelteKit frontend (`web/`) with FastAPI backend (`web_app.py`). Nextcloud OIDC for authentication. Runs as a separate service (`uvicorn istota.web_app:app`). Session-based auth via `SessionMiddleware`, 7-day cookie.

Backend routes: `/istota/login` (OIDC redirect), `/istota/callback` (token exchange), `/istota/logout`, `/istota/api/me` (user info + features), `/istota/api/auth-check` (nginx auth_request for Fava proxy), `/istota/api/feeds` (Miniflux proxy), `/istota/api/feeds/entries/{id}` (mark single entry read), `/istota/api/feeds/entries/batch` (batch mark read), `/istota/api/moneyman/ledgers` (Moneyman API proxy), `/istota/api/moneyman/fava` (Fava instance discovery), `/istota/api/location/*` (places CRUD, pings, day summary, trips, discover, place stats). SvelteKit build served as static files for all other `/istota/*` paths. Fava instances reverse-proxied via nginx at `/istota/fava/{user}/{ledger}/` with `auth_request` gating.

Frontend: SvelteKit with `adapter-static`, dark theme (matching feed page design). Dashboard shows available features. Feeds page has masonry card grid, image/text filter, sort by published/added, grid/list view, image lightbox, viewport-based read tracking. Location pages: today view (current position, day summary, trips), history (date picker, activity filter, heatmap), places (discover unknown clusters, create/edit/delete places). Place sidebar with visit stats (derived from pings), edit form, drag-to-reposition on map. Ledgers page lists beancount ledgers with links to Fava instances. Reads directly from Miniflux API via the backend proxy (no static file generation).

Read tracking: IntersectionObserver marks entries as read in Miniflux after 1.5s visible in viewport (half-visible threshold). Batch API calls debounced at 3s intervals. Read cards render at reduced opacity (85%, full on hover). "New" filter chip shows only unread entries. Status badge shows unread/total count.

User verification: `preferred_username` from OIDC must exist in `config.users`. Session rotated on login (cleared before writing user info). CSRF protection via Origin header validation on state-changing endpoints (PUT/POST/DELETE). Config: `[web]` section — `enabled`, `port`, `oidc_issuer`, `oidc_client_id`, `oidc_client_secret`, `session_secret_key`. Secrets via env vars: `ISTOTA_OIDC_CLIENT_SECRET`, `ISTOTA_WEB_SECRET_KEY`.

Deploy requires Node.js for `npm run build`. Ansible handles this when `istota_web_enabled` is set.

### Filesystem Sandbox (bubblewrap)
Per-user filesystem isolation via `bwrap`. Non-admins see only their Nextcloud subtree + system libs. Admins see full mount + DB (RO by default). Graceful degradation if not Linux or bwrap not found.

### Network Isolation (CONNECT Proxy)
When `[security.network] enabled`, each task's sandbox gets `--unshare-net` (own network namespace, no external connectivity). Outbound traffic goes through a CONNECT proxy on a Unix socket (`network_proxy.py`) that only tunnels allowlisted `host:port` pairs. A TCP-to-Unix bridge script inside the sandbox listens on `127.0.0.1:18080` and forwards to the proxy socket. Claude Code sees `HTTPS_PROXY=http://127.0.0.1:18080`.

Default allowlist: `api.anthropic.com:443`, `mcp-proxy.anthropic.com:443` (Claude API), `pypi.org:443`, `files.pythonhosted.org:443` (package installs, configurable via `allow_pypi`). Per-user resource hosts (Miniflux, Moneyman) scoped to current task's user only. Git remote hosts added from `[developer]` config when the developer skill is selected. Operator extras via `extra_hosts`. No MITM — TLS is end-to-end. Config: `[security.network]` section.

### Credential Isolation (Skill Proxy)
When `skill_proxy_enabled`, secret env vars (CALDAV_PASSWORD, NC_PASS, SMTP_PASSWORD, IMAP_PASSWORD, KARAKEEP_API_KEY, MINIFLUX_API_KEY, MONEYMAN_API_KEY, GITLAB_TOKEN, GITHUB_TOKEN, MONARCH_SESSION_TOKEN, GOOGLE_WORKSPACE_CLI_TOKEN) are stripped from Claude's env. Skill CLI commands run through a Unix socket proxy (`skill_proxy.py`) in the executor thread, which injects credentials server-side. The proxy's allowed skill list is derived from the skill index (`cli: true` in metadata) — no hardcoded allowlist. All CLI-capable skills get their credentials through the proxy regardless of whether they were selected for the current task — skill selection controls which docs are loaded, not credential access. The `istota-skill` client connects to the socket or falls back to direct execution when the proxy is disabled. Config: `[security]` section, `skill_proxy_enabled`, `skill_proxy_timeout`.

### Deferred DB Operations
With sandbox, Claude writes JSON request files to temp dir (`ISTOTA_DEFERRED_DIR`). Scheduler processes after successful completion. Patterns: `task_{id}_subtasks.json`, `task_{id}_tracked_transactions.json`, `task_{id}_email_output.json`, `task_{id}_sent_emails.json`, `task_{id}_user_alerts.json`. Identity fields (`user_id`, `conversation_token`) always come from the task, not from deferred JSON — prevents spoofing via prompt injection. User alerts (`user_alerts.json`) are posted to the user's alerts channel (Talk) for suspicious inbound content (social engineering, prompt injection, exfil attempts).

### Scheduler Robustness
- Stale confirmations auto-cancelled after 120 min
- Stuck/ancient tasks auto-failed
- Old tasks/logs cleaned after `task_retention_days` (7)

## Testing

TDD with pytest + pytest-asyncio, class-based tests, `unittest.mock`. Real SQLite via `tmp_path`. Integration tests marked `@pytest.mark.integration`. Current: ~2750 tests across 54 files.

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
