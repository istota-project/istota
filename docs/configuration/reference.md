# Configuration reference

Complete reference for `config/config.toml`. See `config/config.example.toml` in the repository for a commented example.

## Top-level settings

| Setting | Default | Description |
|---|---|---|
| `bot_name` | `"Istota"` | User-facing name (chat, emails, folder names) |
| `emissaries_enabled` | `true` | Include emissaries.md in system prompts |
| `model` | `""` | Claude model override (empty = CLI default). Pin to a version like `"claude-opus-4-8"`. |
| `effort` | `""` | Effort level: `low`, `medium`, `high`, `xhigh`, or `max` (empty = model default) |
| `custom_system_prompt` | `false` | Use config/system-prompt.md instead of Claude Code default |
| `db_path` | `"data/istota.db"` | SQLite database path |
| `rclone_remote` | `"nextcloud"` | rclone remote name |
| `nextcloud_mount_path` | not set | Local mount path (enables mount mode when set) |
| `skills_dir` | `"config/skills"` | Operator skill overrides directory |
| `disabled_skills` | `[]` | Instance-wide skills to exclude |
| `temp_dir` | `"/tmp/istota"` | Temporary directory for task execution |
| `max_memory_chars` | `0` | Cap total memory in prompts (0 = unlimited) |
| `max_knowledge_facts` | `0` | Cap knowledge graph facts per prompt (0 = unlimited) |

## `[nextcloud]`

| Setting | Default | Description |
|---|---|---|
| `url` | `""` | Nextcloud server URL |
| `username` | `""` | Bot's Nextcloud username |
| `app_password` | `""` | Nextcloud app password |

## `[talk]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable Talk polling |
| `bot_username` | `"istota"` | Bot's username (to filter own messages) |

## `[email]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable email |
| `imap_host` | `""` | IMAP server |
| `imap_port` | `993` | IMAP port |
| `imap_user` | `""` | IMAP username |
| `imap_password` | `""` | IMAP password |
| `smtp_host` | `""` | SMTP server |
| `smtp_port` | `587` | SMTP port |
| `smtp_user` | `""` | SMTP username (defaults to imap_user) |
| `smtp_password` | `""` | SMTP password (defaults to imap_password) |
| `poll_folder` | `"INBOX"` | Folder to poll |
| `bot_email` | `""` | Bot's email address |
| `confirm_sender_match` | `true` | Require confirmation for sender-match routed emails |

## `[conversation]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable conversation context |
| `lookback_count` | `25` | Messages to consider |
| `skip_selection_threshold` | `3` | Include all if history <= this |
| `selection_model` | `"fast"` | Role alias for relevance matching (resolves to Haiku by default) |
| `selection_timeout` | `30.0` | Timeout for selection |
| `use_selection` | `true` | Use LLM selection |
| `always_include_recent` | `5` | Always include this many recent |
| `context_truncation` | `0` | Max chars per bot response (0 = no limit) |
| `context_recency_hours` | `0` | Exclude old messages (0 = disabled) |
| `context_min_messages` | `10` | Min messages when recency filtering |
| `previous_tasks_count` | `3` | Unfiltered tasks to inject |
| `talk_context_limit` | `100` | Messages from Talk API |

## `[logging]`

| Setting | Default | Description |
|---|---|---|
| `level` | `"INFO"` | Log level (INFO or DEBUG) |
| `output` | `"console"` | Destination: console, file, or both |
| `file` | `""` | Log file path |
| `rotate` | `true` | Enable log rotation |
| `max_size_mb` | `10` | Max log file size |
| `backup_count` | `5` | Rotated files to keep |

## `[scheduler]`

### Polling intervals

| Setting | Default | Description |
|---|---|---|
| `poll_interval` | `2` | Seconds between queue checks |
| `dispatch_interval` | `0.5` | Sub-tick cadence for `pool.dispatch()` within a poll tick — bounds cold pending-task pickup latency. 0 or ≥ `poll_interval` = legacy one-dispatch-per-tick |
| `talk_poll_interval` | `10` | Seconds between Talk polls |
| `talk_poll_timeout` | `30` | Talk long-poll timeout |
| `talk_poll_wait` | `2.0` | Max wait before processing available rooms |
| `email_poll_interval` | `60` | Seconds between email polls |
| `briefing_check_interval` | `60` | Seconds between briefing/job/cleanup checks |
| `tasks_file_poll_interval` | `30` | Seconds between TASKS.md polls |
| `shared_file_check_interval` | `120` | Seconds between shared file checks |
| `heartbeat_check_interval` | `60` | Seconds between heartbeat checks |
| `db_health_check_interval` | `86400` | Seconds between SQLite `quick_check` + self-heal `REINDEX` sweeps over framework + per-user DBs (24h) |
| `scheduler_stats_interval` | `60` | Seconds between `scheduler_stats` health-line emits (threads / fds / rss / running-tasks / active-workers) — one `key=value` INFO line per interval on the `istota.scheduler.stats` logger, for catching resource leaks early. 0 disables |

