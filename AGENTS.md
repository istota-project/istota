# Istota - Claude Code Bot

Claude Code-powered assistant bot with Nextcloud Talk interface.

**Production server**: `your-server` (SSH, installed at `/srv/app/istota`).

For module-specific internals, see `.claude/rules/`:
- `brain.md` — Brain protocol + ClaudeCodeBrain
- `executor.md` — `execute_task()`, env mapping, prompt assembly, security
- `scheduler.md` — daemon loop, worker pool, DB tables, deferred ops
- `config.md` — every dataclass field + TOML mapping
- `skills.md` — skill metadata, two-pass selection, CLI modules

## Project Structure

```
src/istota/
├── brain/                # Pluggable model invocation (Brain protocol)
├── memory/               # search.py, knowledge_graph.py, sleep_cycle.py, curation/
├── skills/               # 28 self-contained skills (skill.md + optional CLI)
├── cli.py                # Local CLI (task, resource, run, etc.)
├── config.py             # TOML config + per-user overrides
├── context.py            # Hybrid conversation context selection
├── db.py                 # SQLite operations (all tables)
├── executor.py           # Per-task orchestration (memory/skills/sandbox)
├── scheduler.py          # Task processor, briefings, all polling
├── email_poller.py       # IMAP polling + plus-address routing
├── talk_poller.py        # Nextcloud Talk long-polling
├── tasks_file_poller.py  # TASKS.md monitoring
├── heartbeat.py          # Health-check system
├── webhook_receiver.py   # FastAPI: Overland GPS, etc.
├── web_app.py            # Authenticated web UI (Nextcloud OIDC)
├── notifications.py      # Talk / Email / ntfy dispatcher
├── skill_proxy.py        # Unix-socket proxy for credential isolation
├── network_proxy.py      # CONNECT proxy for network isolation
├── nextcloud_api.py      # NC user metadata
├── nextcloud_client.py   # OCS + WebDAV plumbing
├── storage.py            # Bot-managed Nextcloud storage
├── feeds/                # Native RSS/Atom/Tumblr/Are.na — poller, SQLite, routes, OPML
├── location_logic.py     # Place stats / cluster discovery (shared web ⇄ skill)
├── shared_file_organizer.py
├── commands.py           # !command dispatch
├── cron_loader.py        # CRON.md → DB sync
└── logging_setup.py

config/                   # config.toml, users/, persona.md, emissaries.md, system-prompt.md, guidelines/, skills/
deploy/ansible/           # Role + install.sh + wizard.sh
docker/                   # Full-stack compose (postgres + redis + nextcloud + istota)
web/                      # SvelteKit (adapter-static, base /istota)
tests/                    # pytest + pytest-asyncio (~3900 tests, 91 files)
schema.sql
pyproject.toml
```

## Architecture

```
Talk Poll ──►┐
Email Poll ──►├─► SQLite Queue → Scheduler → Brain → Talk/Email Response
TASKS.md ────►│
CLI ─────────►┘

GPS Webhook ──► Location DB → Place detection → ntfy/Talk
Web App ──► Nextcloud OIDC → Dashboard / Feeds / Location / Money
```

- **Talk poller**: daemon thread, long-poll per conversation, WAL-mode DB.
- **Email poller**: routing precedence — plus-address → sender match → thread match (`sent_emails`). Plus-addressed emails from untrusted senders are held in `pending_confirmation`.
- **Task queue**: atomic locking with `user_id` filter, exponential backoff (1, 4, 16 min).
- **Scheduler**: per-user worker pool, three-tier concurrency (instance fg/bg + per-user).
- **Executor**: builds prompt + env, hands a `BrainRequest` to the configured Brain.
- **Brain**: pluggable model backend. Phase 1 = `ClaudeCodeBrain` (wraps `claude` CLI).

## Key Concepts

### Identity
- Technical IDs (package, env vars, DB, CLI): always `istota`.
- User-facing identity: `bot_name` config (default "Istota"). `bot_dir_name` sanitizes for filesystem use.
- Templated docs use `{BOT_NAME}`, `{BOT_DIR}`, `{user_id}` placeholders.

