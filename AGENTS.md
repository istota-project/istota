# Istota - Claude Code Bot

Claude Code-powered assistant bot with Nextcloud Talk interface.

**Production server**: `your-server` (SSH, installed at `/srv/app/istota`).

For module-specific internals, see `.claude/rules/`:
- `brain.md` — Brain protocol + ClaudeCodeBrain + NativeBrain (in-process agent loop)
- `executor.md` — `execute_task()`, env mapping, prompt assembly, security
- `scheduler.md` — daemon loop, worker pool, DB tables, deferred ops
- `config.md` — every dataclass field + TOML mapping
- `skills.md` — skill metadata, single-axis selection (eager vs menu), CLI modules
- `transport.md` — Transport seam over messaging surfaces (Talk + email; Matrix / web chat designed-for)

## Project Structure

```
src/istota/
├── brain/                # Pluggable model invocation (Brain protocol)
├── memory/               # search.py, knowledge_graph.py, sleep_cycle.py, curation/
├── skills/               # 30 self-contained skills (skill.md + optional CLI)
├── cli.py                # Local CLI (task, resource, briefing, secret, user, run, …)
├── config.py             # TOML loader + DB-overlay (user_profiles / user_resources / briefing_configs)
├── context.py            # Hybrid conversation context selection
├── db.py                 # SQLite operations (framework tables)
├── db_health.py          # `PRAGMA quick_check` + self-healing `REINDEX` for SQLite DBs on the FUSE-backed mount
├── executor.py           # Per-task orchestration (memory/skills/sandbox)
├── events.py             # Task event streaming: TaskEvent, EventWriter, EventSubscriber + task_events log
├── consumers/            # Event consumers: TalkEventSubscriber, LogChannelSubscriber, PushNotificationSubscriber
├── scheduler.py          # Task processor, briefings, all polling
├── transport/            # Transport seam: IncomingMessage, registry, ingest, routing (delivery plan), talk/ email/ ntfy/ istota_file/ repl/ web/ (6 Transports, push + stream)
├── email_support.py      # Shared non-transport email plumbing (get_email_config, thread helpers, cleanup) used by transport + briefing/notifications/tasks-file
├── tasks_file_poller.py  # TASKS.md monitoring
├── heartbeat.py          # Health-check system
├── webhook_receiver.py   # FastAPI: Overland GPS, etc.
├── web_app.py            # Authenticated web UI (Nextcloud OAuth2 + admin dashboard)
├── secrets_store.py      # Encrypted credential store (Fernet via scrypt-derived key)
├── secret_schema.py      # Shared service/key schema for `istota secret` CLI + web UI
├── modules.py            # MODULE_NAMES (feeds, money, location, health) + EXPERIMENTAL_MODULES (empty)
├── experimental.py       # Operator feature-flag gate (`@requires_feature`, env helpers)
├── user_profiles.py      # Per-user profile store (Phase 6)
├── user_briefings.py     # Per-user briefings store (Phase 7b)
├── notifications.py      # Talk / Email / ntfy dispatcher
├── skill_proxy.py        # Unix-socket proxy for credential isolation
├── network_proxy.py      # CONNECT proxy for network isolation
├── devbox_proxy.py       # Per-user host-side daemon: tokens stay out of the devbox container
├── devbox_proxy_protocol.py  # Wire protocol for devbox_proxy (single-line JSON, 16 MiB cap)
├── docker_proxy.py       # Per-user Docker-API allowlist proxy: bound into the sandbox at /var/run/docker.sock in place of the root-equivalent raw socket; permits only exec/cp/inspect/restart on the user's own container
├── nextcloud_api.py      # NC user metadata
├── nextcloud_client.py   # OCS + WebDAV plumbing
├── storage.py            # Bot-managed Nextcloud storage
├── feeds/                # Native RSS/Atom/Tumblr/Are.na — poller, SQLite, routes, OPML
├── health/               # Body stats, bloodwork, biomarker trends, encounters, immunizations, Garmin, OCR
├── location/             # Per-user location.db module (pings, places, visits, state, migration)
├── location_logic.py     # Place stats / cluster discovery (shared web ⇄ skill)
├── scheduler_deferred.py # Deferred-op replay (subtasks, KG, KV, health_ops, …)
├── shared_file_organizer.py
├── commands.py           # surface-agnostic !command dispatch (CommandContext + registry push/stream)
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
Web App ──► Nextcloud OAuth2 → Dashboard / Feeds / Location / Money / Health / Settings / Admin
```

