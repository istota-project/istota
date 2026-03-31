# Architecture

Istota is a self-hosted AI assistant that runs as a regular Nextcloud user. It uses Anthropic's [Claude Code](https://docs.anthropic.com/en/docs/build-with-claude/claude-code) CLI as the execution engine, invoked as a subprocess for each task. Messages arrive from Nextcloud Talk, email, file-based task queues, scheduled jobs, or the CLI. They flow through a SQLite task queue, get claimed by per-user worker threads, and produce responses delivered back to the originating channel.

```
Talk (polling) ──────►┐
Email (IMAP) ────────►├─► SQLite queue ──► Scheduler ──► Claude Code ──► Talk / Email
TASKS.md (file) ─────►│                    (WorkerPool)   (subprocess)
CLI (direct) ────────►│
CRON.md (scheduled) ─►┘
```

Istota is not an agent framework. It is an application built on top of Claude Code. The "intelligence" comes from Claude Code itself; Istota handles the plumbing: input channels, task queuing, context assembly, prompt construction, skill loading, memory, scheduling, multi-user isolation, and response delivery.

---

## Core data flow

Every interaction follows the same path:

1. **Input** arrives from one of several channels (Talk message, email, TASKS.md edit, CLI command, cron trigger)
2. A **task** is created in the SQLite `tasks` table with status `pending`
3. The **scheduler** dispatches a `UserWorker` thread for the task's user
4. The worker **claims** the task (atomic `UPDATE...RETURNING`, setting status to `locked` then `running`)
5. The **executor** assembles the prompt: persona + resources + memory + context + skills + guidelines + the actual request
6. **Claude Code** is invoked as a subprocess (`claude -p <prompt> --output-format stream-json`)
7. The **result** is parsed from the stream, stored in the DB, and delivered to the originating channel
8. Post-completion: conversation indexed for memory search, deferred DB operations processed, scheduled job failure counters reset

Task lifecycle: `pending → locked → running → completed | failed | pending_confirmation → cancelled`

---

## Module map

### Input channels

| Module | What it does |
|---|---|
| `talk_poller.py` | Background daemon thread with its own asyncio event loop. Long-polls each Talk conversation the bot participates in. Creates tasks from user messages. Intercepts `!commands` before task creation. Handles confirmation flow (yes/no replies). |
| `email_poller.py` | Polls INBOX via `imap-tools`. Creates tasks from known senders. Downloads attachments to `/Users/{user_id}/inbox/`. Computes thread IDs from normalized subjects for reply threading. |
| `tasks_file_poller.py` | Watches `/Users/{user_id}/{bot_dir}/config/TASKS.md` for changes. Status markers: `[ ]` pending, `[~]` in-progress, `[x]` completed, `[!]` failed. Tasks identified by SHA-256 content hash. |
| `cli.py` | Direct task execution via `uv run istota task "prompt" -u USER -x`. Supports `--dry-run` to see the assembled prompt without calling Claude. |
| `cron_loader.py` | Reads `/Users/{user_id}/{bot_dir}/config/CRON.md` (markdown with embedded TOML block). Syncs job definitions to `scheduled_jobs` DB table. CRON.md is the source of truth. |

### Core processing

| Module | What it does |
|---|---|
| `scheduler.py` | Main loop. Two modes: daemon (long-running with `WorkerPool`) and single-pass (process-and-exit). Orchestrates all polling, cleanup, briefing checks, heartbeat evaluation, and worker dispatch. |
| `executor.py` | Builds the full prompt, constructs the subprocess environment, invokes Claude Code, parses the result stream. Also contains the bubblewrap sandbox logic. |
| `context.py` | Selects relevant conversation history. Recent messages always included; older messages triaged by a fast model (Haiku) that picks which are relevant to the current request. |
| `skills_loader.py` | Thin wrapper re-exporting from `skills/_loader.py`. Loads skill documentation from self-contained skill directories under `src/istota/skills/`. Skills are selectively included based on keywords, resource types, source types, and file types defined in each skill's `skill.toml` manifest. |
| `stream_parser.py` | Parses Claude Code's `--output-format stream-json` line by line into typed events: `ToolUseEvent`, `TextEvent`, `ResultEvent`. |

### Talk progress

Progress updates during execution use a three-mode `progress_style` config:
- **`replace`** (default): edits the initial ack message in-place, showing the latest tool action + elapsed time. Avoids chat noise.
- **`full`**: appends all tool actions as separate messages (legacy behavior).
- **`none`**: silent, no progress updates.

