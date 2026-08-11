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
- `web-chat.md` — web chat surface: rooms, composer, drafts, send durability, message replies, room-event stream
- `web-ui.md` — web UI backend: route/endpoint map, admin Logs + Configuration panes, settings/module-services split (the design language itself lives in `web/AGENTS.md`)
- `briefings.md` — block/source briefings, shared blocks, titles, HTML email
- `health.md` — health module schema, documents store, OCR/explainer, surfaces
- `location.md` — GPS pings, place detection, visits, Overland/Garmin ingest
- `feeds.md` — native RSS/Atom/Tumblr/Are.na poller, per-user SQLite, image dedupe
- `money.md` — quarterly tax estimator, portfolio snapshots, classifications
- `memory.md` — USER.md/CHANNEL.md, knowledge graph, playbooks, sleep cycle

## Project Structure

```
src/istota/
├── brain/                # Pluggable model invocation (Brain protocol)
├── memory/               # search.py, knowledge_graph.py, sleep_cycle.py, curation/
├── skills/               # 34 self-contained skills (skill.md + optional CLI)
├── cli.py                # Local CLI (task, resource, briefing, secret, user, run, serve, setup, …)
├── serve.py              # Combined local launcher (`istota serve`): scheduler thread + uvicorn in one process
├── setup_wizard.py       # Interactive first-run installer (`istota setup`) for the local single-user shape
├── updater.py            # `istota update` — self-update for the standalone install: reads install.json provenance, git fetch/reset the recorded checkout (stable channel = latest release tag, main channel = branch tip), `uv tool install --reinstall`, fresh-code migrations. Refuses on the server shape
├── config.py             # TOML loader + DB-overlay (user_profiles / user_resources / briefing_configs)
├── context.py            # Hybrid conversation context selection
├── db.py                 # SQLite operations (framework tables)
├── db_health.py          # `PRAGMA quick_check` + self-healing `REINDEX` backstop for local SQLite DBs (module DBs no longer on the FUSE mount)
├── db_relocate.py        # One-time migrator: per-user module DBs from the mount → local disk (`Config.module_db_path`), flipping DELETE→WAL. `python -m istota.db_relocate`
├── db_backup.py          # Timed online-backup snapshot of local DBs (framework + per-user modules) to dated dirs in a backups dir on the mount (off-host durability). Retention (keep N newest), row-count collapse guard (quarantine an emptied DB as `*.suspect`), 0700/0600 perms
├── db_restore.py         # Restore a cold snapshot back to local disk (newest good, or `--date`); row-count sanity refuses an empty snapshot without `--force`. `python -m istota.db_restore [--all|--framework|--user U --module M] [--date …] [--list] [--dry-run]`
├── executor.py           # Per-task orchestration (memory/skills/sandbox)
├── events.py             # Task event streaming: TaskEvent, EventWriter, EventSubscriber + task_events log
├── consumers/            # Event consumers: TalkEventSubscriber, LogChannelSubscriber, PushNotificationSubscriber
├── scheduler.py          # Task processor, briefings, all polling
├── transport/            # Transport seam: IncomingMessage, registry, ingest, routing (delivery plan), talk/ email/ ntfy/ istota_file/ repl/ web/ (6 Transports, push + stream)
├── email_support.py      # Shared non-transport email plumbing (get_email_config, thread helpers, cleanup) used by transport + briefing/notifications/tasks-file
├── tasks_file_poller.py  # TASKS.md monitoring
├── heartbeat.py          # Health-check system
├── webhook_receiver.py   # FastAPI: Overland GPS, etc.
├── garmin_routes.py      # Module-agnostic Garmin auth router (/api/garmin/*), shared by Health + Location
├── web_app.py            # Authenticated web UI (Nextcloud OAuth2 + admin dashboard)
├── admin_logs.py         # Read-only log sources for the admin UI: the rotating app log file (+ its rotation chain, backward-scanning paged reader + live tail) and the `task_logs` table. A caller names a *source id*, never a path
├── admin_config_view.py  # Redacted, sectioned rendering of the loaded Config for the admin UI (field-level + dotted keys, so it can back an editor later; credentials never leave the process)
├── secrets_store.py      # Encrypted credential store (Fernet via scrypt-derived key)
├── secret_schema.py      # Shared service/key schema for `istota secret` CLI + web UI
├── google_scopes.py      # The Google service ↔ OAuth scope table: what a user may pick per service (none / read / read-write), bounded by the operator's configured ceiling; read by the picker, the granted-scope readback and the connect flow
├── modules.py            # MODULE_NAMES (feeds, money, location, health, briefings) + EXPERIMENTAL_MODULES (empty)
├── experimental.py       # Operator feature-flag gate (`@requires_feature`, env helpers)
├── user_profiles.py      # Per-user profile store (Phase 6)
├── user_briefings.py     # Per-user briefings store (Phase 7b)
├── notifications.py      # Talk / Email / ntfy dispatcher
├── ntfy_headers.py       # RFC 2047 encoding for ntfy header values (stdlib-only leaf, shared by transport/ntfy + skills/ntfy so the skill subprocess needn't import the transport package)
├── skill_proxy.py        # Unix-socket proxy for credential isolation
├── skill_host_paths.py   # Host-path allowlist shared by the skill CLIs that take one (devbox `cp-in`/`cp-out`, `kv set --value-file`). A skill CLI runs host-side, so a path argument is an arbitrary read/write unless scoped; the roots mirror what the sandbox binds for that caller. stdlib-only leaf, importable from a skill subprocess
├── network_proxy.py      # CONNECT proxy for network isolation
├── devbox_proxy.py       # Per-user host-side daemon: tokens stay out of the devbox container
├── devbox_proxy_protocol.py  # Wire protocol for devbox_proxy (single-line JSON, 16 MiB cap)
├── docker_proxy.py       # Per-user Docker-API allowlist proxy: bound into the sandbox at /var/run/docker.sock in place of the root-equivalent raw socket; permits only exec/cp/inspect/restart on the user's own container
├── nextcloud_api.py      # NC user metadata
├── nextcloud/            # OCS + WebDAV client: _http (ocs_request/dav_request/OcsError/path scoping), capabilities, shares, users, dav, notifications
├── nextcloud_client.py   # Back-compat shim: the None-returning variants four best-effort daemon paths depend on
├── storage.py            # Bot-managed Nextcloud storage
├── briefings/            # Block/source briefings module — DB, source resolvers, generation, reader/settings routes, migration
├── feeds/                # Native RSS/Atom/Tumblr/Are.na — poller, SQLite, routes, OPML, image_dedupe (repeat-image suppression)
├── health/               # Body stats, bloodwork, biomarker trends, encounters, immunizations, Garmin, OCR
├── location/             # Per-user location.db module (pings, places, visits, state, migration)
├── location_logic.py     # Place stats / cluster discovery (shared web ⇄ skill)
├── scheduler_deferred.py # Deferred-op replay (subtasks, KG, KV, health_ops, …)
├── shared_file_organizer.py
├── commands.py           # surface-agnostic !command dispatch (CommandContext + registry push/stream)
├── cron_loader.py        # CRON.md → DB sync
└── logging_setup.py
```

