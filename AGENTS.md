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
│   ├── logging_setup.py     # Central logging configuration
│   ├── nextcloud_api.py     # Nextcloud API user metadata hydration
│   ├── notifications.py     # Central notification dispatcher (Talk, Email, ntfy)
│   ├── scheduler.py         # Task processor, briefing scheduler, all polling
│   ├── shared_file_organizer.py # Auto-organize files shared with bot
│   ├── skills_loader.py     # Thin wrapper re-exporting from skills/_loader.py
│   ├── sleep_cycle.py       # Nightly memory extraction
│   ├── storage.py           # Bot-managed Nextcloud storage
│   ├── stream_parser.py     # Parse stream-json events
│   ├── commands.py          # !command dispatch (help, stop, status, memory, cron)
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
│       ├── calendar/        # CalDAV helper functions
│       ├── developer/       # Git/GitLab/GitHub workflows (doc-only)
│       ├── feeds_config/    # Feed subscription config (doc-only)
│       ├── email/           # Native IMAP/SMTP operations
│       ├── files/           # Nextcloud file ops (mount-aware, rclone fallback)
│       ├── heartbeat/       # Heartbeat monitoring reference (doc-only)
│       ├── markets/         # yfinance wrapper + FinViz scraping + interactive CLI (quote, summary, finviz)
│       ├── memory/          # Memory file reference (doc-only)
│       ├── memory_search/   # Memory search CLI (search, index, reindex, stats)
│       ├── nextcloud/       # Nextcloud sharing reference (doc-only)
│       ├── notes/           # Notes file reference (doc-only)
│       ├── reminders/       # Time-based reminders via CRON.md (doc-only)
│       ├── schedules/       # CRON.md job management reference (doc-only)
│       ├── scripts/         # User scripts reference (doc-only)
│       ├── sensitive_actions/ # Confirmation rules (doc-only)
│       ├── tasks/           # Subtask/queue reference (doc-only)
│       ├── todos/           # Todo list reference (doc-only)
│       ├── transcribe/      # OCR transcription via Tesseract
│       ├── website/         # Website management reference (doc-only)
│       └── whisper/         # Audio transcription via faster-whisper (CPU, int8)
├── config/
│   ├── config.toml          # Active configuration (gitignored)
│   ├── config.example.toml  # Example configuration
│   ├── users/               # Per-user config files (override [users] section)
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
├── tests/                   # pytest + pytest-asyncio (~1964 tests, 43 files)
├── schema.sql
└── pyproject.toml
```

## Architecture

```
Talk Poll ──►┐
Email Poll ──►├─► SQLite Queue → Scheduler → Claude Code → Talk/Email Response
TASKS.md ────►│
CLI ─────────►┘
```

- **Talk poller**: Background daemon thread, long-polling per conversation, WAL mode for concurrent DB access
- **Email poller**: Polls INBOX via imap-tools, creates tasks from known senders
- **Task queue** (`db.py`): Atomic locking with optional `user_id` filter, retry logic (exponential backoff: 1, 4, 16 min), resource permissions
- **Scheduler**: Per-user worker pool (threaded). Main loop dispatches `UserWorker` threads; each processes tasks serially for one user. `WorkerPool` manages three-tier concurrency: instance-level fg cap (`max_foreground_workers`, default 5), instance-level bg cap (`max_background_workers`, default 3), and per-user limits (global default via `user_max_foreground_workers`/`user_max_background_workers`, overridable per user). Idle timeout via `worker_idle_timeout` (default 30s). Worker ID = hostname-pid-user_id
- **Executor**: Builds prompts (resources + skills + context + memory), invokes Claude Code via `Popen` with `--output-format stream-json`. Auto-discovers CalDAV calendars. 10 min timeout. Auto-retries transient API errors (5xx, 429) up to 3 times before counting against task attempts
- **Context** (`context.py`): Sonnet selects relevant previous messages; ≤3 messages included without selection
- **Storage** (`storage.py`): Bot-owned Nextcloud directories and user memory files

## Key Design Decisions

### Admin/Non-Admin User Isolation
Admin users listed in `/etc/istota/admins` (root-owned, one user ID per line). `Config.is_admin(user_id)` returns True if file is missing/empty (backward compat) or user is in set. Override path via `ISTOTA_ADMINS_FILE` env var (for testing).

**Non-admin restrictions:**
- Prompt: scoped mount path (`{mount}/Users/{user_id}`), no DB path, no sqlite3 tool, no subtask creation
- Env vars: `ISTOTA_DB_PATH` omitted, `NEXTCLOUD_MOUNT_PATH` scoped to user directory
- Skills: `admin_only = true` skills (schedules, tasks) filtered out

### Multi-user Resource Permissions
Resources defined in per-user config (`config/users/{user_id}.toml`) or DB (from shared file organizer), merged at task time:
- `calendar`, `folder`, `todo_file`, `notes_file`, `email_folder`, `shared_file`, `reminders_file`, `ledger`

CalDAV calendars are auto-discovered if Nextcloud is configured; manual `calendar` resources are fallback.

### Per-User Config Files
Individual TOML files in `config/users/` (filename = user ID). Takes precedence over `[users]` in main config. Supports all user fields, `[[resources]]`, `[[briefings]]`, `[sleep_cycle]`. See `config/users/alice.example.toml`.

### Nextcloud API User Metadata Hydration
At startup, user configs enriched from Nextcloud API (display_name, email, timezone). Config values take precedence — API only fills gaps. Failures silently skipped.

### Nextcloud Directory Structure

```
/Users/{user_id}/
├── {bot_name}/      # Shared with user via OCS
│   ├── config/      # Configuration files (USER.md, TASKS.md, BRIEFINGS.md, PERSONA.md, etc.)
│   ├── exports/     # Bot-generated files
│   └── examples/    # Documentation and config reference
├── inbox/           # Files user wants bot to process
├── memories/        # Dated memories (sleep cycle): YYYY-MM-DD.md
├── shared/          # Auto-organized files shared by user
└── scripts/         # User's reusable Python scripts