Rate-limited: `progress_min_interval` (8s), capped at `progress_max_messages` (5) per task. Multi-line tool output is collapsed to the first line. Progress callbacks carry the Talk message ID from the ack so subsequent updates edit the same message.

### Log channel

Per-user verbose logging to a dedicated Talk conversation. When `log_channel` is set in per-user config, every tool action is posted to that room with a `[task_id #channel]` prefix and status emoji (⏳ running, ✓ done, ✗ failed). This provides full observability without cluttering the user's chat. Log channel callbacks compose with progress callbacks — both fire on each event.

### Storage and state

| Module | What it does |
|---|---|
| `db.py` | All SQLite operations. Task CRUD, resource management, conversation history, briefing/heartbeat/sleep cycle state, transaction tracking, key-value store. WAL mode for concurrent access. |
| `config.py` | TOML config loading. Nested dataclasses for every subsystem. Per-user config files override main config. Secret env var overrides for deployment. |
| `storage.py` | Nextcloud filesystem path management. Creates user workspace directories, seeds config files, shares folders via OCS API. All path functions require explicit `bot_dir` parameter. |

### Memory

| Module | What it does |
|---|---|
| `sleep_cycle.py` | Nightly memory extraction. Gathers completed tasks, invokes Claude CLI to extract learnings, writes dated memory files (`/Users/{user_id}/memories/YYYY-MM-DD.md`). Also handles channel-level memory extraction. |
| `memory_search.py` | Hybrid BM25 + vector search over conversations and memory files. Uses `sqlite-vec` for vector storage and `sentence-transformers` for embeddings. Gracefully degrades to BM25-only. |

### Output and notifications

| Module | What it does |
|---|---|
| `talk.py` | Async HTTP client (`httpx`) for the Nextcloud Talk user API. Send messages, poll conversations, download attachments. Messages split at 4000 chars. |
| `notifications.py` | Unified dispatcher for Talk, email, and ntfy push notifications. Used by invoice scheduler, heartbeat alerts, and confirmation timeouts. |
| `commands.py` | `!command` dispatch. Decorator-based registry. Commands handled synchronously in the talk poller thread, bypassing the task queue entirely. |

### Subsystems

| Module | What it does |
|---|---|
| `briefing.py` | Builds briefing prompts from pre-fetched components (calendar, todos, email, markets, news, notes, reminders). Memory intentionally excluded to prevent private context leaking into newsletter-style output. |
| `briefing_loader.py` | Loads and merges briefing configs from user workspace `BRIEFINGS.md`, per-user TOML, and main config. User config takes precedence. |
| `heartbeat.py` | Evaluates health checks defined in `HEARTBEAT.md`. Check types: file-watch, shell-command, url-health, calendar-conflicts, task-deadline, self-check. Per-check cooldowns, quiet hours, and interval controls. |
| `invoice_scheduler.py` | Automated invoice generation for clients with `schedule = "monthly"`. Sends reminders before the schedule day, generates on the schedule day, detects overdue invoices. |
| `shared_file_organizer.py` | Periodically scans the Nextcloud root for files shared with the bot. Determines owner via WebDAV PROPFIND, moves to `/Users/{owner}/shared/`, creates resource entries. |
| `nextcloud_client.py` | Shared Nextcloud HTTP plumbing. OCS wrappers (`ocs_get`, `ocs_post`, `ocs_delete`), WebDAV owner lookup, sharing API helpers (`ocs_list_shares`, `ocs_create_share`, `ocs_share_folder`). Used by `storage.py`, `nextcloud_api.py`, `shared_file_organizer.py`, and the nextcloud skill CLI. |
| `nextcloud_api.py` | Enriches user configs from Nextcloud OCS API at startup (display name, email, timezone). Config values take precedence; API only fills gaps. |
| `logging_setup.py` | Centralized logging configuration. Console, file, or both. Log rotation. |

### Skill CLI modules (`src/istota/skills/`)

Skills expose Python CLIs invoked by Claude Code inside the sandbox via `python -m istota.skills.<name>`. Pattern: `build_parser()` + `main()`, JSON output, credentials via env vars.

