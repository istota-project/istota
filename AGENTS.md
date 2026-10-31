# Istota - Claude Code Bot

## Load project memory before working

These instructions are shared by Claude Code and Codex. The injected copy can be truncated: read this file from disk in bounded sections through the end before starting work. For the affected subsystem, also read the corresponding `.claude/rules/*.md` files listed below; their `paths` frontmatter identifies where they apply. Do not assume Codex has loaded those files automatically.

Read `.claude/project-memory/MEMORY.md` at the start of each task, then open the notes relevant to the request, including feedback. This gitignored directory is a local link to Claude's existing project memory, so both agents read the same notes. In a worktree, resolve the primary checkout with `git rev-parse --git-common-dir` and look for the link under that checkout. If the link is absent, look under `~/.claude/projects/` for the memory directory matching this project's primary checkout, not a sibling project. If no matching memory is available, say so and continue with the repository guidance. Treat remembered implementation status as historical context: verify it against the current tree. Current user instructions and repository rules take precedence over older notes. Never copy private memory into tracked files.

### UI patterns are required reading

Before planning or changing any UI, read `web/node_modules/@minot/ui/AGENTS.md` (the design system) and `web/AGENTS.md` (this app's part of it) from disk, even when working from the repository root or changing a backend response that affects the UI. It is the shared design guide, not an optional reference. Read it in bounded sections through the end; its component tables and later layout rules can exceed the instruction-loading or tool-output limit. Also read `.claude/rules/web-ui.md`, the relevant subsystem rules, and the memory note `feedback_web_alerts_follow_chat.md` when available.

- Start with `web/node_modules/@minot/ui/dist/index.d.ts`, `web/node_modules/@minot/ui/dist/styles/tokens.css`, `web/src/lib/styles/app-tokens.css`, and the nearest sibling page. Reuse the existing primitives and page skeleton. Import UI components from `@minot/ui`. A change to one is made in minot and taken here as a release, never in this tree (`web/AGENTS.md` opens with the loop).
- Use theme tokens and the spacing/type scales. Add new color tokens to both themes. Reuse shared styles rather than redefining primitives or adding per-page theme overrides.
- Follow chat's feedback patterns: out-of-band action failures use `notifyError` / `notifyWarning`; lasting conditions use `Alert`. Field validation and failed sends stay beside their field or message. Do not invent ad-hoc error banners.
- Use `Modal`, `ConfirmDialog`, and `EmptyState` for their existing roles. Use `copyText` for clipboard actions and the platform facade for native-shell behavior.
- Keep desktop and mobile behavior consistent with sibling pages. Run the design lint and the frontend checks specified in `web/AGENTS.md` for UI changes.

Claude Code-powered assistant bot with Nextcloud Talk interface.

**Production server**: `your-server` (SSH, installed at `/srv/app/istota`).

This file is loaded into every session, so it stays an index: what exists, where it lives, and the rules that decide a change before it is written. The reasoning behind each rule lives in `.claude/rules/`, loaded on demand.

Subsystems:

- `brain.md` — Brain protocol + ClaudeCodeBrain + NativeBrain (in-process agent loop), plus per-room brain selection
- `executor.md` — `execute_task()`, env mapping, prompt assembly, security
- `prompts.md` — the two halves of a task prompt, and the control directory that hands them over
- `scheduler.md` — daemon loop, worker pool, DB tables, deferred ops
- `config.md` — the config rules that are not obvious from the dataclasses, and the TOML mapping
- `skills.md` — skill metadata, single-axis selection (eager vs menu), per-skill user overlays, CLI modules
- `transport.md` — Transport seam over messaging surfaces (Talk, email, web chat, SMS and WhatsApp ship; Matrix designed-for), plus the room model, phone rooms (SMS and private WhatsApp: mint, transcript writes, backfill) and multiplayer rooms (speech gate, participants, host and guests, audience, epochs, shared-room delivery refusal)
- `sms.md` — provider-neutral SMS surface, Twilio and Telnyx adapters, delivery states, switching, and the user's private SMS room
- `whatsapp.md` — WhatsApp surface behind a provider seam: the common half (adapter-native identity, the one-send ledger, parked status, opt-out, the private chat's room), Meta's Cloud API adapter (signed webhook, 24-hour window, templates, cost controls) and the Baileys adapter (paired session, Node sidecar, QR pairing)
- `sharing.md` — open-protocol sharing: ICS feeds, Istota's own calendar (invitations or a feed), Open Cloud Mesh folder shares (receiving, accept and decline, remote notifications), where each secret lives, the Files page's shared folders and the known gaps
- `relay.md` — relay questions between users: destinations (room, WhatsApp, SMS), the recipient preference, clean-turn approval, per-surface answers and returns
- `web-chat.md` — web chat surface: rooms, composer, drafts, send durability, message replies, room-event stream, read-only phone rooms
- `web-ui.md` — web UI backend: route/endpoint map, admin Logs + Configuration panes, settings/module-services split
- `search.md` — unified web search: source scope, query matching, deadlines, result links and the search dialog
- `notifications.md` — the notifications table, the resolver seam, and the shipped sources
- `push.md` — native APNs registration, delayed chat alerts, inbox dispatch and app badges
- `briefings.md` — personal/group briefings, source grants, common editions and delivery, All users/default/publisher migration, shared blocks and HTML email
- `health.md` — health module schema, documents store, OCR/explainer, serialisers, surfaces
- `location.md` — GPS pings, place detection, visits, Overland/Garmin ingest
- `feeds.md` — native RSS/Atom/Tumblr/Are.na poller, per-user SQLite, image dedupe
- `wallet.md` — payment cards, purchase approval, private checkout fills and static-card limits
- `money.md` — quarterly tax estimator, portfolio snapshots, classifications
- `memory.md` — USER.md/CHANNEL.md/GROUP.md, per-skill overlays, knowledge graph, playbooks, sleep cycle
- `assignments.md` — goals carried across days: the assignment tables, the `assignment_id` columns on `tasks` and `task_usage`, the store, the charter, lifecycle, caller and ledger, wake creation and the assignment lane, pickup (scratchpad snapshots and the wake prompt), the `assignment` skill, `[assignments]`

Boundaries and operations:

- `sandbox.md` — the security posture in full, plus the modules that build and enforce a task's runtime: `task_env`, the tool server, cgroups, the credential splits, path scoping, git hardening
- `devbox.md` — the development container: what it is, the exec transport, the proxy
- `maintenance.md` — backups, migrators, the worktree and cache sweepers, host pressure, session transcripts
- `doctor.md` — the runtime self-check registry, and what each check may and may not do
- `install.md` — the container's `istota setup`: the terminal wizard, the shared validator and the web setup server
- `deployment.md` — the Docker stack, the dedicated VM (`host/`, `istota-stack`, signed-tag updates, the schema stamp), the optional management role in `ansible/`, the Nextcloud rclone mount
- `testbed.md` — `testbed/`: the two compose shapes, profiles, the `Service` protocol, session-scoped reset, the prompt goldens
- `testing.md` — why each verification rule below is what it is
- `committing.md` — the two pre-commit scans and how they fail
- `releases.md` — cutting a release, and the announcement that opens one
- `leaf-modules.md` — single-purpose modules that belong nowhere else

## Project Structure

```
src/istota/
├── brain/                # Pluggable model invocation (Brain protocol)
│   ├── _cli_config_dir.py  # A fresh per-run `CLAUDE_CONFIG_DIR`: the O_EXCL writer, the onboarding seed, the credentials link, `make_cli_config_dir` → brain.md
│   ├── _tool_harness.py  # The shared tool-server start (native and CLI) and the CLI tool harness: the launcher config, hello and gate files, the pid reader with its descent check, the watcher; `tool_harness`, `HarnessHandle`, `set_cli_root`, `release_calls` → brain.md
│   └── _web_fetch_mcp.py # The harness's second MCP server, `istota_web`: the daemon-side WebFetch for the CLI, unwrapped, env cleared, its policy from a 0600 file → brain.md
├── memory/               # search.py, knowledge_graph.py, sleep_cycle.py, curation/
├── assignments/          # Goals Istota carries across days: store.py (row access), charter.py, validators.py, lifecycle.py (the one state writer), caller.py (user turn, wake or neither), budget.py (the ledger and ceilings), wakes.py (due wakes, the wake task, pickup, orphan reaping), scratchpad.py (snapshots, user edits, repair), prompt.py (the wake prompt) → assignments.md
├── skills/               # Self-contained skills (skill.md + optional CLI)
│   └── _group_access.py  # The one gate for `kv --group` and `memory --group`: current member and in the task's resolved group set → memory.md
├── session/              # The native brain's application layer: compaction, results, transcripts
│   ├── session_log.py       # Append-only JSONL transcript of one NativeBrain task attempt, and its sweep → maintenance.md
│   ├── session_log_read.py  # Reading a transcript back: one set of parsing rules, two consumers → maintenance.md
│   └── tools/dispatch.py # `ToolDispatcher`: the per-call policy for MCP tool calls (subset, argument preparation, readers-writer lock, text cap); no brain or daemon imports → brain.md
├── cli.py                # Local CLI (task, resource, briefing, secret, user, run, serve, setup, …)
├── serve.py              # Combined local launcher (`istota serve`): scheduler thread + uvicorn in one process
├── setup_wizard.py       # First-run setup of the stack (`istota setup`, inside the image) → install.md
├── config.py             # TOML loader + DB-overlay (user_profiles / user_resources / briefing_configs)
├── config_mapper.py      # Maps a parsed TOML document onto the `Config` dataclass tree → leaf-modules.md
├── apply.py              # `istota apply -f plan`: config.toml and the settings plan's other sections, exit 0/2/1 → config.md
├── settings_plan/        # The application-settings plan behind `istota apply` and `istota snapshot`: schema, engine (ordering, preconditions, result), report, one adapter per store (profiles, resources, briefings, shared_briefings, files, secrets), paths, snapshot, and role_input (the management role's variables → plan and sidecar) → config.md
├── config_editor/        # The web config editor's core: reference (example docs, credential files, setup paths), secrets (one secret-path predicate, sentinels), edits (a change set previewed: ops or raw, credentials, admins, the lockout checks, the redacted document diff `istota apply` prints too), schema (read-only and risky keys, table paths, editor kinds, `MODEL_FIELDS`), check (`istota apply --check` in a child process), models (the `model` / `model_unknown` problems that check reports), requests (the `config_change_requests` table, credentials sealed until the host takes a row), handoff (the files `istota config-request take` writes for the host applier), applier (whether the host applier is there, the trigger file, `managed.json` read fail-closed) → config.md, web-ui.md
├── context.py            # Hybrid conversation context selection
├── db.py                 # SQLite operations (framework tables)
├── executor.py           # Per-task orchestration (memory/skills/sandbox)
├── executor_stream.py    # `TaskStreamAdapter`: the brain's `StreamEvent`s adapted to `TaskEvent`s → leaf-modules.md
├── events.py             # Task event streaming: TaskEvent, EventWriter, EventSubscriber + task_events log
├── consumers/            # Event consumers: TalkEventSubscriber, LogChannelSubscriber, PushNotificationSubscriber
├── scheduler.py          # Task processor, briefings, all polling
├── scheduler_deferred.py # Deferred-op replay (subtasks, KG, KV, health_ops, …)
├── commands.py           # surface-agnostic !command dispatch (CommandContext + registry push/stream)
├── doctor.py             # Runtime self-check: every environmental fact istota depends on → doctor.md
├── storage.py            # Bot-managed Nextcloud storage
├── tasks_file_poller.py  # TASKS.md monitoring
├── heartbeat.py          # Health-check system
├── cron_loader.py        # CRON.md → DB sync
├── modules.py            # MODULE_NAMES (feeds, money, location, health, briefings) + EXPERIMENTAL_MODULES (empty)
├── experimental.py       # Operator feature-flag gate (`@requires_feature`, env helpers)
├── user_profiles.py      # Per-user profile store
├── user_briefings.py     # Per-user briefings store
├── shared_file_organizer.py
├── logging_setup.py
├── transport/            # Transport seam: IncomingMessage, registry, ingest, routing, talk/ email/ sms/ whatsapp/ ntfy/ istota_file/ repl/ web/
│   ├── participants.py   # Who wrote a turn: principal / guest / agent, and the one multi-human predicate → transport.md
│   ├── route_checks.py   # Save-time checks on a route a user picked: which rooms and surfaces a descriptor or a briefing's token may name; `RouteRefused` → transport.md
│   ├── whatsapp/groups.py  # A Baileys group as a room: roster, registration, addressing, D14 leave → whatsapp.md
│   ├── email/threads.py  # A multi-party email thread as a room: minting, participants, reply-all → transport.md
│   └── email/private_room.py  # The user's private email room's ref: mail between the user and the bot alone → transport.md
├── rooms/                # The multiplayer room model → transport.md
│   ├── speech_gate.py    # Whether the bot answers a turn in a multi-human room; every turn is recorded first → transport.md
│   ├── ack_reaction.py   # A friendly room's thanks answered with a reaction: the held task and settling it → transport.md
│   ├── scopes.py         # The withheld-scope set every reach seam reads, the ambient-memory rule, and a task's resolved group set → sandbox.md
│   ├── policy.py         # Per-room host, guest_reply, audience class and readers; who may change a shared room → transport.md
│   ├── veto.py           # `!<bot> off|on`, the one-time announcement, and the room-notices queue → transport.md
│   ├── private_replies.py  # What a shared room has for one member, in their own private room: resolver, delivery, linking, whispers, held posts, guest proposals, My notes → transport.md
│   ├── lookup.py         # Finding a room a person named, among their own rooms → transport.md
│   ├── surfaces.py       # What role each surface plays in the room model, in one table → leaf-modules.md
│   └── provision.py      # Default Talk rooms (general/logs/alerts) for a user → leaf-modules.md
├── prompts/              # Loaders for the system half's standing instructions
│   └── persona.py        # The operator persona at `{root}/PERSONA.md`: shipped digests, the conffile sync, the last good copy → prompts.md
├── sharing/              # Shared with Istota over open protocols: sources (the `shared_sources` store and its reach), ics (feeds and their poll), events (Istota's calendar and its feed token), itip (invitations and replies), ocm and ocm_routes (Open Cloud Mesh), feed_routes (the public calendar feed) → sharing.md
├── relay/                # Relay questions between users: relays, destinations, requests (the `whatsapp_skill_requests` table) → relay.md
├── sandbox/              # A task's runtime and its boundaries → sandbox.md
│   ├── task_env.py       # `build_task_runtime`: one task's env, the credential split, the proxies, the bind list → sandbox.md
│   ├── plan.py           # The sandbox mount plan, as data: what `build_bwrap_cmd` binds, masks and unshares
│   ├── bind_launcher.py  # Opens a task's linked resources by descriptor and execs bwrap holding them, so a swapped name cannot change what is bound; imports only `confined` → sandbox.md
│   ├── confined.py       # Opening a file in a task-writable tree by fd from a daemon-composed root, never by name; the guard in `tests/test_confined_file_access.py` → sandbox.md
│   ├── claude_runtime_env.py  # What a task env carries only because the outer process is the `claude` CLI → sandbox.md
│   ├── confined_command.py  # A CRON `command:` job or HEARTBEAT `shell-command` check, run in the task's sandbox with its proxies → sandbox.md
│   ├── tool_server.py    # Every brain's tool server, one process per task attempt, inside the sandbox: `--fd` for native, `--mcp --hello` for the CLI → sandbox.md
│   ├── tool_server_protocol.py  # The wire format between the two, stdlib-only → sandbox.md
│   ├── tool_launcher.py  # What the CLI runs as its MCP server: clears env, reports its pid down a FIFO, execs the bwrapped tool server; stdlib-only → sandbox.md
│   ├── mcp_stdio.py      # MCP over stdio for the `claude` CLI: `serve_stdio`, the method table, version negotiation, content mapping; stdlib-only → brain.md
│   ├── skill_proxy.py    # Unix-socket proxy for credential isolation
│   ├── network_proxy.py  # CONNECT proxy for network isolation
│   ├── kv_namespaces.py  # Which `istota_kv` namespaces the model may not touch → sandbox.md
│   ├── git_hardening.py  # The `-c` overrides that stop a repository's own config running a program → sandbox.md
│   ├── git_remote_scrub.py  # Strips credentials out of the git configs under `developer.repos_dir` → sandbox.md
│   ├── user_scope.py     # Scoping a user id under a root, in one place → sandbox.md
│   ├── attachment_source.py  # Where the daemon may read an inbound attachment from, opened with no link followed → sandbox.md
│   ├── host_paths.py     # Host-path allowlist for the skill CLIs that take one → sandbox.md
│   ├── cgroup.py         # A cgroup v2 group per task: memory.max, pids.max, cpu.max → sandbox.md
│   ├── shell_exec.py     # How a command string becomes a shell argv, with `pipefail` on → sandbox.md
│   ├── process_group.py  # `kill_process_group(pid, sig)`: signal a subprocess and its descendants; `signal_task_worker` only in the scheduler's process → sandbox.md
│   ├── peer_process.py   # Who connected to a Unix socket, and whether they descend from a task's root → sandbox.md
│   ├── forge_cli.py      # The `gh` / `glab` wrapper: deny policy + server-side token injection → sandbox.md
│   ├── guarded_http.py   # HTTP to a host somebody else chose: a request pinned to a checked address, and a guarded send with per-hop redirect checks → sandbox.md
│   └── net_guard.py      # Whether an address is public: the SSRF rule shared by WebFetch and the wordpress skill → leaf-modules.md
├── credentials/          # Secrets and the credential vault
│   ├── store.py          # Encrypted credential store (Fernet via scrypt-derived key)
│   ├── names.py          # Credential names, password generation and isolation
│   ├── kdbx_import.py, kdbx_export.py  # Selected import and fresh in-memory export
│   ├── vault_retire.py   # One-time legacy file import; remove after the next release
│   ├── generated.py      # Credentials Istota generates: stored in the table, exported on request, retired only by the user → sandbox.md
│   ├── schema.py         # Shared service/key schema for `istota secret` CLI + web UI
│   ├── google_scopes.py  # The Google service ↔ OAuth scope table, bounded by the operator's configured ceiling
│   └── broker/           # Credential grants, the broker CA and the intercepting proxy → sandbox.md
├── devbox/               # The development container's daemon side → devbox.md
│   ├── proxy.py          # One credential listener per devbox user, identified by its socket: git credentials and the forge token
│   ├── compose_file.py   # `istota devbox compose-file`: the stack's per-user devbox services and socket volumes → devbox.md
│   ├── proxy_protocol.py # Wire protocol for the devbox proxy (single-line JSON, 16 MiB cap) → devbox.md
│   ├── exec_protocol.py  # The exec transport's wire format → devbox.md
│   └── exec_client.py    # The other end, copied into each task's shim directory → devbox.md
├── workspace/            # Places, task reach, safe listing/writes and personal pins → web-ui.md
├── search/               # Federated web search: core, provider registry, framework sources and links → search.md
├── notifications/        # Delivery, the inbox behind the bell, and its sources → notifications.md
│   ├── delivery.py       # Talk / Email / ntfy dispatcher, distinct from the inbox below
│   ├── store.py          # The `notifications` table: the durable open set behind the bell → notifications.md
│   ├── sources.py        # The resolver seam: rows, views, actions, the registry → notifications.md
│   ├── resolvers/        # One module per source: id, dedup key, producer helpers, resolver → notifications.md
│   └── ntfy_headers.py   # RFC 2047 encoding for ntfy header values (stdlib-only leaf, shared by transport + skill)
├── usage/                # Token usage: telemetry, the cost render rule, the plan's windows → leaf-modules.md
│   ├── telemetry.py      # Normalized per-attempt token/cost telemetry → leaf-modules.md
│   ├── render.py         # The cost render rule for token-usage surfaces → leaf-modules.md
│   └── subscription.py   # The Claude Code plan's rate-limit windows: one fetch, one disk cache → leaf-modules.md
├── webui/                # The FastAPI web UI backend → web-ui.md
│   ├── app.py            # Authenticated web UI (Nextcloud OAuth2 + admin dashboard)
│   ├── auth.py           # Native email identities, scrypt passwords, one-use links and session epochs
│   ├── webhook_receiver.py  # FastAPI: Overland GPS, etc.
│   ├── garmin_routes.py  # Module-agnostic Garmin auth router (/api/garmin/*), shared by Health + Location
│   ├── setup_app.py      # The first-run setup server (`istota setup --web`): runs with no config, one-use setup code, in-memory draft → install.md
│   ├── setup_checks.py   # Its connection checks (model, Nextcloud, email, calendar, address) against the config setup would write, plain messages only → install.md
│   ├── request_guards.py # The origin check and the bounded body reader, shared by `app.py` and `setup_app.py`
│   ├── shutdown.py       # Whether the web process is stopping, where the three SSE generators can see it → leaf-modules.md
│   ├── router_stubs.py   # The auth/CSRF stubs and the user-context factory every module router shares → leaf-modules.md
│   ├── map_basemap.py    # Where the map's background tiles come from → leaf-modules.md
│   ├── admin_logs.py     # Read-only log sources for the admin UI: the rotating app log (+ rotation chain, paged reader, live tail) and `task_logs`
│   ├── admin_config_view.py  # Redacted, sectioned rendering of the loaded Config for the admin UI (credentials never leave the process)
│   ├── chat_files.py     # Which paths `/chat/files` will serve, and the stored-answer check that rewrites links it would refuse
│   └── nextcloud_sharing.py  # The user's calendars and address books, and their DAV shares to the bot, over the user's own token through `nextcloud_files.dav_request` → web-ui.md
├── maintenance/          # Backups, migrators, sweepers, host instrumentation → maintenance.md
│   ├── db_health.py      # `PRAGMA quick_check` + self-healing `REINDEX` backstop for local SQLite DBs → maintenance.md
│   ├── db_relocate.py    # One-time migrator: per-user module DBs from the mount → local disk, DELETE→WAL
│   ├── db_backup.py      # Timed online-backup snapshot of local DBs to dated dirs on the mount; retention, row-count collapse guard, 0700/0600 → maintenance.md
│   ├── db_restore.py     # Restore a cold snapshot back to local disk (newest good, or `--date`); refuses an empty snapshot without `--force` → maintenance.md
│   ├── workspace_restore.py  # `istota migrate restore-workspace`: master key (stdin), config copy and databases from the workspace's backups onto a new stack → maintenance.md
│   ├── standalone_import.py  # `istota migrate import-standalone`: a standalone install (staged by the Mac, istota.env on stdin) onto the local VM's stack → maintenance.md
│   ├── repos_relocate.py # One-shot migrator: `developer.repos_dir` → per-user subtrees
│   ├── workspace_paths.py # A migrated install's old host paths in users' bot directories, rewritten with backups → maintenance.md
│   ├── workspace_localize.py # Moving a mounted workspace to local disk: the copy, the config edit, the index cleanup → maintenance.md
│   ├── room_relocate.py, room_mount_reconcile.py  # The room-identity migration and its mount sweep
│   ├── persona_retire.py # Deletes or retires the old per-user PERSONA.md copies, from `istota init` → maintenance.md
│   ├── worktree_reaper.py   # Removes a developer worktree once its work has landed → maintenance.md
│   ├── talk_media.py     # Retention for Talk attachments fetched into `Channels/<token>/media/` on a local workspace → maintenance.md
│   ├── sandbox_cache_sweeper.py  # Bounds the on-disk package caches the sandbox keeps per user → maintenance.md
│   └── host_pressure.py  # Host memory instrumentation: PSI/meminfo/tmpfs, shmem attribution → maintenance.md
├── nextcloud/            # OCS + WebDAV client: _http (ocs_request/dav_request/OcsError/path scoping), capabilities, shares, users, dav, notifications, talk_files (a local file copied into a Talk conversation's folder for a share)
│   ├── talk.py, ocs.py   # The Talk client and the OCS envelope leaf
│   ├── user_metadata.py  # NC user metadata
│   └── compat.py         # The None-returning variants four best-effort daemon paths depend on
├── mail/                 # Email plumbing that is not a transport: support (get_email_config, thread helpers, cleanup), ownership, outbound_policy, drafts, sender_flags (the per-sender record of suspected injection attempts and the hold it causes), accounts (the mail accounts users connect: registry, passwords, `resolve_for_daemon`), connect (the one place an IMAP or SMTP connection is built: verified TLS, `imap_security`/`smtp_security`, resolve-check-connect for a typed host, `classify_error`), html_template (the document and inline styles of every HTML mail Istota composes itself: briefings and auth mail)
├── browser/              # Admission to the single-threaded browser API, and browser profile identity
│   └── document_render.py  # A web render bundle (`web/src/render/`) sent to the browser's `/render-document`, an image back; the page never calls istota → leaf-modules.md
├── lib/                  # Dependency-free leaves; nothing here imports from `istota` → leaf-modules.md
│   ├── sqlite_util.py    # One SQLite open, each caller's pragma set as parameters; no journal_mode, deliberately → leaf-modules.md
│   ├── du.py             # Du-style tree measurement and the first-level directory scan → leaf-modules.md
│   ├── image_sniff.py    # Which bytes `/chat/files` will serve `inline` on the app's own origin → leaf-modules.md
│   ├── audio_sniff.py    # Which bytes are audio the transcription pipeline decodes, and `AUDIO_EXTENSIONS` → leaf-modules.md
│   ├── video_sniff.py    # Which bytes are an MP4 the GIF frame extractor opens → leaf-modules.md
│   ├── gif_frames.py     # The child that tiles a GIF-as-MP4's frames into one JPEG; stdlib, PyAV and Pillow → leaf-modules.md
│   ├── untrusted.py      # One fence around content somebody else wrote, markers redacted from the content → leaf-modules.md
│   ├── toml_fence.py     # Where a TOML fence starts and ends, for the four markdown-config parsers → leaf-modules.md
│   ├── toml_write.py     # The tree's one TOML writer: the wizard, `istota apply` and the testbed → leaf-modules.md
│   ├── rclone_conf.py    # The VM's rclone remote for a Nextcloud account, and rclone's `obscure` without rclone → leaf-modules.md
│   ├── llm_json.py       # The same, for a fence in *model* output; anchored closer, linear walk → leaf-modules.md
│   ├── markdown_plain.py # Markdown flattened to the words a reader sees, for the iOS chat push preview → leaf-modules.md
│   ├── markdown_headings.py # Headings in somebody else's text pushed below the section it sits under: skill overlays, the assignment charter → leaf-modules.md
│   ├── text_match.py     # Literal query terms, FTS/LIKE matching and plain snippets → search.md
│   ├── date_parse.py     # Loose date parsing for text a model or a person typed, validated → leaf-modules.md
│   ├── duration_parse.py # `30m` / `6h` / `2d` as a timedelta, units chosen by the caller, digits bounded: tasks, location, email, assignments → leaf-modules.md
│   ├── filenames.py      # The one rule for turning a name somebody else chose into a filename → leaf-modules.md
│   ├── unreachable.py    # Why a host could not be reached, as a fixed message, and whether a request read-timed out: setup's checks and doctor → leaf-modules.md
│   ├── mountinfo.py      # Which mount covers a path: the entrypoint's rclone preflight and the cgroup root check → leaf-modules.md
│   ├── workspace_marker.py  # The `.istota-workspace` a host folder's workspace carries, and whether the host requires it → leaf-modules.md
│   ├── dav_xml.py        # Hardened DAV multistatus parsing, principal hrefs and path segments, for CardDAV and the web sharing module → leaf-modules.md
│   ├── url_redact.py     # A configured URL safe to print, userinfo replaced or removed: doctor, the admin cards and the servarr skill → leaf-modules.md
│   ├── fixed_get.py      # One credentialed GET to a fixed URL, no redirect, no proxy: the usage poll and Anthropic's model list → leaf-modules.md
│   ├── rate_limit.py     # A sliding-window hit counter per key, in bounded memory, for public routes with no table behind them → leaf-modules.md
│   ├── browser_console.py  # A user's browser console socket name, shared by the browser pool and the web relay; vendored into the browser image → leaf-modules.md
│   ├── egress_log.py     # `egress-refused.jsonl`: what a task was refused, on its way from a seam with no database to the table → leaf-modules.md
│   └── unix_connect.py   # Connecting to a Unix socket without following a symlink at its leaf → leaf-modules.md
├── briefings/            # Personal/group briefings — source grants, generation, edition/delivery ledger, reader/settings routes (group operations in `group_service.py`, HTTP-free, so a skill CLI can call them) and migration → briefings.md
├── feeds/                # Native RSS/Atom/Tumblr/Are.na — poller, SQLite, routes, OPML, image_dedupe
├── health/               # Body stats, bloodwork, biomarker trends, encounters, immunizations, Garmin, OCR
└── location/             # Per-user location.db module (pings, places, visits, state, migration)
    └── logic.py          # Place stats / cluster discovery (shared web ⇄ skill)
```

The root holds only what `tests/test_package_layout.py`'s `ROOT_ALLOWLIST` names. A new module goes in the package for its subsystem; adding one to the root is a deliberate one-line change to that list. Packages created for the layout keep an empty `__init__.py`, and `lib/` imports nothing from `istota`.

Alongside `src/`: `host/` (the dedicated VM: `provision.sh`, `istota-stack`, the systemd units, the Lima template and cloud-init; `.claude/rules/deployment.md`), `config/` (config.toml, persona.md, emissaries.md, system-prompt.md, guidelines/ — read by the daemon, never bound into the sandbox; skill bodies live in `src/istota/skills/`), `ansible/` (the optional management role for an estate-managed VM: writes `host.env`, the secrets and an `istota apply` plan, then calls `provision.sh` and `istota-stack`; `tests/test_management_role.py` holds its names to the code), `docker/` (full-stack compose), `web/` (SvelteKit, adapter-static, base `/istota`), `docs/` (the documentation markdown, referenced by path from Ansible, compose, the rules files and source docstrings — the files stay here), `docs-site/` (the Docusaurus site that renders it; `.md` is CommonMark, and a new page is listed in its `sidebars.ts`), `tests/`, `testbed/` (the deployment tiers' staging environment; its own `pyproject.toml`, never imported by `src/istota/` — see `.claude/rules/testbed.md`), `schema.sql`.

## Key Concepts

### Identity

- Technical IDs (package, env vars, DB, CLI): always `istota`.
- User-facing identity: `bot_name` config (default "Istota"). `bot_dir_name` sanitizes for filesystem use.
- Templated docs use `{BOT_NAME}`, `{BOT_DIR}`, `{user_id}` placeholders.

### Prompt Layers

1. **Emissaries** (`config/emissaries.md`) — constitutional principles, global only.
2. **Persona** (the operator's `PERSONA.md` at the file root, synced from `config/persona.md` unless edited) — character, one for every user and every room. Per-user preferences go in `USER.md`.
3. **Custom system prompt** (`config/system-prompt.md`, opt-in) — replaces CC default.

**A task prompt is two halves, split by authority** (`executor.ComposedPrompt`). Standing instructions — identity, execution constraints, emissaries, persona, workspace layout, tool descriptions, rules, response guidelines, skills changelog, eager skill bodies — are the `system` half. Retrieved memory, knowledge facts, playbooks, conversation and confirmation history, the request and its attachments are the `user` half, which is what a compaction summary carries forward. The handoff is a file in `{temp_dir}/.control/{user_id}/task_{id}/`, a directory no task can write and only this task can read. Two rules are held by tests: no line in the system half may point at material in the user half, and every scalar interpolated into a system header goes through `_one_line()`. Full rules in `.claude/rules/prompts.md`.

### Admin / Non-Admin Isolation

Admin user IDs in the admins file, `ISTOTA_ADMINS_FILE` (`/data/config/admins` on the stack, `/etc/istota/admins` when unset; empty = all admin). Non-admins: scoped mount, no DB write, no subtasks, `admin_only` skills filtered.

### Shared rooms

A room may hold several humans: a Talk group, a web room with more than one member, a WhatsApp group (Baileys), an email thread with two or more humans besides the bot. **Every turn is recorded first, then the speech gate decides whether to answer** (`[speech_gate] mode = "classifier"` by default, so a cheap model decides whether a group turn is for the bot; an addressed turn is always answered). Talk records guests, non-istota users and other bots as participants; the `config.users` test decides authority, not recording. **A turn runs with its sender's reach** (ISSUE-576): a member's turn runs as that member at full reach, guest present or not, minus the ambient memory (`USER.md`, recall, knowledge facts, playbooks) no shared room loads into the prompt; a guest's turn runs as the room's host with every scope withheld, and so does a task nobody asked in the room (a subtask or CLI task whose conversation is shared, or a cron job or briefing that is not the user's own in a room they are a member of; ISSUE-594). Mail somebody else wrote is a correspondent's turn (`rooms.scopes.turn_author`, on an email thread room or roomless): it runs as the host, withholding all but a short allowlist that `user_profiles.correspondent_scopes` widens per user, with `USER.md` and overlays loaded and no recall. The host's own mail on a thread runs at full reach. Other participants' turns in history are fenced as untrusted. There are no general per-room scope grants. Explicit location sharing grants only a projected current observation after private approval; see `.claude/rules/location.md`. A shared room is never a delivery destination for unsolicited personal content. Full rules in `.claude/rules/transport.md` ("Multiplayer rooms"), the binds in `.claude/rules/sandbox.md`.

### Nextcloud Layout

```
/Users/{user_id}/{bot_name}/{config,exports,scripts,examples}/
/Users/{user_id}/{inbox,memories,shared}/
/Channels/{conversation_token}/{CHANNEL.md,memories/}
/Groups/{group_id}/{GROUP.md,memories/}
```

### Scheduled Jobs (CRON.md)

Markdown with TOML `[[jobs]]`. Types: `prompt`, `prompt_file`, `command`. Per-job `model`/`effort` overrides, and a per-job `brain` — admin-only, dropped at sync for anyone else, bounded by `[brain] room_selectable`. Auto-disable after 5 consecutive failures — recorded in `scheduled_jobs.auto_disabled_at`, which the file cannot express and the sync never writes, so CRON.md saying `enabled = true` does not bring a suspended job back. Three things lift it: a successful run, `!cron enable`, and an edit in CRON.md to what the job dispatches (`cron`, `prompt`, `command`, `skill`, `skill_args`). `skip_log_channel`, `silent_unless_action`, `once = true` supported.

### Heartbeat

`HEARTBEAT.md` — `file-watch`, `shell-command`, `url-health`, `calendar-conflicts`, `task-deadline`, `self-check`. Cooldown + quiet hours.

### Which brain a task runs

`tasks.brain` > `[brain.source_type_overrides][source_type]` > `[brain] kind`, resolved by `brain.resolve_brain_kind`. The column has two producers: `rooms.brain` at task creation (Talk, web and the SMS and WhatsApp phone rooms) and `scheduled_jobs.brain` from a CRON.md job. The column is what makes a room or a file edited mid-flight change nothing already running; retries and subtasks inherit it.

A room or a job may only pin a kind the operator listed in `[brain] room_selectable`, which is empty by default, and only an admin may write it — brain kind decides which process holds the agent loop and which credentials it carries. An unknown or unlisted pin is a WARNING and a fallthrough, never a failed task. A pinned room or job has **no availability failover**: `resolve_brain_kind` clears `fallback` on the admission path, so a turn the pinned brain cannot run fails with that brain's own reason. Set from chat with `!brain`, from the web room settings, or per job with `brain` in CRON.md. Full rules in `.claude/rules/brain.md`.

### Security

Full posture, with the reasoning and the shape-by-shape caveats, in `.claude/rules/sandbox.md`. The operative rules:

- **Sandbox** (`bwrap`): per-user filesystem isolation. Linux + bubblewrap is the only supported deployment. The `daemon` service carries the run contract in `docker/docker-compose.yml`: the shipped seccomp and AppArmor profiles (`docker/istota/seccomp-istota.json`, `apparmor-istota`) plus `systempaths=unconfined`, `cap_drop: ALL` with six root-phase capabilities, `read_only`, `cgroup: private`. The root phase (`docker/istota/root-phase.sh`) delegates the container's cgroup and drops to uid 10001 through `istota-drop`, which every exec path uses too. On Linux a daemon whose sandbox cannot be built refuses to start.
- **Every brain's tools are in the sandbox**, one namespace per task attempt: `istota.sandbox.tool_server` through `build_bwrap_cmd(..., profile=NATIVE)`, and the core tools (Read, Write, Edit, Grep, Glob, Bash, BashOutput, KillShell) run in there. `NativeBrain` spawns it over an inherited socketpair; on `claude_code` the `claude` CLI runs outside the sandbox on the tool harness (`brain/_tool_harness.py`) and reaches the same server through one MCP server, with its machine-acting built-ins denied (a second, `istota_web`, serves the daemon-side WebFetch outside the sandbox). The CLI's credential is in the CLI's env only: no task env carries it and the sandbox's allowlist names no Anthropic host. `executor.native_fs_roots` is still enforced and is the only confinement on the unsandboxed shapes, where a Bash call can read the CLI's process environment as the same uid.
- **Native web credentials**: password hashes and token digests live in the framework DB, bound into no sandbox. Email identities are separate from inbound email routing addresses.
- **No databases in the sandbox**: `build_bwrap_cmd` ends by masking `db_path.parent` and `module_db_root()` with an empty read-only tmpfs, after every other mount. Nothing binds the framework DB for anyone, and `sandbox_ro_paths` defaults to `[]`. Reads and writes go through skill CLIs that run host-side and scope by `ISTOTA_USER_ID`; the masks are defence in depth behind that.
- **Session logs are unbound** and a transcript holds the assembled prompt: `{db_path.parent}/logs/{user_id}/` is bound at no path and is in no `native_fs_roots` root. That is an absence rather than a guard, so keep `sandbox_ro_paths` narrow and add no `user_resources` row naming it. A task reads its own finished transcripts through `istota-skill tasks transcript`.
- **One admin's repositories are not in another's sandbox**: `developer.repos_dir` is a root of per-user subtrees and `build_bwrap_cmd` binds `{repos_dir}/{user_id}`, never the root. The package cache is derived inside that subtree, gated on the repos bind's own condition.
- **Config dir out of the sandbox**: emissaries, persona, guidelines and skill bodies become prompt text, so `config/` is never bound, `config/system-prompt.md` included: the native brain reads it in the daemon and the `claude` CLI, which opens it itself, runs outside the sandbox.
- **Network proxy**: `--unshare-net` + CONNECT proxy on a Unix socket; allowlist of `host:port`. TLS interception only for hosts bound to a credential in the task snapshot, by the credential broker.
- **Credential broker and reveal enforcement, on by default**: a task sends `{{cred:NAME}}` and the proxy substitutes the value for a granted, bound host; `istota-credential get`/`run`/`env` are refused for entries not marked readable. Left unset, both follow the boundary (`config._default_broker_to_boundary`: Linux, `sandbox_enabled`, `[security.network] enabled`). The first task per user runs the one-time `grant_what_exists`.
- **Skill proxy**: strips secret env vars from the model; CLI calls go through a Unix socket that injects credentials server-side. Required wherever the sandbox is — `istota-skill` refuses to run in-sandbox rather than reaching for databases that are not there.
- **Host paths in skill CLIs**: a skill CLI runs host-side with the daemon's filesystem view, so any verb taking a host path is scoped by `sandbox/host_paths.py` — never `NEXTCLOUD_MOUNT_PATH` whole. Consumers open the validated name through a no-follow descriptor walk; pathname-only consumers receive private copies.
- **Shared rooms withhold by reach, for guests, correspondents and unasked tasks**: a guest's turn, and a task nobody asked in a shared room, withhold every scope; a correspondent's turn withholds every scope but its allowlist, which never includes `files`. Then `{mount}/Users/{user_id}` is not bound, the task gets its own `emissary-task-<id>` / `room-task-<id>` temp dir and no Talk bind. A member's own turn binds what their private room does. On a shape with no bwrap this is prompt and env only, and `doctor` warns.
- **No Docker API in the sandbox**: no socket is bound at any path and no `DOCKER_HOST` is exported. Project builds go to the user's devbox over the exec transport (`.claude/rules/devbox.md`).
- **Native WebFetch tool**: daemon-netns, credential-free, SSRF-hardened, and **available to every user**. What bounds it is `[brain.native.web_fetch]`'s own egress policy, which binds every caller alike; `admin_only = true` restores the identity gate it shipped with.
- **Deferred DB**: sandboxed tasks write JSON to the temp dir; the scheduler processes it after success. Identity (`user_id`, `conversation_token`) always from the task, never the JSON. Subtasks rate-limited and admin-only.

## Code Style

Indentation is **spaces, never tabs**, declared in `.editorconfig` at the repo root: Python is 4 spaces, everything under `web/` is 2 spaces. The frontend is formatted by prettier — run `npm run format` in `web/` before committing frontend changes (config in `web/.prettierrc.json`). Exceptions: `web/package-lock.json` (npm-generated) and `docker/devbox/etc/gitconfig` (git-idiomatic tabs).

The web UI's design system is the `@minot/ui` package (its own repository, minot), pinned to an exact version in `web/package.json`; its design-language doc is `web/node_modules/@minot/ui/AGENTS.md`.

Python is **linted but not formatted**. `ruff check` runs clean over the seven paths the command below names; the rule set is pinned in `[tool.ruff.lint]` to ruff's defaults (`E4`, `E7`, `E9`, `F`) with no formatting-adjacent rules. **Do not run `ruff format`**: it is not adopted, the hand formatting in the tree is the baseline, and a reformat would rewrite roughly 715 of 910 files and carry `git blame` with it. A deliberate unused import is marked `# noqa: F401` with the reason, not left to be pruned by the next `--fix` run.

## Verification

There is no single entry point. Run the checks directly, and run only the half the change touches — Python and `web/` are independent. Why each rule below is what it is: `.claude/rules/testing.md`, and `docs/development/testing.md` for the developer-facing version.

**The closing run in this repo is scoped, not the full suite.** A change, an issue fix, a `/job`, a sweep unit and a spec round all close on the named test files that cover what changed, plus `ruff`, plus the `web/` checks scoped to the touched route when `web/` changed. Name the files; report the close as scoped. **Run the full suite only when the user asks for it in this session.** This outranks every skill or global instruction that says "full pass": a full run is about eight minutes here, and a gate that asks for one is satisfied by the scoped close.

Python:

```bash
ruff check --output-format concise src tests testbed docker/browser docker/devbox docker/istota scripts
uv run pytest tests/test_x.py tests/test_y.py -q --no-header   # the closing run: the files that cover the change
scripts/qt                       # the edit loop, Python-only changes in a warm checkout only (see below)
scripts/qtest uv run pytest      # the full suite, only when asked; deselects every marker below
```

Web, from the repo root (needs `npm ci` in `web/` first):

```bash
npm --prefix web run lint:design
npm --prefix web run check       # svelte-check
npm --prefix web run test -- --run src/routes/<area>/   # the closing run: the touched route
scripts/qtest npm --prefix web run test    # the whole vitest run, only when asked
npm --prefix web run format:check
```

- **Install with `uv sync --extra test`.** A bare `uv sync` leaves out everything the suite needs and yields hundreds of collection errors that read as a code regression. `--all-extras` also works and costs about 1.1 GB against the test extra's 291 MB; use it when you want the two heavy ML extras. Test-only dependencies belong in the `dev` group, never in an extra.
- **Use `scripts/qt` while iterating, not a hand-picked subset.** Dependence runs through call chains, not through text: a name-matched test file and a grep of `tests/` both under- and over-select, measured. `qt` wraps pytest-testmon, which records which tests executed which source lines. **It silently becomes the full suite** for any non-Python change (a Svelte file, a shell script, `schema.sql`, Ansible, a fixture) and in a fresh worktree with no `.testmondata`, so check the diff first and name the files instead in those cases. Do not invoke `--testmon` by hand — this repo's `addopts` carries a `-m` expression and testmon switches its selection off entirely when it sees one, so the run looks selective and is not.
- **A test that asserts against source text reads it with `tests/support/drift.py`'s `source_of`, not `inspect.getsource`.** testmon selects a test from the lines it executed, and a drift guard reads lines rather than running them, so every one of them was invisible to `qt` — green because it never ran, red only when invoked by name (ISSUE-459). `source_of` records the range it read and `tests/conftest.py` hands it to testmon; nothing else in the guard changes. `tests/test_drift_selection.py` fails on a raw `inspect.getsource` under `tests/`. Still uncovered, and a real gap: a guard that reads a file's text off disk, such as the `rglob("*.py")` sweeps over `src/`.
- **Wrap a full suite run in `scripts/qtest`.** Both suites size their worker pool from `cpu_count()`, so concurrent runs across worktrees fail on timeouts that have nothing to do with the code. `qtest` is a machine-wide `flock` semaphore and ends every run with a verdict line on stderr (`PASS` / `FAIL` / `KILLED-SIGKILL` / `NO-SLOT`). Exit 75 means the command did not run. Serialize the expensive runs only.
- **Chain the checks in one shell invocation**, and use `-x` / `--bail=1` while iterating; drop the bail flag for the run you report. Never read a result through a pipe unless `pipefail` is on — a task under the daemon inherits it, a terminal in this repo does not.

**Nine markers are deselected by default and none runs unless you ask**: `integration`, `live`, `linux`, `image`, `smoke`, `full`, `testbed`, `vm`, `ml`. A tenth, `requires_dac`, skips itself where the process can bypass permission bits. The seven discretionary tiers:

```bash
scripts/test-linux.sh            # the suite + the linux tests, on a real kernel
uv run pytest -m image -n0       # the built image's contract
uv run pytest -m smoke -n0       # end-to-end against the lean compose stack
uv run pytest -m full -n0        # end-to-end against the full stack, incl. a real Nextcloud
uv run pytest -m testbed -n0     # wire-level email against a real IMAP/SMTP server, no istota image
scripts/test-upgrade.sh          # the current image over an older release's state
scripts/test-vm.sh               # the stack on a VM provisioned by host/provision.sh, in Lima
```

`image`, `smoke`, `full`, `testbed` and `vm` require `-n0`: their fixtures are session-scoped and build one tagged image or drive one VM. Before a release, add `-m full -n0`, `-m image -n0 --platform amd64` and `scripts/test-upgrade.sh --from-floor --shape volume`. Three tiers carry a negative control that must go red (`scripts/test-image-negative-control.sh`, `scripts/test-vm-negative-control.sh`, and the same broken image handed to the upgrade tier via `ISTOTA_IMAGE_TAG`); `scripts/test-stack-negative-control.sh` does the same for `istota-stack`'s release checks and its `setup --web` tests.

**The `vm` tier is the dedicated-VM half (the one-deployment-shape spec, Stage 7).** It boots a Lima VM from `host/lima/istota.yaml`, lets `provision.sh` set it up, deploys a signed snapshot of the checkout through `istota-stack update`, and switches the stack between the local, proxied, direct and full-Nextcloud modes with `istota-stack setup`, `apply` and `provision.sh`, as an operator would. It witnesses the parity rows only a real VM can hold (7, 9, 10, 11, 15, 18, 19, 20, and the halves of 12 and 17 Docker Desktop cannot see), and in its host-folder mode the local VM's rows L1 to L4 (`tests/vm/test_host_folder.py`: the workspace marker, the Mac loopback block, what crosses from the Mac, the Mac CLI's uid). Its appliance mode (row L3 for a release directory, and M11) is the Mac app's and runs in that repository, istota-mac, loading `tests/vm/lima.py` from the istota release it pins. It needs `limactl` rather than Docker on the host, keeps its VM (`istota-vm-1`) between sessions, and costs tens of minutes on a first run. `tests/vm/conftest.py` and `tests/vm/lima.py` say how. **On a tier asserting against an artifact, reading the test tells you almost nothing about whether it can fail** — run the control and write down what it turned red.

**All seven tiers need Docker or a VM — the Linux tier unless it runs natively, the vm tier through Lima — so a sandboxed task cannot run any of them; check `ISTOTA_SANDBOXED` before you plan around one.** No Docker socket is bound into a sandbox, and a task's own sandbox passes `--unshare-user --disable-userns`, which shuts the nested-namespace route too. **When a change touches the sandbox, the network proxy, the skill proxy, a migration or the image, say so in the merge request, name the tier that covers it, and ask for the run before merge.** Report the default suite as what it is: it patches `_bwrap_available` and checks argv, so it has never executed the sandbox path you changed.

Anything added to `testbed/` is bound by four rules, the first three enforced in the default suite: every key a service or profile writes into the stack's `config.toml` is one `load_config` reads (the config is an input on every shape, written by `istota setup` for an operator and by the testbed for a stack); a stub bound off loopback must be given a credential to expect; a negative assertion takes a watermark *and* a discriminating column; a service whose client negotiates capabilities is run for real rather than stubbed. The prompt goldens (`tests/test_prompt_golden.py`) are in the default suite and need no container — regenerate with `uv run env ISTOTA_UPDATE_GOLDEN=1 pytest tests/test_prompt_golden.py -n0` and review the diff.

## Committing

This repo is public, so `.githooks/pre-commit` scans staged content twice: `gitleaks` for credentials and `scripts/check-private-data.sh` for private data (a real name, a production hostname, a home-directory path, an account number). Enabled per clone by `git config core.hooksPath .githooks`, which `scripts/setup.sh` does. **A scan that cannot run refuses the commit when the shell is an unattended one** — a model task, an admin task with a checkout, or `PRECOMMIT_SCANS_REQUIRED=1`; a human gets a warning and the commit. That refusal is a broken install to report, not a step to work around, and `--no-verify` drops both scans. Patterns come from the committed `.private-data-patterns`, the gitignored `.private-data-local` and two terms derived at runtime. Neither scan prints the matched value. Details in `.claude/rules/committing.md` and `docs/development/secret-scanning.md`.

## Configuration

Search order: `config/config.toml` → `~/src/config/config.toml` → `~/.config/istota/config.toml` → `/etc/istota/config.toml`. Override with `-c PATH`.

Per-user data lives in DB tables (`user_profiles`, `user_resources`, `briefing_configs`, `secrets`) populated by `istota user|resource|briefing|secret ensure`. The `[users.X]` block in `config.toml` (docker entrypoint path) is also accepted; DB rows win at config-load time. The retired `config/users/{user}.toml` mechanism is gone. CalDAV derived from Nextcloud. The dataclasses in `config.py` and `config.example.toml` list every field; the non-obvious rules are in `.claude/rules/config.md`.

## Deployment

One shape, the same on a server and a Mac: the stack (compose file and images in `docker/`) on a machine dedicated to it, set up by `host/provision.sh` and updated with `istota-stack update`; the optional `ansible/` role can manage such a machine from an estate inventory, and nothing in an install uses it. On an Apple silicon Mac that machine is a Lima VM (`install.sh --local`): `host/mac/istota-stack` creates and controls it and forwards every other verb into it, `~/Istota` is the workspace (`STORAGE=host-folder`), the master key is copied into the login Keychain, and `restore` rebuilds a lost VM from the two. A bare-metal install moves over with `istota-stack import-bare-metal`, a standalone one with the Mac's `istota-stack import-standalone`. The Mac app is a separate project (istota-mac) that pins an istota release: the same VM made from a release directory it carries, driven through the bundled Mac CLI. What it relies on stays here because a release carries it (the Mac CLI's appliance mode, the update verbs, `local_code`, `istota setup --desktop-app`, `scripts/sign-images-manifest.sh`), and its contract test holds its command table to both `istota-stack` scripts' usage at the pinned tag, so a change to either script's usage is a change to that contract. See `.claude/rules/deployment.md`.

## Task Status

`pending` → `locked` → `running` → `completed` / `failed` / `pending_confirmation` / `cancelled`
