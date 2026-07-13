# Config Module (`src/istota/config.py`)

## Dataclass Definitions

### `LoggingConfig`
```
level: str = "INFO"          output: str = "console"     file: str = ""
rotate: bool = True          max_size_mb: int = 10       backup_count: int = 5
```

### `NextcloudConfig`
```
url: str = ""                username: str = ""          app_password: str = ""
```

### `TalkConfig`
```
enabled: bool = True         bot_username: str = "istota"
```

### `EmailConfig`
```
enabled: bool = False        imap_host/port/user/password    poll_folder: str = "INBOX"
smtp_host/port/user/password                                 bot_email: str = ""
```
Properties: `effective_smtp_user` (L53), `effective_smtp_password` (L57) — fall back to imap creds

### ntfy push notifications

ntfy is a per-user connected service — there is no global `[ntfy]` block or
`NtfyConfig` dataclass. Each user supplies their own server URL, topic, and
(optional) auth via the encrypted `secrets` table. See "ntfy" under
`secret_schema.CONNECTED_SERVICE_SCHEMA`. Priority is hardcoded to 3 (the
ntfy default); per-call overrides flow through `send_notification(...)`.

### `BrowserConfig`
```
enabled: bool = False        api_url: str = "http://localhost:9223"    vnc_url: str = ""
```

### `DevboxConfig`
```
enabled: bool = False                container_prefix: str = "devbox-"
docker_cli: str = "/usr/bin/docker"  docker_socket: str = "/var/run/docker.sock"  # the *real* host socket (proxy upstream)
exec_timeout_seconds: int = 300      max_output_bytes: int = 102_400
api_proxy_enabled: bool = True       api_proxy_socket_dir: str = "/var/run/istota-docker"
api_proxy_exec_ttl_seconds: int = 300  api_proxy_audit_log: str = ""
```
Per-user persistent Docker container — the agent's escape hatch for tasks the bwrap sandbox can't handle (installing packages, network diagnostics, raw sockets). When `enabled`, the executor exports `ISTOTA_DEVBOX_*` env vars (container name = `f"{container_prefix}{task.user_id}"`) and `build_bwrap_cmd` `--ro-bind`s `docker_cli` into the sandbox so the `devbox` skill CLI can issue `docker exec/cp/inspect/restart` against the user's own container. The **raw root-equivalent socket is no longer bound into the sandbox** — instead, when `api_proxy_enabled`, `build_bwrap_cmd` binds the per-user Docker-API allowlist proxy socket (`{api_proxy_socket_dir}/{user_id}.sock`, served by `src/istota/docker_proxy.py`) at the conventional in-sandbox path `/var/run/docker.sock` (the devbox CLI connects there by default, so it's unchanged). The proxy forwards only the allowlisted ops on the user's own container and refuses container create/run/build/privileged/host-mount, so it's safe to bind unconditionally (no selection gate). `api_proxy_exec_ttl_seconds` sweeps created-but-unstarted exec ids; `api_proxy_audit_log` is an optional file sink for the `istota.docker_proxy.audit` logger. Image is built from `docker/devbox/Dockerfile`; compose runs it under the `devbox` profile (`docker compose --profile devbox up`).

### `ConversationConfig`
```
enabled: bool = True                lookback_count: int = 25
selection_model: str = "fast"       selection_timeout: float = 30.0
skip_selection_threshold: int = 3   use_selection: bool = True
always_include_recent: int = 5      context_truncation: int = 0
context_recency_hours: float = 0    context_min_messages: int = 10
previous_tasks_count: int = 3       talk_context_limit: int = 100
```

### `SchedulerConfig`
See `.claude/rules/scheduler.md` for full table of fields and defaults.

### `SleepCycleConfig`
```
enabled: bool = True               cron: str = "0 2 * * *"
memory_retention_days: int = 0     lookback_hours: int = 24
auto_load_dated_days: int = 3      curate_user_memory: bool = False
curation_log_summary: bool = True
extraction_model: str = "general"  curation_model: str = "general"
knowledge_graph_audit_retention_days: int = 365  # KG audit pruning; independent of memory_retention_days
```

### `ChannelSleepCycleConfig`
```
enabled: bool = True         cron: str = "0 3 * * *"
lookback_hours: int = 24     memory_retention_days: int = 0
```