### Prompt Layers
1. **Emissaries** (`config/emissaries.md`) — constitutional principles, global only.
2. **Persona** (`config/persona.md` or user `PERSONA.md`) — character.
3. **Custom system prompt** (`config/system-prompt.md`, opt-in) — replaces CC default.

### Admin / Non-Admin Isolation
Admin user IDs in `/etc/istota/admins` (empty = all admin). Non-admins: scoped mount, no DB write, no subtasks, `admin_only` skills filtered.

### Memory System
- `USER.md` — auto-loaded, optional nightly op-based curation. Runtime writes go through the `memory` skill CLI (`istota-skill memory append|add-heading|remove|show|headings`) — never `echo >>`. The CLI shares the curation `apply_ops` engine, takes a per-file flock, and writes a `source="runtime"` audit entry per call.
- `CHANNEL.md` — loaded with `conversation_token`. Same CLI with `--channel TOKEN` (token must match `ISTOTA_CONVERSATION_TOKEN`).
- `memories/YYYY-MM-DD.md` — last N days auto-loaded (`auto_load_dated_days`).
- Knowledge graph (`knowledge_facts`) — temporal subject/predicate/object triples, freeform predicates, fuzzy dedup (predicate-equality gated), audited. Sandboxed runtime writes via `istota-skill memory_search add-fact|invalidate|delete-fact` are deferred as `task_<id>_kg_ops.json` and applied by the scheduler post-task.
- Classification gate in `memory/skill.md`: temporal events and stable factual claims → KG; behavioral instructions → USER.md.
- Nightly curator self-heals: bypass-write detection (`USER.md.last_seen.json` sidecar), sha256 re-read after the LLM call, agents-header migration, Phase-A lint pass logs date-stamped USER.md bullets without migrating them.
- Memory recall (BM25 + vector) — opt-in via `auto_recall`.
- Briefings exclude all personal memory.
- Subsystem lives under `src/istota/memory/`; `memory/sleep_cycle.py` orchestrates.

### Nextcloud Layout
```
/Users/{user_id}/{bot_name}/{config,exports,scripts,examples}/
/Users/{user_id}/{inbox,memories,shared}/
/Channels/{conversation_token}/{CHANNEL.md,memories/}
```

### Skills
Self-contained `src/istota/skills/<name>/skill.md` (YAML frontmatter + body). Two-pass selection: deterministic Pass 1 (always_include / source_types / file_types / triggers / sticky / companions / excludes), then optional LLM Pass 2 (Haiku). CLI skills expose `python -m istota.skills.<name>` and run through the credential-injecting skill proxy. Full details in `.claude/rules/skills.md`.

### Input Channels
- **Talk**: long-poll, message cache, ack/progress/result via referenceId. `!commands` intercepted in poller.
- **Email**: IMAP poll, attachments to `inbox/`, threaded replies via deferred `email output` JSON. Outbound tracked in `sent_emails` for emissary thread matching.
- **TASKS.md**: 30s poll, `[ ] [~] [x] [!]` markers, SHA-256 identity.

### Briefings
Sources: user `BRIEFINGS.md` > per-user config > main config. Cron in user TZ. Components: calendar, todos, email, markets, news, headlines, notes, reminders. Claude returns structured JSON (`{subject, body}`); scheduler delivers. Email skill excluded.

### Scheduled Jobs (CRON.md)
Markdown with TOML `[[jobs]]`. Types: `prompt`, `prompt_file`, `command`. Per-job `model`/`effort` overrides. Auto-disable after 5 consecutive failures. `skip_log_channel`, `silent_unless_action`, `once = true` supported.

### Sleep Cycle
Nightly extraction goes through the configured Brain (no streaming, no sandbox). Per-feature model overrides via `[sleep_cycle]` and `[channel_sleep_cycle]`. Writes dated memory files with `ref:TASK_ID`, inserts KG facts, optionally curates `USER.md` op-by-op.