- **Talk poller**: daemon thread, long-poll per conversation, WAL-mode DB.
- **Email poller**: routing precedence — plus-address → sender match → thread match (`sent_emails`). Plus-addressed emails from untrusted senders are held in `pending_confirmation`.
- **Task queue**: atomic locking with `user_id` filter, exponential backoff (1, 4, 16 min).
- **Scheduler**: per-user worker pool, three-tier concurrency (instance fg/bg + per-user).
- **Executor**: builds prompt + env, hands a `BrainRequest` to the configured Brain.
- **Brain**: pluggable model backend. Three brains ship: `ClaudeCodeBrain` (default, wraps headless `claude -p` CLI), `NativeBrain` (Istota's in-process agent loop against any OpenAI-compatible model; loop in `agent/`, providers in `llm/`, turn state in `session/`), and `TmuxClaudeBrain` (drives the interactive `claude` TUI in a detached tmux session to keep traffic on subscription billing; full-switch via `brain.kind = "tmux_claude"`, with `claude_code` as the automatic fallback + circuit breaker — see `.claude/rules/brain.md`). Each brain owns its own model namespace — canonical IDs, provider aliases (`opus-high`, `opus-46-high`), and default role-target mappings (`fast`/`general`/`smart`). Consumers always go through `make_brain(config.brain).resolve_alias(...)` / `.resolve_model_name(...)`. Operator role overrides via `[models.roles]` TOML are global and provider-agnostic.
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
- **Modules** — on-by-default features with their own UI tab and a settings page reachable via a cog icon (`feeds`, `money`, `location`, `health`). Names live in `istota.modules.MODULE_NAMES`. Per-user opt-out via `disabled_modules`. Single source of truth: `Config.is_module_enabled(user_id, module)`. Names that also appear in `EXPERIMENTAL_MODULES` are AND-gated on the matching `[experimental] features` flag — disabled-by-default until the operator opts in, after which they behave like any other module. (`EXPERIMENTAL_MODULES` is currently empty; the mechanism is kept for future modules.)
- **Connected services** — per-user external API credentials consumed by skills (`karakeep`, `google_workspace`, `ntfy`). Stored encrypted in the `secrets` table (Fernet over scrypt-derived key from `ISTOTA_SECRET_KEY`); the bookmarks skill resolves both `KARAKEEP_BASE_URL` and `KARAKEEP_API_KEY` from there. Provisioned via `istota secret ensure|list|remove` (Ansible) or `/istota/settings` (web). Schema for both surfaces lives in `secret_schema.py`.

### Experimental features
Operator-scoped feature flags for in-tree-but-off-by-default work. Configured via `[experimental] features = [...]` in `config.toml` (or `istota_experimental_features` in Ansible). Off by default; never exposed in the web UI; not toggleable per user. The flag list flows to every subprocess builder as `ISTOTA_EXPERIMENTAL_FEATURES` (CSV), so subprocess-paths see the same gate as the LLM path. Four surfaces honor the gate:

- **CLI subcommands** — `@requires_feature("name")` Click decorator (`src/istota/experimental.py`). Gated-off calls emit the standard `{"status":"error","error":"…"}` JSON envelope; in `_execute_command_task` / `_execute_skill_task` the envelope detector reclassifies stdout-OK exits as task failures with the human-readable message intact. Currently used by `money lots` (`money_tax`) and `money wash-sales` (`money_wash_sales`).
- **Skills** — `experimental: true` in `skill.md` frontmatter requires `skill_<name>` in the enabled set. The gate fires in the selection main loop, the sticky path, the companion pull-in, and the menu-catalogue filter (`eligible_skill_names`) so a gated-off skill reaches neither selection nor the on-demand menu.
- **Modules** — `EXPERIMENTAL_MODULES` mapping in `modules.py` (currently empty). When populated, `Config.is_module_enabled` AND-s the flag in before the per-user DB read; the `/settings/modules` web endpoint and `_coerce_profile_value("disabled_modules", …)` validation consult the same gate so a disabled experimental module never appears in the user-facing surface at all.
- **Web routes** — module-shaped surfaces only register when the gate is on; `/api/me` filters its `features` payload through the same check.

`istota experimental list` prints the `KNOWN_FEATURES` registry with on/off status from the loaded config. Unknown names in TOML log a warning but don't fail startup, so graduating a feature in code stays a code-only change. Naming convention: `module_<x>` for module gates, `skill_<x>` for skill gates, free-form for CLI subcommand gates. See `docs/EXPERIMENTAL.md` for the registry and graduation policy.

### ntfy push notifications
ntfy is a per-user connected service — there is no global `[ntfy]` block. Each user supplies their own server URL, topic, and (optional) auth via the encrypted `secrets` table (web settings or `istota secret ensure -s ntfy ...`). `notifications._send_ntfy` reads everything from the user's secret rows; if the user has no `topic` set, ntfy is a no-op for them. Default priority is hardcoded to `3`; per-call overrides flow through `send_notification(...)`.

What it IS: a one-way push channel (bot → device) used by heartbeat alerts, scheduled-job output (when `output_target=ntfy`), and `surface="ntfy"` notifications. What it ISN'T: two-way (you can't reply over ntfy), a Talk replacement, operator-shared infrastructure, or required (most users won't configure it).

### Memory System
- `USER.md` — auto-loaded, optional nightly op-based curation. Runtime writes go through the `memory` skill CLI (`istota-skill memory append|add-heading|remove|show|headings`) — never `echo >>`. The CLI shares the curation `apply_ops` engine, takes a per-file flock, and writes a `source="runtime"` audit entry per call.
- `CHANNEL.md` — loaded with `conversation_token`. Same CLI with `--channel TOKEN` (token must match `ISTOTA_CONVERSATION_TOKEN`). Channel writes are not audited (no per-channel audit infrastructure yet) and do not update `USER.md.last_seen.json`; the audit/curation pipeline is USER.md-only.
- `memories/YYYY-MM-DD.md` — last N days auto-loaded (`auto_load_dated_days`).
- Knowledge graph (`knowledge_facts`) — temporal subject/predicate/object triples, freeform predicates, fuzzy dedup (predicate-equality gated), audited. Sandboxed runtime writes via `istota-skill memory_search add-fact|invalidate|delete-fact` are deferred as `task_<id>_kg_ops.json` and applied by the scheduler post-task.
- Classification gate in `memory/skill.md`: temporal events and stable factual claims → KG; behavioral instructions → USER.md; reusable task procedures → playbooks (sleep-cycle-generated in v1).
- Learned playbooks (`playbooks.enabled`, off by default) — per-user markdown task procedures distilled by the sleep cycle from successful multi-step tasks, stored under the user's bot `playbooks/` dir, indexed into `memory_chunks` as `source_type="playbook"`, and recalled by relevance into a "## Learned Playbooks" prompt section. Markdown-only, never executed; excluded from briefings. See `.claude/rules/scheduler.md` (generation) + `.claude/rules/executor.md` (recall).
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
Self-contained `src/istota/skills/<name>/skill.md` (YAML frontmatter + body). **Single-axis model:** a skill is either **eager** (full body in the prompt, because a deterministic rule in `select_skills` picked it — `always_include` / `source_types` / `file_types` / `sticky` / `companions`, minus `excludes`) or in the **menu** (a one-line entry the model pulls in full via `istota-skill skills show <name>`, which also delivers that skill's companions). The menu is the full eligible catalogue (`eligible_skill_names` — every loadable skill not already eager), so the capable main model self-selects from it. Keyword (`triggers`) and `resource_types` matching are NOT selectors (kept as `!skills`-surfaced documentation); `resource_types` survives only as a menu-membership gate. There is no eager/lazy `disclosure` field, no `progressive_disclosure` flag, no `always_eager` list — the menu is intrinsic. CLI skills expose `python -m istota.skills.<name>` and run through the credential-injecting skill proxy. The `skills` core skill is the on-demand loader. (The former LLM "Pass 2 semantic routing" pre-router and the two-axis eager/lazy disclosure model were both removed — the menu replaced them.) Full details in `.claude/rules/skills.md`.

### Input Channels
- **Talk**: long-poll, message cache, ack/progress/result via referenceId. `!commands` intercepted in poller.
- **Email**: IMAP poll, attachments to `inbox/`, threaded replies via deferred `email output` JSON. Outbound tracked in `sent_emails` for emissary thread matching.
- **TASKS.md**: 30s poll, `[ ] [~] [x] [!]` markers, SHA-256 identity.
- **REPL** (`istota repl`): interactive terminal loop (`src/istota/repl/`). Each line becomes a `source_type="repl"` task with `output_target="stream"`, run inline via `scheduler.run_task_inline` (no daemon needed); `task_events` stream to the terminal via `TerminalSubscriber`. REPL tasks are inline-only — `db.claim_task` and the daemon's pending-user discovery exclude `source_type="repl"` so a running daemon never double-executes them.
- **Web chat**: always-on in-app chat surface in the web UI (full-page console at `/chat`, "Chat" nav tab before Feeds). Rooms are per-user channel tokens in `web_chat_rooms` (each gets its own `CHANNEL.md` + sleep-cycle handling); a sent message becomes a `source_type="web"` task (interactive: loads context + `CHANNEL.md` + `guidelines/web.md`) with `output_target="web"`, which routes as a stream surface (no Talk/email push — the result and progress live in `task_events`, tailed by the existing `/api/chat/tasks/{id}/stream` SSE endpoint). Endpoints under `/api/chat/*` in `web_app.py`: rooms CRUD (incl. `DELETE /chat/rooms/{id}` — a hard, token-scoped cascade across `task_events`/`tasks`/`web_chat_messages`/`channel_sleep_cycle_state` via `db.delete_web_chat_room`, guarded by `count_active_web_tasks` → 409 when a task is in flight, plus best-effort `Channels/<token>/` removal; channel `memory_chunks` are a documented residual), message send/history, task confirm/cancel, attachment upload. Per-room UI: a kebab → `RoomSettings.svelte` modal renames a room (token invariant), copies its token (for a `web:<token>` route), and hard-deletes it behind a GitHub-style type-the-name confirm. Deep link `/chat?room=<token>` selects a room on load (`selectRoomByToken`), silently falling back for an unknown/foreign token. `!commands` (and the `!model <alias>` prefix) work identically across surfaces — `commands.dispatch(... surface=...)` runs the handler over a `CommandContext` and delivers via the resolved transport on a push surface (Talk) or returns the text inline on a stream surface (web → `inline_result`); per-user rate limit counts `source_type='web'` rows in `tasks`. Knobs under `[web.chat]`. Frontend engine in `web/src/lib/stores/chat.ts` (SSE with polling fallback) + widgets in `web/src/lib/components/chat/`. **Body render model**: an assistant turn is an ordered list of `text`/`tool`/`thinking` segments (`segments.ts`); `renderGroups` reduces it to interleaved render groups — substantial prose blocks (trimmed length ≥ `SUBSTANTIAL_TEXT_CHARS`, mirrors the backend gate) render as prominent markdown in true order, consecutive tools coalesce into one `ActivityTrace` chip, short lead-in narration + reasoning are dropped, and the trailing text is always the answer. This keeps a meaty intermediate block (analysis the model wrote, then acted on) visible as its own prose block instead of vanishing when the next tool settles it — the live stream and a reloaded-from-`execution_trace` turn build the same groups. Not a Talk replacement — an in-app companion. Web chat is also a *delivery* surface (ISSUE-121): alerts, the verbose execution log, and notifications routed to `web` are appended to a room as unsolicited system messages (distinct from task-backed turns), merged into room history by time and surfaced live in an open room by an idle poll. See `WebTransport` in the Transport abstraction section. **Unread indicators**: the sidebar bolds a room name and shows a count chip for rooms with unseen messages. Counts are server-computed in the `/chat/rooms` payload (`db.count_unread_messages` — `messages` past the per-user/per-surface `room_read_state` cursor, excluding `role='user'` so your own/Talk-mirrored turns don't ring your own room); `db.initialize_room_read_state` seeds a freshly-surfaced room's cursor to its current max so a backlog doesn't read as unread. Opening a room (or `POST /chat/rooms/{id}/read` → `_chat_mark_room_read`) advances the cursor to the room's newest message; the client persists on open / notif-append / stream-settle / `visibilitychange` (visibility-gated) and runs a 5s rooms-refresh that merges counts so a non-active room lights up on its own. The active room is held at 0.

### Unified Talk / web room sync
Talk and web chat share one surface-independent **room** model (spec in `Specs/Done/unified-talk-web-room-sync.md`). A `rooms` registry (PK = canonical `conversation_token`, `origin` talk|web) + `room_bindings` (per-surface ref) + a canonical `messages` store (role user|assistant|system, `task_id`, `origin_surface`, `external_ids`) + `room_read_state` supersede the de-facto `tasks`-as-history store; a markered one-time migration folds `web_chat_rooms`/`web_chat_messages`/distinct Talk tokens in and backfills. `get_conversation_history` reads `messages` with task-id re-pairing behind a self-healing dual-read (falls back to `tasks` until the store is caught up — a *completeness* check: every completed turn mirrored, not just the newest, so a partial migration / mid-rollout window can't truncate history to the mirrored subset); Talk keeps its metadata-rich `_build_talk_api_context` for Talk-origin context. Inbound flows through one `transport.ingest.record_inbound` choke point (resolve canonical token → echo-check → store user turn → create task), used by `ingest_message` (Talk/email) and the web POST. The scheduler stores the assistant turn on completion; `output_target="room"` fans out by live bindings with an **asymmetric mirror** — web→Talk is a real push, Talk→web pushes nothing (the web loader already renders Talk turns from the shared store), and confirmations never mirror. On a web→Talk mirror leg the bot can't post as the user, so it reposts the user's question attributed (`💬 <name> (via web):`) before its reply — a pure Talk-surface artifact never written to the canonical `messages` store, so web history/context is unaffected (`_format_mirror_user_repost`). The web room list is **membership-driven** (`db.list_member_rooms`, a `room_members` join) rather than keyed on the single-owner `rooms.user_id`, so a *shared* room (one token, one transcript) surfaces for every participant — a group Talk room with the bot plus two humans appears in *both* humans' web lists, each via their own `web_chat_rooms` handle (`UNIQUE(user_id, token)`, not globally unique). Membership is added by `register_room` and by every inbound sender (`record_inbound`), backfilled for existing deploys by the `room_members_v1` migration; a per-user delete/hide drops only that user's membership (the global `rooms.archived` flag is reserved for `archive_orphaned_talk_rooms` — "the bot left the Nextcloud room", which a fresh inbound un-archives). Fixes ISSUE-134, where a group room was visible to only one arbitrary participant. Room titles are backfilled from Talk's `displayName` every poll cycle, not just on the next inbound message, so a migrated room stops showing the generic "Talk room". The same poll cycle reconciles the other direction (`db.archive_orphaned_talk_rooms`): a Talk room the bot is no longer in (deleted in Nextcloud / bot removed) is archived so it stops surfacing in web — guarded against a transient empty conversation fetch, archive-not-delete so mirror history survives. An "Also open in Talk" promote (`POST /chat/rooms/{id}/promote`) creates a real Talk conversation via new OCS `TalkClient` methods, with two-way rename propagation, and a Talk turn streams live into an open web room. Full reference in `.claude/rules/transport.md`.

### Transport abstraction
A uniform seam over messaging surfaces (`src/istota/transport/`). Inbound, a `Transport.poll()` normalizes a surface's messages into `IncomingMessage`; `ingest_message` maps those onto `db.create_task`. Outbound, `deliver` / `edit` push a task's result to a resolved channel; `resolve_target` picks the channel. `TransportRegistry` (`make_registry(config)`, no I/O on construction) holds the enabled surfaces and `for_task(task)` resolves the primary one by `source_type` (`email`→email, `repl`→repl, everything else→talk). Six transports ship — `TalkTransport`, `EmailTransport`, `NtfyTransport`, `IstotaFileTransport` (all `surface_class="push"`), `ReplTransport` (`surface_class="stream"` — `deliver` is a no-op; outbound is the `task_events` log a terminal tails), and `WebTransport` (`surface_class="stream"`, `user_routable=True`) — and **Matrix is the designed-for next consumer** (a new surface = one `Transport` subclass + a `make_registry` line). `WebTransport` is the web chat *delivery* surface (ISSUE-121): an interactive `source_type="web"` task still streams its own result over `task_events` (routing short-circuits `web` to a stream destination), but the transport's `deliver()` is a real write — alerts, the verbose execution log, and any notification routed to `web` append an unsolicited system message to the user's room via the `web_chat_messages` table, rendered merged into the room transcript. Because it's `user_routable`, web auto-appears in every routing UI that reads `registry.routable_names()`. `conversation_token` stays the opaque per-surface channel id and `source_type` the routing key — neither renamed; no DB/config change. Talk delivery/edit (the only `TalkClient` construction outside the CLI) and `notifications._send_talk` flow through `TalkTransport`; email delivery flows through `EmailTransport`; `scheduler.post_result_to_talk` / `edit_talk_message` / `post_result_to_email` are thin shims. The progress-ack subscriber is gated on `capabilities.supports_progress_ack`. Both surfaces are subpackages with both directions co-located (`__init__.py` seam + `inbound.py`; email adds `outbound.py`) and both self-create their tasks inside `poll` via the shared `ingest_message` (the `create_task` must share the inbound `db.get_db` transaction with the poll-cursor advance, or a create failure would lose messages): Talk's inbound body is `transport/talk/inbound.py` (`poll_talk_conversations`), email's is `transport/email/inbound.py` (`poll_emails`); both transports' `poll` return `[]`. Email's shared non-transport helpers live in `istota.email_support`; the low-level clients stay outside the seam (`istota.talk.TalkClient`, `istota.skills.email`). Outbound fan-out (a task delivering to several surfaces) is `transport.routing.resolve_delivery_plan`'s job: it parses `output_target` (`talk`/`email`/`ntfy`/`istota_file`/`stream`/`both`/`all`/`surface:channel`/comma lists), resolves channels, and drops unregistered/unconfigured destinations with a warning. Separately, the per-user **purpose-keyed routing table** (`UserConfig.routing`, purposes `reply`/`alert`/`log`/`briefing`/`notification`) routes *notifications* via `notifications.send_notification(..., purpose=…)`; the `log` purpose additionally drives the verbose per-task execution log to any user-routable surface (`notifications.effective_log_destinations`, opt-in: `routing["log"]` > legacy `log_channel` > off; Talk streams live, email/ntfy get one final summary). Full reference in `.claude/rules/transport.md`.

### Task Event Streaming
One persistent, typed event stream per task feeds every output surface. The executor adapts the brain's (widened) `StreamEvent` union into `TaskEvent`s via an `EventWriter` (`events.py`), which persists them to the `task_events` table (WAL, shared scheduler ⇄ web) and notifies in-process subscribers. Event kinds: `task_started`, `tool_start`, `tool_end` (NativeBrain only — carries loop-measured `duration_ms`), `tool_progress` (NativeBrain, SSE only), `progress_text`, `text_delta` (stream surfaces only — incremental answer text coalesced by the executor at ~250ms/120char/boundary; pruned on the terminal path once the canonical `result` lands, so steady state retains zero. **Narration gate (a substance classifier):** a text run streams nothing until it crosses `scheduler.stream_text_gate_chars` (default 280) without an intervening tool call. At a tool boundary the two cases split (`executor._settle_deltas_at_tool_boundary`): a short lead-in ("Let me check…") that stayed under the gate is *dropped* (it never streamed, so it can't flash in the answer area); a SUBSTANTIAL block that crossed the gate is *kept* — its unflushed tail is flushed so the full block reaches the stream surface, where the web client renders it as its own prose block (analysis the model wrote, then acted on, is content — not throwaway narration). The gate is thus not an answer-vs-narration split: the final answer (after the last tool) always streams, and a short *final* answer that never crosses the gate still arrives whole via `result`. The earlier 250ms-timer flush *raced* the tool boundary and leaked narration permanently; the gate has no time-flush while held. Tune against the `stream_gate:` logs the executor emits per flush/discard. `0` disables the gate), `context_management`, `confirmation`, `result`, `error`, `cancelled`, `done`. Consumers: `TalkEventSubscriber` (edits the ack message in place), `LogChannelSubscriber` (accumulating edit), `PushNotificationSubscriber` (ntfy on long tasks) are in-process subscribers; the web SSE endpoint (`/istota/api/chat/tasks/{id}/stream`), the snapshot endpoint (`…/events`), and the admin endpoint (`/api/admin/tasks/{id}/events`) poll the table directly — the table is the bus, no IPC. **Retry continuity:** the event log is kept across retry-eligible failures (it is *not* wiped). `set_task_pending_retry` leaves the rows in place, the retry branch emits a `progress_text` "⏳ Attempt failed — retrying in N min…" notice, and the next attempt's `EventWriter` resumes `seq` from `db.get_max_task_event_seq` so it stays monotonic (no UNIQUE(task_id, seq) collision) and a watching web client's resume cursor stays valid — it sees the notice and the next attempt's events instead of a silent spinner. The live view therefore accumulates across attempts (attempt 1's tools, the notice, attempt 2's tools); history reconstruction is unaffected (it reads `execution_trace`, the final attempt's). **Terminal backstop:** the SSE + snapshot endpoints (`web_app._synthetic_terminal_events`) synthesize a terminal frame from the task row — numbered above the client's cursor — whenever a task is terminal in the DB but has no `done` deliverable to that client (a crash that skipped `finish()`, or any future log-reset path). A terminal task always yields a terminal frame. `seq` is monotonic per task, assigned by the writer; events are hand-deleted only in `cleanup_old_tasks` (the `ON DELETE CASCADE` clause is decorative — `PRAGMA foreign_keys` is unset). The brain owns dispatching the executor callback off any event loop (NativeBrain's `run_in_executor` hop), keeping the synchronous subscribers' `asyncio.run` calls safe (ISSUE-111 generalized). Config under `[scheduler]`: `progress_show_tool_use`, `progress_show_text`, `event_log_enabled`, `stream_text_gate_chars`, `push_notification_threshold_seconds`, `push_notification_sources`.