/Channels/{conversation_token}/
├── CHANNEL.md       # Persistent channel memory
└── memories/        # Reserved for future use
```

Users only see their own `/Users/` subdirectory. The `{bot_name}/` folder is auto-shared back via OCS Sharing API.

### User & Channel Memory
- **User memory**: `/Users/{user_id}/{bot_name}/config/USER.md` — auto-loaded into prompts (except briefings), written via mount
- **Channel memory**: `/Channels/{token}/CHANNEL.md` — loaded when `conversation_token` set
- **Dated memories**: `/Users/{user_id}/memories/YYYY-MM-DD.md` — stored for on-demand search, NOT auto-loaded into prompts
- Briefing tasks exclude all personal memory (USER.md, dated memories) to prevent private context leaking into newsletter-style output
- Directories auto-created on first execution. Graceful degradation if Nextcloud unreachable.

### Shared File Auto-Organization
Scheduler scans root level periodically (default: 120s), determines owner via WebDAV PROPFIND, moves to `/Users/{owner}/shared/`, creates resource entries in DB.

### Talk Integration
Polling-based (user API, not bot API). Istota runs as a regular Nextcloud user.

- Background daemon thread with own asyncio event loop; main loop stays synchronous
- Long-polling (`lookIntoFuture=1`) per conversation, state in `talk_poll_state` table
- First poll: sets `lastKnownMessageId` to `latest_id - 1`, processes most recent message
- Only processes messages from configured users; ignores own messages

**Progress updates**: Random ack before execution, then streaming progress to Talk (rate-limited: min 8s apart, max 5/task). Ephemeral — not stored in results or context.

**File attachments**: Talk files appear in `/Talk/` folder, accessed via mount. `{file0}` placeholders replaced with `[filename]`.

**Message limits**: Truncated at 4000 chars.

**Reply tracking**: `talk_message_id`/`talk_response_id` on tasks. Replied-to task force-included in context. Fallback to `reply_to_content` if no DB match.

**Confirmation flow**: Regex-detected confirmation requests → `pending_confirmation` status → user replies yes/no → resume or cancel.

**Multi-user rooms**: In rooms with 3+ participants, the bot only responds when @mentioned (`mention-user` or `mention-federated-user` in `messageParameters`). Rooms with 2 participants (bot + 1 user) behave like DMs — respond to everything. Participant counts are cached (5 min TTL) via `get_participants()` API. `mention-call` (@all) is excluded to avoid responding to every broadcast. Bot's own mention is stripped from the prompt; other mentions are replaced with `@DisplayName`. Tasks created from group rooms have `is_group_chat=True`, and conversation context shows usernames as speaker labels instead of "User". Falls back to DM behavior on participants API failure. Final responses in group chats use `reply_to` on the first message part (threads the reply to the original message) and prepend `@{user_id}` so the triggering user gets a notification. Intermediate messages (ack, progress updates) are sent without reply threading to avoid noise.

**!Commands**: `!`-prefixed messages intercepted in the poller before task creation. Handled synchronously without Claude Code. Commands: `!help` (list commands), `!stop` (cancel active task via DB flag + SIGTERM), `!status` (show tasks grouped by interactive/background + system stats), `!memory user`/`!memory channel` (show memory files), `!cron` (list/enable/disable scheduled jobs), `!usage` (show Claude API usage/billing stats), `!check` (run system health check). Long responses split via `split_message()`. Registry in `commands.py` with decorator-based registration.

### Skills
Self-contained directories under `src/istota/skills/`, each with a `skill.toml` manifest and `skill.md` doc. `config/skills/` serves as an operator override directory (skill.md placed there takes precedence over bundled). Discovery uses `_loader.py` with layered priority: bundled `skill.toml` dirs < operator override dirs.

Selection criteria (from `skill.toml`):
- `always_include`: files, sensitive_actions, memory, scripts, memory_search
- `resource_types`: calendar → calendar, ledger → accounting, karakeep → bookmarks
- `source_types`: briefing → calendar, markets, notes, briefing
- `keywords`: pattern match on prompt (e.g., "email" → email skill)
- `file_types`: attachment extensions (e.g., `.wav` → whisper)
- `companion_skills`: when a skill is selected, also pull in listed companions (respects admin_only and dependency checks)
- `admin_only`: schedules, tasks (filtered out for non-admin users)

**Pre-transcription**: Audio attachments are transcribed before skill selection (`_pre_transcribe_attachments()` in executor.py). This enriches the prompt with actual spoken words so keyword-based skills (reminders, schedules, calendar) match naturally on voice memos.

Env var wiring is declarative via `[[env]]` sections in `skill.toml`. Skills with complex env setup export `setup_env(ctx)` in `__init__.py`.

**Skill CLI pattern**: Action skills expose `python -m istota.skills.<name>` CLI with `build_parser()`/`main()`, JSON output, env-var config. Executor passes credentials as env vars.

### Response Guidelines
- **Persona**: User workspace `PERSONA.md` overrides global `config/persona.md`. Seeded from global on first run. Always loaded.
- **Channel guidelines** (`config/guidelines/{source_type}.md`): Loaded per source type. Both optional.

### Conversation Context
Hybrid approach: recent N messages (`always_include_recent`, default 5) always included without model call; older messages triaged by selection model. Short histories (≤ `skip_selection_threshold`) skip selection entirely. Reply-to tasks force-included. `use_selection=false` disables triage and includes all lookback messages. `context_truncation` controls max chars per bot response (0 = no truncation). Config: `[conversation]` section (enabled, lookback_count=25, skip_selection_threshold=3, always_include_recent=5, selection_model=haiku, use_selection, context_truncation=0). Graceful degradation on errors — falls back to guaranteed recent messages.

**Actions tracking**: Tool use descriptions from streaming execution are stored as `actions_taken` (JSON array) on completed tasks. Context formatter appends compact `[Actions: ...]` lines after bot responses so the bot can see what tools it used previously. Capped at 15 actions, pipe-separated. Also included in triage text for the selection model.

### Email Input Channel
Polls INBOX via IMAP, creates tasks from known senders. Attachments uploaded to `/Users/{user_id}/inbox/`. Thread ID from normalized subject + participants. Responses sent as email replies with threading headers.

**Output format**: JSON `{"subject": "...", "body": "...", "format": "plain"|"html"}`. Falls back to raw text. Config: `[email]` section.

### TASKS.md File Input Channel
Polls `/Users/{user_id}/{bot_name}/config/TASKS.md` (default: 30s). Status markers: `[ ]` pending, `[~]` in-progress, `[x]` completed, `[!]` failed. Task identity via SHA-256 hash. Tracked in `istota_file_tasks` table.

### Briefings
Two sources (user config takes precedence): user `BRIEFINGS.md` > per-user config > main config. Merged at briefing name level. Empty/commented-out user file falls back to admin config (truthy check, not `is not None`).

Cron evaluated in user's timezone. Mode: morning (before noon) = futures, evening = index closes. `conversation_token` only required for `output = "talk"` or `"both"` — email-only briefings don't need it.

**Boolean expansion**: `markets = true` or `news = true` in user BRIEFINGS.md expands using `[briefing_defaults]` from config.toml.

**Components**: `calendar`, `todos`, `email` (booleans), `markets` ({futures, indices}), `news` ({lookback_hours, sources}), `notes`, `reminders` (random from REMINDERS notes_file)

**Pre-fetching**: Market data and newsletter IDs fetched before execution. State in `briefing_state` table. Priority 8.

**Memory isolation**: Briefing prompts exclude USER.md and dated memories to prevent private context from leaking into output. Briefings use only their pre-fetched components.

### Scheduled Jobs
Defined in `/Users/{user_id}/{bot_name}/config/CRON.md` (markdown with TOML `[[jobs]]` block). Synced to `scheduled_jobs` DB table on each scheduler cycle. Users edit CRON.md to add/remove/modify jobs; Claude edits the file via `schedules.md` skill. Scheduler evaluates cron per user timezone, queues as `source_type="scheduled"`. Skill loaded on keywords: "schedule", "recurring", "cron", "daily", "weekly". Available to all users (not admin-only).

**Job types**: Each job has either `prompt` (runs through Claude Code) or `command` (runs shell command directly via `subprocess.run()`). Mutually exclusive — exactly one must be set. Command jobs flow through the same task queue and get retry logic, `!stop`, failure tracking, and auto-disable. Env vars passed to commands: `ISTOTA_TASK_ID`, `ISTOTA_USER_ID`, `ISTOTA_DB_PATH`, `NEXTCLOUD_MOUNT_PATH`, `ISTOTA_CONVERSATION_TOKEN`.

**One-time jobs**: `once = true` on a `[[jobs]]` entry marks it as one-shot. After successful execution, the scheduler automatically deletes the job from both DB and CRON.md. Failed jobs are kept for retry. Used by reminders skill for fire-and-forget entries.

**Migration**: If a user has DB jobs but no CRON.md, the file is auto-generated from DB entries on first sync. After that, CRON.md is the source of truth.

**Isolation**: Scheduled job results excluded from interactive conversation context. Background workers capped separately via `max_background_workers` (default 3). `silent_unless_action=1` suppresses output unless response has `ACTION:` prefix. Jobs auto-disable after `scheduled_job_max_consecutive_failures` (default 5) consecutive failures. Re-enable via `!cron enable <name>`. Tasks link back to originating job via `scheduled_job_id` column.

### Sleep Cycle (Nightly Memory Extraction)
Direct subprocess (not queued task). Gathers completed tasks from DB → Claude CLI extracts memories → writes `/Users/{user}/memories/YYYY-MM-DD.md` → cleanup old files.

Dated memories are stored for reference but NOT auto-loaded into prompts. They are available at `/Users/{user_id}/memories/` for Claude to read on demand if needed.

Config: `[sleep_cycle]` (global) — enabled (default false), cron (`0 2 * * *`, evaluated in each user's timezone), memory_retention_days (0 = unlimited, default), lookback_hours (24). State in `sleep_cycle_state` table (per-user). Per-user temp dirs via `get_user_temp_dir()`.

### Channel Sleep Cycle
Channel-level memory extraction (parallel to user sleep cycle). Auto-discovers active channels from recent completed tasks — no explicit channel list needed. Extracts shared context (decisions, agreements, project status) from channel conversations, writes to `/Channels/{token}/memories/YYYY-MM-DD.md`, and indexes into memory search with `user_id="channel:{token}"`.

Config: `[channel_sleep_cycle]` — enabled (default false), cron (`0 3 * * *` UTC), lookback_hours (24), memory_retention_days (0 = unlimited, default). State in `channel_sleep_cycle_state` table. Cron evaluated in UTC since channels span users in different timezones.

Memory search integration: Channel conversations are indexed under `channel:{token}` namespace at task completion time. When searching from a channel context (`ISTOTA_CONVERSATION_TOKEN` env var), channel memories are automatically included via `include_user_ids`.

### Per-User Worker Pool
Daemon mode uses `WorkerPool` + `UserWorker` threading. Main loop calls `pool.dispatch()` each iteration with two-phase dispatch: first spawns foreground workers capped at `max_foreground_workers` (default 5), then background workers capped at `max_background_workers` (default 3). Per-user limits (`UserConfig.max_foreground_workers`, `max_background_workers`, both default 1) enforced by `(user_id, queue_type)` key uniqueness. Each worker loops calling `process_one_task(config, user_id=...)` with user-filtered `claim_task()`. Workers exit after `worker_idle_timeout` seconds of no tasks. Thread safety: fresh DB connections per call, `threading.Lock` on workers dict, atomic `UPDATE...RETURNING` for claiming, `asyncio.run()` creates new event loop per worker. One-shot mode (`run_scheduler`) unchanged — stays single-threaded.

**Per-channel gate**: Talk poller rejects messages when there's already an active foreground task for the same conversation (`has_active_foreground_task_for_channel()`). User gets "Still working on a previous request" response. This is a reject (not queue) — user can resend after the active task completes.

### Scheduler Robustness
- **Stale confirmations**: Auto-cancelled after timeout (default: 120 min), user notified
- **Stuck tasks**: Age-checked before retry; older than `max_retry_age_minutes` (60) failed immediately
- **Ancient tasks**: Auto-failed after `stale_pending_fail_hours` (2)
- **Retention**: Old tasks/logs deleted after `task_retention_days` (7), old emails after `email_retention_days` (7)

### Heartbeat Monitoring
Periodic health check system that evaluates user-defined conditions and alerts on failures. Config in `/Users/{user_id}/{bot_name}/config/HEARTBEAT.md` (TOML block in markdown).

**Check types**:
- `file-watch`: Check file age/existence (`path`, `max_age_hours`)
- `shell-command`: Run command, evaluate condition (`command`, `condition`, `message`, `timeout`)
- `url-health`: HTTP health check (`url`, `expected_status`, `timeout`)
- `calendar-conflicts`: Find overlapping events (`lookahead_hours`)
- `task-deadline`: Check for overdue tasks from TASKS.md (`warn_hours_before`)
- `self-check`: System health diagnostics — Claude binary, bwrap, DB, failure rate, execution test (`execution_test`)

For running tasks on a schedule (including AI-powered checks with `silent_unless_action`), use CRON.md instead.

**Cooldown**: Configurable per-check or global `default_cooldown_minutes`. Prevents alert fatigue.

**Check interval**: Per-check `interval_minutes` to run expensive checks less frequently. Default: every scheduler cycle. Uses `last_check_at` from state to skip checks that ran too recently.

**Quiet hours**: Time ranges (e.g., `22:00-07:00`) suppress alerts but checks still run. Cross-midnight ranges supported.

State tracked in `heartbeat_state` table (last_check_at, last_alert_at, last_healthy_at, consecutive_errors). Scheduler checks heartbeats every `heartbeat_check_interval` seconds (default: 60).

### Memory Search
Hybrid BM25 + vector search over conversations and memory files using sqlite-vec and sentence-transformers. Gracefully degrades to BM25-only if sqlite-vec or torch is unavailable. Disabled by default.

**Schema**: `memory_chunks` table + FTS5 virtual table (auto-synced via triggers) + `memory_chunks_vec` vec0 table (created programmatically).

**Core module** (`memory_search.py`): chunking (paragraph/sentence/word boundaries with overlap), content-hash dedup, lazy-loaded `all-MiniLM-L6-v2` embeddings (384 dims), BM25 + vector search fused via Reciprocal Rank Fusion.

**CLI**: `python -m istota.skills.memory_search {search|index|reindex|stats}`. Env vars: `ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, `NEXTCLOUD_MOUNT_PATH`, `ISTOTA_CONVERSATION_TOKEN`.

