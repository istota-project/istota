# Architecture overview

Istota is a self-hosted AI assistant that runs as a regular Nextcloud user. It uses Anthropic's Claude Code CLI as the execution engine, invoked as a subprocess for each task. Messages arrive from Nextcloud Talk, email, file-based task queues, scheduled jobs, or the CLI. They flow through a SQLite task queue, get claimed by per-user worker threads, and produce responses delivered back to the originating channel.

```
Talk (polling) ──────►┐
Email (IMAP) ────────►├─► SQLite queue ──► Scheduler ──► Claude Code ──► Talk / Email
TASKS.md (file) ─────►│                    (WorkerPool)   (subprocess)
CLI (direct) ────────►│
CRON.md (scheduled) ─►┘
```

Istota is not an agent framework. It doesn't implement tool calling, function dispatch, or agent loops. It constructs prompts and invokes the existing Claude Code CLI. New Claude Code capabilities (tool use, model improvements) are automatically available.

## Core data flow

Every interaction follows the same path:

1. **Input** arrives from one of several channels (Talk message, email, TASKS.md edit, CLI command, cron trigger)
2. A **task** is created in the SQLite `tasks` table with status `pending`
3. The **scheduler** dispatches a `UserWorker` thread for the task's user
4. The worker **claims** the task (atomic `UPDATE...RETURNING`, setting status to `locked` then `running`)
5. The **executor** assembles the prompt: persona + resources + memory + context + skills + guidelines + the actual request
6. **Claude Code** runs as a subprocess (`claude -p <prompt> --output-format stream-json`)
7. The **result** is parsed from the stream, stored in the DB, and delivered to the originating channel
8. Post-completion: conversation indexed for memory search, deferred DB operations processed, scheduled job counters reset

Task lifecycle: `pending` -> `locked` -> `running` -> `completed` | `failed` | `pending_confirmation` -> `cancelled`

## Module map

### Input channels

| Module | Purpose |
|---|---|
| `talk_poller.py` | Long-polls Talk conversations, creates tasks, intercepts `!commands`, handles confirmations |
| `email_poller.py` | Polls INBOX via IMAP, creates tasks from known senders, downloads attachments |
| `tasks_file_poller.py` | Watches TASKS.md files for changes, identifies tasks by SHA-256 content hash |
| `cli.py` | Direct task execution (`istota task "prompt" -u USER -x`), supports `--dry-run` |
| `cron_loader.py` | Reads CRON.md (markdown with embedded TOML), syncs jobs to `scheduled_jobs` DB table |

### Core processing

| Module | Purpose |
|---|---|
| `scheduler.py` | Main loop: daemon mode (long-running with WorkerPool) and single-pass mode |
| `executor.py` | Builds prompts, constructs subprocess environment, invokes Claude Code, parses results |
| `context.py` | Selects relevant conversation history using hybrid recent + LLM-triaged approach |
| `skills/_loader.py` | Loads skill documentation selectively based on keywords, resources, source types |
| `stream_parser.py` | Parses Claude Code's `stream-json` output into typed events |

### Storage and state

| Module | Purpose |
|---|---|
| `db.py` | All SQLite operations: task CRUD, resources, conversation history, state tracking |
| `config.py` | TOML config loading with nested dataclasses, per-user overrides, secret env vars |
| `storage.py` | Nextcloud filesystem path management, user workspace creation, OCS sharing |

### Memory

| Module | Purpose |
|---|---|
| `sleep_cycle.py` | Nightly memory extraction from completed tasks, writes dated memory files |
| `memory_search.py` | Hybrid BM25 + vector search over conversations and memory files |

### Output

| Module | Purpose |
|---|---|
| `talk.py` | Async HTTP client for Nextcloud Talk API (send, poll, download attachments) |
| `notifications.py` | Unified dispatcher for Talk, email, and ntfy push notifications |
| `commands.py` | `!command` dispatch, handled synchronously in the talk poller thread |

### Subsystems

| Module | Purpose |
|---|---|
| `heartbeat.py` | Evaluates health checks from HEARTBEAT.md |
| `shared_file_organizer.py` | Scans for files shared with the bot, auto-organizes by owner |
| `nextcloud_client.py` | Shared Nextcloud HTTP plumbing (OCS + WebDAV) |
| `nextcloud_api.py` | Enriches user configs from Nextcloud OCS API at startup |
| `web_app.py` | Authenticated web interface (FastAPI + Nextcloud OIDC) |
| `webhook_receiver.py` | FastAPI webhook receiver (Overland GPS) |
| `briefing.py` | Builds briefing prompts from pre-fetched components |
| `briefing_loader.py` | Loads and merges briefing configs from user workspace, per-user TOML, and main config |
| `invoice_scheduler.py` | Automated invoice generation, reminders, and overdue detection |
| `logging_setup.py` | Centralized logging configuration (console, file, rotation) |

## Browser container

The headless browser runs in a Docker container (`docker/browser/`) exposing a Flask API for Playwright operations:

| Module | Purpose |
|---|---|
| `browse_api.py` | Flask API endpoints: get, screenshot, extract, interact, close, health |
| `chrome.py` | Chrome process lifecycle and CDP connection management |
| `browsing.py` | Human simulation: Gaussian mouse movements, Bezier curves, scrolling patterns, captcha detection |
| `xdotool.py` | X11 input helpers for CDP-free browser interaction |
| `stealth-extension/` | Chrome extension (manifest v3): overrides navigator properties, WebGL fingerprints, handles cookie consent |

Anti-detection strategy: Chrome launches with the stealth extension natively. Patchright connects via CDP only for content extraction, then disconnects. Navigation uses xdotool keyboard input rather than CDP commands. Human simulation adds 5-10s delays between page actions with realistic mouse movement patterns.

## Design decisions

**Claude Code as execution engine, not a framework.** Istota constructs prompts and invokes the existing Claude Code CLI. No custom tool dispatch or agent loops.

**Regular Nextcloud user, not bot API.** The bot runs as an ordinary user. File sharing, CalDAV, and Talk messaging work through standard protocols. No special server configuration.

**File-as-config for user self-service.** Users configure briefings, cron jobs, heartbeats, and persona through markdown files in their Nextcloud workspace. No CLI access needed.

**Functional over object-oriented.** Most code is module-level functions. Classes exist only where shared state across calls is necessary (TalkClient, UserWorker, WorkerPool).

**Graceful degradation everywhere.** Memory search falls back to BM25-only without sqlite-vec. Bubblewrap degrades to unsandboxed on macOS. Mount falls back to rclone CLI. Indexing failures never affect core processing.

**Security by environment, not tool restriction.** Rather than limiting Claude Code tools, credentials are stripped from the subprocess environment and optionally routed through a credential proxy.

**Worker-per-user for fairness.** Each user gets their own serial worker thread per queue type (foreground/background). One user's slow task never blocks another.

**Deferred writes for sandbox compatibility.** With bubblewrap making the DB read-only inside the sandbox, skills write JSON files to a writable temp dir. The scheduler processes these after task completion.