### Briefings
Sources: user `BRIEFINGS.md` > `briefing_configs` DB table > `[[users.X.briefings]]` block. Provision via `istota briefing ensure` or the web UI; `enabled=0` mutes a row. Cron in user TZ. Components: calendar, todos, email, markets, news, headlines, notes, reminders. Claude returns structured JSON (`{subject, body}`); scheduler delivers. Email skill excluded.

### Scheduled Jobs (CRON.md)
Markdown with TOML `[[jobs]]`. Types: `prompt`, `prompt_file`, `command`. Per-job `model`/`effort` overrides. Auto-disable after 5 consecutive failures. `skip_log_channel`, `silent_unless_action`, `once = true` supported.

### Sleep Cycle
Nightly extraction goes through the configured Brain (no streaming, no sandbox). Per-feature model overrides via `[sleep_cycle]` and `[channel_sleep_cycle]`. Writes dated memory files with `ref:TASK_ID`, inserts KG facts, optionally curates `USER.md` op-by-op.

### Heartbeat
`HEARTBEAT.md` — `file-watch`, `shell-command`, `url-health`, `calendar-conflicts`, `task-deadline`, `self-check`. Cooldown + quiet hours.

### GPS Location
Overland webhook → `webhook_receiver.py`. Asymmetric place detection (hysteresis on entry, continuous away on exit). Reconciler re-derives closed visits. Discovered clusters dismissable. Per-user `location.db` at `{workspace}/location/data/location.db` holds `location_pings`, `places`, `visits`, `location_state`, `dismissed_clusters` — module package at `src/istota/location/` with `resolve_for_user(user_id, config) -> LocationContext` (mirrors `feeds` / `money`). Skill CLI subcommands and web routes that need reverse-geocoding open a second connection to framework `istota.db` for the global `geocode_cache` / `reverse_geocode_cache` tables (cross-user Nominatim dedup); `location.db.with_geocode_conn(framework_db_path)` is the dual-conn entry point. One-time framework→per-user migrator at `python -m istota.location._migrate`, gated in Ansible by a `location_pings`-presence check.