| Module | Purpose |
|---|---|
| `accounting.py` | Beancount ledger operations, Monarch Money sync, CSV import, transaction management |
| `invoicing.py` | Invoice generation, PDF export (WeasyPrint), cash-basis income posting |
| `calendar/` | CalDAV read/write/update (auto-discovered from Nextcloud credentials). Subcommands: `list` (`--date`, `--week`), `create`, `update`, `delete`. |
| `email.py` | IMAP/SMTP: send, reply, search, list, delete, newsletter extraction |
| `files.py` | Nextcloud file operations (mount-aware, rclone fallback) |
| `browse/` | Headless browser via Dockerized Playwright container (Flask API) |
| `garmin/` | Garmin Connect data access: activities, stats, health metrics. Subcommands: `connect`, `user`, `activities`, `stats`, `health`. JSON output. |
| `markets.py` | yfinance wrapper for market data |
| `transcribe.py` | OCR via Tesseract |
| `whisper/` | Audio transcription via faster-whisper (CPU, int8) |
| `nextcloud/` | Nextcloud sharing CLI: list, create, delete shares; search sharees. Uses `nextcloud_client.py`. |
| `memory_search.py` | Memory search CLI: search, index, reindex, stats |
| `bookmarks/` | Karakeep bookmark management CLI |

---

## Browser container

The headless browser runs in a Docker container (`docker/browser/`) exposing a Flask API for Playwright operations. The container is structured into focused modules:

| Module | Purpose |
|---|---|
| `browse_api.py` | Flask API endpoints: `get`, `screenshot`, `extract`, `interact`, `close`, `health` |
| `chrome.py` | Chrome process lifecycle and CDP connection management. Launches Chrome with a stealth extension, manages Patchright CDP connections (lazy connect for extraction, disconnect before navigation to avoid Cloudflare detection). |
| `browsing.py` | Human simulation: Gaussian mouse movements, Bezier curves, scrolling patterns. Captcha detection via frame URL matching. |
| `xdotool.py` | X11 input helpers for CDP-free browser interaction. URL navigation via keyboard input to avoid CDP detection by bot protection systems. |
| `stealth-extension/` | Chrome extension (manifest v3) injected at launch. Overrides navigator properties, WebGL fingerprints, and handles cookie consent banners. Replaces Patchright's route-based script injection. |

Anti-detection strategy: Chrome launches with the stealth extension natively. Patchright connects via CDP only for content extraction, then disconnects. Navigation uses xdotool keyboard input rather than CDP commands. Human simulation adds 5-10s delays between page actions with realistic mouse movement patterns.

---

## Scheduler

The scheduler is the central coordinator. In daemon mode, it runs a main loop that checks every subsystem on configurable intervals and dispatches worker threads.

### Main loop (daemon mode)

```python
while not shutdown_requested:
    check_briefings()           # every briefing_check_interval (60s)
    check_scheduled_jobs()      # every briefing_check_interval
    check_sleep_cycles()        # every briefing_check_interval
    check_channel_sleep_cycles()# every briefing_check_interval
    poll_emails()               # every email_poll_interval (60s)
    organize_shared_files()     # every shared_file_check_interval (120s)
    poll_tasks_files()          # every tasks_file_poll_interval (30s)
    run_cleanup_checks()        # every briefing_check_interval
    check_heartbeats()          # every heartbeat_check_interval (60s)
    check_invoice_schedules()   # every briefing_check_interval
    pool.dispatch()             # spawn workers for users with pending tasks
    sleep(poll_interval)        # 2s
```

### Worker pool

`WorkerPool` manages concurrent `UserWorker` threads with three-tier concurrency control:

1. **Per-channel gate**: before creating a task, the Talk poller checks if an active foreground task already exists for the conversation. If so, it sends "Still working on a previous request — I'll be with you shortly" but still queues the message as a normal task. The scheduler processes it after the active task completes.
2. **Instance-level caps**: `max_foreground_workers` (default 5) and `max_background_workers` (default 3) limit total concurrent workers by queue type. Dispatch is two-phase: foreground first, then background.
3. **Per-user limits**: `user_max_foreground_workers` (default 2) and `user_max_background_workers` (default 1) set global per-user defaults. Individual users can override via `max_foreground_workers`/`max_background_workers` in their per-user config (0 = use global default).

Each `UserWorker` is a thread that processes tasks serially for one user. Workers are keyed by `(user_id, queue_type)`, so a user can have at most one foreground and one background worker simultaneously. Workers exit after `worker_idle_timeout` (30s) of no tasks. Thread safety: fresh DB connections per call, new `asyncio.run()` event loop per worker, `threading.Lock` on the workers dict.

### Task claiming

`claim_task()` uses atomic `UPDATE...RETURNING` with stale lock detection:
1. Fail old stale locked tasks (created > `max_retry_age`, locked > 30min)
2. Release recent stale locks for retry
3. Fail old stuck running tasks
4. Release recent stuck running tasks for retry
5. Claim next pending task: `ORDER BY priority DESC, created_at ASC`