### `LocationReceiverConfig`
```
enabled: bool = False                    webhooks_port: int = 8765
accuracy_threshold_m: float = 100.0      # pings worse than this skip place match + state machine
visit_exit_minutes: float = 5.0          # continuous away time before closing an open visit
reconcile_enabled: bool = True           # periodic re-derivation of closed visits from pings
reconcile_lookback_hours: float = 6.0    # reconcile window
reconcile_buffer_minutes: float = 10.0   # don't touch pings newer than this (keeps open visit safe)
reconcile_grace_minutes: float = 10.0    # gap between at-place pings before splitting
reconcile_min_pings: int = 3             # walk-by filter
reconcile_min_dwell_sec: int = 60
```

### `SiteConfig`
```
enabled: bool = False        hostname: str = ""           base_path: str = ""
```

### `NetworkConfig`
```
enabled: bool = True         allow_pypi: bool = True      extra_hosts: list[str] = []
```

### `SecurityConfig`
```
sandbox_enabled: bool = True         skill_proxy_enabled: bool = True
sandbox_admin_db_write: bool = False skill_proxy_timeout: int = 300
passthrough_env_vars: list[str] = ["LANG", "LC_ALL", "LC_CTYPE", "TZ"]
network: NetworkConfig = NetworkConfig()
```

### `SkillsConfig` — removed

`SkillsConfig` and the `[skills]` config section are **gone** (no
`progressive_disclosure`, `auto_lazy_threshold_chars`, or `always_eager` knobs).
The two-axis eager/lazy "progressive disclosure" model collapsed into one axis:
a skill is either **eager** (full body in the prompt, because `select_skills`
picked it deterministically) or in the **menu** (a one-line "load on demand"
entry the model pulls in full via `istota-skill skills show <name>`). The menu —
the full eligible catalogue (`eligible_skill_names`) minus the eager set and its
`exclude_skills` — is intrinsic, with no master gate and no per-skill
body-deferral flag, so there are no routing knobs left to configure. A stale
`[skills]` block in `config.toml` logs a warning at load time but doesn't fail.
See `.claude/rules/skills.md` for the single-axis model.

### `PlaybooksConfig`
```
enabled: bool = False        # Part B master gate (learned playbooks / procedural memory)
recall_limit: int = 3        # top-K playbooks injected per task
min_tool_calls: int = 4      # a task must use >= this many tools to qualify (LLM-judged in the extraction prompt)
retention_days: int = 0      # 0 = keep forever; >0 = age-prune by file mtime
max_chars: int = 0           # 0 = share the global max_memory_chars budget
```
Parsed from `[playbooks]`. A playbook is a per-user markdown procedure distilled
by the sleep cycle from a successful multi-step task, stored under the user's
bot `playbooks/` dir, indexed into `memory_chunks` with `source_type="playbook"`,
and recalled by relevance (`executor._recall_playbooks`). Off by default.
`extraction_model` is reused from `[sleep_cycle]` (no new model knob).

### `BrainConfig`
```
kind: str = "claude_code"                       # "claude_code" | "native" | "tmux_claude"
native: NativeBrainConfig                       # [brain.native] block (native harness)
tmux: TmuxBrainConfig                           # [brain.tmux] block (tmux-driven interactive TUI)
source_type_overrides: dict[str, str] = {}      # [brain.source_type_overrides] — per-source-type routing
```
`TmuxBrainConfig` (`[brain.tmux]`): `fallback_trip_threshold` (5),
`fallback_cooldown_seconds` (300), `ready_timeout_seconds` (30),
`tmux_command_timeout` (10), `cli_version_pin` ("2.1.168"), plus the readiness /
dialog / error marker lists (`ready_markers`, `trust_markers`, `theme_markers`,
`bypass_warning_marker`, `bypass_accept_marker`, `error_markers`). All defaulted
to the prototype's hardcoded values; see `.claude/rules/brain.md` "TmuxClaudeBrain".
Selects which `Brain` implementation handles model invocation. `source_type_overrides`
maps a task `source_type` to a brain kind, overriding `kind` for matching tasks
(gradual rollout: cron/heartbeat on native, interactive on claude_code). The
executor routes per task via `brain.resolve_brain_kind(task.source_type, config.brain)`;
unknown target kinds are logged and ignored. See `.claude/rules/brain.md` for the
protocol, ClaudeCodeBrain, NativeBrain, and `NativeBrainConfig` fields.

