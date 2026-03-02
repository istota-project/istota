# Istota - Claude Code Bot

Claude Code-powered assistant bot with Nextcloud Talk interface.

**Production server**: `your-server` (accessible via SSH, installed at `/srv/app/istota`)

## Project Structure

```
istota/
├── src/istota/
│   ├── briefing.py          # Briefing prompt builder with pre-fetching
│   ├── briefing_loader.py   # Workspace BRIEFINGS.md loading + admin defaults merging
│   ├── cron_loader.py       # CRON.md loading + DB sync for scheduled jobs
│   ├── cli.py               # CLI for local testing and administration
│   ├── config.py            # TOML configuration loading
│   ├── context.py           # Conversation context selection (Sonnet-based)
│   ├── db.py                # SQLite operations (all tables)
│   ├── email_poller.py      # Email polling and task creation
│   ├── executor.py          # Claude Code execution wrapper (Popen + stream-json)
│   ├── heartbeat.py         # Heartbeat monitoring system
│   ├── invoice_scheduler.py # Scheduled invoice generation + reminders
│   ├── location_loader.py   # LOCATION.md parser + place sync
│   ├── webhook_receiver.py   # FastAPI webhook receiver (Overland GPS, etc.)
│   ├── logging_setup.py     # Central logging configuration
│   ├── nextcloud_api.py     # Nextcloud API user metadata hydration
│   ├── nextcloud_client.py  # Shared Nextcloud HTTP plumbing (OCS + WebDAV)
│   ├── notifications.py     # Central notification dispatcher (Talk, Email, ntfy)
│   ├── scheduler.py         # Task processor, briefing scheduler, all polling
│   ├── shared_file_organizer.py # Auto-organize files shared with bot
│   ├── skills_loader.py     # Thin wrapper re-exporting from skills/_loader.py
│   ├── sleep_cycle.py       # Nightly memory extraction
│   ├── storage.py           # Bot-managed Nextcloud storage
│   ├── stream_parser.py     # Parse stream-json events
│   ├── commands.py          # !command dispatch (help, stop, status, memory, cron, skills, usage, check, export)
│   ├── talk.py              # Nextcloud Talk API client (user API)
│   ├── talk_poller.py       # Talk conversation polling
│   ├── tasks_file_poller.py # TASKS.md file monitoring
│   ├── memory_search.py     # Hybrid BM25 + vector search over conversations/memories
│   └── skills/              # Self-contained skill directories (skill.toml + skill.md + optional Python)
│       ├── _types.py        # SkillMeta, EnvSpec dataclasses
│       ├── _loader.py       # Skill discovery, manifest loading, doc resolution
│       ├── _env.py          # Declarative env var resolver + setup_env() hook dispatch
│       ├── accounting/      # Beancount ledger ops + Monarch Money sync + invoicing
│       ├── bookmarks/       # Karakeep bookmark management
│       ├── briefing/        # Briefing format reference (doc-only)
│       ├── briefings_config/ # User briefing schedule config (doc-only)
│       ├── browse/          # Web browsing CLI (Docker container API)
│       ├── calendar/        # CalDAV operations CLI
│       ├── developer/       # Git/GitLab/GitHub workflows (doc-only)
│       ├── feeds_config/    # Feed subscription config (doc-only)
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
│       ├── garmin/          # Garmin Connect data access (activities, stats, health)
│       ├── location/       # GPS location tracking CLI (Overland)
│       └── whisper/         # Audio transcription via faster-whisper
├── config/
│   ├── config.toml          # Active configuration (gitignored)
│   ├── config.example.toml  # Example configuration
│   ├── users/               # Per-user config files (override [users] section)
│   ├── emissaries.md        # Constitutional principles (global only, not user-overridable)
│   ├── persona.md           # Default personality (user workspace PERSONA.md overrides)
│   ├── guidelines/          # Channel-specific formatting (talk.md, email.md, briefing.md)
│   └── skills/              # Operator override directory (empty by default)
├── deploy/
│   ├── ansible/             # Ansible role (defaults, tasks, handlers, templates)
│   ├── render_config.py     # Python config generator for install.sh
│   ├── install.sh           # Main deployment script
│   └── README.md            # Deployment documentation
├── docker/browser/          # Playwright browser container (Flask API)
├── scripts/                 # setup.sh, scheduler.sh
├── tests/                   # pytest + pytest-asyncio (~2400 tests, 51 files)
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

```