### Retry logic

Failed tasks retry with exponential backoff: 1 min, 4 min, 16 min (up to `max_attempts`, default 3). Transient API errors (5xx, 429) get 3 retries with 5s delay before counting against task attempts.

### Cleanup

Runs every `briefing_check_interval`:
- Cancel stale confirmations after 120 min, notify user
- Auto-fail tasks pending longer than 2 hours
- Delete completed tasks older than 7 days
- Delete processed emails from IMAP older than 7 days
- Delete temp files older than configured retention

---

## Executor

The executor is responsible for prompt construction and Claude Code subprocess management.

### Prompt assembly

The prompt is built in this order:

1. **Header**: role definition, user_id, current datetime, task_id, conversation_token, db_path
2. **Persona**: user workspace `PERSONA.md` (overrides global `config/persona.md`)
3. **Resources**: calendars, folders, todos, email folders, notes, reminders (from DB + config)
4. **User memory**: `USER.md` content (skipped for briefings to prevent private context leakage)
5. **Channel memory**: `CHANNEL.md` content (if `conversation_token` is set)
6. **Dated memories**: last N days of extracted memories (via `auto_load_dated_days`, skipped for briefings)
6b. **Recalled memories**: BM25 search results from memory index (via `auto_recall`, skipped for briefings)
8. **Tools section**: available tools documentation (file access, browser, CalDAV, sqlite3, email)
9. **Rules**: resource restrictions, confirmation flow, subtask creation, output format
10. **Conversation context**: previous messages (selected by context module)
11. **Request**: the actual prompt text + file attachments
12. **Guidelines**: channel-specific formatting from `config/guidelines/{source_type}.md`
13. **Skills changelog**: "what's new" if skills updated since last interaction
14. **Skills documentation**: concatenated skill .md files, selectively loaded

### Subprocess invocation

```
claude -p <prompt> --allowedTools Read Write Edit Grep Glob Bash --output-format stream-json --verbose
```

Environment variables pass credentials (Nextcloud, CalDAV, SMTP/IMAP, browser API, ledger paths, etc.) to the subprocess. `build_clean_env()` constructs a minimal environment (PATH, HOME, PYTHONUNBUFFERED, configured passthrough vars). When the skill proxy is enabled (default), credentials are stripped from Claude's env and routed through a Unix socket proxy instead.

### Streaming execution

The executor reads Claude Code's stdout line-by-line, parsing `stream-json` events:
- `ToolUseEvent` → forwarded as progress updates to Talk
- `TextEvent` → forwarded as progress (lower priority than tool events)
- `ResultEvent` → final result (success or error)

Cancellation is checked on each event via `db.is_task_cancelled()`.

Both streaming and simple execution modes retry transient API errors (5xx, 429) up to 3 times with 5s delays. The scheduler additionally detects API errors in successful results (Claude Code may exit 0 with error text) and retries those too. Background tasks (briefings, scheduled jobs) suppress error notifications to avoid noise — failures are logged to the DB and log channel only.

---

## Conversation context

The context module (`context.py`) selects which previous messages to include in the prompt. This keeps token usage reasonable while preserving relevant history.

1. Fetch last `lookback_count` (25) messages for the conversation
2. Apply recency window: if `context_recency_hours` > 0, exclude messages older than the cutoff while always keeping at least `context_min_messages` (10) recent messages
3. If total <= `skip_selection_threshold` (3): include all, skip selection
4. Most recent `always_include_recent` (5) messages are always included
5. Older messages are triaged by a selection model (Haiku via Claude CLI subprocess) that returns which message IDs are relevant
6. Selected older messages + guaranteed recent messages are combined in chronological order
7. On any error: fall back to guaranteed recent messages only

Reply-to messages are force-included regardless of selection. Actions taken (tool use descriptions) are appended after bot responses so Claude can see what it did previously.

Talk context uses a poller-fed message cache (`talk_messages` table) populated by the talk poller, avoiding redundant API calls. Bot responses are captured in the cache via `:result` reference IDs. Cache size is bounded per conversation (`talk_cache_max_per_conversation`, default 200).

---

## Skills system

Each skill is a self-contained directory under `src/istota/skills/` with a `skill.toml` manifest and `skill.md` doc. Skills are reference docs loaded into the prompt so Claude knows how to use available tools and CLIs. Some skills also contain Python modules (CLIs, libraries).