### Progress & event streaming

One persisted, typed event stream per task (the `task_events` table) feeds Talk, the web SSE endpoint, the log channel, and push notifications.

| Setting | Default | Description |
|---|---|---|
| `progress_updates` | `true` | Master toggle for Talk progress updates |
| `progress_show_tool_use` | `true` | Emit `tool_start` / `tool_end` events |
| `progress_show_text` | `false` | Emit `progress_text` events (intermediate text; noisy) |
| `event_log_enabled` | `true` | Write events to the `task_events` table (kill-switch for task-event-streaming) |
| `stream_text_gate_chars` | `200` | Narration gate for streamed answer text on stream surfaces (web/REPL). A text run emits no `text_delta` until it crosses this many chars without an intervening tool call, so short lead-in narration ("Let me check…") is discarded at the tool boundary instead of leaking into the answer area. Never loses text — only animation. 0 disables |
| `push_notification_threshold_seconds` | `30` | Min task duration before an ntfy completion push fires |
| `push_notification_sources` | `[]` | Source types that trigger a completion push; empty = ntfy opt-in only (never a default surface) |

### Worker pool

| Setting | Default | Description |
|---|---|---|
| `max_foreground_workers` | `5` | Instance-level fg worker cap |
| `max_background_workers` | `3` | Instance-level bg worker cap |
| `user_max_foreground_workers` | `2` | Global per-user fg default |
| `user_max_background_workers` | `1` | Global per-user bg default |
| `worker_idle_timeout` | `30` | Seconds before idle worker exits |

### Robustness

| Setting | Default | Description |
|---|---|---|
| `task_timeout_minutes` | `30` | Claude Code execution timeout |
| `confirmation_timeout_minutes` | `120` | Auto-cancel confirmations after |
| `stale_pending_warn_minutes` | `30` | Warn for long-pending tasks |
| `stale_pending_fail_hours` | `2` | Auto-fail ancient tasks |
| `worker_heartbeat_seconds` | `60` | How often a running worker pings liveness (0 disables). Stuck-task reclaim uses the heartbeat to tell a slow-but-alive worker from a dead one. |
| `worker_stuck_minutes` | `5` | Reclaim a heartbeating worker's task after this much heartbeat silence. Independent of `task_timeout_minutes`. |
| `task_retention_days` | `7` | Delete old completed tasks |
| `email_retention_days` | `7` | Delete old IMAP emails (0 = disable) |
| `talk_cache_max_per_conversation` | `200` | Max cached Talk messages |
| `scheduled_job_max_consecutive_failures` | `5` | Auto-disable threshold |
| `cron_max_staleness_minutes` | `60` | Skip cron-driven catch-up fires older than this (jobs + briefings). After a long daemon outage, fires missed by more than N minutes are skipped and `last_run_at` is bumped so the schedule resumes from the next future fire. 0 = legacy unconditional catch-up. |
| `log_channel_show_skills` | `true` | Include selected skills in log channel messages |

## `[security]`

| Setting | Default | Description |
|---|---|---|
| `sandbox_enabled` | `true` | Bubblewrap filesystem isolation (Linux only) |
| `sandbox_admin_db_write` | `false` | Allow admin DB writes in sandbox |
| `skill_proxy_enabled` | `true` | Credential proxy via Unix socket |
| `skill_proxy_timeout` | `300` | Proxy command timeout (seconds) |
| `passthrough_env_vars` | `["LANG", "LC_ALL", "LC_CTYPE", "TZ"]` | Extra env vars for subprocess |

### `[security.network]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Network isolation via CONNECT proxy |
| `allow_pypi` | `true` | Allow PyPI access |
| `extra_hosts` | `[]` | Additional allowed hosts |

## `[skills]`

| Setting | Default | Description |
|---|---|---|
| `progressive_disclosure` | `true` | Defer lazy skill bodies to a one-line index and widen that index to the full eligible catalogue (model loads bodies on demand via `istota-skill skills show`). Set `false` for legacy all-eager rendering. |
| `auto_lazy_threshold_chars` | `0` | `>0`: a CLI skill whose body exceeds N chars defaults to lazy (`0` = explicit `disclosure: lazy` frontmatter only) |
| `always_eager` | `["sensitive_actions", "untrusted_input", "files", "scripts", "memory"]` | Skills never deferred (their rules must stay fully in context) |