Alongside `src/`: `config/` (config.toml, persona.md, emissaries.md, system-prompt.md, guidelines/, skills/ — read by the daemon, never bound into the sandbox), `deploy/ansible/`, `docker/` (full-stack compose), `web/` (SvelteKit, adapter-static, base `/istota`), `tests/`, `schema.sql`.

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

### Nextcloud Layout

```
/Users/{user_id}/{bot_name}/{config,exports,scripts,examples}/
/Users/{user_id}/{inbox,memories,shared}/
/Channels/{conversation_token}/{CHANNEL.md,memories/}
```

### Scheduled Jobs (CRON.md)

Markdown with TOML `[[jobs]]`. Types: `prompt`, `prompt_file`, `command`. Per-job `model`/`effort` overrides. Auto-disable after 5 consecutive failures. `skip_log_channel`, `silent_unless_action`, `once = true` supported.

### Heartbeat

`HEARTBEAT.md` — `file-watch`, `shell-command`, `url-health`, `calendar-conflicts`, `task-deadline`, `self-check`. Cooldown + quiet hours.

### Security

- **Sandbox** (`bwrap`): per-user filesystem isolation. Linux + bubblewrap is the only supported deployment.
- **No databases in the sandbox**: `build_bwrap_cmd` ends by masking `db_path.parent` and `module_db_root()` with an empty, read-only tmpfs — the last mount operations, so no earlier bind shows through (`--remount-ro` after each and `--disable-userns` + the `--unshare-user` it requires, where bwrap supports them: on a writable mask a `sqlite3` probe creates a zero-byte file and reports `no such table`, which reads as corruption rather than as a boundary, and a tmpfs can be unmounted from a nested user namespace, so keep `sandbox_ro_paths` narrow rather than relying on the mask to make a broad entry safe). Nothing binds the framework DB for anyone (`sandbox_admin_db_write` is retired), `sandbox_ro_paths` defaults to `[]`, and `native_fs_roots` matches. Reads and writes go through skill CLIs that run host-side and scope by `ISTOTA_USER_ID` — that scoping is the per-user boundary; the masks are defence in depth behind it.
- **Config dir out of the sandbox**: emissaries, persona, guidelines and skill bodies are read by the daemon and become prompt text, so `config/` is never bound — `config.toml` lives there. The one exception is `config/system-prompt.md` under `custom_system_prompt`, which the CLI opens itself via `--system-prompt-file`; `build_bwrap_cmd` binds that single file (`custom_system_prompt_path`). It used to arrive only via the old `sandbox_ro_paths = ["/srv/app"]` default.
- **Network proxy**: `--unshare-net` + CONNECT proxy on Unix socket; allowlist of `host:port`. No MITM.
- **Skill proxy**: strips secret env vars from Claude; CLI calls go through Unix socket that injects credentials server-side. Authorization decoupled from skill selection. Started unconditionally when enabled, and **required wherever the sandbox is** — `istota-skill` refuses to run a skill in-sandbox (`ISTOTA_SANDBOXED`) rather than reaching for databases that aren't there. Database paths (`ISTOTA_DB_PATH`, and manifest `proxy_only` vars like `HEALTH_DB_PATH`) go to the proxy, never to the model.
- **Host paths in skill CLIs**: because the proxy spawns a skill CLI outside the sandbox with the daemon's filesystem view, any verb taking a *host* path is an arbitrary read or write unless it is scoped — and the model picks the path. `skill_host_paths.py` holds the one rule (`devbox cp-in`/`cp-out`, `kv set --value-file`; `scheduler_deferred._source_path_allowed` applies the same one to deferred health-op paths): roots are `$ISTOTA_DEFERRED_DIR`, `{mount}/Users/{ISTOTA_USER_ID}`, the task's own `{mount}/Channels/{token}` and `{mount}/Talk` read-only — never `NEXTCLOUD_MOUNT_PATH` whole, which is the shared root for every user. Symlinks rejected, callers operate on the returned resolved path, destinations checked before any `mkdir`.
- **Native WebFetch tool**: the native harness ships a daemon-side `WebFetch` tool (`session/tools/web_fetch.py`, native-only, `[brain.native.web_fetch]`). It runs in the daemon netns (not gated by the CONNECT allowlist), but is credential-free (`trust_env=False`, no cookies) and SSRF-hardened — every resolved IP validated against a private/reserved blocklist on each request and redirect hop, connection pinned to the validated IP (DNS-rebinding mitigation), GET/text-only, size/time capped, content wrapped in an untrusted-content delimiter (`untrusted_input` folded into the eager set when enabled). Off via `enabled = false`; `require_url_provenance` locks fetches to task-seen URLs for sensitive deployments. See `.claude/rules/brain.md`.
- **Deferred DB**: sandboxed Claude writes JSON to temp dir; scheduler processes after success. Identity (`user_id`, `conversation_token`) always from task, not JSON. Subtasks rate-limited (`max_subtasks_per_task`, `max_subtask_depth`, `max_subtask_prompt_chars`), admin-only.