Infrastructure lives in `skills/_types.py` (SkillMeta, EnvSpec dataclasses), `skills/_loader.py` (discovery, manifest loading, doc resolution), and `skills/_env.py` (declarative env var resolver + setup_env() hook dispatch). `skills_loader.py` at the package root is a thin re-export wrapper.

### Discovery and selection

Skill discovery uses layered priority: bundled `skill.toml` directories < operator overrides in `config/skills/`. A skill is selected if any of these match (from its `skill.toml`):
- `always_include = true` (files, sensitive_actions, memory, scripts, memory_search)
- `source_types` matches the task's source type (e.g., briefing → calendar, markets, notes)
- Any `keywords` found in the prompt text (e.g., "email" → email skill). If the skill also declares `resource_types`, the user must have at least one matching resource — keyword alone is not enough.
- Attachment file extensions match (`file_types`, e.g., `.wav` → whisper)

Admin-only skills (`tasks`, `schedules`) are filtered out for non-admin users. Skills with `dependencies` are skipped with a warning if the dependency is not installed.

### Env var wiring

Skills declare env var requirements in `[[env]]` sections in `skill.toml`. Source types: `config` (dotted config path with optional guard), `resource` (DB resource mount path), `resource_json` (all resources as JSON), `user_resource_config` (from per-user TOML `[[resources]]`), `template_file` (auto-create from template). Declarative env vars don't override hardcoded ones in executor.py.

Skills with complex env setup (e.g., developer) export `setup_env(ctx) -> dict[str, str]` in their `__init__.py`, called after declarative resolution.

### Fingerprinting

Skills have a SHA-256 fingerprint (of all `skill.toml` + `skill.md` files). When the fingerprint changes between interactions, a "what's new" changelog is appended to the prompt for interactive tasks, so the user learns about new capabilities.

### Placeholder substitution

`{BOT_NAME}` and `{BOT_DIR}` in skill docs are substituted at load time, allowing the technical identifier (`istota`) to be separated from the user-facing name.

---

## Memory

Istota has a multi-tiered memory system. Each tier has different scope, lifecycle, and loading behavior. All personal memory is excluded from briefing prompts to prevent private context leaking into newsletter-style output.

### Tier 1: User memory (USER.md)

Persistent per-user memory at `/Users/{user_id}/{bot_dir}/config/USER.md`. Auto-loaded into every interactive prompt (skipped for briefings). Claude reads and writes this file during task execution. Contains preferences, facts, and ongoing context about the user.

Optional nightly curation: when `curate_user_memory = true`, the sleep cycle runs a second Claude Sonnet pass that promotes durable facts from dated memories into USER.md and removes outdated entries. Controlled by `[sleep_cycle]` config.

### Tier 2: Channel memory (CHANNEL.md)

Per-conversation memory at `/Channels/{conversation_token}/CHANNEL.md`. Loaded when `conversation_token` is set. Contains shared context for group conversations (decisions, agreements, project status). Written by Claude during task execution and by the channel sleep cycle.

### Tier 3: Dated memories (YYYY-MM-DD.md)

Written by the nightly sleep cycle to `/Users/{user_id}/memories/`. Auto-loaded into prompts for the last N days (configurable via `auto_load_dated_days`, default 3, set 0 to disable). Skipped for briefings. Each entry includes task provenance references (`ref:TASK_ID`) for traceability. Managed retention via `memory_retention_days` (0 = unlimited).

### Tier 4: Memory recall (BM25 auto-recall)

When `auto_recall = true` in `[memory_search]` config, the executor performs a BM25 full-text search using the task prompt as query against indexed conversations and memory files. Returns up to `auto_recall_limit` (default 5) results formatted as bullet points. Independent of context triage — no LLM call needed, just SQLite FTS5. Skipped for briefings. When a `conversation_token` is set, also searches the channel namespace (`channel:{token}`).

### Memory search index

Hybrid BM25 + vector search (`memory_search.py`). Text is chunked (paragraph/sentence/word boundaries with overlap), content-hash deduped, and stored in `memory_chunks`. FTS5 provides BM25 ranking, `sqlite-vec` provides vector similarity (384-dim `all-MiniLM-L6-v2` embeddings). Results fused via Reciprocal Rank Fusion.

Auto-indexed after task completion and after sleep cycle writes. Both wrapped in try/except — indexing failures never affect core processing. Enabled by default.

### Memory size cap

`max_memory_chars` (default 0 = unlimited) limits the total memory injected into prompts. When the cap is exceeded, components are truncated in order: recalled memories first, then dated memories. If the cap is still exceeded after removing both, a warning is logged but user memory and channel memory are preserved (they are the most stable and curated tiers).