### Health
Body stats, bloodwork panels, biomarker trends, medical history (encounters + diagnoses), immunization tracking, and Garmin Connect integration. On by default; per-user opt-out via `disabled_modules`. Per-user SQLite at `{workspace}/health/data/health.db` (stats, panels, biomarkers, biomarker_explainers, encounters, diagnoses, immunizations, immunization_refs, profile, display settings). All measurements stored metric (kg, cm, °C, mmHg, bpm); display layer converts to the user's preferred units. Module package at `src/istota/health/` with `models`, `db`, `units`, `routes`, `workspace`, `_loader`, `_migrate`, `ocr`, `explainer`, `garmin_sync`. Bundled `biomarker_refs.json` seeds 60+ canonical markers with sex-specific reference ranges, alias lists, and one-paragraph clinical descriptions; Istota's canonical ranges drive flagging (lab-printed ranges are preserved per-row but not the flagging source). Blood-pressure / resting-HR biomarker rows fan out to `stats` so the unified time series picks them up. Bundled `immunization_refs.json` seeds 19 adult-relevant vaccine schedules; `immunization_explainers.json` provides static clinical detail per vaccine.

OCR pipeline (`ocr.py`): PDF via `pdftotext`/`pypdf` with `pdftoppm` + Tesseract fallback; image via Tesseract. Extracted text plus the canonical marker list goes to the active brain (general role alias) which returns structured JSON; values >10× outside the widest canonical range surface as a likely-OCR-error warning. Optional deps degrade gracefully.