### `ModelsConfig`
```
roles: dict[str, str] = {}   # operator-rebound role aliases ([models.roles] in TOML)
```
Provider-agnostic role aliases. Defaults (`fast`→Haiku, `general`→Sonnet,
`smart`→Opus) live in the active brain (e.g.
`brain.claude_code.DEFAULT_ROLE_TARGETS`). Operators rebind via
`[models.roles]` — values may be canonical IDs or any provider alias from
the active brain's `MODEL_ALIASES`. `_apply_user_resources` is followed
by `set_role_overrides(config.models.roles)` so every consumer that calls
`brain.resolve_model_name(...)` picks up the operator mapping. Custom
role names beyond the three defaults are accepted (e.g. `deep`,
`cheap`). Every wired field that takes a model name (`selection_model`,
`extraction_model`, `curation_model`, the top-level `model`, per-task
`model`, `[[jobs]] model`) accepts canonical IDs, provider aliases, or
role aliases.

### `ExperimentalConfig`
```
features: list[str] = []     # operator opt-in for rough features ([experimental] features in TOML)
```
Operator-scoped feature flags. Flat list of feature names; off by default.
`is_enabled(feature) -> bool` is the check used by `Config.is_module_enabled`
(via `EXPERIMENTAL_MODULES` in `modules.py`), by the `@requires_feature`
Click decorator (`src/istota/experimental.py`), and by `select_skills` /
`eligible_skill_names` (gated on `skill_<name>` flags).
`load_config()` logs a warning when a configured name isn't in the
`KNOWN_FEATURES` registry but keeps the entry — operators can graduate
features in code without breaking deployments that still list them.
Naming convention: `module_<x>` for module gates, `skill_<x>` for skill
gates, free-form for CLI subcommand gates (`money_tax`, `money_wash_sales`).
See `docs/EXPERIMENTAL.md` for the registry and graduation policy.

### `BriefingConfig`
```
name: str                    cron: str                   conversation_token: str = ""
output: str = "talk"         components: dict = {}
```

### `ResourceConfig`
```
type: str                    path: str = ""              name: str = ""
permissions: str = "read"    base_url: str = ""          api_key: str = ""
extra: dict = {}            # unrecognized TOML keys
```

### `UserConfig`
```
display_name: str = ""                    email_addresses: list[str] = []
timezone: str = "UTC"                     briefings: list[BriefingConfig] = []
resources: list[ResourceConfig] = []
log_channel: str = ""                     # Talk room for verbose execution logs
alerts_channel: str = ""                  # Talk room for confirmations/alerts
site_enabled: bool = False
max_foreground_workers: int = 0           max_background_workers: int = 0  # 0 = use global default
disabled_skills: list[str] = []           # per-user skills to exclude
trusted_email_senders: list[str] = []     # patterns for trusted senders (email gate)
disabled_modules: list[str] = []          # modules to opt out of (default-on otherwise)
email_reply_routing: str = "origin+thread" # email-reply mirror policy: origin+thread | origin | thread
```

`email_reply_routing` is a `user_profiles` column read via `Config.email_reply_routing_for(user_id)` (invalid value → default + warning). It controls where a reply to a bot-sent email is delivered — the origin surface (`web:`/`talk:` descriptor stored on `sent_emails.origin_target`), the email thread, or both. Set via `istota user ensure --email-reply-routing`. See `.claude/rules/transport.md` "Email-reply origin routing".

### `MemorySearchConfig`
```
enabled: bool = True         auto_index_conversations: bool = True
auto_index_memory_files: bool = True
auto_recall: bool = False    auto_recall_limit: int = 5
```

### `DeveloperConfig`
```
enabled: bool = False        repos_dir: str = ""
gitlab_url: str = "https://gitlab.com"
gitlab_token: str = ""       gitlab_username: str = ""
gitlab_default_namespace: str = ""  # Default namespace for short repo names
gitlab_reviewer_id: str = ""
gitlab_api_allowlist: list[str] = [default safe set]  # Endpoint allowlist for API wrapper
github_url: str = "https://github.com"
github_token: str = ""       github_username: str = ""
github_default_owner: str = ""  # Default org/user for short repo names
github_reviewer: str = ""
github_api_allowlist: list[str] = [default safe set]  # Endpoint allowlist for API wrapper
```