### Sleep cycle

Direct subprocess (not a queued task), evaluated per user's timezone:

1. Gather completed tasks from the last 24 hours
2. Invoke `claude -p` with a memory extraction prompt (excludes existing USER.md to avoid duplication)
3. Extracted memories include task provenance: `- Fact learned (2026-01-28, ref:1234)`
4. Write extracted memories to dated file, or output `NO_NEW_MEMORIES`
5. Cleanup old files per retention policy
6. Trigger memory search indexing
7. If `curate_user_memory` enabled: run a second Claude pass to update USER.md from accumulated dated memories (outputs `NO_CHANGES_NEEDED` if nothing to update)

Channel sleep cycle works the same way but runs in UTC and writes to `/Channels/{token}/memories/`.

### Prompt assembly order

Memory components appear in the prompt in this order:

1. **User memory** (USER.md) — always loaded for interactive tasks
2. **Channel memory** (CHANNEL.md) — loaded when in a conversation
3. **Dated memories** — last N days of extracted memories
4. **Recalled memories** — BM25 search results from the memory index
5. *(then tools, rules, context, request, guidelines, skills)*

---

## Multi-user isolation

### Configuration

Each user can be configured at three levels (later overrides earlier):
1. `[users.alice]` in main `config/config.toml`
2. `config/users/alice.toml` (per-user TOML file, overrides main config)
3. User workspace files: `PERSONA.md`, `BRIEFINGS.md`, `CRON.md`, `HEARTBEAT.md` (user self-service)

### Admin vs non-admin

Admin users listed in `/etc/istota/admins` (root-owned, one user ID per line). Empty file = all users are admin (backward compat).

Non-admin restrictions:
- Prompt: scoped mount path (`/Users/{user_id}` only), no DB path, no sqlite3 tool, no subtask creation
- Env vars: `ISTOTA_DB_PATH` omitted, `NEXTCLOUD_MOUNT_PATH` scoped to user directory
- Skills: `admin_only` skills filtered out

### Filesystem sandbox (bubblewrap)

When `sandbox_enabled = true`, each Claude Code invocation runs inside a `bwrap` mount namespace. Linux-only; gracefully degrades on macOS.

**Non-admin users see**: system libraries (RO), Python venv + source (RO), their own Nextcloud subtree (RW), active channel dir (RW), their temp dir (RW), extra resource paths. Hidden: DB, other users' dirs, `/etc/istota/`, user config files.

**Admin users additionally see**: full Nextcloud mount (RW), DB file (RO by default), developer repos.

PID namespace isolation (`--unshare-pid`). No network isolation (agent needs API access). Merged-usr compatibility for Debian 13+.

### Deferred DB operations

With the sandbox, the DB is read-only inside the subprocess. Skills write JSON request files to the always-writable temp dir. The scheduler (unsandboxed) processes these after successful task completion:
- `task_{id}_subtasks.json` → subtask creation (admin-only)
- `task_{id}_tracked_transactions.json` → transaction dedup tracking

---

## Nextcloud integration

Istota runs as a regular Nextcloud user, not a bot API. This is a deliberate design choice — it means no special server configuration, and file sharing works exactly like sharing with any other user.

### File access

Two methods:
- **rclone mount** (preferred): Full VFS cache, mounted at `/srv/mount/nextcloud/content`. Real filesystem access for Claude Code.
- **rclone CLI** (fallback): Used when mount is unavailable. Individual file operations via `rclone copyto`, `rclone lsjson`.

### Directory structure

```
/Users/{user_id}/
├── {bot_dir}/               # Shared back to user via OCS API
│   ├── config/              # USER.md, TASKS.md, BRIEFINGS.md, PERSONA.md, CRON.md, etc.
│   ├── exports/             # Bot-generated files
│   └── examples/            # Documentation and config reference
├── inbox/                   # Files from email attachments
├── memories/                # Dated memories from sleep cycle
├── shared/                  # Auto-organized files shared with bot
└── scripts/                 # User's reusable Python scripts

/Channels/{conversation_token}/
├── CHANNEL.md               # Persistent channel memory
└── memories/                # Channel-level dated memories
```

### Talk polling

The talk poller runs in a background daemon thread. It long-polls each conversation the bot is part of. First poll initializes state; subsequent polls use `lookIntoFuture=1` for real-time message delivery. Fast rooms (with new messages) are processed immediately without waiting for slow (quiet) rooms.

