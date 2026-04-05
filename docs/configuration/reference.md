# Configuration reference

Complete reference for `config/config.toml`. See `config/config.example.toml` in the repository for a commented example.

## Top-level settings

| Setting | Default | Description |
|---|---|---|
| `bot_name` | `"Istota"` | User-facing name (chat, emails, folder names) |
| `emissaries_enabled` | `true` | Include emissaries.md in system prompts |
| `model` | `""` | Claude model override (empty = CLI default) |
| `custom_system_prompt` | `false` | Use config/system-prompt.md instead of Claude Code default |
| `db_path` | `"data/istota.db"` | SQLite database path |
| `rclone_remote` | `"nextcloud"` | rclone remote name |
| `nextcloud_mount_path` | not set | Local mount path (enables mount mode when set) |
| `skills_dir` | `"config/skills"` | Operator skill overrides directory |
| `disabled_skills` | `[]` | Instance-wide skills to exclude |
| `temp_dir` | `"/tmp/istota"` | Temporary directory for task execution |
| `max_memory_chars` | `0` | Cap total memory in prompts (0 = unlimited) |

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

## `[conversation]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable conversation context |
| `lookback_count` | `25` | Messages to consider |
| `skip_selection_threshold` | `3` | Include all if history <= this |
| `selection_model` | `"haiku"` | Model for relevance matching |
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
| `talk_poll_interval` | `10` | Seconds between Talk polls |
| `talk_poll_timeout` | `30` | Talk long-poll timeout |
| `talk_poll_wait` | `2.0` | Max wait before processing available rooms |
| `email_poll_interval` | `60` | Seconds between email polls |
| `briefing_check_interval` | `60` | Seconds between briefing/job/cleanup checks |
| `tasks_file_poll_interval` | `30` | Seconds between TASKS.md polls |
| `shared_file_check_interval` | `120` | Seconds between shared file checks |
| `heartbeat_check_interval` | `60` | Seconds between heartbeat checks |

### Progress updates

| Setting | Default | Description |
|---|---|---|
| `progress_updates` | `true` | Enable Talk progress updates |
| `progress_min_interval` | `8` | Min seconds between updates |
| `progress_max_messages` | `5` | Max updates per task |
| `progress_show_tool_use` | `true` | Show tool descriptions |
| `progress_show_text` | `false` | Show intermediate text |
| `progress_text_max_chars` | `200` | Max chars for text progress |
| `progress_style` | `"replace"` | Display style: replace, full, none |
| `progress_max_display_items` | `20` | Max items in full mode |

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
| `stale_pending_fail_hours` | `24` | Auto-fail ancient tasks |
| `task_retention_days` | `7` | Delete old completed tasks |
| `email_retention_days` | `7` | Delete old IMAP emails (0 = disable) |
| `talk_cache_max_per_conversation` | `200` | Max cached Talk messages |
| `scheduled_job_max_consecutive_failures` | `5` | Auto-disable threshold |

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
| `semantic_routing` | `true` | Enable LLM-based Pass 2 skill selection |
| `semantic_routing_model` | `"haiku"` | Model for classification |
| `semantic_routing_timeout` | `3.0` | Seconds, falls back to Pass 1 |

## `[sleep_cycle]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable nightly memory extraction |
| `cron` | `"0 2 * * *"` | Schedule (user's timezone) |
| `memory_retention_days` | `0` | Auto-delete old files (0 = unlimited) |
| `lookback_hours` | `24` | How far back to look |
| `auto_load_dated_days` | `3` | Days of dated memories to auto-load |
| `curate_user_memory` | `false` | Nightly USER.md curation |

## `[channel_sleep_cycle]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable channel memory extraction |
| `cron` | `"0 3 * * *"` | Schedule (UTC) |
| `lookback_hours` | `24` | How far back to look |
| `memory_retention_days` | `0` | Auto-delete old files (0 = unlimited) |

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

## `[ntfy]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable ntfy notifications |
| `server_url` | `"https://ntfy.sh"` | ntfy server URL |
| `topic` | `""` | Default topic |
| `token` | `""` | Bearer token auth |
| `username` | `""` | Basic auth username |
| `password` | `""` | Basic auth password |
| `priority` | `3` | Default priority (1-5) |

## `[moneyman]`

| Setting | Default | Description |
|---|---|---|
| `cli_path` | `""` | Local CLI binary path |
| `config_path` | `""` | Moneyman config file path |
| `api_url` | `""` | Remote HTTP API URL |
| `api_key` | `""` | API key for remote mode |

## `[google_workspace]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable Google Workspace skill |
| `client_id` | `""` | Google OAuth client ID |
| `client_secret` | `""` | Google OAuth client secret (or `ISTOTA_GOOGLE_CLIENT_SECRET` env var) |
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
| `oidc_issuer` | `""` | Nextcloud OIDC issuer URL |
| `oidc_client_id` | `""` | OAuth client ID |
| `oidc_client_secret` | `""` | OAuth client secret |
| `session_secret_key` | `""` | Session encryption key |

## `[location]`

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable GPS webhook receiver |
| `webhooks_port` | `8765` | Receiver port |