**Integration**: Auto-indexes conversations after task completion (scheduler.py) and memory files after sleep cycle writes (sleep_cycle.py). Channel conversations also indexed under `channel:{token}` namespace. Both wrapped in try/except — never affects core processing.

**Channel support**: When `ISTOTA_CONVERSATION_TOKEN` is set, search and stats automatically include `channel:{token}` memories. Reindex scans `/Channels/*/memories/*.md` for channel memory files.

**Config**: `[memory_search]` section — `enabled` (default false), `auto_index_conversations`, `auto_index_memory_files`.

**Dependencies**: Optional group `memory-search` — `sqlite-vec>=0.1.6`, `sentence-transformers>=3.0.0`. Install: `uv sync --extra memory-search`.

### Invoicing System
Config-driven invoice generation with PDF export. Uses cash-basis accounting: no ledger entries at invoice time, income recognized when payment is received. Fully deterministic — no data sent to Claude during generation.

**Config**: `INVOICING.md` (markdown with TOML code block) in user `{bot_name}/config/`. Defines company info, clients, services, and settings. Auto-created from template on first run.

**Multi-entity support**: Two company formats — single `[company]` (backward compat, wrapped as key `"default"`) or multi-entity `[companies.<key>]`. Each entity can override `bank_account`, `currency`, `logo`, and `payment_instructions`. Config field `default_entity` selects the default (falls back to first key).