**Multi-user rooms**: In rooms with 3+ participants, the bot only responds when @mentioned. 2-person rooms behave like DMs. Participant counts are cached (5 min TTL). The bot's own @mention is stripped from the prompt; other mentions are resolved to `@DisplayName`. Falls back to DM behavior on API errors.

**Reply threading**: Final responses in group chats use `reply_to` on the original message and prepend `@{user_id}` for notification. Intermediate messages (ack, progress updates) are sent without reply threading to avoid noise.

Progress updates during task execution: random acknowledgment before execution starts, then streaming tool-use descriptions. Default `progress_style = "replace"` edits the ack message in-place with the latest action + elapsed time. Legacy `"full"` mode appends separate messages. Rate-limited: min 8s apart, max 5 per task.

### CalDAV

Derived from Nextcloud credentials (`{url}/remote.php/dav`). Calendars auto-discovered at execution time. No separate configuration needed.

---

## Database

SQLite with WAL mode. All operations in `db.py`. Schema in `schema.sql`.

### Tables

| Table | Purpose |
|---|---|
| `tasks` | Core task queue with full execution lifecycle |
| `user_resources` | Per-user resource permissions (calendar, folder, todo_file, etc.) |
| `briefing_configs` | DB-stored briefing configurations (legacy, superseded by BRIEFINGS.md) |
| `briefing_state` | Last-run timestamps per briefing per user |
| `task_logs` | Structured task-level observability |
| `processed_emails` | Email dedup with RFC 5322 thread tracking |
| `talk_poll_state` | Last message ID per Talk conversation |
| `talk_messages` | Poller-fed message cache for conversation context |
| `istota_file_tasks` | Tasks sourced from TASKS.md files (content-hash identity) |
| `scheduled_jobs` | Cron job definitions (synced from CRON.md) |
| `sleep_cycle_state` | Per-user nightly memory extraction state |
| `channel_sleep_cycle_state` | Per-channel memory extraction state |
| `heartbeat_state` | Per-check monitoring state (timestamps, consecutive errors) |
| `reminder_state` | Shuffle queue for briefing reminders |
| `monarch_synced_transactions` | Monarch Money sync dedup + reconciliation |
| `csv_imported_transactions` | CSV import dedup |
| `invoice_schedule_state` | Automated invoice generation timing |
| `invoice_overdue_notified` | Prevents duplicate overdue alerts |
| `memory_chunks` | Text chunks for hybrid search |
| `memory_chunks_fts` | FTS5 virtual table (trigger-synced from memory_chunks) |
| `user_skills_fingerprint` | Skills version tracking for "what's new" |
| `feed_state` | RSS/Tumblr/Are.na polling state |
| `feed_items` | Aggregated feed content |
| `istota_kv` | Key-value store for script runtime state |

---

## Briefings

Scheduled summaries delivered to Talk and/or email. Configuration sources (precedence): user workspace `BRIEFINGS.md` > per-user config TOML > main config. Merged at briefing name level.

