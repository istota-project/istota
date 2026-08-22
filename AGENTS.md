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
├── skills/               # 36 self-contained skills (skill.md + optional CLI)
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
├── host_pressure.py      # Host memory instrumentation: PSI/meminfo/tmpfs sampling, the fixed-cadence `host_pressure` breadcrumb line (incl. `shmem_unaccounted` = Shmem − Σ tmpfs used, and the daemon's own cgroup `memory.events` counters — read by walking *up* to the unit, since the delegated leaf carries no controller files), and a threshold snapshot that attributes shmem to mounts, containers, running tasks' bwrap sandboxes and `memfd` fd holders. The sandbox rows are `read_sandbox_shm`, which the scheduler feeds `(task_id, worker_pid)` for **every** running task from the task table: `read_tmpfs_usage` reads the *daemon's* mount table and a task's private `/` and `/tmp` appear in no table it consults, so the residue's most common holder was the one thing attribution could never name (ISSUE-286). **`worker_pid` is not the pid to read** — it is the outer `bwrap`, which stays in the daemon's own mount namespace, so reading it hands back the daemon's mount table and the rows restate the host tmpfs listed above them, once per task, under a `sandbox task=` label; `find_sandboxed_pid` descends breadth-first (bounded, cycle-safe) to the nearest descendant whose `ns/mnt` differs from `/proc/self/ns/mnt`, and a row is only ever emitted against a pid observed to differ. No descendant differs on an uncontained deployment, which reports unavailable rather than falling back. This is the same bwrap fork that forced cgroup placement into `preexec_fn` (ISSUE-285). Not folded into `tmpfs_sum_kb` or the residue — those feed `snapshot_trigger`, and the point is to explain the residue, not move it. `task_pids=None` renders `sandbox not-queried`, `[]` renders `sandbox none-running`; a caller with no task table has not established that nothing is running, and a running task with no `worker_pid` (NativeBrain never calls `on_pid`) is carried as `0` and rendered per-task rather than filtered out, since dropping it printed `none-running` on a busy host. Only the `linux` tier can check any of this — every other test writes the `/proc` tree it then reads. Two predicates, not one: `is_under_pressure` (PSI + MemAvailable) gates the scheduler's admission of new work, `snapshot_trigger` adds a third arm on the shmem residue and gates attribution only — a residue swap is absorbing is a reason to collect evidence, not to refuse work. stdlib-only leaf, every reader takes its `/proc` root as a parameter, never raises. `python -m istota.host_pressure [--snapshot]`
├── webhook_receiver.py   # FastAPI: Overland GPS, etc.
├── garmin_routes.py      # Module-agnostic Garmin auth router (/api/garmin/*), shared by Health + Location
├── web_app.py            # Authenticated web UI (Nextcloud OAuth2 + admin dashboard)
├── usage.py              # Normalized per-attempt token/cost telemetry (`BrainUsage`, `ModelUsage`, `from_cli_result`, `from_task_usage`). The one place each brain's own reporting shape is converted to a single vocabulary, so the schema and the read surfaces never learn which brain produced a row. Pure: no DB, no config, no brain imports, and neither adapter raises — they sit on the brain's return path
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
├── git_hardening.py      # The `-c` overrides that stop a repository's own config running a program (`core.fsmonitor`, `diff.external`, the `gpg.*` programs, plus the output-reshaping keys a parser depends on). Repo-local config is not covered by `GIT_CONFIG_NOSYSTEM`/`GIT_CONFIG_GLOBAL`, and under `developer.repos_dir` it is model-written. Extracted from `skills/code_review/engine.py`, which paid for the list and still re-exports it, so `worktree_reaper` can reach it without importing `istota.skills` (whose `__init__` star-imports every skill, ~190ms) — same reason `forge_bin.py` exists. stdlib-only leaf; no imports at all
├── git_remote_scrub.py   # Strips credentials out of the git configs under `developer.repos_dir` (ISSUE-270), run from the developer skill's `setup_env` before `repos_dir` is bound into the sandbox. `git config` is the parser, never the file: `--includes` + `--show-origin` so a value pulled in from another file is found and corrected where it lives, `-z` so a value containing a newline cannot forge an entry, `--fixed-value` so a multivar `remote.*.url` keeps its clean siblings. Covers the key as well as the value (`url.<base>.insteadOf` hides the secret in the key while `remote -v` looks clean), per-worktree `config.worktree`, and `http.*.extraheader`. Detects a userinfo password or a known forge-token prefix — not a bare `@`, so `git@host:path` is left alone. Never raises (a setup-path guard), never logs the value, and reports a credential it could not remove rather than returning silence. stdlib-only leaf
├── skill_proxy.py        # Unix-socket proxy for credential isolation
├── worktree_reaper.py    # Removes a developer worktree under `developer.repos_dir` once its work has landed (ISSUE-288). Runs from the **scheduler**, on `scheduler.worktree_reap_interval` — not from the developer skill's `setup_env`, because `dispatch_setup_env_hooks` calls every skill's hook whatever the task selected, so a sweep there fired before every Talk reply, every cron job and every heartbeat tick (and the heartbeat builds a task with `id=0`). A delete path belongs on a stated cadence. A worktree goes only when losing it cannot lose anything: inside `repos_dir`, not a repository's main worktree, unlocked, idle for `worktree_retention_hours` (measured across *both* the checkout and its administrative dir under the bare clone, because a `git commit` touches no working-tree file and an edit touches no admin file — the pointer may be relative, and resolving it against the daemon's cwd would silently drop the git half), `status --porcelain --untracked-files=all --ignored` empty, head not a merge commit, and `git cherry refs/remotes/origin/HEAD <head>` reporting no `+` line. `git cherry`, not `merge-base --is-ancestor`: a squash or rebase merge is how an MR normally lands and leaves the branch an ancestor of nothing while every commit on it has a patch-id equivalent upstream — but it never examines a merge's own delta, which is why a merge head is refused outright. Fetches before comparing, best-effort — the git credential helper is registered per task via `setup_env`'s `GIT_CONFIG_KEY_*`, so the scheduler has none and the fetch usually fails on a private repo. Safe in the only direction that matters (a stale `origin/HEAD` holds more back, never reaps more); the cost is timeliness, since freshness then comes from the developer skill's own fetch at the next task on that repo, and a repository nobody touches again keeps its merged worktrees. The listing is read `--porcelain -z`: git does not quote a newline in a worktree path in the line-oriented form, so one truncates its record and forges the next, naming a victim path with an attacker-chosen head — and a duplicate path refuses the whole repository. Every git call carries `GIT_HARDENING` (repo config is model-written, and a plain `git status` runs `core.fsmonitor` as the daemon user) and `GIT_OPTIONAL_LOCKS=0` (without it `status` rewrites the index, which is one of the mtimes the window reads, so every sweep would reset the clock it depends on). `worktree remove` without `--force`, with the dirty check repeated immediately before it, because git's own refusal covers tracked modifications and would delete a gitignored `.env` without a word; the branch ref goes with `update-ref -d <ref> <oldvalue>`, pinned to the head `cherry` approved, since a bare clone's HEAD deliberately points at a deleted ref (ISSUE-125). Retention is clamped to a one-hour floor — a shorter window deletes the checkout of a task still setting it up. Held-back worktrees are counted and logged. stdlib-only leaf, root is a parameter, never raises
├── skill_host_paths.py   # Host-path allowlist shared by the skill CLIs that take one (devbox `cp-in`/`cp-out`, `kv set --value-file`). A skill CLI runs host-side, so a path argument is an arbitrary read/write unless scoped; the roots mirror what the sandbox binds for that caller. stdlib-only leaf, importable from a skill subprocess
├── task_cgroup.py        # A cgroup v2 group per task (A6): `<unit cgroup>/task-<id>-<attempt>/` with `memory.max`, `pids.max` and `cpu.max`, so a runaway process tree is OOM-killed inside its own cgroup instead of taking the host. The directory is a *sibling* of the daemon's `supervisor/` leaf, not a child of it — cgroup v2 refuses to let a cgroup both hold processes and enable controllers for its children, so a group made inside the daemon's own would silently contain no `memory.max` at all; `resolve_root` takes the *last* `.service`/`.scope` component (the first is `user@N.service` under a user manager, which is delegated too and so fails by succeeding). The kernel is the capability probe: interface files cannot be created by a writer, so a successful `memory.max` write *is* the proof the controller is delegated — which is why the startup report calls `probe()` rather than trusting `resolve_root`, since that resolves on every systemd host and would print "containment on" for a deployment where every task runs uncontained. Placement is `preexec_fn`, not a write after `Popen` returns: membership is inherited *at* `fork`, so moving a pid afterwards leaves the children it already forked outside the group forever, and `bwrap` forks during namespace setup every time (ISSUE-285) — the parent opens `cgroup.procs`, the child writes `0` between `fork` and `exec`, and `verify_placement` reads the membership back because the child has no way to report a failure. `place()` remains only for TmuxClaudeBrain, whose pane pid the tmux server spawned. `destroy` writes `cgroup.kill` on `EBUSY` (Linux 5.14+), killing descendants that escaped their process group, and `read_events` names an OOM kill before `rmdir` takes the counters. Fails open on any deployment without `Delegate=` and says so at startup. stdlib-only leaf, roots are parameters, never raises
├── process_group.py      # `kill_process_group(pid, sig)` — signal a subprocess and every descendant sharing its group, falling back to the single process when the pid leads no group of its own (a non-leader shares the daemon's group, so signalling it would kill the scheduler; both of today's `worker_pid` writers record leaders, so the fallback guards a future caller rather than either brain). Used by both kill paths in ClaudeCodeBrain's streaming spawn, by `!stop` and the web cancel endpoint, and by the scheduler / native-bash timeout kills. Never raises — the brain's timeout calls it from a `threading.Timer` callback, where an exception would report a timeout while leaving the process alive. stdlib-only leaf
├── network_proxy.py      # CONNECT proxy for network isolation
├── forge_cli.py          # The `gh` / `glab` wrapper. One file serving the sandbox and the devbox (`docker/devbox/lib/istota_forge_cli.py` is a byte-identical copy kept in sync by `scripts/sync-devbox-lib.sh`): it decides which forge it is from argv[0], checks the argv against a code-owned deny policy, fetches the token from whichever credential socket is present, and execs the real binary with the token in its own environment. It locates its policy beside the copy of itself that is executing and takes `real_bin`, the forge URL and the config dirs from that file — never from `os.environ`, since the wrapper runs as a child of the model's own shell. Refuses rather than falling back to a public host when no URL resolves. stdlib-only leaf
├── devbox_proxy.py       # Per-user host-side daemon: git credentials injected server-side, plus the forge token (and its URL) for the container's `gh` / `glab`. Makes no outbound requests of its own
├── devbox_proxy_protocol.py  # Wire protocol for devbox_proxy (single-line JSON, 16 MiB cap)
├── docker_proxy.py       # Per-user Docker-API allowlist proxy: bound into the sandbox at /var/run/docker.sock in place of the root-equivalent raw socket; permits only exec/cp/inspect/restart on the user's own container. A real HTTP/1.1 intermediary, because HTTP/1.1 is keep-alive and an allowlist applied to a connection's first request only is not an allowlist: every request on a connection is classified, the ordinary ops are fully mediated and the connection loops, and the two ops that end a connection are terminal — `archive` relays its tar opaquely but reads no more from the client than the request declared, and `exec start` reads the *response* head first and goes full-duplex only on a real hijack, because moby does not hijack a detached or failed start and the connection is still parsing HTTP there. The head is forwarded verbatim, so a head the daemon's Go parser would read differently (bare LF, obs-fold, non-token header name) is refused rather than guessed at. Denies are terminal for the same reason a leftover body is: it would be read as the next request head
├── nextcloud_api.py      # NC user metadata
├── provision_rooms.py    # Default Talk rooms (#general/#logs/#alerts) for a user: participant-scoped idempotent lookup, group (not public) rooms, and seeding log_channel/alerts_channel only where empty. Behind `istota nextcloud provision-rooms`, called by the Ansible role — the bare-metal counterpart to the docker entrypoint
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