**Entity resolution chain**: `entry.entity > client.entity > config.default_entity`. Bank account resolves `entity > config.default_bank_account`; currency resolves `entity > config.currency`.

**Work log**: `_INVOICES.md` (markdown with TOML `[[entries]]`). Each entry has `date`, `client`, `service`, `qty` (for hours/days/flat) or `amount` (for other/expenses), optional `discount`, `description`, `entity` (override), `invoice` (auto-set when invoiced, e.g. `"INV-000042"`), and `paid_date` (auto-set when payment recorded).

**Service types**: `hours` (qty × rate), `days` (qty × rate), `flat` (fixed rate per entry), `other` (uses `amount` directly).

**Invoice generation flow**: Parse config → parse work log → select uninvoiced entries (optional period upper bound) → group by (client, entity) then by bundle rules → resolve per-entity logo → generate HTML → export PDF via WeasyPrint → increment invoice number → stamp processed entries with invoice number in work log. No ledger entries created. Invoice numbering is global (single sequence across entities).

**Payment recording flow**: `invoice paid INV-XXXX --date YYYY-MM-DD` → find matching entries in work log → compute income lines per service → create income posting (Bank debit + Income credits) → append to ledger → stamp `paid_date` on work log entries. Use `--no-post` when bank transaction already imported via Monarch.

