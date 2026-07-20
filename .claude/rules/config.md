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

### `WebConfig` (`[web]`) — auth mode + token retention
```
auth: str = "nextcloud"            # "nextcloud" | "none"; env ISTOTA_WEB_AUTH; unknown → warning + "nextcloud"
port: int = 8766
token_storage: str = "ephemeral"   # "ephemeral" | "encrypted"; anything else → warning + ephemeral
```
`auth = "none"` is the local single-user no-auth mode: `web_app._require_api_auth`
early-returns the fixed local user (`Config.local_user_id`), `_user_is_web_admin`
is True for that user, `_verify_origin` no-ops, and `_resolve_session_secret`
generates a random per-process key instead of crashing import. `serve` refuses to
bind no-auth to a non-loopback host (`web_app.assert_no_auth_bind_safe`). Default
`"nextcloud"` = unchanged server behaviour. See AGENTS.md "Local single-user
install".
`"encrypted"` retains the login's user-scoped Nextcloud OAuth pair in the
`web_user_tokens` framework table, encrypted with the **web-only**
`ISTOTA_WEB_TOKEN_KEY` env var (≥32 chars; distinct scrypt salt from the
`ISTOTA_SECRET_KEY` store — see `src/istota/web_tokens.py`). The key is a
runtime env var like `ISTOTA_SECRET_KEY`, *not* a config field, and is
delivered only to the web unit (Ansible `web-secrets.env` /
Docker `/data/.web_token_key`). `"encrypted"` without the key logs one ERROR
at web startup and behaves as ephemeral. Docker-path override:
`ISTOTA_WEB_TOKEN_STORAGE` env var (validated the same way).

### `WebChatConfig` (`[web.chat]`) — read-sync knob
```
talk_read_sync_interval: int = 60   # Talk→web read pull cadence (s); 0 disables the pull
```

### `CaldavConfig` (`[caldav]`)
```
url: str = ""    username: str = ""    password: str = ""
```
Explicit CalDAV override. When any field is set it overrides the value the
`Config.caldav_url` / `caldav_username` / `caldav_password` properties otherwise
derive from `[nextcloud]` — so a local install can point calendar at an external
CalDAV server (Radicale, Fastmail, Google) with no Nextcloud. All-blank (default)
= NC derivation, so server deployments are unchanged. Related: `Config.is_standalone`
(blank `nextcloud.url` + `web.auth == "none"`) and `Config.local_user_id` (the
sole configured user, for no-auth mode).