## `[models.roles]`

Provider-agnostic role aliases that map to brain-specific model identifiers. Used by `!model <role> <prompt>` in Talk and by internal subsystems (`fast` for triage/classification, `general` for sleep cycle, `smart` is user-facing only).

Defaults (when no override is set):

| Role | Default target |
|---|---|
| `fast` | Haiku |
| `general` | Sonnet |
| `smart` | Opus |

Override in config to rebind:

```toml
[models.roles]
smart = "opus-46-high"    # pin smart to Opus 4.6
deep  = "opus-max"        # define a custom role
```

Role aliases never carry effort — `smart = "opus-46-high"` resolves the model to claude-opus-4-6 only; effort tracks the top-level `effort` field (or the per-task override) unless the user types the provider alias directly via `!model opus-46-high <prompt>`. Invalid override targets (neither a known alias nor a canonical `claude-*` ID) are warned at config-load time via `Brain.validate_role_override`.

## `[brain]`

Selects which model-invocation backend the executor uses. See [architecture/brain](../architecture/brain.md) for the protocol and the [native brain runbook](native-brain.md) for the full `[brain.native]` settings.

| Setting | Default | Description |
|---|---|---|
| `kind` | `"claude_code"` | Brain implementation. `"claude_code"` (default) wraps the headless `claude -p` CLI subprocess; `"native"` runs Istota's own in-process agent loop against any OpenAI-compatible model (configured under `[brain.native]`); `"tmux_claude"` drives the interactive `claude` TUI in a detached tmux session to keep traffic on subscription billing (configured under `[brain.tmux]`, with automatic fallback to `claude_code`). |
| `source_type_overrides` | `{}` | Per-`source_type` brain override (e.g. route `scheduled` to `native` while interactive tasks stay on `claude_code`). |

`[brain.native]` (used when `kind = "native"` or a `source_type_overrides` entry routes to it): `provider` (only `"openai_compat"`), `model` (explicit id), `base_url`, `extra_headers`, `context_window`, `max_turns`, `max_tokens`, `prompt_caching`. The API key comes from `ISTOTA_BRAIN_NATIVE_API_KEY`, never the TOML file.

`[brain.tmux]` (used when `kind = "tmux_claude"` or routed-to): every field defaults in code to the prototype's pinned values, so an absent block is behavioral parity. Knobs include `fallback_trip_threshold`, `fallback_cooldown_seconds`, `ready_timeout_seconds`, `tmux_command_timeout`, `cli_version_pin`, and the pane-text marker lists (`ready_markers`, `trust_markers`, `theme_markers`, `bypass_warning_marker`, `bypass_accept_marker`, `error_markers`) — heuristics pinned to a `claude` CLI version, so a CLI reword that breaks readiness detection is a config hotfix, not a code release. See `config.example.toml` for the full annotated block.