**Uninvoiced entry selection**: Primary filter = entries where `invoice` field is empty. `--period` is optional — when set, acts as upper date bound (entries with `date <= last day of month`). When omitted, all uninvoiced entries are selected. After generation, entries are stamped with `invoice = "INV-XXXXXX"` in the raw markdown file. Dry run does not stamp.

**Outstanding tracking**: Via work log — entries with `invoice` set but no `paid_date` are outstanding. `invoice list` shows outstanding by default, `--all` includes paid.

**Beancount integration**: Cash-basis — income postings created at payment time only. Configurable account names via `income_account` (per service), `default_bank_account`, `currency` (per entity or global). Postings append to main ledger file (`LEDGER_PATH` env var).

**CLI**: `python -m istota.skills.accounting invoice {generate|list|paid|create}`. `generate` accepts optional `--period/-p` (upper date bound, YYYY-MM) and `--entity/-e` flag. `paid` accepts `--no-post` for Monarch-synced transactions. `create` accepts `--entity/-e`. Environment: `INVOICING_CONFIG`, `LEDGER_PATH`, `NEXTCLOUD_MOUNT_PATH`.

**Config location**: INVOICING.md is always resolved from the user's `{bot_name}/config/` folder. Auto-created from template on first use.

**Scheduled generation**: Clients with `schedule = "monthly"` get invoices auto-generated by `invoice_scheduler.py`. Scheduler checks on `briefing_check_interval` cadence. Sends reminder `reminder_days` before `schedule_day`, then generates on `schedule_day`. State tracked in `invoice_schedule_state` table. Notifications sent directly (not Claude tasks) via Talk/email/both — surface resolved: `client.notifications > config.notifications > user.invoicing_notifications > "talk"`. User config: `invoicing_notifications`, `invoicing_conversation_token`.

