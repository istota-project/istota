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

**Hidden from non-admin**: database, other users' directories, `/etc/istota/`, user config files.

**Admin users additionally see**: full Nextcloud mount (read-write), database (read-only by default, writable with `sandbox_admin_db_write`), developer repos.

Linux-only and merged-usr compatible for Debian 13+. See [Supported deployment](#supported-deployment) above for the policy on non-Linux / no-bwrap configurations.

## Credential proxy

When `skill_proxy_enabled = true` (default), secret env vars are stripped from Claude's environment and routed through a Unix socket proxy instead. See [credentials](../configuration/credentials.md) for the full inventory of which credentials are global vs per-user and how they're provisioned.

The set of stripped variables is **manifest-derived**: `derive_credential_set(skill_index)` collects every env var declared with `sensitive: true` across all loaded skill manifests. Today's set:

- `CALDAV_PASSWORD`, `NC_PASS`, `SMTP_PASSWORD`, `IMAP_PASSWORD`
- `KARAKEEP_API_KEY`
- `GITLAB_TOKEN`, `GITHUB_TOKEN`, `MONARCH_SESSION_TOKEN`, `GOOGLE_WORKSPACE_CLI_TOKEN`
- `NTFY_TOKEN`, `NTFY_PASSWORD`, `TUMBLR_API_KEY`
- `ISTOTA_SECRET_KEY` (master Fernet key — routed to module-skill subprocesses that need to decrypt per-user secrets, blocked at the proxy lookup endpoint)

Adding a sensitive credential to a skill's `env:` block is the only step needed to route it through the proxy; there is no longer a hand-maintained `_PROXY_CREDENTIAL_VARS` list to keep in sync.

Skill CLI commands run through the proxy (`skill_proxy.py`) in the executor thread. The proxy injects credentials server-side, scoped per skill: `derive_skill_credential_map(authorized, skill_index)` returns the per-skill credential map, so a CLI invocation only ever sees credentials its own manifest declared. The `istota-skill` client connects to the socket or falls back to direct execution when the proxy is disabled.

The proxy's Unix socket path includes the host process PID — `istota-proxy-{pid}-{task_id}.sock` (and the same shape for the network proxy). This prevents collisions when multiple processes (xdist test workers, parallel `istota run` instances, the daemon plus a manual scheduler) pick the same `task.id` from independent SQLite databases.

### Authorization model

Credential authorization is **decoupled from skill selection**. A skill is authorized for credential access if any of its sensitive `EnvSpec`s actually resolves under the task's context — that is, if the user has the corresponding resource configured (Karakeep, etc.) or the relevant instance config is set (SMTP, GitLab/GitHub tokens). Selection (Pass 1 keyword matching + Pass 2 semantic routing) controls only which skill *docs* go into the prompt, not which credentials can be requested at runtime.

This avoids the failure mode where a keyword miss locks a skill out: e.g. a user has a Karakeep resource configured, the prompt didn't say "bookmark", `bookmarks` wasn't selected — under the old model the proxy would refuse to inject `KARAKEEP_API_KEY` and the CLI invocation would fail mysteriously. Under the new model the credential is injectable as soon as Claude decides it needs the bookmarks skill, regardless of selection.

Doc-only skills (no CLI module) are eligible too: the `developer` skill consumes `GITLAB_TOKEN`/`GITHUB_TOKEN` via `credential-fetch` from the git credential helper and the `gitlab-api`/`github-api` wrappers its `setup_env` hook bind-mounts into the sandbox. Gating authorization on `cli=true` (the prior heuristic) would lock it out.

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

Use these logs together with the Pass 1/Pass 2 selection logs (see [skills](../features/skills.md#selection-observability)) to count selection misses and decide whether the semantic-routing prompt or timeout needs tuning.

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
- Operator extras via `extra_hosts`

No MITM -- TLS is end-to-end between Claude Code and the destination.

## Deferred DB operations

With the sandbox making the DB read-only, skills write JSON request files to the always-writable temp dir. The scheduler (unsandboxed) processes them after successful completion:

- `task_{id}_subtasks.json` -- subtask creation (admin-only)
- `task_{id}_tracked_transactions.json` -- transaction dedup
- `task_{id}_sent_emails.json` -- outbound email tracking
- `task_{id}_kv_ops.json` -- KV store set/delete operations
- `task_{id}_kg_ops.json` -- knowledge-graph fact add/invalidate/delete (per-op commit)
- `task_{id}_user_alerts.json` -- suspicious email alerts posted to user's alerts channel
- `task_{id}_email_output.json` -- deferred email sends (SMTP delivery after task completion)

Handlers and the shared envelope helper (`_load_deferred_json`) live in `scheduler_deferred.py`. Identity fields (`user_id`, `conversation_token`) come from the task, not the JSON, preventing spoofing via prompt injection. See [scheduler](../architecture/scheduler.md#deferred-db-operations) for retry-replay safety and the unconsumed-file warning.

## Configuration

```toml
[security]
sandbox_enabled = true
sandbox_admin_db_write = false
skill_proxy_enabled = true
skill_proxy_timeout = 300
passthrough_env_vars = ["LANG", "LC_ALL", "LC_CTYPE", "TZ"]

[security.network]
enabled = true
allow_pypi = true
extra_hosts = []
```