- **Talk poller**: Background daemon thread, long-polling per conversation, WAL mode for concurrent DB access
- **Email poller**: Polls INBOX via imap-tools, creates tasks from known senders
- **Task queue** (`db.py`): Atomic locking with `user_id` filter, retry logic (exponential backoff: 1, 4, 16 min)
- **Scheduler**: Per-user threaded worker pool. Three-tier concurrency: instance-level fg/bg caps, per-user limits. Workers keyed by `(user_id, queue_type, slot)`.
- **Executor**: Builds prompts (resources + skills + context + memory), invokes Claude Code via `Popen` with `--output-format stream-json`. Auto-retries transient API errors (5xx, 429) up to 3 times.
- **Context** (`context.py`): Hybrid triage — recent N messages always included, older messages selected by LLM
- **Storage** (`storage.py`): Bot-owned Nextcloud directories and user memory files

## Key Design Decisions

### Admin/Non-Admin User Isolation
Admin users listed in `/etc/istota/admins`. Empty file = all users are admin (backward compat). Override path via `ISTOTA_ADMINS_FILE`.

Non-admin restrictions: scoped mount path, no DB access, no subtask creation, `admin_only` skills filtered out.

### Multi-user Resources
Resources defined in per-user config or DB, merged at task time. Types: `calendar`, `folder`, `todo_file`, `email_folder`, `shared_file`, `reminders_file`, `ledger`. CalDAV calendars auto-discovered from Nextcloud.

### Nextcloud Directory Structure

```
/Users/{user_id}/
├── {bot_name}/      # Shared with user via OCS
│   ├── config/      # USER.md, TASKS.md, BRIEFINGS.md, PERSONA.md, etc.
│   ├── exports/     # Bot-generated files
│   └── examples/    # Documentation and config reference
├── inbox/           # Files user wants bot to process
├── memories/        # Dated memories (sleep cycle): YYYY-MM-DD.md
├── shared/          # Auto-organized files shared by user
└── scripts/         # User's reusable Python scripts

/Channels/{conversation_token}/
├── CHANNEL.md       # Persistent channel memory
└── memories/        # Channel sleep cycle memories
```

### Memory System
- **User memory** (`USER.md`): Auto-loaded into prompts (except briefings). Optional nightly curation via `curate_user_memory` (sleep cycle promotes durable facts from dated memories).
- **Channel memory** (`CHANNEL.md`): Loaded when `conversation_token` set
- **Dated memories** (`memories/YYYY-MM-DD.md`): Auto-loaded into prompts (last N days via `auto_load_dated_days`, default 3). Includes task provenance references (`ref:TASK_ID`).
- **Memory recall** (BM25): Auto-recall via `auto_recall` config — searches indexed memories/conversations using task prompt as query, independent of context triage.
- **Memory cap** (`max_memory_chars`): Limits total memory in prompts. Truncation order: recalled → dated → warn about user/channel. Default 0 (unlimited).
- Briefings exclude all personal memory to prevent leaking into newsletter-style output

### Talk Integration
Polling-based (user API, not bot API). Istota runs as a regular Nextcloud user.

- Long-polling per conversation, message cache in `talk_messages` table
- Progress updates: random ack before execution, streaming progress (rate-limited: min 8s, max 5/task). `progress_style`: `replace` (edit ack in-place, default), `full` (append), `none` (silent)
- Per-user log channel (`log_channel` config): verbose tool-by-tool execution logs posted to a dedicated Talk room
- Multi-user rooms: only responds when @mentioned; 2-person rooms behave like DMs
- `!commands`: intercepted in poller before task creation — `!help`, `!stop`, `!status`, `!memory`, `!cron`, `!usage`, `!check`, `!export` (conversation history export), `!skills` (list available skills)
- Confirmation flow: regex-detected → `pending_confirmation` → user replies yes/no

### Skills
Self-contained directories under `src/istota/skills/`, each with `skill.toml` manifest and `skill.md` doc. Selection based on: `always_include`, `source_types`, `keywords` (if skill also has `resource_types`, requires both keyword match + user has resource), `file_types`, `companion_skills`. Skills can be excluded via `disabled_skills` at instance level (top-level config) and per-user level (user config), both merged at selection time.

Audio attachments pre-transcribed before skill selection so keyword matching works on voice memos.

Env var wiring is declarative via `[[env]]` in `skill.toml`. Action skills expose `python -m istota.skills.<name>` CLI with JSON output.

### Conversation Context
Talk tasks use a poller-fed local cache (`talk_messages` table, bounded by `talk_cache_max_per_conversation`). Email tasks use DB-based context. Both paths use hybrid selection: recent N messages always included, older messages triaged by LLM. Recency window (`context_recency_hours`, default 0 = disabled) filters out old messages while guaranteeing at least `context_min_messages` (10). Config in `[conversation]` section.