**Overdue detection**: `days_until_overdue` setting (global or per-client, 0 = disabled). Invoice date = max entry date per invoice number. Overdue when `today > invoice_date + days_until_overdue`. One-time notification per invoice (tracked in `invoice_overdue_notified` table). Multiple overdue invoices consolidated into single notification. Paid invoices ignored. Resolution: `client.days_until_overdue > 0 ? client : config.days_until_overdue`.

### Per-User Filesystem Sandbox (bubblewrap)
Per-user filesystem isolation via bubblewrap (`bwrap`). When `sandbox_enabled = true`, each Claude Code invocation runs inside a mount namespace that restricts filesystem visibility.

**Config**: `[security]` section — `sandbox_enabled` (default false), `sandbox_admin_db_write` (default false).

**Implementation**: `build_bwrap_cmd()` in `executor.py` wraps the `claude` CLI command. The scheduler process itself remains unsandboxed (needs cross-user DB access for task dispatch).

**Non-admin users see**: system libraries (RO), Python venv + source (RO), their own Nextcloud subtree (`/Users/{user_id}` RW), active channel dir (RW if `conversation_token` set), their temp dir (RW), extra resource paths (per `permissions`). Hidden: DB file, other users' directories, `/etc/istota/`, `config/users/*.toml` (masked with tmpfs).