### `SiteConfig`
```
enabled: bool = False        hostname: str = ""           base_path: str = ""
```
`hostname` is the deployment's public DNS name (web OAuth2 redirect + origin/CSRF
checks + location webhook URL), used regardless of `enabled`. `enabled` +
`base_path` are the bot's own instance-wide static web root, bound RW into the
sandbox so the agent can edit it. Per-user `/~user/` static sites were removed
(ISSUE-171).

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
retention_days: int = 90     # 0 = keep forever; >0 = age-prune by last-use mtime (recall stamps it)
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
fallback: str = ""                              # brain kind to fall back to when primary unavailable
fallback_on_transient: bool = False             # also reroute a persistent transient_api_error
fallback_cooldown_seconds: int = 900            # skip an unavailable primary this long; 0 disables stickiness
```
`fallback` / `fallback_on_transient` / `fallback_cooldown_seconds` drive
availability failover (brain-fallback spec). When the primary brain is
unavailable (usage limit / missing binary / tmux launch failure) the executor
reruns the attempt through the fallback brain with that brain's own settings.
`""` = no fallback (a `tmux_claude` primary still defaults to `claude_code` via
`brain._fallback.effective_fallback_kind`). `_validate_brain_fallback` (config
load) neutralizes an unknown kind or a self-fallback with one WARNING. See
`.claude/rules/brain.md` "Brain fallback" + `.claude/rules/executor.md`.

`TmuxBrainConfig` (`[brain.tmux]`): `fallback_trip_threshold` (5),
`fallback_cooldown_seconds` (300), `ready_timeout_seconds` (30),
`tmux_command_timeout` (10), `cli_version_pin` ("2.1.168"), plus the readiness /
dialog / error / usage-limit marker lists (`ready_markers`, `trust_markers`,
`theme_markers`, `bypass_warning_marker`, `bypass_accept_marker`, `error_markers`,
`usage_limit_markers` — pane substrings → `stop_reason=usage_limit` → fallback,
checked before `error_markers`). All defaulted to the prototype's hardcoded
values; see `.claude/rules/brain.md` "TmuxClaudeBrain".
Selects which `Brain` implementation handles model invocation. `source_type_overrides`
maps a task `source_type` to a brain kind, overriding `kind` for matching tasks
(gradual rollout: cron/heartbeat on native, interactive on claude_code). The
executor routes per task via `brain.resolve_brain_kind(task.source_type, config.brain)`;
unknown target kinds are logged and ignored. See `.claude/rules/brain.md` for the
protocol, ClaudeCodeBrain, NativeBrain, and `NativeBrainConfig` fields.

`NativeBrainConfig` (`[brain.native]`) — model-agnosticism knobs (see `.claude/rules/brain.md` "NativeBrain"):
- `model_overrides: dict = {}` (`[brain.native.model_overrides."<model-id>"]`) — per-model partial `ModelInfo` (any of `context_window`, `max_output_tokens`, `supports_thinking`, `supports_vision`, prices). Applied globally at config load via `llm.catalog.set_model_overrides`, merged over the bundled entry (or the conservative default) in `get_model_info`. Lets a non-Anthropic reasoning/vision or small-window model declare real capabilities instead of being degraded to no-thinking / no-vision / 200k (NB-4). Unknown keys are dropped.
- `compaction_reserve_tokens: int = 0` / `compaction_keep_recent_tokens: int = 0` — compaction sizing; `0` = derive from the model window (`session.compaction.derive_reserve_tokens` / `derive_keep_recent_tokens`, capped at the legacy 16k/20k so a 200k model is unchanged), so a small-window model compacts sensibly instead of using Anthropic-sized constants (NB-14).
- `web_fetch: WebFetchConfig` (`[brain.native.web_fetch]`) — the daemon-side, SSRF-hardened `WebFetch` tool for the native harness (native-only; runs in the daemon netns, not gated by the bwrap CONNECT allowlist). All fields defaulted to safe values, so an absent block enables the tool. Maps 1:1 onto `session.tools.WebFetchPolicy` in `NativeBrain._build_tools`. Fields: `enabled` (True), `timeout_seconds` (20.0), `max_bytes` (5_000_000), `max_content_chars` (100_000), `max_redirects` (5), `allow_http` (False — HTTPS-only), `allowed_ports` ([80, 443]), `user_agent` ("IstotaBot/1.0"), `allow_hosts` ([] = default-open suffix allowlist), `block_hosts` ([]), `extra_blocked_cidrs` ([] — operator additions to the private/reserved IP blocklist), `require_url_provenance` (False — only fetch URLs seen in the task; for sensitive deployments). See `.claude/rules/brain.md` "Native WebFetch tool".

Built-in role aliases (`fast`/`general`/`smart`) resolve to `native.model` on the native brain unless remapped via `[models.roles]` (NB-3) — so stock config's `extraction_model`/`curation_model = "general"` never reaches the wire as a literal alias string.

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
blocks: list[dict] = []      # config-authored rich blocks; in-memory only
```
`blocks` (config-authored-rich-briefing-blocks spec) is the full block/source
authoring shape (`[[users.X.briefings.blocks]]` + `[[...blocks.sources]]`): a raw
dict passthrough parsed in `_parse_user_data`, threaded through
`_apply_user_briefings` (re-attached to the DB-shadowed entry by name) and
`get_briefings_for_user` (carried through the `_expand_boolean_components`
rebuild), and read **once** by the module-DB seeder (`briefings/_migrate.normalize_block_specs`
→ `_seed_blocks`) as an editable baseline. `compare=False`/`repr=False`; it is
**never persisted to `briefing_configs`** (content is module-DB territory, so the
framework row stays byte-unchanged). `blocks` present and non-empty wins over
`components` for seeding; seed-once via the same per-briefing sentinel.

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
- `storage_is_nextcloud`: `bool` — whether a Nextcloud server backs the file workspace. Keyed on `bool(nextcloud.url)`, deliberately **not** `is_standalone` (which folds in web auth, an axis orthogonal to file storage): a URL means the files are Nextcloud whether reached via mount or rclone; no URL means a plain local folder. The single source of truth for storage vocabulary in prompts/skills.
- `storage_backend`: `str` — `"nextcloud"` | `"local"`, derived from `storage_is_nextcloud`.
- `storage_label`: `str` — short noun for prose: `"Nextcloud"` when Nextcloud-backed, else `"your workspace"` (a mid-sentence noun phrase).
- `workspace_root(user_id=None) -> Path | None`: on-disk root of the workspace (mount mode only; `None` under rclone). Scoped to `{mount}/Users/{user_id}` when `user_id` is given, else the bare mount root. De-dups the `mount / "Users" / uid` idiom inlined across the codebase — not a storage abstraction (no I/O, no backend switch).
Methods:
- `get_user(nc_username) -> UserConfig | None`
- `is_admin(user_id) -> bool` — True if `admin_users` empty or user in set
- `available_capabilities() -> set[str]` — backing-service capabilities currently deployed; the single map from a capability name to its config flag (`browser`→`config.browser.enabled`, `devbox`→`config.devbox.enabled`). Drives the skill capability gate: a skill declaring `requires_capability: [name]` whose capability isn't in this set is folded into the effective `disabled_skills` (dropped from selection, the on-demand menu, and shown disabled in `!skills`) via `skills._loader.effective_disabled_skills`. Both flags default off, so `browse`/`devbox` disappear automatically in the standalone install (no headless browser / no devbox container). Adding a service-backed skill = declare the capability here + in the skill frontmatter. See `.claude/rules/skills.md` "Capability gate".
- `is_module_enabled(user_id, module) -> bool` — True unless ``module`` appears in the user's `disabled_modules`. Unknown users default to True (docker auto-seed path). Module names are validated against `istota.modules.MODULE_NAMES` (`feeds`, `money`, `location`, `health`); unknown names always return False. Reads from the `user_profiles` DB row when `db_path` is set (so web edits to `disabled_modules` take effect across web/scheduler/webhook processes without SIGHUP), falls back to the in-memory `UserConfig.disabled_modules` for init/test paths or unseeded rows. **Experimental gate**: if `module` appears in `modules.EXPERIMENTAL_MODULES` (currently empty), the method also requires the matching flag to be enabled in `config.experimental.features`; this check runs before the user-profile DB read so a disabled experimental module short-circuits without a DB hit. **Dependency-availability gate**: if `module` has an install extra declared in `modules.MODULE_DEPENDENCIES` (`money → beancount`) and `modules.module_available(module)` finds the import missing, the method returns False — also before the DB read — so a lean install (e.g. `istota[local]` without beancount) hides the module everywhere instead of half-shipping it and crashing on first use. Surfaces that need to enumerate visible modules (the `/settings/modules` web endpoint, `disabled_modules` profile-write validation in `_coerce_profile_value`) filter against the same gate.
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
7. **Phase 6**: `_apply_user_profiles(config)` overlays the `user_profiles` DB table onto `config.users`. Profile-shaped scalar fields (display_name, timezone, log_channel, alerts_channel, max_foreground_workers, max_background_workers) are unconditionally replaced from the DB row when one exists; list fields (email_addresses, disabled_skills, trusted_email_senders) replace TOML only when non-empty (so an auto-seeded blank row doesn't wipe ansible-templated lists). Best-effort: missing/unreadable DB doesn't fail config loading.
8. **Phase 7a**: `_apply_user_resources(config)` overlays the `user_resources` DB table onto `config.users[*].resources`. Each row becomes a `ResourceConfig` entry with extras decoded from JSON. Dedup is keyed on `(type, path)` — DB wins. Distinct paths coexist.
9. **Modules refactor (between 7a and 7b)**: `_migrate_obsolete_resources(config)` first calls `secrets_store.import_from_user_configs` (idempotent — extends `_IMPORT_MAP` to absorb karakeep `base_url`, overland `ingest_token`, monarch creds), then `db.cleanup_obsolete_resources(db_path)` deletes `user_resources` rows whose type is in the retired set (`feeds`, `money`, `monarch`, `moneyman`, `karakeep`, `overland`), then filters those types out of `uc.resources` in memory so the rest of the load cycle sees post-cleanup state.
10. **Phase 7b**: `_apply_user_briefings(config)` overlays the `briefing_configs` DB table onto `config.users[*].briefings`. Each row becomes a `BriefingConfig` entry. Dedup is keyed on `name` — DB wins. Disabled DB rows (`enabled=0`) drop the matching TOML name without scheduling, so the web UI can mute a TOML-templated briefing without re-templating. **Config-authored `blocks` re-attach**: before dropping TOML briefings claimed by a DB name, it captures `{name: blocks}` from the TOML entries and re-attaches `blocks` onto the appended DB-sourced entry when the name matches (DB rows never carry `blocks`; the field lives only in TOML/`Config`). Without this the module-DB seeder would never see config-authored blocks on a briefing that already has a `briefing_configs` row (every imported TOML briefing gets one after first startup).
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

**Secret env var overrides** (applied after TOML, enables `EnvironmentFile=`). Naming convention is `ISTOTA_<SECTION>_<FIELD>` matching the config dataclass path — same convention as docker-compose env vars, so a single env-var name works across both deploy paths. The literal `ISTOTA_SECRET_KEY` and `ISTOTA_WEB_TOKEN_KEY` (Fernet key sources, not config fields) and runtime injection vars (`ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, `ISTOTA_TASK_ID`, etc.) are intentionally outside this convention — they aren't config overrides. `ISTOTA_WEB_TOKEN_STORAGE` *is* an override (→ `web.token_storage`, value-validated), added for the docker path.

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