### Heartbeat
`HEARTBEAT.md` — `file-watch`, `shell-command`, `url-health`, `calendar-conflicts`, `task-deadline`, `self-check`. Cooldown + quiet hours.

### GPS Location
Overland webhook → `webhook_receiver.py`. Asymmetric place detection (hysteresis on entry, continuous away on exit). Reconciler re-derives closed visits. Discovered clusters dismissable. Tables: `location_pings`, `places`, `visits`, `location_state`, `dismissed_clusters`.

### Web UI
SvelteKit (`web/`, `adapter-static`, base `/istota`) + FastAPI (`web_app.py`). Nextcloud OIDC auth, 7-day session. Routes: dashboard, feeds (reader + sprocket-icon settings page; backend selectable via `[feeds] backend = "miniflux" | "native"` — native serves `istota.feeds.routes` against per-user SQLite), location (today + history with cluster discovery), money (`/istota/money/*`). Dev: `VITE_MOCK_API=1 npm run dev` for in-process mock backend. Frontend primitives in `web/src/lib/components/ui/` (AppShell, ShellHeader, Sidebar, Chip, Button, Select, Modal, etc.).

### Security
- **Sandbox** (`bwrap`): per-user filesystem isolation. Linux + bubblewrap is the only supported deployment.
- **Network proxy**: `--unshare-net` + CONNECT proxy on Unix socket; allowlist of `host:port`. No MITM.
- **Skill proxy**: strips secret env vars from Claude; CLI calls go through Unix socket that injects credentials server-side. Authorization decoupled from skill selection.
- **Deferred DB**: sandboxed Claude writes JSON to temp dir; scheduler processes after success. Identity (`user_id`, `conversation_token`) always from task, not JSON. Subtasks rate-limited (`max_subtasks_per_task`, `max_subtask_depth`, `max_subtask_prompt_chars`), admin-only.

## Testing

TDD with pytest + pytest-asyncio, class-based, `unittest.mock`, real SQLite via `tmp_path`. Integration tests `@pytest.mark.integration`.

```bash
uv run pytest tests/ -v
uv run pytest -m integration -v
uv run pytest tests/ --cov=istota --cov-report=term-missing
```

## Development Commands

```bash
uv sync                                            # Install deps
uv run istota init                                 # Initialize DB
uv run istota task "prompt" -u USER -x [--dry-run] # Execute task
uv run istota task "prompt" -u USER -t ROOM -x     # With conversation context
uv run istota resource add|list -u USER ...        # Resources
uv run istota run [--once] [--briefings]           # Process pending
uv run istota email list|poll|test
uv run istota user list|lookup|init|status
uv run istota calendar discover|test
uv run istota tasks-file poll|status [-u USER]
uv run istota kv get|set|list|delete|namespaces
uv run istota list [-s STATUS] [-u USER]
uv run istota show <task-id>
uv run istota-scheduler [-d] [-v] [--max-tasks N]
```

## Configuration

Search order: `config/config.toml` → `~/src/config/config.toml` → `~/.config/istota/config.toml` → `/etc/istota/config.toml`. Override with `-c PATH`.

Per-user: `config/users/{user_id}.toml` overrides `[users]` in main config. CalDAV derived from Nextcloud. Field-by-field reference in `.claude/rules/config.md`.

## Deployment

**Ansible**: role at `deploy/ansible/` (symlinked from `~/Repos/ansible-server/roles/istota/`). When adding config fields, update `defaults/main.yml` + `templates/config.toml.j2`.

**Docker**: `docker/docker-compose.yml` brings up postgres + redis + nextcloud + istota. Sandbox / skill proxy / network proxy disabled inside container (container provides isolation). Key env: `CLAUDE_CODE_OAUTH_TOKEN`, `ADMIN_PASSWORD`, `USER_NAME`/`USER_PASSWORD`, `BOT_PASSWORD`, `POSTGRES_PASSWORD`.

**Nextcloud mount**: `/srv/mount/nextcloud/content` via rclone (`istota_use_nextcloud_mount: true`).

## Task Status

`pending` → `locked` → `running` → `completed` / `failed` / `pending_confirmation` / `cancelled`