**Admin users see**: everything non-admins see, plus full Nextcloud mount (RW), DB file (RO by default, RW if `sandbox_admin_db_write`), developer repos (RW if enabled).

**Selective /etc**: Only DNS (`resolv.conf`, `hosts`, `nsswitch.conf`), TLS (`ssl/`, `ca-certificates/`), user lookup (`passwd`, `group`), timezone (`localtime`), and dynamic linker (`ld.so.cache`) are bound RO. No blanket `/etc` — avoids exposing secrets.env, shadow, hostname.

**PID namespace**: `--unshare-pid` + `--proc /proc` gives a clean procfs scoped to the sandbox.

**Claude CLI**: `~/.local/bin` and `~/.local/share/claude` RO, `~/.local/state/claude` RW (lock files). `~/.claude` is tmpfs with `.credentials.json` RW (OAuth token refresh) and `settings.json` RO.

**Merged-usr compat**: Debian 13+ uses merged-usr where `/bin`, `/lib`, `/lib64`, `/sbin` are symlinks to `/usr/*`. These are handled with bwrap `--symlink` directives instead of `--ro-bind`. Bind helpers (`_ro_bind`/`_bind`) preserve original paths as dest to handle symlinked `/etc` files (e.g., `resolv.conf` → `/run/systemd/resolve/resolv.conf`).