Biomarker explainer (`explainer.py`): out-of-range marker pages display an educational alert generated by the active brain. Hard guardrails in the prompt (never diagnose, never prescribe, always hedge, JSON-only output); strict response parsing rejects malformed output and falls back to a fixed safe payload so the UI never shows raw model output. Cached per-user per `(name, direction)` in `biomarker_explainers`; fallback responses are NOT cached so later brain availability still produces real content.

Surfaces: FastAPI router at `/istota/api/health/*` (stats CRUD + series + latest; panels CRUD + upload + extract + source streaming; biomarker trend + summary + canonical refs + matrix endpoint + cached explainer; CSV import/export at `/csv/import` (multipart, `on_collision=skip|replace|append`) and `/csv/export` (text/csv stream of confirmed panels in the same layout); encounters CRUD + history summary; diagnoses CRUD + resolve; immunizations CRUD + refs + coverage + parse + bulk + explainer; Garmin status/connect/mfa/sync/disconnect; profile / display settings; dashboard aggregator). SvelteKit pages under `/health/*` — `/stats` (netdata-style grid with sparklines), `/bloodwork` (dates-as-rows × markers-as-columns spreadsheet with category bands and flag-colored cells, plus Import/Export CSV controls), `/bloodwork/panel?id=…` (panel detail + inline-edit table + source preview), `/bloodwork/upload` (drag-and-drop OCR review-and-confirm), `/bloodwork/marker?name=…` (trend chart with out-of-range zones shaded, related-marker chips, clinical description, history table, LLM explainer card), `/health/history` (timeline + conditions list), `/health/history/encounter?id=…` (per-encounter detail), `/health/history/diagnoses` (diagnoses list), `/health/history/import` (encounter import from paperwork), `/health/immunizations` (coverage strip + quick-log), `/health/immunizations/paste` (parser preview), `/health/immunizations/detail` (per-record edit), `/health/immunizations/vaccine` (drill-down + explainer), `/health/immunizations/import` (paste-text + screenshot/PDF upload), `/health/settings` (DOB/height/sex + display preferences + Garmin connect/disconnect). The two formerly-dynamic routes use query params because `adapter-static` can't crawl runtime-only ids (commit 629c2ac).