Components: `calendar` (today's events), `todos` (pending items), `email` (newsletter content), `markets` (futures/indices via yfinance), `news` (lookback_hours + sources), `notes` (recent notes summary), `reminders` (random from REMINDERS file).

Market data and newsletter IDs are pre-fetched before Claude execution. Briefing prompts intentionally exclude USER.md and dated memories to prevent private context from leaking into what may be a shared/newsletter-style output.

Boolean expansion: `markets = true` in BRIEFINGS.md expands using admin-configured `[briefing_defaults]`.

---

## Scheduled jobs

Defined in `/Users/{user_id}/{bot_dir}/config/CRON.md`. Two types:
- **Prompt jobs**: run through Claude Code (AI-powered tasks)
- **Command jobs**: run shell commands directly via `subprocess.run()`

Both go through the same task queue and get retry logic, `!stop` support, failure tracking, and auto-disable after 5 consecutive failures.

Isolation: scheduled job results are excluded from interactive conversation context. `silent_unless_action=1` suppresses output unless the response has an `ACTION:` prefix.

---

## Heartbeat monitoring

User-defined health checks in `/Users/{user_id}/{bot_dir}/config/HEARTBEAT.md`.

Check types: `file-watch` (file age/existence), `shell-command` (run command + evaluate condition), `url-health` (HTTP status check), `calendar-conflicts` (overlapping events), `task-deadline` (overdue TASKS.md items), `self-check` (system diagnostics: Claude binary, bwrap, DB, failure rate).

Per-check controls: `interval_minutes` (run expensive checks less frequently), `cooldown_minutes` (prevent alert fatigue), `quiet_hours` (suppress alerts during off-hours, cross-midnight supported). State tracked in `heartbeat_state` table.

---

## !Commands

Commands prefixed with `!` are intercepted in the talk poller before task creation and handled synchronously. No Claude Code invocation.

| Command | What it does |
|---|---|
| `!help` | List all commands |
| `!stop` | Cancel active task (`cancel_requested` flag + SIGTERM worker PID) |
| `!status` | Show running/pending tasks + system stats |
| `!memory user/channel` | Show memory file contents |
| `!cron` | List/enable/disable scheduled jobs |
| `!check` | Run system health check (self-check heartbeat) |
| `!export [markdown\|text]` | Export conversation history to a file. First run exports all messages; subsequent runs incrementally append new messages. Exports to `{bot_dir}/exports/conversations/`. |

---

## Deployment

Target: Debian 13+ VM. Two paths:

**Standalone** (`install.sh`): Interactive wizard. `render_config.py` generates all config files from a settings TOML.

**Ansible role** (`deploy/ansible/`): Full automation. Jinja2 templates generate config, systemd services, nginx, logrotate, backups. The `~/Repos/ansible-server/roles/istota/` path is a symlink to `deploy/ansible/`.

Nextcloud mount via rclone: full VFS cache, 1h max age, 5s dir cache, 10s poll interval. Set up by Ansible with `istota_use_nextcloud_mount: true`.

---

## Testing

TDD with pytest + pytest-asyncio. ~2400 tests across 51 files. Real SQLite via `tmp_path` (no DB mocking). `unittest.mock` for external dependencies.

Shared fixtures in `conftest.py`: `db_path` (initialized from schema.sql), `db_conn`, `make_task`, `make_config`, `make_user_config`.

Integration tests marked `@pytest.mark.integration`, deselected by default. These require real Nextcloud connectivity.

```bash
uv run pytest tests/ -v                              # Unit tests
uv run pytest -m integration -v                       # Integration tests
uv run pytest tests/ --cov=istota --cov-report=term-missing  # Coverage
```

---

## Dependencies

Core: `httpx` (HTTP), `caldav` + `icalendar` (CalDAV), `croniter` (cron), `tomli` (TOML), `yfinance` (markets), `imap-tools` (email), `beancount` + `beanquery` (accounting), `weasyprint` (PDF), `feedparser` (RSS), `pytesseract` (OCR), `garminconnect` (Garmin data).

Optional extras: `memory-search` (sqlite-vec, sentence-transformers), `whisper` (faster-whisper), `dev` (pytest).

External tooling: `claude` CLI (Anthropic), `rclone` (Nextcloud file access), `bwrap` (bubblewrap, Linux sandbox), `tesseract` (OCR engine), Docker (browser container).

---

## Key design decisions

**Claude Code as execution engine, not a framework.** Istota doesn't implement tool calling, function dispatch, or agent loops. It constructs prompts and invokes the existing Claude Code CLI. This means new Claude Code capabilities (tool use, model improvements) are automatically available.

**Regular Nextcloud user, not bot API.** The bot runs as an ordinary user. File sharing works like sharing with any other user. No special server configuration. Polling-based Talk integration means no public network access required.

**File-as-config for user self-service.** Users configure briefings, cron jobs, heartbeats, and persona through markdown files in their Nextcloud workspace. TOML blocks embedded in markdown. This avoids needing admin intervention for routine config changes.

**Functional over object-oriented.** Most code is module-level functions. The few classes (TalkClient, UserWorker, WorkerPool) exist where shared state across calls is necessary.

**Graceful degradation everywhere.** Memory search falls back to BM25-only without sqlite-vec. Bubblewrap degrades to unsandboxed on macOS. Mount falls back to rclone CLI. Nextcloud API hydration silently skips on failure. Indexing failures never affect core processing.

**Security by environment, not tool restriction.** Rather than limiting which Claude Code tools are available, credentials are stripped from the subprocess environment. For heartbeat/cron commands, no credentials are passed. The bubblewrap sandbox restricts filesystem visibility per user.

**Worker-per-user for fairness.** Each user gets their own serial worker thread per queue type (foreground/background). One user's slow task doesn't block another user. Interactive tasks get priority via two-phase dispatch with separate instance-level and per-user caps.

**Deferred writes for sandbox compatibility.** With bubblewrap making the DB read-only inside the sandbox, skills write JSON files to a writable temp dir. The unsandboxed scheduler processes these after task completion.
