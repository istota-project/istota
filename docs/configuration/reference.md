# Configuration reference

Complete reference for `config/config.toml`. See `config/config.example.toml` in the repository for a commented example.

## Top-level settings

| Setting | Default | Description |
|---|---|---|
| `bot_name` | `"Istota"` | User-facing name (chat, emails, folder names) |
| `emissaries_enabled` | `true` | Include emissaries.md in system prompts |
| `model` | `""` | Claude model override (empty = CLI default). Pin to a version like `"claude-opus-5"`. |
| `effort` | `""` | Effort level: `low`, `medium`, `high`, `xhigh`, or `max` (empty = model default) |
| `advisor_model` | `""` | Advisor model — Anthropic-namespace brains only (`claude_code` / `tmux_claude`); resolves through the same alias table as `model` but carries no effort. Must resolve to a model capable of *being* an advisor (a weak/cheap tier fails every task it runs on). Dropped for any task carrying its own model pin (`!model`, `!room model`, a `[[jobs]] model`). |
| `custom_system_prompt` | `false` | Use config/system-prompt.md instead of Claude Code default. That file — and nothing else in the config directory — is bind-mounted read-only into the sandbox, since the CLI opens it there |
| `namespace` | `"istota"` | Instance namespace — prefixes systemd units, lock paths and similar, so two instances can share a host |
| `db_path` | `"data/istota.db"` | SQLite database path |
| `module_data_dir` | derived | Root for per-user module DBs; defaults to `{db_path.parent}/modules` |
| `rclone_remote` | `"nextcloud"` | rclone remote name |
| `nextcloud_mount_path` | not set | Local mount path (enables mount mode when set) |
| `skills_dir` | `"config/skills"` | Operator skill overrides directory |
| `disabled_skills` | `[]` | Instance-wide skills to exclude |
| `temp_dir` | `"/tmp/istota"` | Temporary directory for task execution |
| `max_memory_chars` | `0` | Cap total memory in prompts (0 = unlimited) |
| `max_knowledge_facts` | `50` | Cap knowledge graph facts per prompt (0 = unlimited) |

## `[nextcloud]`