CSV import/export (`src/istota/health/csv_io.py`): 3-row header (category banner + `Marker (unit)` headers + reference range) followed by `date, lab, value…` data rows. Aliases (`Hgb` → Hemoglobin, `LDL-C` → LDL, `Na` → Sodium, `D3 Total` → Vitamin_D, etc.) resolve via `biomarker_refs`; unknown markers store under the printed name and land in the matrix's "Other" category. Imported panels are written confirmed (`draft=0`) and the BP/HR fan-out to stats mirrors the OCR confirm flow.

`health` skill (`src/istota/skills/health/`): `log|stats|latest|panels|panel|add-panel|add-biomarker|trend|upload|import-csv|export-csv|summary|settings|set|encounters|encounter|add-encounter|update-encounter|delete-encounter|diagnoses|diagnosis|add-diagnosis|update-diagnosis|resolve-diagnosis|delete-diagnosis|history-summary|immunizations|immunization|add-immunization|update-immunization|delete-immunization|vaccine-refs|coverage|import-immunizations|explain-immunization|garmin-status|garmin-sync|garmin-disconnect`. `setup_env` hook injects `HEALTH_DB_PATH` (no-op when the user has opted out via `disabled_modules`). Writes flow through the deferred-op file `task_<id>_health_ops.json` under sandbox; `_process_deferred_health_ops` replays them post-task (including the `import_csv` op, which re-reads the source file from the deferred path on the host side).