## `[sleep_cycle]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable nightly memory extraction |
| `cron` | `"0 2 * * *"` | Schedule (user's timezone) |
| `lookback_hours` | `24` | How far back to gather day data |
| `memory_retention_days` | `0` | Prune dated memory files **and** ephemeral `memory_chunks` rows (`conversation`, `memory_file`, `channel_memory`) older than N days. Durable `user_memory` chunks are not touched. 0 = unlimited |
| `auto_load_dated_days` | `3` | Days of dated memories injected into prompts; 0 disables |
| `curate_user_memory` | `false` | Run op-based USER.md curation after extraction |
| `curation_log_summary` | `true` | Post a one-line summary to the user's `log_channel` after applied curation ops |
| `knowledge_graph_audit_retention_days` | `365` | Prune `knowledge_facts_audit` rows older than N days. Independent of `memory_retention_days`. 0 = unlimited |

## `[channel_sleep_cycle]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable channel memory extraction |
| `cron` | `"0 3 * * *"` | Schedule (UTC) |
| `lookback_hours` | `24` | How far back to gather channel data |
| `memory_retention_days` | `0` | Prune dated channel files and `channel_memory` chunks older than N days. 0 = unlimited |

## `[memory_search]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable memory search |
| `auto_index_conversations` | `true` | Index after task completion |
| `auto_index_memory_files` | `true` | Index after sleep cycle |
| `auto_recall` | `false` | BM25 auto-recall in prompts |
| `auto_recall_limit` | `5` | Max recall results |

## `[briefing_defaults]`

Admin-level defaults expanded when users set `markets = true` or `news = true`:

```toml
[briefing_defaults.news]
lookback_hours = 12
sources = [
    { type = "domain", value = "semafor.com" },
    { type = "email", value = "briefing@nytimes.com" },
]

[briefing_defaults.headlines]
sources = ["ap", "reuters", "guardian", "ft", "aljazeera", "lemonde", "spiegel"]
```

## `[developer]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable developer skill |
| `repos_dir` | `""` | Base directory for clones + worktrees |
| `gitlab_url` | `"https://gitlab.com"` | GitLab instance URL |
| `gitlab_token` | `""` | API token |
| `gitlab_username` | `""` | GitLab username for HTTPS auth |
| `gitlab_default_namespace` | `""` | Default namespace for short repo names |
| `gitlab_reviewer_id` | `""` | User ID for MR reviewer |
| `github_url` | `"https://github.com"` | GitHub instance URL |
| `github_token` | `""` | Personal access token |
| `github_username` | `""` | GitHub username |
| `github_default_owner` | `""` | Default org/user for short repo names |
| `github_reviewer` | `""` | PR reviewer username |

## ntfy push notifications

ntfy is a **per-user connected service** — there is no `[ntfy]` config block. Each user supplies their own server URL, topic, and (optional) auth via the encrypted `secrets` table (see [credentials](credentials.md) for the full per-user credential inventory):

```bash
istota secret ensure --user alice --service ntfy --key topic --value alice-alerts
istota secret ensure --user alice --service ntfy --key server_url --value https://ntfy.example.com
istota secret ensure --user alice --service ntfy --key token --value tk_…
```

Or via the web UI at `/istota/settings` (Connected services → ntfy push). Priority is hardcoded to `3` (the ntfy default).

What it IS: a one-way push channel (bot → device) used by heartbeat alerts and scheduled-job output (`output_target = "ntfy"`). What it ISN'T: two-way (no replies), a Talk replacement, operator-shared infrastructure, or required.

## Money

There is no instance-level `[money]` config section. Money is a **module** (on by default; opt out per user via `disabled_modules = ["money"]`). The bot auto-discovers `*.beancount` files at the top level of `{user_workspace}/ledgers/` — no per-resource path is required. Monarch credentials live in the encrypted `secrets` table (provision via `istota secret ensure --user alice --service monarch --key session_token --value …` or the web settings UI).

## `[google_workspace]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable Google Workspace skill |
| `client_id` | `""` | Google OAuth client ID |
| `client_secret` | `""` | Google OAuth client secret (or `ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET` env var) |
| `scopes` | Drive, Gmail, Calendar, Sheets, Docs | OAuth scopes to request |

See [Google Workspace](../features/google-workspace.md) for setup instructions.

## `[site]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable static site hosting |
| `hostname` | `""` | Public hostname |
| `base_path` | `""` | Local directory for site files |

## `[web]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable web interface |
| `port` | `8766` | Web app port |
| `oauth2_provider` | `""` | Public Nextcloud URL (browser-facing), no trailing slash |
| `oauth2_client_id` | `""` | NC OAuth 2.0 client ID |
| `oauth2_client_secret` | `""` | NC OAuth 2.0 client secret (or `ISTOTA_WEB_OAUTH2_CLIENT_SECRET` env) |
| `oauth2_token_endpoint` | `""` | Optional server-to-server token URL override |
| `oauth2_userinfo_endpoint` | `""` | Optional server-to-server userinfo URL override |
| `oauth2_redirect_uri` | `""` | Explicit redirect URI override; otherwise derived from request |
| `session_secret_key` | `""` | Session signing key (or `ISTOTA_WEB_SESSION_SECRET_KEY` env) |

### `[web.chat]`

Knobs for the in-app web chat surface (the "Chat" tab). The surface is always enabled when the web UI is on; these tune limits and streaming cadence.

| Setting | Default | Description |
|---|---|---|
| `max_prompt_chars` | `32000` | Max characters accepted per chat message |
| `max_attachment_mb` | `25` | Max attachment size, in MB |
| `attachment_extensions` | (image/pdf/text set) | Allowed attachment file extensions |
| `rate_limit_messages` | `30` | Messages allowed per user per window |
| `rate_limit_window_seconds` | `300` | Rate-limit window (5 minutes) |
| `sse_poll_interval_ms` | `200` | Server-side `task_events` poll cadence for the SSE stream |
| `client_poll_interval_ms` | `1500` | Client fallback poll cadence when SSE is unavailable |

## `[location]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable GPS webhook receiver |
| `webhooks_port` | `8765` | Receiver port |