### Input Channels
- **Talk**: Long-polling, message cache, referenceId tagging for ack/progress/result messages
- **Email**: IMAP polling, attachments to `/Users/{user_id}/inbox/`, threaded replies. Output via `python -m istota.skills.email output` (deferred file pattern)
- **TASKS.md**: Polls user config file (30s). Status markers: `[ ]` `[~]` `[x]` `[!]`. Identity via SHA-256 hash.

### Briefings
Sources: user `BRIEFINGS.md` > per-user config > main config. Cron in user's timezone. Components: `calendar`, `todos`, `email`, `markets`, `news`, `reminders`. Market data pre-fetched. Memory isolated from briefing prompts.

### Scheduled Jobs
Defined in user's `CRON.md` (markdown with TOML `[[jobs]]`). Job types: `prompt` (Claude Code) or `command` (shell). One-time jobs (`once = true`) auto-deleted after success. Auto-disable after 5 consecutive failures. Results excluded from interactive context. Per-job `skip_log_channel = true` suppresses log channel output for frequent jobs.

### Sleep Cycle
Nightly memory extraction (direct subprocess). Gathers completed tasks → Claude extracts memories → writes dated memory files with task provenance (`ref:TASK_ID`). Channel sleep cycle runs in parallel for shared context. Optional USER.md curation pass (`curate_user_memory`). Config: `[sleep_cycle]`, `[channel_sleep_cycle]`.

### Heartbeat Monitoring
User-defined health checks in `HEARTBEAT.md`. Types: `file-watch`, `shell-command`, `url-health`, `calendar-conflicts`, `task-deadline`, `self-check`. Cooldown, check intervals, and quiet hours supported.

### Memory Search
Hybrid BM25 + vector search using sqlite-vec and sentence-transformers. Auto-indexes conversations and memory files. Channel support via `channel:{token}` namespace. Degrades to BM25-only if deps unavailable. Optional: `uv sync --extra memory-search`.

### Invoicing System
Config-driven invoice generation (`INVOICING.md`) with PDF export via WeasyPrint. Cash-basis accounting — income recognized at payment time. Multi-entity support. Work log in `_INVOICES.md`. Scheduled generation for `schedule = "monthly"` clients. Overdue detection with notifications.

### GPS Location Tracking
Overland GPS webhook receiver (`webhook_receiver.py`) ingests location pings and detects place transitions. Runs as a separate FastAPI service (`uvicorn istota.webhook_receiver:app`).

Config: `[location]` section — `enabled: bool = False`, `webhooks_port: int = 8765`.

Per-user config via `LOCATION.md` in the user's bot config folder (TOML format):
- `[settings]`: `ingest_token`, `default_radius`
- `[[places]]`: named locations with `lat`, `lon`, `radius_meters`, `category`
- `[[actions]]`: triggered on `enter`/`exit` events — surfaces: `ntfy`, `talk`, `silent`, `cron_prompt`

Place detection uses hysteresis (2 consecutive pings required) to avoid flapping. LOCATION.md is reloaded on every ping batch — changes take effect without restart.

DB tables: `location_pings`, `location_places`, `location_visits`, `location_state`. Old pings cleaned after `location_ping_retention_days` (365).

### Filesystem Sandbox (bubblewrap)
Per-user filesystem isolation via `bwrap`. Non-admins see only their Nextcloud subtree + system libs. Admins see full mount + DB (RO by default). No network isolation. Graceful degradation if not Linux or bwrap not found.

### Deferred DB Operations
With sandbox, Claude writes JSON request files to temp dir (`ISTOTA_DEFERRED_DIR`). Scheduler processes after successful completion. Patterns: `task_{id}_subtasks.json`, `task_{id}_tracked_transactions.json`, `task_{id}_email_output.json`.

### Scheduler Robustness
- Stale confirmations auto-cancelled after 120 min
- Stuck/ancient tasks auto-failed
- Old tasks/logs cleaned after `task_retention_days` (7)

## Testing

TDD with pytest + pytest-asyncio, class-based tests, `unittest.mock`. Real SQLite via `tmp_path`. Integration tests marked `@pytest.mark.integration`. Current: ~2400 tests across 51 files.

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

## Ansible Deployment

Role at `deploy/ansible/` (symlinked from `~/Repos/ansible-server/roles/istota/`). When adding config fields, update `defaults/main.yml` and `templates/config.toml.j2`.

Fava: per-user systemd services for Beancount ledger viewing. Controlled by `istota_fava_enabled`.

## Nextcloud File Access

Mounted at `/srv/mount/nextcloud/content` via rclone. Setup via Ansible (`istota_use_nextcloud_mount: true`).

## Task Status Values

`pending` → `locked` → `running` → `completed`/`failed`/`pending_confirmation` → `cancelled`