### `BriefingDefaultsConfig`
```
markets: dict = {}           news: dict = {}              headlines: dict = {}
```

### `Config`
```
db_path: Path = Path("data/istota.db")
bot_name: str = "Istota"            emissaries_enabled: bool = True
model: str = ""                     # Claude model override; pin to a version like "claude-opus-4-8" so updates don't silently switch us. Empty = CLI default
effort: str = ""                    # Effort level: low/medium/high/xhigh/max (Opus 4.8 + Opus 4.7 + Opus 4.6 + Sonnet 4.6). Empty = model default
custom_system_prompt: bool = False  # Use config/system-prompt.md instead of CC default
nextcloud: NextcloudConfig          talk: TalkConfig
email: EmailConfig                  conversation: ConversationConfig
scheduler: SchedulerConfig          browser: BrowserConfig
devbox: DevboxConfig
logging: LoggingConfig
briefing_defaults: BriefingDefaultsConfig
brain: BrainConfig                          # selects model-invocation backend
security: SecurityConfig
memory_search: MemorySearchConfig   playbooks: PlaybooksConfig
sleep_cycle: SleepCycleConfig
channel_sleep_cycle: ChannelSleepCycleConfig
developer: DeveloperConfig          site: SiteConfig
location: LocationReceiverConfig
models: ModelsConfig                experimental: ExperimentalConfig
users: dict[str, UserConfig] = {}
admin_users: set[str] = set()      # from /etc/istota/admins (empty = all admin)
rclone_remote: str = "nextcloud"
nextcloud_mount_path: Path | None = None
skills_dir: Path = Path("config/skills")
temp_dir: Path = Path("/tmp/istota")
module_data_dir: Path | None = None  # local-disk root for per-user module DBs (feeds/health/location/money); None derives {db_path.parent}/modules. MUST be local (WAL -shm SIGBUSes on the FUSE mount); an explicit value under nextcloud_mount_path is refused
max_memory_chars: int = 0  # cap total memory in prompts (0 = unlimited)
max_knowledge_facts: int = 0  # cap knowledge graph facts per prompt (0 = unlimited)
disabled_skills: list[str] = []    # instance-wide skills to exclude
bundled_skills_dir: Path | None = None  # override for testing
```
Properties / methods:
- `use_mount`: `bool` — True if `nextcloud_mount_path` set
- `module_db_path(user_id, module) -> Path`: local-disk path for a per-user module DB (`{module_data_dir or db_path.parent/modules}/{user}/{module}.db`). The seam each module loader passes as its `db_path=` override; workspace/`data_dir` stays on the mount. Explicit `module_data_dir` under the mount raises `ValueError` (WAL SIGBUS guard); the derived default is trusted-local, unguarded. Single enumerator for `db_health.check_db_health` + `db_backup` + `db_relocate`
- `bot_dir_name`: `str` — sanitized `bot_name` for filesystem use (ASCII lowercase, spaces→underscores)
- `caldav_url`: derived from `nextcloud.url + /remote.php/dav`
- `caldav_username`: `nextcloud.username`
- `caldav_password`: `nextcloud.app_password`
Methods:
- `get_user(nc_username) -> UserConfig | None`
- `is_admin(user_id) -> bool` — True if `admin_users` empty or user in set
- `is_module_enabled(user_id, module) -> bool` — True unless ``module`` appears in the user's `disabled_modules`. Unknown users default to True (docker auto-seed path). Module names are validated against `istota.modules.MODULE_NAMES` (`feeds`, `money`, `location`, `health`); unknown names always return False. Reads from the `user_profiles` DB row when `db_path` is set (so web edits to `disabled_modules` take effect across web/scheduler/webhook processes without SIGHUP), falls back to the in-memory `UserConfig.disabled_modules` for init/test paths or unseeded rows. **Experimental gate**: if `module` appears in `modules.EXPERIMENTAL_MODULES` (currently empty), the method also requires the matching flag to be enabled in `config.experimental.features`; this check runs before the user-profile DB read so a disabled experimental module short-circuits without a DB hit. Surfaces that need to enumerate visible modules (the `/settings/modules` web endpoint, `disabled_modules` profile-write validation in `_coerce_profile_value`) filter against the same gate.
- `find_user_by_email(email_address) -> str | None`
- `is_trusted_email_sender(user_id, sender_email) -> bool` — checks user's own emails + `trusted_email_senders` patterns via fnmatch