## Code Style

Indentation is **spaces, never tabs**, declared in `.editorconfig` at the repo root: Python is 4 spaces, everything under `web/` is 2 spaces. The frontend is formatted by prettier — run `npm run format` in `web/` before committing frontend changes (config in `web/.prettierrc.json`). Exceptions: `web/package-lock.json` (npm-generated) and `docker/devbox/etc/gitconfig` (git-idiomatic tabs).

Python is **linted but not formatted**. `ruff check` runs clean over `src/` and `tests/`; the rule set is pinned in `[tool.ruff.lint]` to ruff's defaults (`E4`, `E7`, `E9`, `F`) with no formatting-adjacent rules — no line-length, whitespace or indentation checks. **Do not run `ruff format`**: it is not adopted, the hand formatting in the tree is the baseline, and a reformat would rewrite roughly 525 of 637 files and carry `git blame` with it. A deliberate unused import (a re-export, an import kept for a side effect) is marked `# noqa: F401` with the reason, not left to be pruned by the next `--fix` run.

## Verification

There is no wrapper script and no single entry point. Run the checks directly, and run only the half the change touches — Python and `web/` are independent.

Python:

```bash
ruff check --output-format concise src tests
uv run pytest                    # pyproject pins `-m 'not integration and not live' -n auto`
```