| Setting | Default | Description |
|---|---|---|
| `url` | `""` | Nextcloud server URL |
| `username` | `""` | Bot's Nextcloud username |
| `app_password` | `""` | Nextcloud app password |
| `share_default_expire_days` | `14` | Default expiry on links the bot creates |

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
| `imap_timeout_seconds` | `30` | IMAP socket timeout |
| `imap_user` | `""` | IMAP username |
| `imap_password` | `""` | IMAP password |
| `smtp_host` | `""` | SMTP server |
| `smtp_port` | `587` | SMTP port |
| `smtp_user` | `""` | SMTP username (defaults to imap_user) |
| `smtp_password` | `""` | SMTP password (defaults to imap_password) |
| `poll_folder` | `"INBOX"` | Folder to poll |
| `bot_email` | `""` | Bot's email address |
| `outbound_approval_floor` | `"untrusted"` | Minimum outbound email approval policy for every user: `off` (never hold) \| `untrusted` (hold unless every recipient is trusted) \| `all` (hold unless every recipient is one of the user's own addresses). A user may tighten past the floor but never loosen below it. An invalid value fails the config load rather than falling back — see [the outbound approval gate](../features/email.md#the-outbound-approval-gate) |
| `confirm_sender_match` | `"off"` | What an own-address claim buys: `off` (the header is proof — assumes the MTA enforces DMARC) \| `verify` (proof only when the MTA's own stamp says so; needs `authserv_id`, and the daemon refuses to load without it) \| `gate` (never proof; every self-sent message is held). Legacy `false`/`true` still load as `off`/`gate` — see [`confirm_sender_match`](../features/email.md#confirm_sender_match) |
| `dmarc_canary` | `true` | Warns when mail routed on a user's own address arrives without a `dmarc=pass` from the receiving MTA. Monitoring for the assumption above; never blocks mail — see [the DMARC canary](../features/email.md#the-dmarc-canary) |
| `dmarc_canary_warn_on_missing` | `false` | Also warn when your MTA's stamp carries no DMARC verdict at all. Off by default because a path that stamps nothing would warn on every message |
| `authserv_id` | `""` | Your receiving MTA's authserv-id — the first field of the `Authentication-Results` header it stamps. Set it and headers from any other authserv-id are discarded rather than read; blank keeps the older topmost-header-only read, which a sender can forge once the MTA stops stamping. Setting it also makes mail arriving without your stamp warn on its own — see [the DMARC canary](../features/email.md#the-dmarc-canary) |

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
| `email_poll_batch_size` | `50` | Messages one email poll walks. The remainder is left for the next tick and drains in arrival order, rather than falling off the end |
| `email_rate_limit_messages` | `60` | Inbound email tasks one user's account will pay for per window. Over-budget mail is filed (`routing_method="throttled"`, left in the mailbox, one alert per window), never dropped. 0 disables |
| `email_sender_rate_limit_messages` | `20` | The same budget narrowed to one correspondent, so a single loud sender throttles alone rather than consuming the user's whole allowance. 0 disables |
| `email_rate_limit_window_seconds` | `3600` | The sliding window both counts run over, and the window the throttle alert and the collapsed confirmation prompts are deduplicated on |
| `email_task_queue` | `background` | Which worker queue inbound mail lands on. Background by default: email is the one surface an unauthenticated stranger can create work on, and the one whose turnaround nobody is watching. `foreground` restores the previous behaviour at the cost of a flood competing with live chat |
| `email_confirmation_prompts_per_window` | `3` | Untrusted-sender confirmation prompts per (user, sender) per window before they collapse into one summary notice. The held mail stays held and individually approvable with `!confirm <task-id>`; only the interruption collapses. 0 = never collapse |
| `email_max_body_chars` | `32000` | The body is interpolated whole into the prompt, so a single large message is its own amplification. Truncated with a marker past this; the full mail stays in the mailbox |
| `email_max_attachment_bytes` | `26214400` | Attachment bytes **written to disk and uploaded** per message (25 MiB). Not a bound on the IMAP transfer — the client fetches and decodes the whole message before any part can be inspected. Whole attachments only: one that would cross the budget is skipped rather than truncated, and the prompt names it. 0 = unlimited |
| `email_max_attachment_bytes_per_poll` | `104857600` | The same budget across one whole poll tick (100 MiB). A per-message cap alone bounds one message and not a batch of fifty. 0 = unlimited |
| `briefing_check_interval` | `60` | Seconds between briefing/job/cleanup checks |
| `tasks_file_poll_interval` | `30` | Seconds between TASKS.md polls |
| `shared_file_check_interval` | `120` | Seconds between shared file checks |
| `heartbeat_check_interval` | `60` | Seconds between heartbeat checks |
| `db_health_check_interval` | `86400` | Seconds between SQLite `quick_check` + self-heal `REINDEX` sweeps over framework + per-user DBs (24h) |
| `scheduler_stats_interval` | `60` | Seconds between `scheduler_stats` health-line emits (threads / fds / rss / running-tasks / active-workers) — one `key=value` INFO line per interval on the `istota.scheduler.stats` logger, for catching resource leaks early. 0 disables |
| `loop_stall_alert_seconds` | `180` | Defense-in-depth: a watchdog thread logs an ERROR and fires one operator alert if the single-threaded main dispatch loop hasn't ticked in this long (a slow call that slipped onto the loop thread, a wedged check), then re-arms when the loop recovers. Suspended around known multi-minute in-loop work (sleep cycles, DB-health sweep) to avoid false pages. 0 disables |

### Progress & event streaming

One persisted, typed event stream per task (the `task_events` table) feeds Talk, the web SSE endpoint, the log channel, and push notifications.

| Setting | Default | Description |
|---|---|---|
| `progress_updates` | `true` | Master toggle for Talk progress updates |
| `progress_show_tool_use` | `true` | Emit `tool_start` / `tool_end` events |
| `progress_show_text` | `false` | Emit `progress_text` events (intermediate text; noisy) |
| `event_log_enabled` | `true` | Write events to the `task_events` table (kill-switch for task-event-streaming) |
| `stream_text_gate_chars` | `280` | Narration gate for streamed answer text on stream surfaces (web/REPL). A text run emits no `text_delta` until it crosses this many chars without an intervening tool call, so short lead-in narration ("Let me check…") is discarded at the tool boundary instead of leaking into the answer area. Never loses text — only animation. 0 disables |
| `push_notification_threshold_seconds` | `30` | Min task duration before an ntfy completion push fires |
| `push_notification_sources` | `[]` | Source types that trigger a completion push; empty = ntfy opt-in only (never a default surface) |

### Worker pool

| Setting | Default | Description |
|---|---|---|
| `max_foreground_workers` | `5` | Instance-level fg worker cap |
| `max_background_workers` | `3` | Instance-level bg worker cap |
| `user_max_foreground_workers` | `2` | Global per-user fg default |
| `user_max_background_workers` | `1` | Global per-user bg default |
| `long_task_threshold_minutes` | `10` | A *running* foreground task older than this stops counting against its user's interactive cap (0 = disabled) |
| `user_max_long_workers` | `1` | Per-user allowance of discounted long tasks; additive, so the per-user fg thread ceiling becomes 3 |
| `max_long_workers` | `2` | Instance-wide budget of discounts, partitioned inside `max_foreground_workers`, which stays the hard thread ceiling |
| `worker_idle_timeout` | `10` | Seconds before idle worker exits |
| `worker_idle_poll_interval` | `0.5` | Idle worker's queue re-check cadence |
| `main_loop_read_timeout_ms` | `2000` | SQLite read timeout on the main loop |

### Robustness

| Setting | Default | Description |
|---|---|---|
| `task_timeout_minutes` | `30` | Claude Code execution timeout |
| `confirmation_timeout_minutes` | `120` | Auto-cancel confirmations after |
| `stale_pending_warn_minutes` | `30` | Warn for long-pending tasks |
| `stale_pending_fail_hours` | `2` | Auto-fail ancient tasks |
| `worker_heartbeat_seconds` | `60` | How often a running worker pings liveness (0 disables). Stuck-task reclaim uses the heartbeat to tell a slow-but-alive worker from a dead one. |
| `worker_stuck_minutes` | `10` | Reclaim a heartbeating worker's task after this much heartbeat silence. Independent of `task_timeout_minutes`. |
| `task_retention_days` | `7` | Delete old completed tasks |
| `usage_retention_days` | `180` | Prune token/cost records. Kept far longer than tasks so spend history survives task cleanup; `0` disables |
| `email_retention_days` | `7` | Delete old IMAP emails (0 = disable) |
| `processed_email_retention_days` | `90` | Prune the processed-email dedup ledger (0 = disable). Floored at `email_retention_days + 1`; disabled entirely when `email_retention_days` is 0 |
| `talk_cache_max_per_conversation` | `200` | Max cached Talk messages |
| `scheduled_job_max_consecutive_failures` | `5` | Auto-disable threshold |
| `cron_max_staleness_minutes` | `60` | Skip cron-driven catch-up fires older than this (jobs + briefings). After a long daemon outage, fires missed by more than N minutes are skipped and `last_run_at` is bumped so the schedule resumes from the next future fire. 0 = legacy unconditional catch-up. |
| `log_channel_show_skills` | `true` | Include selected skills in log channel messages |
| `max_retry_age_minutes` | `60` | A task older than this is failed rather than retried |
| `temp_file_retention_days` | `7` | Delete task temp files older than this |
| `location_ping_retention_days` | `365` | Prune per-user `location.db` pings older than this |

### Subtasks

| Setting | Default | Description |
|---|---|---|
| `max_subtasks_per_task` | `10` | Cap on subtasks one task may queue |
| `max_subtask_depth` | `3` | Cap on subtask nesting depth |
| `max_subtask_prompt_chars` | `8000` | Cap on a subtask's prompt length |

### Database backup

| Setting | Default | Description |
|---|---|---|
| `db_backup_enabled` | `true` | Take timed online-backup snapshots of the local DBs |
| `db_backup_interval` | `86400` | Seconds between snapshots (24h) |
| `db_backup_dir` | `""` | Destination for dated snapshot dirs; empty derives `{nextcloud_mount_path}/istota-db-backups`. Use `db_backup_enabled = false` to disable |
| `db_backup_retention` | `7` | Keep this many snapshot dirs |

### Host memory breadcrumb

| Setting | Default | Description |
|---|---|---|
| `host_pressure_enabled` | `true` | Master switch for host-memory sampling |
| `host_pressure_breadcrumb_interval` | `300` | Seconds between `host_pressure` lines (0 = disabled) |

One line per interval carrying `MemAvailable` / `Shmem` / `SwapFree` / PSI / per-tmpfs usage / `shmem_unaccounted`, written whether or not the host is under pressure — 288 lines a day at the default. See [host memory breadcrumb](../architecture/scheduler.md#host-memory-breadcrumb) for why it is unconditional and what `shmem_unaccounted` answers.

## `[security]`

| Setting | Default | Description |
|---|---|---|
| `sandbox_enabled` | `true` | Bubblewrap filesystem isolation (Linux only) |
| `skill_proxy_enabled` | `true` | Credential proxy via Unix socket. Required wherever `sandbox_enabled` is true — the databases are masked out of the sandbox, so a skill CLI that can't reach the proxy refuses rather than reading nothing |
| `skill_proxy_timeout` | `300` | Proxy command timeout (seconds) |
| `passthrough_env_vars` | `["LANG", "LC_ALL", "LC_CTYPE", "TZ"]` | Extra env vars for subprocess |
| `sandbox_ro_paths` | `[]` | Extra RO bind-mounts in the sandbox, for co-located services. Keep entries narrow — a broad path sweeps in whatever lives under it. The DB directories are masked after this list either way |

`sandbox_admin_db_write` was removed: the framework DB is no longer bound into the sandbox for anyone, so there is no bind left to widen. A stale key logs a warning and is ignored.

### `[security.network]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Network isolation via CONNECT proxy |
| `allow_pypi` | `true` | Allow PyPI access |
| `extra_hosts` | `[]` | Additional allowed hosts |

## `[skills]`

There is no `[skills]` config section. Skill disclosure is single-axis (a skill is either eager or a menu entry the model loads on demand) with no config knobs. A stale `[skills]` block only logs a warning at load time.

## `[models.aliases]`

The operator-visible model alias registry — one table holding **both** the portable tiers and the provider shortcuts, overlaying the code-shipped defaults (`brain.claude_code.DEFAULT_ALIASES`). Used by `!model <name> <prompt>` in Talk/web and by internal subsystems (`fast` for triage/classification, `general` for sleep cycle, `smart` is user-facing only).

Shipped defaults (base names, no baked effort):

| Alias | Default target |
|---|---|
| `fast` | Haiku |
| `general` | Sonnet |
| `smart` | Opus |
| `opus` / `sonnet` / `haiku` | current-latest of each |
| `default` | no override (brain/config default) |

Effort is an orthogonal **`:effort` modifier** appended to any reference (`opus:high`, `smart:low`, `claude-opus-5:xhigh`) — never baked into a name. An alias override is **per model namespace** so one definition covers every brain family: `anthropic` = the CLI brains (`claude_code` / `tmux_claude`), `openai_compat` = native. Two forms, both accepted:

```toml
# Flat (namespace-agnostic, resolved by whichever brain runs the task):
[models.aliases]
smart = "claude-opus-4-6:high"   # pin smart to Opus 4.6, effort high
deep  = "opus:max"               # a custom alias

# Per-namespace (define once, correct on every brain):
[models.aliases.smart]
anthropic     = "opus:high"                                          # CLI brains
openai_compat = { model = "anthropic/claude-opus-4.8", effort = "high" }  # native endpoint slug
[models.aliases.general]
anthropic     = "claude-sonnet-5"
openai_compat = "anthropic/claude-sonnet-4.6"                        # bare string = no effort
[models.aliases.deep]
anthropic     = "opus:max"
openai_compat = "anthropic/claude-opus-4.8"
portable      = true                                                # a cross-brain custom tier
```

Alias targets **carry effort**: a `:effort` modifier on the target (`opus:high` → high) or an explicit `effort =` on a per-namespace table reaches the wire (explicit wins). An alias uses one form (TOML: a key can't be both a string and a table); a per-namespace table missing the active brain's key falls to that brain's code default. A custom alias is a non-portable pin unless flagged `portable = true` (then it re-resolves across the cross-brain fallback boundary like the built-in tiers). Invalid *anthropic* targets (neither a known alias nor a canonical `claude-*` ID) are warned at config-load time via `Brain.validate_alias_override`; `openai_compat` slugs are sent verbatim (no alias table to validate against).

The old `[models.roles]` key is a **hard rename** to `[models.aliases]` — no longer read; a stale one present logs a one-time migration warning. The old effort-in-name forms (`opus-high`, `opus-46`) no longer resolve.

## `[brain]`

Selects which model-invocation backend the executor uses. See [architecture/brain](../architecture/brain.md) for the protocol and the [native brain runbook](native-brain.md) for the full `[brain.native]` settings.

| Setting | Default | Description |
|---|---|---|
| `kind` | `"claude_code"` | Brain implementation. `"claude_code"` (default) wraps the headless `claude -p` CLI subprocess; `"native"` runs Istota's own in-process agent loop against any OpenAI-compatible model (configured under `[brain.native]`); `"tmux_claude"` drives the interactive `claude` TUI in a detached tmux session to keep traffic on subscription billing (configured under `[brain.tmux]`, with automatic fallback to `claude_code`). |
| `source_type_overrides` | `{}` | Per-`source_type` brain override (e.g. route `scheduled` to `native` while interactive tasks stay on `claude_code`). |
| `fallback` | `""` | Brain to rerun a request on when the primary is unavailable |
| `fallback_on_transient` | `true` | Also reroute a persistent `transient_api_error` |
| `fallback_cooldown_seconds` | `900` | Skip an unavailable primary this long before retrying it; 0 disables |

`[brain.native]` (used when `kind = "native"`, when it is the `fallback`, or when a `source_type_overrides` entry routes to it): `provider` (only `"openai_compat"`), `model` (explicit id), `base_url`, `effort`, `model_overrides`, `extra_headers`, `context_window`, `max_turns`, `max_tokens`, `prompt_caching`, `compaction_reserve_tokens`, `compaction_keep_recent_tokens`, `bash_spill_full_output`, `turn_budget_nudge`, `turn_budget_nudge_early_percent`, `turn_budget_nudge_remaining`, `model_catalog_fetch`, `model_catalog_cache_ttl_hours`, plus the nested `[brain.native.web_fetch]` SSRF-policy block. The API key comes from `ISTOTA_BRAIN_NATIVE_API_KEY`, never the TOML file. Full annotations in the [native brain runbook](native-brain.md).

`[brain.tmux]` (used when `kind = "tmux_claude"` or routed-to): every field defaults in code to the prototype's pinned values, so an absent block is behavioral parity. Knobs include `fallback_trip_threshold`, `fallback_cooldown_seconds`, `ready_timeout_seconds`, `tmux_command_timeout`, `cli_version_pin`, and the pane-text marker lists (`ready_markers`, `trust_markers`, `theme_markers`, `bypass_warning_marker`, `bypass_accept_marker`, `error_markers`, `usage_limit_markers` — the last is what drives `stop_reason=usage_limit` and therefore failover) — heuristics pinned to a `claude` CLI version, so a CLI reword that breaks readiness detection is a config hotfix, not a code release. See `config.example.toml` for the full annotated block.

## `[sleep_cycle]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable nightly memory extraction |
| `cron` | `"0 2 * * *"` | Schedule (user's timezone) |
| `lookback_hours` | `24` | How far back to gather day data |
| `memory_retention_days` | `0` | Prune dated memory files **and** ephemeral `memory_chunks` rows (`conversation`, `memory_file`, `channel_memory`) older than N days. Durable `user_memory` chunks are not touched. 0 = unlimited |
| `auto_load_dated_days` | `3` | Days of dated memories injected into prompts; 0 disables |
| `extraction_model` | `"general"` | Role used for the nightly extraction call |
| `curation_model` | `"general"` | Role used for the USER.md curation call |
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
| `recency_half_life_days` | `180.0` | Age half-life for the recency down-weight; 0 disables |

## `[briefings]`

Module-level settings for the briefings module (per-user content store + archive). All defaulted, so an absent block is the shipped behaviour.

| Setting | Default | Description |
|---|---|---|
| `archive_retention_days` | `90` | Prune archived briefing results older than this, on insert (0 = keep forever) |
| `default_lookback_hours` | `12` | Seeds the `email` / `rss` source window when a source omits it |
| `newsletter_max_links_per_source` | `20` | Cap on links pulled from one newsletter source |
| `max_source_chars` | `5000` | Cap on a single source's gathered text. The `todos` source spends it item by item and never cuts one in half — a half-item would read as a todo the file does not contain — dropping from the end and saying in its provenance line how many were left out |
| `max_browse_chars` | `20000` | The same cap for a `browse` source, which gathers markdown rather than flattened text. Bigger because the URLs the markdown keeps cost characters, and a frontpage spends its first couple of thousand on masthead chrome before the headline grid starts. A `browse` source's own `max_chars` wins over either cap — it is the only kind that reads one; `email`, `notes` and `todos` take the module cap directly. |
| `shared_block_timezone` | `"UTC"` | Timezone module-owned shared blocks evaluate their cron in. Shared blocks are global (generated once, no per-user timezone), so this is one operator-chosen zone — typically the operator's own, so morning/evening regeneration lines up with their day. An invalid name falls back to UTC at run time. |

## `[[briefing_shared_blocks]]`

Module-owned shared blocks generated once globally under the reserved `__system__` identity and read by any user's briefing through a `shared_block` source. Seeded once into the `shared_block_configs` table, after which the DB is authoritative and admins manage them from the web UI or `istota briefings shared`. Leave unset for the canonical defaults (`world-headlines`, `markets-summary`); an explicit empty list opts out. Only user-agnostic source kinds are allowed (`browse`, `markets`, `email`). See [briefings](../features/briefings.md#shared-curated-content).

## `[[default_briefings]]`

A canonical shared briefing set, seeded once into each opted-in user (per-user
`default_briefings` flag, default on). Same `name`/`cron`/`output`/`blocks`
shape as a per-user briefing; content is blocks-only. (Replaces the retired
`[briefing_defaults]` boolean-component defaults.)

```toml
[[default_briefings]]
name = "Daily"
cron = "0 7 * * *"
output = "talk"

  [[default_briefings.blocks]]
  title = "World News"
  render_mode = "synthesis"

    [[default_briefings.blocks.sources]]
    kind = "browse"
    config = { preset = "ap" }
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
| `gitlab_reviewer_id` | `""` | GitLab **username** for MR reviewer (`glab mr create --reviewer` takes a username, not the numeric ID the name suggests) |
| `github_url` | `"https://github.com"` | GitHub instance URL |
| `github_token` | `""` | Personal access token |
| `github_username` | `""` | GitHub username |
| `github_default_owner` | `""` | Default org/user for short repo names |
| `github_reviewer` | `""` | PR reviewer username |
| `author_credit` | `""` | Name credited as author on commits the bot makes |
| `forge_cli_extra_denied` | `[]` | Extra verbs the `gh` / `glab` wrapper refuses, written as typed (`"gh repo delete"`); no binary name applies to both |
| `forge_cli_permit` | `[]` | Baseline deny entries to turn off — each one removes a guard |
| `gh_bin_path` | `"/usr/local/bin/gh"` | Real `gh` the wrapper execs. Both deploy shapes render the path they installed to, since neither matches this default: Ansible `/usr/bin/gh` (Debian archive), docker `/usr/local/lib/istota_forge/gh` (off PATH, so the wrapper stays the only `gh` a task can resolve) |
| `glab_bin_path` | `"/usr/local/bin/glab"` | Real `glab` the wrapper execs, rendered the same way |
| `devbox_proxy_enabled` | `true` | Keep tokens host-side behind the devbox proxy |
| `devbox_proxy_socket_dir` | `"/var/run/istota"` | Where the per-user devbox proxy sockets live |
| `devbox_proxy_audit_log` | `""` | Optional path for a devbox proxy audit log |

### `[developer.review]`

The `code_review` skill's models, caps and budget. There is no separate feature flag — the skill is already gated by `developer.enabled` and an admin check, so `enabled = false` here is the off switch.

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Run a review before opening a merge request |
| `conformance_model` | `"general"` | Role alias for the spec-conformance reviewer. A `:effort` modifier is honoured |
| `bughunt_model` | `"smart:high"` | Role alias for the second, skeptical reviewer |
| `both_agents_threshold_lines` | `150` | Diffs at or above this get both reviewers |
| `boundary_patterns` | auth, secret, credential, token, password, migration, schema.sql, billing, payment, money, crypto, sandbox, proxy, deploy, ansible | Case-insensitive substrings matched against changed paths. A hit puts both reviewers on the diff however small it is |
| `max_diff_chars` | `200000` | Cap on the diff handed to a reviewer |
| `max_context_chars` | `60000` | Cap on the assembled surrounding context |
| `max_file_chars` | `20000` | Per changed file, for whole-body inclusion; over it that file falls back to its own hunks |
| `max_callers_per_symbol` | `8` | Cap on caller sites gathered per changed symbol |
| `max_need_files` | `6` | Files a reviewer may request on its one re-invocation. `0` disables the round trip, and the offer is then kept out of the prompt rather than made and refused |
| `timeout_seconds` | `120` | Per agent. Both run concurrently, so this is wall time |
| `max_calls_per_task` | `8` | Review rounds per task |

`max_calls_per_task` counts *waves* of model calls, not `code_review run` invocations. One run charges 1, or 2 when a reviewer took its `max_need_files` round trip; a wave is up to four invocations, since each of two agents may retry a malformed answer once. Guard refusals and breaker skips are free. At the cap the review degrades to `skipped` rather than erroring — a blocking cap would stop a task that had already finished its work from landing it. `0` or less permits no reviews at all rather than reading as "unlimited"; use `enabled = false` to switch the feature off.

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

Money is a **module** (on by default; opt out per user via `disabled_modules = ["money"]`). Per-user money settings live in the per-user money DB, not in `config.toml`; the one instance-level knob is:

| Setting | Default | Purpose |
|---|---|---|
| `autoclass_lookup` | `true` | Allow transaction auto-classification to look up unknown payees |
 The bot auto-discovers `*.beancount` files at the top level of `{user_workspace}/ledgers/` — no per-resource path is required. Monarch credentials are a cookie pair in the encrypted `secrets` table — provision both keys (`session_id` and `csrftoken`) via the CLI or the web settings UI:

```bash
istota secret ensure --user alice --service monarch --key session_id --value …
istota secret ensure --user alice --service monarch --key csrftoken --value …
```

## `[google_workspace]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable Google Workspace skill |
| `client_id` | `""` | Google OAuth client ID |
| `client_secret` | `""` | Google OAuth client secret (or `ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET` env var) |
| `scopes` | Drive, Gmail, Calendar, Sheets, Docs | OAuth scopes to request |

See [Google Workspace](../features/google-workspace.md) for setup instructions.

## `[site]`

`hostname` is load-bearing beyond OAuth2 and the origin check: minting a location ingest token refuses with a 409 while it is unset, because the webhook URL it assembles would be a relative path and the phone's QR decoder accepts only `https://`. A standalone local install with no hostname therefore cannot provision a device by QR.

The deployment's public DNS name.

| Setting | Default | Description |
|---|---|---|
| `hostname` | `""` | Public DNS name; used by the web app for OAuth2 redirect derivation, origin/CSRF checks, and webhook URLs |

The agent-writable static web root (`enabled` / `base_path`) was removed. A publicly-served directory the agent could write to with an ordinary `cp` was an outbound egress channel the confirmation model treated as a benign local write, so anything the agent could read could be published to a public URL without a gate. Serve static assets outside istota, from a directory the agent cannot reach. A stale `enabled` / `base_path` key logs a warning at config load and is ignored.

## `[web]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable web interface |
| `auth` | `"nextcloud"` | Auth mode. `"nextcloud"` is OAuth2 against Nextcloud; `"none"` disables auth entirely for the single-user local install and must never be used on a reachable host. Env override: `ISTOTA_WEB_AUTH` |
| `token_storage` | `"ephemeral"` | Where per-user Nextcloud tokens live. `"ephemeral"` keeps them in the session only; `"encrypted"` retains them in `web_user_tokens` and requires `ISTOTA_WEB_TOKEN_KEY`. Any other value warns and falls back to ephemeral. Env override: `ISTOTA_WEB_TOKEN_STORAGE` |
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
| `max_attachment_mb` | `25` | Max attachment size, in MB. Application default — the Ansible role sets `100` and renders nginx's `client_max_body_size` from the same variable (see below) |
| `attachment_extensions` | `pdf png jpg jpeg heic webp gif txt md csv wav mp3 m4a ogg webm docx xlsx` | Allowed attachment file extensions — images (including `heic`, what an iPhone photo is), documents, text, and the audio formats a voice message arrives in |
| `rate_limit_messages` | `30` | Messages allowed per user per window |
| `rate_limit_window_seconds` | `300` | Rate-limit window (5 minutes) |
| `sse_poll_interval_ms` | `200` | Server-side `task_events` poll cadence for the SSE stream |
| `client_poll_interval_ms` | `1500` | Client fallback poll cadence when SSE is unavailable |
| `talk_read_sync_interval` | `60` | Talk→web read-state pull cadence, seconds (0 disables) |
| `room_stream_poll_interval_ms` | `1000` | Server-side `messages` tail cadence for the live room stream |
| `room_stream_keepalive_seconds` | `20` | SSE comment-frame cadence, so a proxy can't drop an idle stream |
| `room_stream_max_batch` | `500` | Rows before a `gap` frame tells the client to reload instead of replay |
| `room_stream_max_bytes` | `2000000` | Serialized-byte budget for the same guard |
| `room_stream_room_check_seconds` | `10` | Per-connection room-metadata diff cadence (0 disables) |

## `[location]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable GPS webhook receiver |
| `webhooks_port` | `8765` | Receiver port |
| `accuracy_threshold_m` | `100.0` | Discard pings less accurate than this, in metres |
| `visit_exit_minutes` | `5.0` | Minutes away from a place before a visit is closed |
| `reconcile_enabled` | `true` | Batch-reconcile visits from pings, cleaning up state-machine drift |
| `reconcile_lookback_hours` | `6.0` | How far back a reconcile pass looks |
| `reconcile_buffer_minutes` | `10.0` | Buffer around the lookback window |
| `reconcile_grace_minutes` | `10.0` | Grace period before a gap counts as a departure |
| `reconcile_min_pings` | `3` | Minimum pings for a reconstructed visit to count |
| `reconcile_min_dwell_sec` | `60` | Minimum dwell for a reconstructed visit to count |

## `[caldav]`

Explicit CalDAV override. When any field is set it wins over the value derived from `[nextcloud]`, which is how a standalone install points calendar at an external CalDAV server (Radicale, Fastmail, Google) with no Nextcloud in the picture. All-blank — the default — falls back to the Nextcloud derivation, so server deployments are unaffected.

| Setting | Default | Description |
|---|---|---|
| `url` | `""` | CalDAV base URL |
| `username` | `""` | CalDAV username |
| `password` | `""` | CalDAV password |

## `[browser]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable the headless browser container |
| `api_url` | `"http://localhost:9223"` | Browser container's Flask API |
| `vnc_url` | `""` | External noVNC URL, surfaced to the user for observation |

## `[devbox]`

A persistent per-user Linux container — the escape hatch for work the bwrap sandbox can't do (installing packages, network diagnostics, compiling).

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable the devbox skill |
| `container_prefix` | `"devbox-"` | Container name is `{prefix}{user_id}` |
| `docker_cli` | `"/usr/bin/docker"` | Host path to the Docker CLI binary |
| `docker_socket` | `"/var/run/docker.sock"` | Host path to the real Docker socket (the proxy's upstream) |
| `exec_timeout_seconds` | `300` | Default per-exec timeout |
| `max_output_bytes` | `102400` | Cap per output stream |
| `api_proxy_enabled` | `true` | Bind an allowlist proxy at `/var/run/docker.sock` instead of the raw socket |
| `api_proxy_socket_dir` | `"/var/run/istota-docker"` | Where the per-user Docker-API proxy sockets live (distinct from `[developer] devbox_proxy_socket_dir`) |
| `api_proxy_audit_log` | `""` | Optional path for a proxy audit log |

The raw Docker socket is root-equivalent, so it is never bound into the sandbox. The allowlist proxy permits only exec/cp/inspect/restart against the user's own container.

The devbox runs the same real `gh` and `glab` behind the same wrapper the sandbox uses. `docker/devbox/lib/istota_forge_cli.py` is a byte-identical copy of `src/istota/forge_cli.py`, kept in sync by `scripts/sync-devbox-lib.sh`. The wrapper locates its policy beside whichever copy of itself is executing, and takes `real_bin`, the forge URL and the config dirs from that file rather than from `os.environ` — it runs as a child of the model's own shell, so the environment is not a trust anchor. With no URL resolvable it refuses rather than falling back to a public host. The token comes from the [devbox credential proxy](#developer), which injects it server-side.

## `[playbooks]`

Procedural memory — see [memory](../features/memory.md#layer-6-learned-playbooks-procedural-memory).

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Master switch |
| `recall_limit` | `3` | Top-K playbooks injected per task |
| `min_tool_calls` | `4` | Tool calls a task needs to qualify for distillation |
| `retention_days` | `90` | Age-prune by last use; 0 = keep forever |
| `max_chars` | `0` | 0 = share the global `max_memory_chars` budget |

## `[experimental]`

| Setting | Default | Description |
|---|---|---|
| `features` | `[]` | Operator-enabled feature flags. See [experimental features](../EXPERIMENTAL.md) |

## `[health]`

| Setting | Default | Description |
|---|---|---|
| `max_document_bytes` | `26214400` (25 MiB) | Cap on a single stored document (scan, discharge summary, vaccination card). 0 = unlimited |