### Web UI
SvelteKit (`web/`, `adapter-static`, base `/istota`) + FastAPI (`web_app.py`). Nextcloud-hosted OAuth2 (the legacy generic OIDC fallback was retired — Docker provisions the OAuth2 client via `provision-nc.sh`; Ansible templates `[web.oauth2]` directly), 7-day session. Routes: dashboard, feeds (reader + sprocket-icon settings page served by `istota.feeds.routes` against per-user SQLite), location (today + history with cluster discovery; `/location/settings` for Overland ingest token), money (`/istota/money/*`), health (see "Health" above), admin (read-only system health at `/istota/admin`, gated by a new `_user_is_web_admin` helper that uses the `/etc/istota/admins` allowlist and fails closed on empty allowlist — distinct from `Config.is_admin`, which retains its back-compat "empty = all admin" rule for sandbox/skill/command checks). Single-payload `GET /istota/api/admin/stats` aggregator; all timestamps normalized to canonical ISO 8601 UTC via `_iso_utc()`. Dev: `VITE_MOCK_API=1 npm run dev` for in-process mock backend. Frontend primitives in `web/src/lib/components/ui/` (AppShell, ShellHeader, Sidebar, Chip, Button, Select, Modal, etc.); shared settings primitives in `web/src/lib/components/settings/` (`SecretField`, `ServiceCard`).

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
