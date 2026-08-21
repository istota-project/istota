# Security

Istota isolates Claude Code invocations through layered security: clean environment, filesystem sandbox, credential proxy, and network isolation.

## Supported deployment

Linux with [bubblewrap](https://github.com/containers/bubblewrap) is the only supported deployment configuration. The filesystem sandbox is the boundary between users and between Claude and the host — without it, env-var scoping in the prompt is the only thing keeping one user's tasks from reading another user's data, and that boundary depends on the model following instructions.

macOS and any Linux without bwrap (or where bwrap can't create user namespaces — e.g. containers without `CAP_SYS_ADMIN`) are **development configurations only**. They will run, but they provide no isolation guarantees and are not suitable for multi-user deployments. The scheduler logs a `SECURITY UNSUPPORTED CONFIGURATION` warning at startup when it detects either condition with more than one user configured.

If you disable the sandbox or run on an unsupported platform, you accept that:

- A prompt injection in one user's task may exfiltrate any other user's data on the same host.
- Claude has access to the full filesystem visible to the istota service account, not just the per-user subtree.
- The credential proxy and network proxy still run, but their effectiveness drops without the sandbox boundary (Claude can read arbitrary files, including ones holding the credentials the proxy exists to hide).

## Clean environment

Every Claude Code subprocess gets a minimal environment built by `build_clean_env()`: only PATH, HOME, PYTHONUNBUFFERED, and configured passthrough vars (`LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`). Task-specific variables (Nextcloud credentials, CalDAV, email, etc.) are added per-task.

For heartbeat/cron shell commands, `build_stripped_env()` removes all credential-pattern vars (PASSWORD, TOKEN, SECRET, API_KEY, etc.) from the environment.

## Filesystem sandbox (bubblewrap)

When `sandbox_enabled = true` (default), each Claude Code invocation runs inside a `bwrap` mount namespace with PID isolation.

**Non-admin users see**:

- System libraries (read-only)
- Python venv + source (read-only)
- Their own Nextcloud subtree (read-write)
- Active channel directory (read-write)
- Their temp directory (read-write)
- Extra resource paths

**Hidden from everyone, admin included**: every SQLite database the daemon owns. The framework DB directory and the per-user module-DB root (`module_data_dir`, holding each user's `health.db` / `money.db` / `location.db` / `feeds.db`) are covered by an empty tmpfs, applied as the last mount operations so no earlier bind shows through. Local DB backups and the browser profile live under the same directory and go with them.

Each mask is then remounted read-only (`--remount-ro`, where bwrap supports it — 0.2+). A writable mask makes the dead end lie: `sqlite3 {db_dir}/istota.db "select …"` creates the file on the tmpfs and answers `no such table`, which reads as a missing schema rather than as "this file is not in the namespace", and leaves a zero-byte `istota.db` behind for the rest of the task. Read-only, the same command fails at open. Nothing a task writes under a database directory can survive to be mistaken for a database.

A mask hides rather than revokes: `kernel.unprivileged_userns_clone` is on (bwrap needs it), so a sandboxed process can enter a nested user namespace and unmount a tmpfs to reveal whatever was bound underneath. `--disable-userns` is passed where bwrap supports it (0.8+), together with the `--unshare-user` it requires — without that companion flag bwrap refuses the argv, which is why the support check answered "unsupported" on every host and the flag reached no sandbox at all until this was found. In the shipped default nothing is bound underneath, so there was nothing to reveal either way. Both of those are reasons to keep `sandbox_ro_paths` narrow rather than to rely on the mask to make a broad entry safe.

**Also hidden from non-admin**: other users' directories, `/etc/istota/`, user config files.

The config directory is not bound — it holds `config.toml`. `emissaries.md`, `persona.md`, `guidelines/*.md` and the skill bodies reach the model as content the daemon read and put in the prompt, so they never needed to be there. `system-prompt.md` is the exception, since `custom_system_prompt = true` makes the CLI open the path itself; that one file is bound read-only, which leaves `config.toml` outside. Until now the file arrived only via the `sandbox_ro_paths = ["/srv/app"]` default, which is why narrowing that default broke every task on such an install.

**Admin users additionally see**: full Nextcloud mount (read-write), developer repos.

The masks are unconditional rather than a matter of not binding the files, because not binding them was the previous design and it did not hold. `module_data_dir` defaults under the framework DB's directory, the reference deployment puts that under `istota_home`, and `sandbox_ro_paths` defaulted to the `/srv/app` containing it — so one RO bind that named no database exposed all of them, to every task. `sandbox_ro_paths` now defaults to `[]` and is honoured from config (it was previously never parsed), but the masks are what makes the property hold regardless.

Reads and writes reach the databases only through skill CLIs, which the credential proxy runs **outside** the sandbox and which scope their queries by `ISTOTA_USER_ID`. That scoping, not the filesystem, is the per-user boundary; the sandbox is defence in depth behind it.

Linux-only and merged-usr compatible for Debian 13+. See [Supported deployment](#supported-deployment) above for the policy on non-Linux / no-bwrap configurations.

### The `.developer` carve-out

Each task's scratch space holds a `.developer` directory, written by the `developer` skill's `setup_env` hook. It contains the helper scripts that fetch that task's credentials: `credential-fetch`, the git credential helper, and the `gh` / `glab` wrappers. A task that could replace one of them could intercept a forge token on its next use.

`build_bwrap_cmd` therefore re-binds `.developer` read-only *after* the read-write bind of its parent, so the sandboxed path has always been covered. The in-process agent loop (the native brain) runs without bwrap, and its confinement is a list of writable roots — pure containment, which cannot express a hole inside a root. Every path under the task's temp directory, `.developer` included, was writable there.

`ToolEnv` now takes `write_denied_roots`, checked before the allow loop and on the write path only. Reads still pass, matching what the read-only bind gives the other brain. `native_fs_roots()` returns the carve-outs as a third element rather than leaving them to a second call a future caller could forget, and the executor passes them through as `BrainRequest.fs_write_denied_roots`.

The deny root is appended unconditionally rather than only when the directory exists. bwrap re-checks on every Bash call and self-heals; this list is built once, so an existence gate would hand a task that started before `.developer` existed an empty deny set for its whole life — and `Write` creates parent directories, so the model could then make the directory itself. A refused write reports read-only rather than "outside the allowed workspace", which is the one thing it is not.

## Credential proxy

When `skill_proxy_enabled = true` (default), secret env vars are stripped from Claude's environment and routed through a Unix socket proxy instead. See [credentials](../configuration/credentials.md) for the full inventory of which credentials are global vs per-user and how they're provisioned.

The set of stripped variables is **manifest-derived**: `derive_credential_set(skill_index)` collects every env var declared with `sensitive: true` across all loaded skill manifests. Today's set:

- `CALDAV_PASSWORD`, `NC_PASS`, `SMTP_PASSWORD`, `IMAP_PASSWORD`
- `KARAKEEP_API_KEY`
- `GITLAB_TOKEN`, `GITHUB_TOKEN`, `MONARCH_SESSION_ID`, `MONARCH_CSRFTOKEN`, `GOOGLE_WORKSPACE_CLI_TOKEN`
- `NTFY_TOKEN`, `NTFY_PASSWORD`, `TUMBLR_API_KEY`

`ISTOTA_SECRET_KEY` (the master Fernet key) is **not** in the manifest-derived set. It is the proxy's hard-reject lookup var (`_PROXY_LOOKUP_BLOCKED`) and never enters any subprocess env.

Adding a sensitive credential to a skill's `env:` block is the only step needed to route it through the proxy; there is no longer a hand-maintained `_PROXY_CREDENTIAL_VARS` list to keep in sync.

Skill CLI commands run through the proxy (`skill_proxy.py`) in the executor thread. The proxy injects credentials server-side, scoped per skill: `derive_skill_credential_map(authorized, skill_index)` returns the per-skill credential map, so a CLI invocation only ever sees credentials its own manifest declared. The `istota-skill` client connects to the socket or falls back to direct execution when the proxy is disabled.

The proxy's Unix socket path includes the host process PID — `istota-proxy-{pid}-{task_id}.sock` (and the same shape for the network proxy). This prevents collisions when multiple processes (xdist test workers, parallel `istota run` instances, the daemon plus a manual scheduler) pick the same `task.id` from independent SQLite databases.

### Authorization model

Credential authorization is **decoupled from skill selection**. A skill is authorized for credential access if any of its sensitive `EnvSpec`s actually resolves under the task's context — that is, if the user has the corresponding resource configured (Karakeep, etc.) or the relevant instance config is set (SMTP, GitLab/GitHub tokens). Skill selection controls only which skill *docs* go into the prompt, not which credentials can be requested at runtime.

This avoids the failure mode where a keyword miss locks a skill out: e.g. a user has a Karakeep resource configured, the prompt didn't say "bookmark", `bookmarks` wasn't selected — under the old model the proxy would refuse to inject `KARAKEEP_API_KEY` and the CLI invocation would fail mysteriously. Under the new model the credential is injectable as soon as Claude decides it needs the bookmarks skill, regardless of selection.

Doc-only skills (no CLI module) are eligible too: the `developer` skill consumes `GITLAB_TOKEN`/`GITHUB_TOKEN` via `credential-fetch` from the git credential helper and the `gh` / `glab` wrappers its `setup_env` hook writes into the task's `.developer` directory. Gating authorization on `cli=true` (the prior heuristic) would lock it out.

Auto-authorization uses `_resolve_env_spec(spec, ctx, fallbacks_disabled=True)` so an instance-wide `EnvironmentFile` fallback for an operator-set value cannot fan out and auto-authorize every user — preserving the per-user privacy posture.

`derive_lookup_allowlist(authorized, skill_index)` is the union the proxy will respond to over `credential-fetch`, with `_PROXY_LOOKUP_BLOCKED = {"ISTOTA_SECRET_KEY"}` subtracted as a defense-in-depth hard reject. The master Fernet key flows into specific module-skill subprocess envs (so they can decrypt per-user secrets in-process) but is never returned over the lookup channel — `bash -c '.developer/credential-fetch ISTOTA_SECRET_KEY'` from inside Claude is rejected.

Threat model: a compromised Claude can only request credentials that already exist for this user (resources are user-scoped, instance config is operator-controlled).

### Rejection observability

Every proxy rejection emits a structured WARNING log:

```
proxy_rejected task_id=42 type=skill skill=evil_skill reason=unknown_skill
proxy_rejected task_id=42 type=credential name=NC_PASS reason=not_authorized
```

Reason codes: `unknown_skill` (skill name not in the CLI whitelist), `not_authorized_credential` (credential not in this task's allowed set), `credential_not_present` (credential genuinely missing from env).

Rejection responses include the structured `reason` field and, for unknown skills, an `authorized_skills` list — surfaced to the model via the client's stderr so it can adapt rather than retry blindly.

Use these logs together with the selection logs (`pass1_selection`, `disclosure:`; see [skills](../features/skills.md#selection-observability)) to count selection misses and decide whether a skill's keywords or disclosure mode need tuning.

## Admin-gated job types

Two scheduled-job types can run arbitrary shell, so they're gated to admin users:

- **`command:` rows in CRON.md** — `cron_loader.sync_cron_jobs_to_db` drops command-type rows for non-admin authors at sync time and orphan-deletes any DB row left over from a prior admin sync. `_execute_command_task` refuses non-admin tasks at runtime as defense in depth. Auto-seeded `_module.*` rows are scheduler-inserted, not user-authored, so they're unaffected.
- **`type: shell-command` heartbeat checks** — `heartbeat.run_check` refuses these for non-admin users.

CRON.md `command:` rows of the shape `istota-skill <name> [args]` (no shell metacharacters) auto-promote to skill-tasks at sync time and dispatch through `_execute_skill_task` instead, which is not admin-gated — operators can give non-admin users access to specific skills without granting full shell.

## Network isolation

When `[security.network] enabled = true` (default, requires sandbox), each task's sandbox gets `--unshare-net` (own network namespace, no external connectivity). Outbound traffic goes through a CONNECT proxy on a Unix socket.

Default allowlist:

- `api.anthropic.com:443` -- Claude API
- `mcp-proxy.anthropic.com:443` -- Claude API
- `pypi.org:443`, `files.pythonhosted.org:443` -- package installs (when `allow_pypi = true`)

Additional hosts added automatically:

- Git remote hosts from `[developer]` config when the developer skill is selected
- `results-receiver.actions.githubusercontent.com:443` on github.com, where `gh run view --log-failed` fetches job logs. Measured through a logging CONNECT proxy rather than assumed: it is one stable hostname across independent uncached runs, so an exact entry covers it and the proxy needs no wildcard support
- Operator extras via `extra_hosts`

`gh run download` is deliberately **not** covered. Artifacts come from `productionresultssa<N>.blob.core.windows.net` with the shard varying per repository, and the only entry that would cover that is `*.blob.core.windows.net` — all of Azure Blob Storage, a general-purpose exfiltration channel reachable from the sandbox. The CI feedback loop needs logs, not artifacts.

The forge wrapper sets `GH_TELEMETRY=0` and `DO_NOT_TRACK=1`, so no telemetry host needs allowlisting and no command spends a rejected CONNECT on one. GitHub Enterprise Server needs no extra entry: its API is a path on the same host (`<host>/api/v3`), already added as the git remote.

No MITM -- TLS is end-to-end between Claude Code and the destination.

## Deferred DB operations

With no database reachable from inside the sandbox at all, skills write JSON request files to the always-writable temp dir. The scheduler (unsandboxed) processes them after successful completion:

- `task_{id}_subtasks.json` -- subtask creation (admin-only)
- `task_{id}_tracked_transactions.json` -- transaction dedup
- `task_{id}_sent_emails.json` -- outbound email tracking
- `task_{id}_kv_ops.json` -- KV store set/delete operations
- `task_{id}_kg_ops.json` -- knowledge-graph fact add/invalidate/delete (per-op commit)
- `task_{id}_user_alerts.json` -- suspicious email alerts posted to user's alerts channel
- `task_{id}_email_output.json` -- deferred email sends (SMTP delivery after task completion)
- `task_{id}_health_ops.json` -- health module writes (stats, bloodwork, encounters)
- `task_{id}_garmin_import.json` -- Garmin Connect sync requests

One recovery artifact is written *by* the scheduler rather than read by it: `task_{id}_health_op_failures.json`, left behind when a health op fails mid-batch so an operator can recover the lost rows. It is recognized but never purged on retry.

Handlers and the shared envelope helper (`_load_deferred_json`) live in `scheduler_deferred.py`. Identity fields (`user_id`, `conversation_token`) come from the task, not the JSON, preventing spoofing via prompt injection. See [scheduler](../architecture/scheduler.md#deferred-db-operations) for retry-replay safety and the unconsumed-file warning.

## Configuration

```toml
[security]
sandbox_enabled = true
skill_proxy_enabled = true   # needed wherever sandbox_enabled is true; turning it
                             # off with the sandbox on warns at startup and leaves
                             # skill commands with nothing to read
skill_proxy_timeout = 300
passthrough_env_vars = ["LANG", "LC_ALL", "LC_CTYPE", "TZ"]
sandbox_ro_paths = []        # extra RO binds for co-located services; keep narrow

[security.network]
enabled = true
allow_pypi = true
extra_hosts = []
```