Alongside `src/`: `config/` (config.toml, persona.md, emissaries.md, system-prompt.md, guidelines/ — read by the daemon, never bound into the sandbox; skill bodies live in `src/istota/skills/`), `deploy/ansible/`, `docker/` (full-stack compose), `web/` (SvelteKit, adapter-static, base `/istota`), `tests/`, `testbed/` (the deployment tiers' staging environment — compose stacks, service stubs, DB probe; its own `pyproject.toml`, importable by the tests and by two rigs outside this repo, never by `src/istota/`), `schema.sql`.

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

Python is **linted but not formatted**. `ruff check` runs clean over `src/`, `tests/` and `testbed/`; the rule set is pinned in `[tool.ruff.lint]` to ruff's defaults (`E4`, `E7`, `E9`, `F`) with no formatting-adjacent rules — no line-length, whitespace or indentation checks. **Do not run `ruff format`**: it is not adopted, the hand formatting in the tree is the baseline, and a reformat would rewrite roughly 525 of 637 files and carry `git blame` with it. A deliberate unused import (a re-export, an import kept for a side effect) is marked `# noqa: F401` with the reason, not left to be pruned by the next `--fix` run.

## Verification

There is no single entry point. Run the checks directly, and run only the half the change touches — Python and `web/` are independent.

Python:

```bash
ruff check --output-format concise src tests testbed
scripts/qtest uv run pytest      # pyproject deselects every marker below; `-n auto`
```

**What to install: `uv sync --extra test`.** A bare `uv sync` is never right — everything the suite needs from `[project.optional-dependencies]` is left out, and the result is hundreds of `ModuleNotFoundError` collection errors that read as a code regression. `--all-extras` works and costs about 1.1 GB in the venv; the `test` extra is `all` minus the two heavy ML ones and costs 291 MB, which matters because the venv is per-worktree and per-container. It is what `scripts/setup.sh` and `docker/test/Dockerfile` install. The difference is `memory-search` (torch, sentence-transformers) and `whisper` (faster-whisper, av, onnxruntime); every heavy import in `src/` is inside a function, deliberately, so nothing needs them to collect, and the one test that needs them at run time carries the `ml` marker. Use `--all-extras` when you want that test or the real libraries to hand-test against. Test-only dependencies belong in the `dev` group, never in an extra — `jinja2` and `psutil` used to arrive as somebody else's transitive, and `tests/test_lean_install.py` is what keeps that from happening again.

Web, from the repo root (needs `npm ci` in `web/` first):

```bash
npm --prefix web run lint:design
npm --prefix web run check       # svelte-check
scripts/qtest npm --prefix web run test    # vitest run
npm --prefix web run format:check
```

**Eight markers are deselected by default, and none of them runs unless you ask.** Each has a different prerequisite, so they are selectable independently: `integration` (a live Nextcloud or Garmin credentials), `live` (a real LLM key; costs money), `linux` (a real kernel and a usable bubblewrap), `image` and `smoke` (a Docker daemon), `full` (a Docker daemon, minutes, and the network — `provision-nc.sh` fetches Talk and Calendar from the Nextcloud app store at first install), `testbed` (a Docker daemon and no istota image — the wire-level email suite, which runs a real IMAP/SMTP server and calls `poll_emails` against it with no stack at all), `ml` (one of the two heavy ML extras). A ninth, `requires_dac`, is not deselected — it skips itself where the process can bypass permission bits, which is what happens as root inside the Linux runner. The six discretionary tiers, none automatic:

```bash
scripts/test-linux.sh            # the suite + the linux tests, on a real kernel
uv run pytest -m image -n0       # the built image's contract
uv run pytest -m smoke -n0       # end-to-end against the lean compose stack
uv run pytest -m full -n0        # end-to-end against the full stack, incl. a real Nextcloud
uv run pytest -m testbed -n0     # wire-level email against a real IMAP/SMTP server, no istota image
scripts/test-upgrade.sh          # the current image over an older release's state
```

`image`, `smoke`, `full` and `testbed` require `-n0`: their fixtures are session-scoped and build one tagged image, and N xdist workers would each race to build it — the two compose tiers would also bring up their own stacks under one project prefix and sweep each other's projects. `smoke` and `full` are the same fixtures over two compose files: the lean stack (one container, entrypoint bypassed, config rendered on the host) and the deployment as shipped, which is the only thing that executes `provision-nc.sh` or reaches the half of `entrypoint.sh` past the config write. Before a release, add `-m full -n0`, `-m image -n0 --platform amd64` (checks the deployment architecture; since ISSUE-280 the devbox image builds natively too, so a plain `-m image` already runs its assertions) and `scripts/test-upgrade.sh --from-floor --shape volume`. Two of the tiers carry a negative control that must go **red** — `scripts/test-image-negative-control.sh`, and the same broken image handed to the upgrade tier through `ISTOTA_IMAGE_TAG`. That script covers both images: one control for the istota half, and four for the devbox half, because no single broken image reaches all thirteen of that file's assertions. Each control names the exact node ids it must turn red and requires them in pytest's `FAILED` summary, since a control can otherwise pass on an unrelated failure. On a tier asserting against an artifact, reading the test tells you almost nothing about whether it can fail. Details and a when-to-run-each table in `docs/development/testing.md`.

**All six tiers need Docker, so a sandboxed task cannot run any of them — check `ISTOTA_SANDBOXED` before you plan around one.** A task's Docker access is the devbox allowlist proxy, which permits no call that creates or starts a container; the Linux tier also wants `CAP_SYS_ADMIN` and `CAP_NET_ADMIN`, which is what the sandbox exists to deny. Both shell drivers refuse up front and say so. **When a change touches the sandbox, the network proxy, the skill proxy, a migration or the image, say so in the merge request, name the tier that covers it, and ask for the run before merge.** Report the default suite as what it is: it patches `_bwrap_available` and checks argv, so it has never executed the sandbox path you changed.

Chain them in one shell invocation rather than one call each, and use `-x` / `--bail=1` while iterating so the first real failure stops the run. Drop those flags for the full run before a commit. Never read the result through a pipe: a pipeline reports its *last* command's status, so `uv run pytest … | tail` exits 0 on a suite that failed. Set `set -o pipefail` in the same shell, or redirect to a file and check `$?` before reading it. A run wrapped in `scripts/qtest` also says the answer in words, on stderr, which no stdout filter can drop.

**Wrap every full suite run in `scripts/qtest`.** Both suites size their worker pool from `cpu_count()` — pytest via `-n auto`, vitest by default — so each run claims the whole machine. That is correct for one run and pathological for several: work spread across worktrees means three jobs can ask for 36 workers on 12 cores, and runs then fail on timeouts that have nothing to do with the code. `qtest` is a `flock` semaphore holding one machine-wide slot (`QTEST_SLOTS` to raise it, `QTEST_TIMEOUT` to bound the wait, `QTEST_DISABLE=1` to bypass); it queues the run instead, so suites finish sooner and their results mean something. Serialize the expensive runs only — the fast feedback loop of a single test file needs no slot, and neither do `ruff`, `svelte-check` or `format:check`. **Every qtest run ends with one verdict line on stderr** — `qtest: PASS exit=0 time=3m41s cmd: uv run pytest`, or `FAIL`, or `KILLED-SIGKILL`, or `NO-SLOT` — because a pipeline reports the pipe's status and a failed suite then reads as exit 0. Read that line. stdout stays the command's own output, verbatim. Exit code 75 (`NO-SLOT`) means no slot came free and **the command did not run**; that is not a test failure. The lock lives in `~/.cache/qtest`, outside any repo, because the resource being shared is the laptop: other checkouts queue against the same slot by design.

## Committing

This repo is public, so `.githooks/pre-commit` scans staged content twice: `gitleaks` for credentials (shape + entropy) and `scripts/check-private-data.sh` for private data (a real name, a production hostname, a home-directory path, an account number — things that have no universal shape and that gitleaks therefore cannot see). Enabled per clone by `git config core.hooksPath .githooks`, which `scripts/setup.sh` does. **A scan that cannot run refuses the commit when the shell is an unattended one**, where a warning nobody reads is not a control (ISSUE-291); a human gets the warning and the commit. Three markers, because the daemon spawns unattended shells three ways: `ISTOTA_SANDBOXED` (every model task), `DEVELOPER_REPOS_DIR` (a task authorized for the developer skill), and `PRECOMMIT_SCANS_REQUIRED=1`, which `build_stripped_env` sets for cron `command` jobs and heartbeat shell commands since those carry neither of the others. `PRECOMMIT_SCANS_REQUIRED` also overrides by hand either way (`1`/`true`/`yes`/`on`, `0`/`false`/`no`/`off`; anything else warns and demands the scans), and is the thing to reach for rather than `--no-verify`, which drops both scans. A refusal is a broken install to report, not a step to work around — the hook deliberately does not print the override in that branch. gitleaks below 8.19 counts as unable to run: `gitleaks git` replaced `detect`/`protect` there, and Debian 13 still packages 8.16, so the Ansible role installs a pinned upstream release instead of using apt. Patterns come from the committed `.private-data-patterns` (generic shapes only), the **gitignored** `.private-data-local` (your own literals — on a public repo a checked-in denylist is itself the leak), and two terms derived at runtime (`$HOME` path, `git config user.email`). Neither scan prints the matched value; you get `file:line` and a class. A false positive is fixed by narrowing the pattern or by putting `private-data-ok` on the line; documentation placeholders (`xxxx`, `<your-token>`, `CHANGEME`) are exempt automatically. `CHANGELOG.md` and `DEVLOG.md` are deliberately not allowlisted in `.gitleaks.toml` — prose written up from a terminal session is where a pasted credential lands. `tests/test_private_data_scan.py` gives every pattern class a positive control, because a scanner whose regex quietly stops matching reports a clean tree. Full reference in `docs/development/secret-scanning.md`.

## Configuration

Search order: `config/config.toml` → `~/src/config/config.toml` → `~/.config/istota/config.toml` → `/etc/istota/config.toml`. Override with `-c PATH`.

Per-user data lives in DB tables (`user_profiles`, `user_resources`, `briefing_configs`, `secrets`) populated by `istota user|resource|briefing|secret ensure`. The `[users.X]` block in `config.toml` (docker entrypoint path) is also accepted; DB rows win at config-load time. The retired `config/users/{user}.toml` mechanism and `config/users/` directory are gone — Ansible no longer renders per-user TOML. CalDAV derived from Nextcloud. Field-by-field reference in `.claude/rules/config.md`.

## Deployment

Ansible role (`deploy/ansible/`), Docker stack (`docker/`), and the Nextcloud rclone mount — see `.claude/rules/deployment.md`.

## Task Status

`pending` → `locked` → `running` → `completed` / `failed` / `pending_confirmation` / `cancelled`
