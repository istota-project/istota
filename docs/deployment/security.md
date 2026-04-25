# Security

Istota isolates Claude Code invocations through layered security: clean environment, filesystem sandbox, credential proxy, and network isolation.

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

Linux-only. Gracefully degrades to unsandboxed on macOS or when bwrap is not found. Merged-usr compatible for Debian 13+.

## Credential proxy

When `skill_proxy_enabled = true` (default), secret env vars are stripped from Claude's environment:

- `CALDAV_PASSWORD`, `NC_PASS`, `SMTP_PASSWORD`, `IMAP_PASSWORD`
- `KARAKEEP_API_KEY`, `MINIFLUX_API_KEY`
- `GITLAB_TOKEN`, `GITHUB_TOKEN`, `MONARCH_SESSION_TOKEN`

Skill CLI commands run through a Unix socket proxy (`skill_proxy.py`) in the executor thread. The proxy injects credentials server-side, scoped to the skills that need them. The `istota-skill` client connects to the socket or falls back to direct execution when the proxy is disabled.

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

Identity fields (`user_id`, `conversation_token`) come from the task, not the JSON, preventing spoofing via prompt injection.

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