## Config Loading

### `load_config()`
Search order: `config/config.toml` → `~/src/config/config.toml` → `~/.config/istota/config.toml` → `/etc/istota/config.toml`

1. Parse TOML file
2. Build each sub-config from sections: `[logging]`, `[nextcloud]`, `[talk]`, `[email]`, `[browser]`, `[conversation]`, `[scheduler]`, `[memory_search]`, `[channel_sleep_cycle]`, `[briefing_defaults]`, `[location]`, `[site]`, `[developer]`
3. Parse `[users.*]` section → `_parse_user_data()` for each
4. Parse `[security]` section → `SecurityConfig`
5. Call `load_admin_users()` → `config.admin_users`
6. Apply env var overrides for secrets (`ISTOTA_NEXTCLOUD_APP_PASSWORD` → `nextcloud.app_password`, etc.)
7. **Phase 6**: `_apply_user_profiles(config)` overlays the `user_profiles` DB table onto `config.users`. Profile-shaped scalar fields (display_name, timezone, log_channel, alerts_channel, site_enabled, max_foreground_workers, max_background_workers) are unconditionally replaced from the DB row when one exists; list fields (email_addresses, disabled_skills, trusted_email_senders) replace TOML only when non-empty (so an auto-seeded blank row doesn't wipe ansible-templated lists). Best-effort: missing/unreadable DB doesn't fail config loading.
8. **Phase 7a**: `_apply_user_resources(config)` overlays the `user_resources` DB table onto `config.users[*].resources`. Each row becomes a `ResourceConfig` entry with extras decoded from JSON. Dedup is keyed on `(type, path)` — DB wins. Distinct paths coexist.
9. **Modules refactor (between 7a and 7b)**: `_migrate_obsolete_resources(config)` first calls `secrets_store.import_from_user_configs` (idempotent — extends `_IMPORT_MAP` to absorb karakeep `base_url`, overland `ingest_token`, monarch creds), then `db.cleanup_obsolete_resources(db_path)` deletes `user_resources` rows whose type is in the retired set (`feeds`, `money`, `monarch`, `moneyman`, `karakeep`, `overland`), then filters those types out of `uc.resources` in memory so the rest of the load cycle sees post-cleanup state.
10. **Phase 7b**: `_apply_user_briefings(config)` overlays the `briefing_configs` DB table onto `config.users[*].briefings`. Each row becomes a `BriefingConfig` entry. Dedup is keyed on `name` — DB wins. Disabled DB rows (`enabled=0`) drop the matching TOML name without scheduling, so the web UI can mute a TOML-templated briefing without re-templating.
11. Return `Config`

**Modules vs resources vs connected services.** Three distinct concepts that used to be conflated under `[[resources]]`:
- **Resources** — paths/identifiers (calendars, folders, todos). Multiple per user. `[[users.X.resources]]` + `user_resources` DB table. Picker types: `calendar`, `folder`, `todo_file`, `notes_folder`, `email_folder`, `reminders_file`.
- **Modules** — on-by-default features with their own UI tab + cog (`feeds`, `money`, `location`). Per-user opt-out via `disabled_modules`. Module names live in `istota.modules.MODULE_NAMES`. Gated everywhere by `Config.is_module_enabled(user_id, module)`.
- **Connected services** — per-user external API credentials (karakeep, google_workspace) consumed by skills. Stored encrypted in the `secrets` table.

**user_profiles.disabled_modules.** New JSON-array column added in Phase 1 of the modules refactor. Migration runs in `_run_migrations` via `ALTER TABLE … ADD COLUMN … DEFAULT '[]'`. Mirrors `disabled_skills` in handling: list-field rule in `merge_into_user_config` (DB row owns the list once it exists; auto-seed carries TOML lists in). Surfaced in the web UI as a multiselect on `/settings → Preferences`; values are validated against `MODULE_NAMES` server-side via `_coerce_profile_value("disabled_modules", …)`.

**Settings split (modules refactor, Phase 2).** `web_app._SERVICE_SCHEMA` is gone. In its place:
- `_CONNECTED_SERVICE_SCHEMA` — services that aren't owned by any single module (`karakeep`, `google_workspace`). Each entry carries `used_by` (skill names) and an optional `oauth: True` flag. Surfaced via `GET /settings/services`.
- `_MODULE_SERVICE_SCHEMA` — per-module schema map (`feeds → {feeds.tumblr_api_key}`, `money → {monarch.*}`, `location → {overland.ingest_token}`). Surfaced via `GET /settings/module-services/{module}` which also returns `module_enabled` so the page can render its banner instead of the config UI when the module is disabled.
- `_all_known_services()` is the union the secret PUT/DELETE handlers validate against — module pages write their secrets through the same `/settings/secrets/{service}/{key}` route.
- `GET /settings/modules` returns `{modules, disabled, enabled_for_user}` for the Preferences card.
- `_service_status` no longer takes `user_resource_types`; status is purely a function of which keys are configured. The old "unavailable when no resource declaration" path is gone — module gating is the new "unavailable" signal and lives behind `is_module_enabled`.
- `/location/settings-info` returns the webhook-URL placeholder (`https://<host>/webhooks/location?token=<token>`) plus read-only place-detection knobs for `/location/settings`. The token is never echoed back to the browser.

**user_profiles table (Phase 6).** Per-user profile fields live in `user_profiles` (one row per user). The scheduler imports any profile-shaped fields from TOML on startup via `user_profiles.import_from_user_configs(db_path, config.users)` (idempotent — only writes rows that don't yet exist). DB row wins at config-load time. The web UI reads/writes via `/istota/api/settings/profile` (GET, PUT). Ansible deploys provision via the `istota user ensure --name <user> ...` CLI (idempotent partial update). See `src/istota/user_profiles.py`.

**user_resources table (Phase 7a).** Per-user resources live in `user_resources` (id PK, `UNIQUE(user_id, resource_type, resource_path)`). The `extras` column is a JSON dict for resource-type-specific config (overland `ingest_token`, money `data_dir`/`ledgers`, feeds `tumblr_api_key`, etc.). At config-load time, `_apply_user_resources` decodes extras and merges DB rows into `config.users[uid].resources` so existing call sites (`executor.py:1738`, `webhook_receiver.py:55`, `money/_loader.py`, `feeds/_loader.py`, `secrets_store._IMPORT_MAP`) read DB and TOML rows uniformly. Web UI reads/writes via `GET/POST /istota/api/settings/resources` and `DELETE /istota/api/settings/resources/{id}`; payload accepts `{type, path, name, permissions, extras}`. Ansible deploys provision via `istota resource ensure --user … --type … [--path …] [--extras key=value | --extras-json '{…}']` (idempotent upsert with `STATE: created|updated|noop` output). The `add_user_resource()` helper preserves existing extras when called without the kwarg and overwrites when an explicit dict (including `{}`) is passed. The CLI's `--extras-clear` flag is the operator-facing equivalent of "explicit empty."

**briefing_configs table (Phase 7b).** Per-user briefings live in `briefing_configs` (id PK, `UNIQUE(user_id, name)`). The `cron_expression` column stores the cron string, `components` is a JSON dict of per-component flags, and `enabled` lets the web UI mute a briefing without deleting it. Output (`talk` / `email` / `both`) is packed into `components.__output__` since the legacy schema has no dedicated column; reads hoist it back into the dataclass. The scheduler imports `[[briefings]]` blocks from TOML on startup via `user_briefings.import_from_user_configs(db_path, config.users)` (idempotent — only writes rows whose `(user_id, name)` pair doesn't already exist). At config-load time, `_apply_user_briefings` merges DB rows into `config.users[uid].briefings` so `check_briefings` and `get_briefings_for_user` (in `skills/briefing`) read DB and TOML rows uniformly. Web UI reads/writes via `GET/POST /istota/api/settings/briefings` and `DELETE /istota/api/settings/briefings/{id}`; payload accepts `{name, cron, conversation_token?, output?, components?, enabled?}`. The GET response also returns a `rooms` list (auto-provisioned `log_channel` + `alerts_channel` tokens) so the UI can offer them as conversation_token picks. Ansible deploys provision via `istota briefing ensure --user … --name … --cron … [--conversation-token …] [--output …] [--components-json '{…}'] [--component k=v] [--disabled]` (idempotent upsert with `STATE: created|updated|noop` output). See `src/istota/user_briefings.py`.

**Secret env var overrides** (applied after TOML, enables `EnvironmentFile=`). Naming convention is `ISTOTA_<SECTION>_<FIELD>` matching the config dataclass path — same convention as docker-compose env vars, so a single env-var name works across both deploy paths. The literal `ISTOTA_SECRET_KEY` (master Fernet key, not a config field) and runtime injection vars (`ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, `ISTOTA_TASK_ID`, etc.) are intentionally outside this convention — they aren't config overrides.

| Env Var | Config Field |
|---|---|
| `ISTOTA_NEXTCLOUD_APP_PASSWORD` | `nextcloud.app_password` |
| `ISTOTA_EMAIL_IMAP_PASSWORD` | `email.imap_password` |
| `ISTOTA_EMAIL_SMTP_PASSWORD` | `email.smtp_password` |
| `ISTOTA_DEVELOPER_GITLAB_TOKEN` | `developer.gitlab_token` |
| `ISTOTA_DEVELOPER_GITHUB_TOKEN` | `developer.github_token` |
| `ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET` | `google_workspace.client_secret` |
| `ISTOTA_WEB_OAUTH2_CLIENT_SECRET` | `web.oauth2_client_secret` |
| `ISTOTA_WEB_SESSION_SECRET_KEY` | `web.session_secret_key` |

### `load_admin_users(path=None) -> set[str]`
Loads admin user IDs from plain text file (one per line, `#` comments, blank lines ignored).
- Check `ISTOTA_ADMINS_FILE` env var, then default `/etc/istota/admins`
- Returns empty set if file missing (all users = admin for backward compat)

### `_parse_user_data()`
Parses user dict → `UserConfig`:
- Parses `[[briefings]]` → `BriefingConfig` list
- Parses `[sleep_cycle]` → `SleepCycleConfig`
- Parses `[[resources]]` → `ResourceConfig` list
- Backward compat: migrates `reminders_file` string to `ResourceConfig(type="reminders_file")`

## UserResource (DB Model, in db.py)
```python
@dataclass
class UserResource:
    id: int
    user_id: str
    resource_type: str      # "calendar", "folder", "todo_file", "email_folder",
                            # "reminders_file", "shared_file", "ledger",
                            # "invoicing", "karakeep", "monarch", "money", "feeds"
    resource_path: str
    display_name: str | None
    permissions: str        # "read" or "readwrite"
```
Note: Uses `resource_name` field alias at executor.py L645 (historical quirk; field is `display_name` on class, but DB column may differ — check actual column name if modifying).

## How to Add a New Config Field

### To an existing sub-config (e.g., SchedulerConfig):
1. Add field with default to dataclass in `config.py`
2. It will auto-load from TOML `[scheduler]` section (matching field name)
3. Update `config.example.toml` with documentation
4. Update Ansible: `defaults/main.yml` + `templates/config.toml.j2`

### To add a new sub-config section:
1. Create new `@dataclass` in `config.py`
2. Add field to `Config` dataclass
3. Add parsing in `load_config()` for the TOML section
4. Update `config.example.toml`, Ansible role

### To add a new per-user field:
1. Add field with default to `UserConfig` dataclass
2. Parse it in `_parse_user_data()` if non-trivial
3. It loads from `[users.NAME.field]` in main config (docker entrypoint path)
4. If profile-shaped, plumb it through `user_profiles` (DB row, web UI, `istota user ensure`)

## How to Add a New Resource Type

1. Choose a `resource_type` string (e.g., `"my_data"`)
2. Users add via: `uv run istota resource add -u USER -t my_data -p /path/to/file`
3. In `executor.py` `execute_task()` (L643-725), add env var mapping:
   ```python
   my_data = [r for r in user_resources if r.resource_type == "my_data"]
   if my_data:
       env["MY_DATA_PATH"] = str(config.nextcloud_mount_path / my_data[0].resource_path.lstrip("/"))
   ```
4. In `build_prompt()` (L180-242), add resource display section if user should see it
5. In `skill.md` frontmatter, add `resource_types: [my_data]` to relevant skill
6. Document in skill markdown file
