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
├── cli.py                # Local CLI (task, resource, briefing, secret, user, run, …)
├── config.py             # TOML loader + DB-overlay (user_profiles / user_resources / briefing_configs)
├── context.py            # Hybrid conversation context selection
├── db.py                 # SQLite operations (all tables)
├── executor.py           # Per-task orchestration (memory/skills/sandbox)
├── scheduler.py          # Task processor, briefings, all polling
├── email_poller.py       # IMAP polling + plus-address routing
├── talk_poller.py        # Nextcloud Talk long-polling
├── tasks_file_poller.py  # TASKS.md monitoring
├── heartbeat.py          # Health-check system
├── webhook_receiver.py   # FastAPI: Overland GPS, etc.
├── web_app.py            # Authenticated web UI (Nextcloud OAuth2 + admin dashboard)
├── secrets_store.py      # Encrypted credential store (Fernet via scrypt-derived key)
├── secret_schema.py      # Shared service/key schema for `istota secret` CLI + web UI
├── modules.py            # MODULE_NAMES registry (feeds, money, location)
├── user_profiles.py      # Per-user profile store (Phase 6)
├── user_briefings.py     # Per-user briefings store (Phase 7b)
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

config/                   # config.toml, persona.md, emissaries.md, system-prompt.md, guidelines/, skills/
deploy/ansible/           # Role + install.sh + wizard.sh + validate_config.py
docker/                   # Full-stack compose (nginx + nextcloud + postgres + redis + istota scheduler/web/webhooks)
web/                      # SvelteKit (adapter-static, base /istota)
tests/                    # pytest + pytest-asyncio (~4100 tests, 99 files)
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
Web App ──► Nextcloud OAuth2 → Dashboard / Feeds / Location / Money / Settings / Admin
```

- **Talk poller**: daemon thread, long-poll per conversation, WAL-mode DB.
- **Email poller**: routing precedence — plus-address → sender match → thread match (`sent_emails`). Plus-addressed emails from untrusted senders are held in `pending_confirmation`.
- **Task queue**: atomic locking with `user_id` filter, exponential backoff (1, 4, 16 min).
- **Scheduler**: per-user worker pool, three-tier concurrency (instance fg/bg + per-user).
- **Executor**: builds prompt + env, hands a `BrainRequest` to the configured Brain.
- **Brain**: pluggable model backend. Phase 1 = `ClaudeCodeBrain` (wraps `claude` CLI). Each brain owns its own model namespace — canonical IDs, provider aliases (`opus-high`, `opus-46-high`), and default role-target mappings (`fast`/`general`/`smart`). Consumers always go through `make_brain(config.brain).resolve_alias(...)` / `.resolve_model_name(...)`. Operator role overrides via `[models.roles]` TOML are global and provider-agnostic.
- **`!model <alias> <prompt>`**: per-task model override prefix in Talk. Aliases (resolved by the active brain): `default`, role aliases (`fast`/`general`/`smart`/custom), provider aliases (`opus`, `opus-high`, `opus-xhigh`, `opus-max`, `opus-46`, `opus-46-high`, `sonnet`, `sonnet-high`, `haiku`). Stored canonical on the task row so the DB stays version-pinned. Companion: `!models` lists the resolved table.

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

### Modules vs resources vs connected services
- **Resources** — paths/identifiers a user owns (calendars, folders, todo files, notes folders, email folders, reminders files). Multiple per user. Live in `[[users.X.resources]]` + the `user_resources` DB table.
- **Modules** — on-by-default features with their own UI tab and a settings page reachable via a cog icon (`feeds`, `money`, `location`). Names live in `istota.modules.MODULE_NAMES`. Per-user opt-out via `disabled_modules`. Single source of truth: `Config.is_module_enabled(user_id, module)`.
- **Connected services** — per-user external API credentials consumed by skills (`karakeep`, `google_workspace`, `ntfy`). Stored encrypted in the `secrets` table (Fernet over scrypt-derived key from `ISTOTA_SECRET_KEY`); the bookmarks skill resolves both `KARAKEEP_BASE_URL` and `KARAKEEP_API_KEY` from there. Provisioned via `istota secret ensure|list|remove` (Ansible) or `/istota/settings` (web). Schema for both surfaces lives in `secret_schema.py`.

### ntfy push notifications
ntfy is a per-user connected service — there is no global `[ntfy]` block. Each user supplies their own server URL, topic, and (optional) auth via the encrypted `secrets` table (web settings or `istota secret ensure -s ntfy ...`). `notifications._send_ntfy` reads everything from the user's secret rows; if the user has no `topic` set, ntfy is a no-op for them. Default priority is hardcoded to `3`; per-call overrides flow through `send_notification(...)`.

What it IS: a one-way push channel (bot → device) used by heartbeat alerts, scheduled-job output (when `output_target=ntfy`), and `surface="ntfy"` notifications. What it ISN'T: two-way (you can't reply over ntfy), a Talk replacement, operator-shared infrastructure, or required (most users won't configure it).

### Memory System
- `USER.md` — auto-loaded, optional nightly op-based curation. Runtime writes go through the `memory` skill CLI (`istota-skill memory append|add-heading|remove|show|headings`) — never `echo >>`. The CLI shares the curation `apply_ops` engine, takes a per-file flock, and writes a `source="runtime"` audit entry per call.
- `CHANNEL.md` — loaded with `conversation_token`. Same CLI with `--channel TOKEN` (token must match `ISTOTA_CONVERSATION_TOKEN`). Channel writes are not audited (no per-channel audit infrastructure yet) and do not update `USER.md.last_seen.json`; the audit/curation pipeline is USER.md-only.
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
Sources: user `BRIEFINGS.md` > `briefing_configs` DB table > `[[users.X.briefings]]` block. Provision via `istota briefing ensure` or the web UI; `enabled=0` mutes a row. Cron in user TZ. Components: calendar, todos, email, markets, news, headlines, notes, reminders. Claude returns structured JSON (`{subject, body}`); scheduler delivers. Email skill excluded.

### Scheduled Jobs (CRON.md)
Markdown with TOML `[[jobs]]`. Types: `prompt`, `prompt_file`, `command`. Per-job `model`/`effort` overrides. Auto-disable after 5 consecutive failures. `skip_log_channel`, `silent_unless_action`, `once = true` supported.

### Sleep Cycle
Nightly extraction goes through the configured Brain (no streaming, no sandbox). Per-feature model overrides via `[sleep_cycle]` and `[channel_sleep_cycle]`. Writes dated memory files with `ref:TASK_ID`, inserts KG facts, optionally curates `USER.md` op-by-op.

### Heartbeat
`HEARTBEAT.md` — `file-watch`, `shell-command`, `url-health`, `calendar-conflicts`, `task-deadline`, `self-check`. Cooldown + quiet hours.

### GPS Location
Overland webhook → `webhook_receiver.py`. Asymmetric place detection (hysteresis on entry, continuous away on exit). Reconciler re-derives closed visits. Discovered clusters dismissable. Tables: `location_pings`, `places`, `visits`, `location_state`, `dismissed_clusters`.

### Web UI
SvelteKit (`web/`, `adapter-static`, base `/istota`) + FastAPI (`web_app.py`). Nextcloud-hosted OAuth2 (the legacy generic OIDC fallback was retired — Docker provisions the OAuth2 client via `provision-nc.sh`; Ansible templates `[web.oauth2]` directly), 7-day session. Routes: dashboard, feeds (reader + sprocket-icon settings page served by `istota.feeds.routes` against per-user SQLite), location (today + history with cluster discovery; `/location/settings` for Overland ingest token), money (`/istota/money/*`), admin (read-only system health at `/istota/admin`, gated by a new `_user_is_web_admin` helper that uses the `/etc/istota/admins` allowlist and fails closed on empty allowlist — distinct from `Config.is_admin`, which retains its back-compat "empty = all admin" rule for sandbox/skill/command checks). Single-payload `GET /istota/api/admin/stats` aggregator; all timestamps normalized to canonical ISO 8601 UTC via `_iso_utc()`. Dev: `VITE_MOCK_API=1 npm run dev` for in-process mock backend. Frontend primitives in `web/src/lib/components/ui/` (AppShell, ShellHeader, Sidebar, Chip, Button, Select, Modal, etc.); shared settings primitives in `web/src/lib/components/settings/` (`SecretField`, `ServiceCard`).

Settings split (modules refactor, Phase 2): `GET /settings/services` returns only Connected services (`karakeep`, `google_workspace`); module-owned services (`monarch`, `feeds.tumblr_api_key`, `overland.ingest_token`) live on the per-module settings page and are reachable via `GET /settings/module-services/{module}`. Both endpoints share `_build_service_card`. `GET /settings/modules` returns the module registry plus per-user `disabled_modules` so the Preferences card can render the opt-out multiselect. Secret PUT/DELETE accept any service in `_all_known_services()` (connected ∪ module). Each per-module page calls `getModuleServices(<module>)` first; when `module_enabled=false`, it shows a "Module disabled — enable in /settings → Preferences" banner instead of the configuration UI. `/location/settings` adds an `/location/settings-info` endpoint that returns the webhook-URL placeholder (`https://<host>/webhooks/location?token=<token>`) and read-only place-detection knobs.

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
uv run istota resource ensure|add|list -u USER ... # Resources (DB-backed)
uv run istota briefing ensure -u USER -n NAME ...  # Briefings (DB-backed)
uv run istota secret ensure|list|remove -u USER -s SERVICE -k KEY [-v VALUE]
uv run istota user ensure|list|lookup|init|status -u USER ...
uv run istota run [--once] [--briefings]           # Process pending
uv run istota email list|poll|test
uv run istota calendar discover|test
uv run istota tasks-file poll|status [-u USER]
uv run istota kv get|set|list|delete|namespaces
uv run istota list [-s STATUS] [-u USER]
uv run istota show <task-id>
uv run istota-scheduler [-d] [-v] [--max-tasks N]
```

## Configuration

Search order: `config/config.toml` → `~/src/config/config.toml` → `~/.config/istota/config.toml` → `/etc/istota/config.toml`. Override with `-c PATH`.

Per-user data lives in DB tables (`user_profiles`, `user_resources`, `briefing_configs`, `secrets`) populated by `istota user|resource|briefing|secret ensure`. The `[users.X]` block in `config.toml` (docker entrypoint path) is also accepted; DB rows win at config-load time. The retired `config/users/{user}.toml` mechanism and `config/users/` directory are gone — Ansible no longer renders per-user TOML. CalDAV derived from Nextcloud. Field-by-field reference in `.claude/rules/config.md`.

## Deployment

**Ansible**: role at `deploy/ansible/` (symlinked from `~/Repos/ansible-server/roles/istota/`). When adding config fields, update `defaults/main.yml` + `templates/config.toml.j2`. The role runs `files/validate_config.py` against the rendered TOML before any handler can restart the scheduler — structural bugs fail the play instead of the running daemon. Three systemd units (`istota-scheduler`, `istota-web`, `istota-webhooks`) all read `ISTOTA_ADMINS_FILE` and `ISTOTA_SECRET_KEY` from the same EnvironmentFile.

**Docker**: `docker/docker-compose.yml` brings up nginx (single host port, reverse-proxies `/` → Nextcloud and `/istota/` → web service) + nextcloud + postgres + redis + istota (multi-stage Dockerfile builds the SvelteKit frontend; separate scheduler / web / webhooks services). Sandbox / skill proxy / network proxy disabled inside container (container provides isolation). The entrypoint auto-provisions `#general`, `#logs`, `#alerts` group rooms (lookups are scoped by `USER_NAME` participation, so identically-named rooms across users on a shared NC don't collide), registers an OAuth2 client in NC via inline PHP, generates `LOCATION_INGEST_TOKEN` + `ISTOTA_SECRET_KEY` (persisted to `/data/.secret_key`), and seeds workspace files. Modules default on (`ISTOTA_*_ENABLED=true`). Key env: `CLAUDE_CODE_OAUTH_TOKEN`, `ADMIN_PASSWORD`, `USER_NAME`/`USER_PASSWORD`, `BOT_PASSWORD`, `POSTGRES_PASSWORD`, `DOMAIN`, `ISTOTA_WEB_INSECURE_COOKIES` (toggle for plaintext localhost).

**Nextcloud mount**: `/srv/mount/nextcloud/content` via rclone (`istota_use_nextcloud_mount: true`).

## Task Status

`pending` → `locked` → `running` → `completed` / `failed` / `pending_confirmation` / `cancelled`