**Graceful degradation**: Returns original command unchanged if not Linux, or if `bwrap` binary not found.

**Limitations**: No network isolation (agent needs Anthropic API, CalDAV, etc.). No resource limits (needs cgroups/systemd). Filesystem isolation only.

### Deferred DB Operations
With sandbox enabled and DB mounted read-only, Claude and skill CLIs cannot write to the DB directly. Instead, they write JSON request files to the user temp dir (`ISTOTA_DEFERRED_DIR`, always RW in sandbox). The scheduler processes these after successful task completion.

**File patterns** (in `{config.temp_dir}/{user_id}/`):
- `task_{id}_subtasks.json` — subtask creation requests (admin-only)
- `task_{id}_tracked_transactions.json` — transaction dedup tracking (monarch sync, CSV import)

**Backward compat**: Accounting skill falls back to direct DB write if `ISTOTA_DEFERRED_DIR` not set. Deferred files are only processed on successful completion (not failure, not confirmation).

## Testing

TDD with pytest + pytest-asyncio, class-based tests, `unittest.mock`. Real SQLite via `tmp_path`. Integration tests marked `@pytest.mark.integration`. Shared fixtures in `conftest.py`. Current: ~1964 tests across 43 files.

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

Config searched: `config/config.toml` → `~/.config/istota/config.toml` → `/etc/istota/config.toml`. Override: `-c PATH`.

**CalDAV**: Derived from Nextcloud settings (`{url}/remote.php/dav`, same credentials). No separate config needed.

**Logging**: `[logging]` section — level (INFO/DEBUG), output (console/file/both), rotation options. CLI `-v` flag overrides to DEBUG.

## Ansible Deployment Role

The Ansible role lives at `deploy/ansible/` inside this repo. The path `ansible-server/roles/istota/` is a symlink pointing here, so all edits should be made in `deploy/ansible/` directly.

When adding config fields, update:
- `deploy/ansible/defaults/main.yml` — Ansible variables with defaults matching `config.py`
- `deploy/ansible/templates/config.toml.j2` — Jinja2 template lines

### Fava Web UI (Beancount Ledger Viewer)

Per-user Fava instances deployed as systemd services. Each user with `fava_port` in their config and ledger resources gets `istota-fava-{user_id}.service`. Controlled by `istota_fava_enabled` (default: false). Fava runs read-only against the Nextcloud mount. Access restricted to wireguard/private networks via existing UFW rules.

Ansible vars: `istota_fava_enabled`, `istota_fava_host` (default: `0.0.0.0`). Per-user: `fava_port` in `istota_users` config.

## Nextcloud File Access

Mounted at `/srv/mount/nextcloud/content` via rclone (`nextcloud_mount_path` in config). Setup via Ansible (`istota_use_nextcloud_mount: true`). Mount options: full VFS cache, 1h max age, 5s dir cache, 10s poll interval.

For local testing: create directory structure at mount path, add resources, test with `uv run istota task`.

## Task Status Values

`pending` → `locked` → `running` → `completed`/`failed`/`pending_confirmation` → `cancelled`