Web, from the repo root (needs `npm ci` in `web/` first):

```bash
npm --prefix web run lint:design
npm --prefix web run check       # svelte-check
npm --prefix web run test        # vitest run
npm --prefix web run format:check
```

Chain them in one shell invocation rather than one call each, and use `-x` / `--bail=1` while iterating so the first real failure stops the run. Drop those flags for the full run before a commit.

## Committing

This repo is public, so `.githooks/pre-commit` scans staged content twice: `gitleaks` for credentials (shape + entropy) and `scripts/check-private-data.sh` for private data (a real name, a production hostname, a home-directory path, an account number — things that have no universal shape and that gitleaks therefore cannot see). Enabled per clone by `git config core.hooksPath .githooks`, which `scripts/setup.sh` does. Patterns come from the committed `.private-data-patterns` (generic shapes only), the **gitignored** `.private-data-local` (your own literals — on a public repo a checked-in denylist is itself the leak), and two terms derived at runtime (`$HOME` path, `git config user.email`). Neither scan prints the matched value; you get `file:line` and a class. A false positive is fixed by narrowing the pattern or by putting `private-data-ok` on the line; documentation placeholders (`xxxx`, `<your-token>`, `CHANGEME`) are exempt automatically. `CHANGELOG.md` and `DEVLOG.md` are deliberately not allowlisted in `.gitleaks.toml` — prose written up from a terminal session is where a pasted credential lands. `tests/test_private_data_scan.py` gives every pattern class a positive control, because a scanner whose regex quietly stops matching reports a clean tree. Full reference in `docs/development/secret-scanning.md`.

## Configuration

Search order: `config/config.toml` → `~/src/config/config.toml` → `~/.config/istota/config.toml` → `/etc/istota/config.toml`. Override with `-c PATH`.

Per-user data lives in DB tables (`user_profiles`, `user_resources`, `briefing_configs`, `secrets`) populated by `istota user|resource|briefing|secret ensure`. The `[users.X]` block in `config.toml` (docker entrypoint path) is also accepted; DB rows win at config-load time. The retired `config/users/{user}.toml` mechanism and `config/users/` directory are gone — Ansible no longer renders per-user TOML. CalDAV derived from Nextcloud. Field-by-field reference in `.claude/rules/config.md`.

## Deployment

Ansible role (`deploy/ansible/`), Docker stack (`docker/`), and the Nextcloud rclone mount — see `.claude/rules/deployment.md`.

## Task Status

`pending` → `locked` → `running` → `completed` / `failed` / `pending_confirmation` / `cancelled`
