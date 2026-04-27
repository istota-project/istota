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

When `skill_proxy_enabled = true` (default), secret env vars are stripped from Claude's environment:

- `CALDAV_PASSWORD`, `NC_PASS`, `SMTP_PASSWORD`, `IMAP_PASSWORD`
- `KARAKEEP_API_KEY`, `MINIFLUX_API_KEY`
- `GITLAB_TOKEN`, `GITHUB_TOKEN`, `MONARCH_SESSION_TOKEN`, `GOOGLE_WORKSPACE_CLI_TOKEN`

Skill CLI commands run through a Unix socket proxy (`skill_proxy.py`) in the executor thread. The proxy injects credentials server-side, scoped per skill: `_CREDENTIAL_SKILL_MAP` maps each credential to the set of skills that may use it, so a CLI invocation only ever sees the credentials its own skill is mapped to. The `istota-skill` client connects to the socket or falls back to direct execution when the proxy is disabled.

### Authorization model

Credential authorization is **decoupled from skill selection**. A CLI skill is authorized for credential access if any of its mapped credentials is actually present in the user's task environment — that is, if the user has the corresponding resource configured (Karakeep, Miniflux, etc.) or the relevant instance config is set (SMTP, GitLab/GitHub tokens). Selection (Pass 1 keyword matching + Pass 2 semantic routing) controls only which skill *docs* go into the prompt, not which credentials can be requested at runtime.

This avoids the failure mode where a keyword miss locks a skill out: e.g. a user has a Miniflux resource configured, the prompt didn't say "feed", `feeds` wasn't selected — under the old model the proxy would refuse to inject `MINIFLUX_API_KEY` and the CLI invocation would fail mysteriously. Under the new model the credential is injectable as soon as Claude decides it needs the feeds skill, regardless of selection.

Threat model is unchanged: a compromised Claude can only request credentials that already exist for this user (resources are user-scoped, instance config is operator-controlled).

### Rejection observability

Every proxy rejection emits a structured WARNING log:

```
proxy_rejected task_id=42 type=skill skill=evil_skill reason=unknown_skill
proxy_rejected task_id=42 type=credential name=NC_PASS reason=not_authorized
```

Reason codes: `unknown_skill` (skill name not in the CLI whitelist), `not_authorized_credential` (credential not in this task's allowed set), `credential_not_present` (credential genuinely missing from env).

Rejection responses include the structured `reason` field and, for unknown skills, an `authorized_skills` list — surfaced to the model via the client's stderr so it can adapt rather than retry blindly.

Use these logs together with the Pass 1/Pass 2 selection logs (see [skills](../features/skills.md#selection-observability)) to count selection misses and decide whether the semantic-routing prompt or timeout needs tuning.

## Network isolation

When `[security.network] enabled = true` (default, requires sandbox), each task's sandbox gets `--unshare-net` (own network namespace, no external connectivity). Outbound traffic goes through a CONNECT proxy on a Unix socket.

Default allowlist:

- `api.anthropic.com:443` -- Claude API
- `mcp-proxy.anthropic.com:443` -- Claude API
- `pypi.org:443`, `files.pythonhosted.org:443` -- package installs (when `allow_pypi = true`)

Additional hosts added automatically:

- Per-user resource hosts (Miniflux, Moneyman) scoped to current task's user
- Git remote hosts from `[developer]` config when the developer skill is selected
- Operator extras via `extra_hosts`

No MITM -- TLS is end-to-end between Claude Code and the destination.

## Deferred DB operations

With the sandbox making the DB read-only, skills write JSON request files to the always-writable temp dir. The scheduler (unsandboxed) processes them after successful completion:

- `task_{id}_subtasks.json` -- subtask creation (admin-only)
- `task_{id}_tracked_transactions.json` -- transaction dedup
- `task_{id}_sent_emails.json` -- outbound email tracking
- `task_{id}_kv_ops.json` -- KV store set/delete operations
- `task_{id}_user_alerts.json` -- suspicious email alerts posted to user's alerts channel
- `task_{id}_email_output.json` -- deferred email sends (SMTP delivery after task completion)
- `task_{id}_pending_send.json` -- outbound recipient gate (Layer A); the queued draft for an unknown recipient awaiting user confirmation

Identity fields (`user_id`, `conversation_token`) come from the task, not the JSON, preventing spoofing via prompt injection.

## Outbound recipient gate (email)

Layer A of the adversarial-defense plan. Closes the most realistic exfiltration channel for a prompt-injected agent: being steered into mailing user data to an attacker. The skill proxy keeps SMTP credentials out of the agent's env, but the agent can still call `istota-skill email send --to <anywhere>` and the proxy injects the credentials into the skill subprocess.

The executor builds a per-user "known recipients" set (sent + received addresses, runtime trusted senders, the user's own addresses, plus addresses extracted from the verbatim task prompt for trusted task sources only — not for email-sourced tasks where the prompt is the inbound body). The set is passed via `ISTOTA_KNOWN_RECIPIENTS` (newline-separated) and `ISTOTA_TRUSTED_RECIPIENT_PATTERNS` (fnmatch globs). The skill checks `--to` against both before sending; on miss it writes `task_{id}_pending_send.json` and exits with `{"status": "pending_confirmation", ...}`.

The scheduler picks up the deferred file post-task, transitions to `pending_confirmation` with a natural-language prompt in the same `"I need your confirmation to proceed: Action: ..."` format the `sensitive_actions` skill uses for other gated actions. The prompt is posted in the user's main conversation (`task.conversation_token`), falling back to the alerts channel only for tasks without a conversation (CLI / scheduled / cron). The existing three-path Talk reply matcher handles `yes` / `yes trust` / `no`. On approval the task re-runs with the previously-blocked recipients added to the per-task allowlist; the executor also injects the structured draft (to / subject / body) from `pending_send.json` into the re-run's `confirmation_context` so the agent sends the exact body the user approved instead of re-improvising. On `yes trust` the recipients are added to `trusted_email_senders` (semantics are bidirectional). On `no` the file is unlinked.

### Coordination with the agent-driven `sensitive_actions` flow

Other sensitive actions (file delete, calendar event delete, external file sharing) are confirmed by the **agent** in natural language: it produces a `"I need your confirmation"` text instead of executing, and the `CONFIRMATION_PATTERN` regex in `scheduler.py` intercepts that text. For email this approach was abandoned because (a) the agent improvises the wording each turn, (b) there's no structured recipient data for the trust-list operation when the user replies `yes trust`, and (c) the agent's self-confirmation is non-deterministic — it can be skipped or worded differently depending on context.

Layer A produces the same `"I need your confirmation to proceed: ..."` format, so the user-side experience is identical to the agent-driven flow. The difference is that the format is fixed (server-rendered from `pending_send.json`), the recipient check is deterministic (DB query, not agent judgement), and `yes trust` durably persists the approved address. The `sensitive_actions` skill instructs the agent to **not** pre-confirm email sends — it should call `email send` directly and respond to the gated `pending_confirmation` status with a one-sentence acknowledgment ("Drafted — waiting for your approval").

Fail-open when env vars unset (preserves direct CLI use). Operator kill switch: `outbound_gate_email = false`.

See [features/email.md](../features/email.md#outbound-recipient-gate) for the user-facing description.

## Configuration

```toml
[security]
sandbox_enabled = true
sandbox_admin_db_write = false
skill_proxy_enabled = true
skill_proxy_timeout = 300
outbound_gate_email = true
passthrough_env_vars = ["LANG", "LC_ALL", "LC_CTYPE", "TZ"]

[security.network]
enabled = true
allow_pypi = true
extra_hosts = []
```
