"""Configuration loading for istota."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import tomli

logger = logging.getLogger("istota.config")


@dataclass
class LoggingConfig:
    """Logging configuration."""
    level: str = "INFO"           # INFO or DEBUG
    output: str = "console"       # console, file, or both
    file: str = ""                # log file path
    rotate: bool = True           # enable rotation
    max_size_mb: int = 10         # max file size before rotation
    backup_count: int = 5         # rotated files to keep


@dataclass
class NextcloudConfig:
    url: str = ""
    username: str = ""
    app_password: str = ""


@dataclass
class TalkConfig:
    enabled: bool = True
    bot_username: str = "istota"  # istota's Nextcloud username (to filter own messages)


@dataclass
class EmailConfig:
    enabled: bool = False
    # IMAP settings (for receiving)
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    # SMTP settings (for sending) - defaults to IMAP credentials if empty
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # Polling settings
    poll_folder: str = "INBOX"
    bot_email: str = ""  # bot's email address (to skip own messages)
    confirm_sender_match: bool = True  # require confirmation for sender-match routing (prevents From: spoofing)

    @property
    def effective_smtp_user(self) -> str:
        return self.smtp_user or self.imap_user

    @property
    def effective_smtp_password(self) -> str:
        return self.smtp_password or self.imap_password


@dataclass
class BrowserConfig:
    """Browser container configuration."""
    enabled: bool = False
    api_url: str = "http://localhost:9223"
    vnc_url: str = ""  # external noVNC URL for user access


@dataclass
class DevboxConfig:
    """Per-user devbox container — persistent Linux workbench.

    The scheduler exposes an ``istota-skill devbox`` CLI that shells into
    ``devbox-<user_id>`` via the host's Docker socket. Everything else
    (image, network, volume) is provisioned by docker-compose / Ansible.
    """
    enabled: bool = False
    container_prefix: str = "devbox-"           # container name = f"{prefix}{user_id}"
    docker_cli: str = "/usr/bin/docker"         # host path to the Docker CLI binary
    docker_socket: str = "/var/run/docker.sock"  # host path to the Docker socket
    exec_timeout_seconds: int = 300             # default per-exec timeout
    max_output_bytes: int = 102_400             # stdout/stderr cap per stream


@dataclass
class ConversationConfig:
    enabled: bool = True
    lookback_count: int = 25
    selection_model: str = "fast"  # role alias — resolves to HAIKU by default; operator-overridable
    selection_timeout: float = 30.0
    skip_selection_threshold: int = 3  # Include all messages if history ≤ this
    use_selection: bool = True  # If False, include all messages without LLM selection
    always_include_recent: int = 5  # Always include this many recent messages without selection
    context_truncation: int = 0  # Max chars per bot response in context (0 to disable)
    context_recency_hours: float = 0  # Include older messages only if within this window (0 to disable)
    context_min_messages: int = 10  # Always include at least this many recent messages regardless of age
    previous_tasks_count: int = 3  # Number of recent unfiltered tasks to inject into context
    talk_context_limit: int = 100  # Messages to fetch from Talk API for context (max 200)


@dataclass
class SchedulerConfig:
    poll_interval: int = 2  # seconds between task queue checks
    dispatch_interval: float = 0.5  # seconds between pending-task dispatch scans within a poll tick (0 or >= poll_interval = legacy single dispatch per tick)
    email_poll_interval: int = 60  # seconds between email polls
    briefing_check_interval: int = 60  # seconds between briefing checks
    tasks_file_poll_interval: int = 30  # seconds between TASKS.md file polls
    shared_file_check_interval: int = 120  # seconds between shared file organization checks
    heartbeat_check_interval: int = 60  # seconds between heartbeat checks
    db_health_check_interval: int = 86400  # seconds between SQLite quick_check sweeps over per-user DBs
    scheduler_stats_interval: int = 60  # seconds between scheduler_stats health-line emits (0 = disabled)
    talk_poll_interval: int = 10  # seconds between Talk polls
    talk_poll_timeout: int = 30  # long-poll timeout for Talk API
    talk_poll_wait: float = 2.0  # max seconds to wait for all rooms before processing available results
    # Progress / event streaming. The event log (task_events table) is the
    # shared bus for all output surfaces; progress_show_* gate whether the
    # executor adapter emits tool_* / progress_text events at all.
    progress_updates: bool = True          # master toggle for Talk progress
    progress_show_tool_use: bool = True    # emit tool_start / tool_end events
    progress_show_text: bool = False       # emit progress_text events (noisy)
    event_log_enabled: bool = True         # write events to task_events table (kill-switch)
    # Narration gate for streamed answer text (stream surfaces — web/repl). A
    # text run emits no text_delta until it crosses this many chars without an
    # intervening tool call; lead-in narration ("Let me check…") stays under it
    # and is discarded at the tool boundary. Higher = fewer narration leaks but
    # short answers token-stream less (they still arrive whole via `result`);
    # lower = more answers animate but longer narration can leak. Watch the
    # `stream_gate:` logs to tune. 0 disables the gate (legacy: deltas stream
    # immediately, narration can leak).
    stream_text_gate_chars: int = 200
    push_notification_threshold_seconds: int = 30  # min task duration before push fires
    push_notification_sources: list[str] = field(default_factory=list)  # source_types that trigger a push; empty = ntfy opt-in only (never a default surface)
    task_timeout_minutes: int = 30  # kill task execution after this
    # Robustness settings
    confirmation_timeout_minutes: int = 120  # auto-cancel pending_confirmation after this
    stale_pending_warn_minutes: int = 30  # log warning for tasks pending longer than this
    stale_pending_fail_hours: int = 2  # auto-fail tasks pending longer than this
    max_retry_age_minutes: int = 60  # don't retry stuck tasks older than this
    worker_heartbeat_seconds: int = 60  # running worker pings liveness this often (0 disables)
    worker_stuck_minutes: int = 10  # reclaim a heartbeating worker's task after this much heartbeat silence (higher = fewer false-dead reclaims of a slow-but-alive worker, slower genuine-crash recovery)
    task_retention_days: int = 7  # delete completed/failed/cancelled tasks older than this
    email_retention_days: int = 7  # delete emails older than N days from IMAP, 0 to disable
    temp_file_retention_days: int = 7  # delete temp files older than N days, 0 to disable
    worker_idle_timeout: int = 10    # cumulative-idle seconds a worker lingers (re-checking) before exiting
    worker_idle_poll_interval: float = 0.5  # idle re-check cadence (0 or >= worker_idle_timeout = legacy single coarse wait + recheck)
    max_foreground_workers: int = 5  # instance-level foreground (interactive) worker cap
    max_background_workers: int = 3  # instance-level background (scheduled/briefing) worker cap
    user_max_foreground_workers: int = 2  # global per-user fg worker default
    user_max_background_workers: int = 1  # global per-user bg worker default
    scheduled_job_max_consecutive_failures: int = 5  # auto-disable after N failures (0 = never)
    # Insertion-time staleness gate for cron-driven tasks. When the daemon
    # comes back from a long outage, jobs and briefings whose computed
    # next_run is older than this threshold are skipped (last_run_at bumped
    # to now so the schedule resumes cleanly) instead of all firing on the
    # first tick. 0 = unlimited (legacy unconditional catch-up).
    cron_max_staleness_minutes: int = 60
    max_subtasks_per_task: int = 10  # cap deferred subtask creations per task (prompt-injection blast radius)
    max_subtask_depth: int = 3  # reject deferred subtask creation when parent chain is this deep (0 = unlimited)
    max_subtask_prompt_chars: int = 8000  # skip deferred subtasks whose prompt exceeds this (0 = unlimited)
    talk_cache_max_per_conversation: int = 200  # max cached talk messages per conversation
    location_ping_retention_days: int = 365  # delete location pings older than this (0 = unlimited)
    log_channel_show_skills: bool = True  # include selected skills in log channel messages


@dataclass
class SleepCycleConfig:
    """Sleep cycle (nightly memory extraction) configuration."""
    enabled: bool = True
    cron: str = "0 2 * * *"  # 2am in user's timezone
    memory_retention_days: int = 0  # 0 = unlimited retention
    lookback_hours: int = 24
    auto_load_dated_days: int = 3  # auto-load N days of dated memories into prompts (0 = disabled)
    curate_user_memory: bool = False  # nightly USER.md curation from dated memories
    curation_log_summary: bool = True  # post one-line summary to user's log_channel after applied ops
    extraction_model: str = "general"  # role alias — resolves to SONNET by default; operator-overridable
    curation_model: str = "general"  # role alias — resolves to SONNET by default; operator-overridable
    # Independent of memory_retention_days so default deployments still
    # prune the audit table — KG audit rows are tiny but accumulate
    # several per night per user. 0 = unlimited.
    knowledge_graph_audit_retention_days: int = 365


@dataclass
class ChannelSleepCycleConfig:
    """Channel-level sleep cycle (memory extraction from shared conversations)."""
    enabled: bool = True
    cron: str = "0 3 * * *"  # UTC (after user sleep cycles)
    lookback_hours: int = 24
    memory_retention_days: int = 0  # 0 = unlimited retention
    extraction_model: str = "general"  # role alias — resolves to SONNET by default; operator-overridable


@dataclass
class BriefingConfig:
    """Briefing configuration."""
    name: str
    cron: str  # cron expression, evaluated in user's timezone
    conversation_token: str = ""  # Talk room to post to
    output: str = "talk"  # delivery surface(s): talk / email / ntfy or a comma list
    components: dict = field(default_factory=dict)
    # Marks entries appended by ``_apply_user_briefings`` from the DB. The
    # web listing endpoint skips these so post-delete in-memory staleness
    # cannot resurface a removed briefing as "managed=config".
    from_db: bool = field(default=False, repr=False, compare=False)


@dataclass
class ResourceConfig:
    """User resource configuration (defined in per-user TOML files)."""
    type: str           # calendar, folder, todo_file, email_folder, shared_file, reminders_file, notes_folder
    path: str = ""
    name: str = ""
    permissions: str = "read"
    # Service-specific credentials (e.g. karakeep, moneyman)
    base_url: str = ""
    api_key: str = ""
    # Arbitrary extra fields for plugin skills (unrecognized keys go here)
    extra: dict = field(default_factory=dict)
    # Marks entries appended by ``_apply_user_resources`` from the DB. The
    # web listing endpoint skips these so post-delete in-memory staleness
    # cannot resurface a removed row as "managed=config".
    from_db: bool = field(default=False, repr=False, compare=False)
    # Set by the TOML/DB loaders so the migration step can construct obsolete
    # types in flight (it absorbs their credentials into the secrets table
    # and then drops the rows). Tests that bypass load_config see the guard.
    _allow_obsolete: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._allow_obsolete:
            return
        from . import db as _db
        if self.type in _db._OBSOLETE_RESOURCE_TYPES:
            raise ValueError(
                f"ResourceConfig type {self.type!r} was retired by the modules "
                f"refactor. Live data flows through is_module_enabled "
                f"(feeds, money, location) or the encrypted secrets table "
                f"(karakeep, monarch, overland). Update the fixture or pass "
                f"_allow_obsolete=True if this is a load-time migration path."
            )


@dataclass
class UserConfig:
    """Per-user configuration."""
    display_name: str = ""  # friendly name for prompts
    email_addresses: list[str] = field(default_factory=list)  # for email-to-user mapping
    timezone: str = "UTC"  # user's timezone for briefing scheduling
    briefings: list[BriefingConfig] = field(default_factory=list)
    resources: list[ResourceConfig] = field(default_factory=list)
    log_channel: str = ""  # Talk room token for verbose task execution logs
    alerts_channel: str = ""  # Talk room token for confirmations and alerts
    site_enabled: bool = False  # static website hosting at /~user/
    max_foreground_workers: int = 0  # per-user fg worker override (0 = use global default)
    max_background_workers: int = 0  # per-user bg worker override (0 = use global default)
    disabled_skills: list[str] = field(default_factory=list)  # skills to exclude from selection
    trusted_email_senders: list[str] = field(default_factory=list)  # patterns for trusted senders
    disabled_modules: list[str] = field(default_factory=list)  # modules to disable (default-on otherwise)
    routing: dict[str, str] = field(default_factory=dict)  # purpose -> output_target descriptor
    default_destination: str = "talk"  # fallback delivery descriptor
    email_reply_routing: str = "origin+thread"  # origin+thread | origin | thread


@dataclass
class MemorySearchConfig:
    """Memory search configuration."""
    enabled: bool = True
    auto_index_conversations: bool = True
    auto_index_memory_files: bool = True
    auto_recall: bool = False  # BM25 search using task prompt as query
    auto_recall_limit: int = 5  # max results for auto-recall
    # ISSUE-109 #1 — half-life (days) for recency decay applied to recall
    # results so a dense old cluster can't dominate on mass. 0 = no decay.
    recency_half_life_days: float = 180.0


@dataclass
class DeveloperConfig:
    """Developer skill configuration for git + GitLab/GitHub workflows."""
    enabled: bool = False
    repos_dir: str = ""           # Base directory for repo clones/worktrees
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""        # API token (read_api + write_repository scope recommended)
    gitlab_username: str = ""     # GitLab username for HTTPS auth
    gitlab_default_namespace: str = ""  # Default namespace for resolving short repo names (e.g., "myorg")
    gitlab_reviewer_id: str = ""       # GitLab user ID to assign as MR reviewer
    # Patterns are matched against the bare path the shim emits — the
    # devbox proxy strips ``/api/v4`` into the upstream ``base_url``
    # (devbox_proxy.py:handle_gitlab_api) before matching. Don't add the
    # ``/api/v4`` prefix here or every GitLab call will reject as
    # not_allowed. The legacy host-side gitlab-api wrapper used the
    # prefixed form; this codepath is different.
    gitlab_api_allowlist: list[str] = field(default_factory=lambda: [
        "GET /user",
        "GET /projects/*",
        "GET /groups/*",
        "GET /users*",
        "POST /projects/*/merge_requests",
        "POST /projects/*/merge_requests/*/notes",
        "POST /projects/*/issues",
        "POST /projects/*/issues/*/notes",
        "PUT /projects/*/merge_requests/*/merge",
    ])
    github_url: str = "https://github.com"
    github_token: str = ""        # Personal access token (repo scope recommended)
    github_username: str = ""     # GitHub username for HTTPS auth (defaults to x-access-token if empty)
    github_default_owner: str = ""  # Default org/user for resolving short repo names
    github_reviewer: str = ""     # GitHub username to request as PR reviewer
    author_credit: str = ""       # Appended to every commit message (e.g., "Co-Authored-By: Name <email>")
    github_api_allowlist: list[str] = field(default_factory=lambda: [
        "GET /user",
        "GET /repos/*",
        "GET /orgs/*",
        "GET /users/*",
        "GET /search/*",
        "POST /repos/*/pulls",
        "POST /repos/*/pulls/*/reviews",
        "POST /repos/*/issues",
        "POST /repos/*/issues/*/comments",
        "POST /repos/*/pulls/*/comments",
        "PUT /repos/*/pulls/*/merge",
        "PATCH /repos/*/pulls/*",
        "PATCH /repos/*/issues/*",
    ])
    # Devbox credential proxy. See src/istota/devbox_proxy.py + the
    # `devbox-credential-proxy` spec for the design. The proxy injects
    # tokens server-side for the in-container `git`, `gitlab-api`,
    # `github-api`, `gh`, and `glab` wrappers — the container never sees
    # the token.
    api_timeout_seconds: int = 30
    devbox_proxy_enabled: bool = True
    devbox_proxy_socket_dir: str = "/var/run/istota"
    devbox_proxy_audit_log: str = ""   # empty = journal only; set to a path for file fan-out


@dataclass
class LocationReceiverConfig:
    """Location receiver (Overland GPS) configuration."""
    enabled: bool = False
    webhooks_port: int = 8765
    accuracy_threshold_m: float = 100.0  # drop pings with accuracy worse than this from place matching
    visit_exit_minutes: float = 5.0       # continuous "away" time before a visit is closed
    reconcile_enabled: bool = True         # re-derive closed visits from pings periodically
    reconcile_lookback_hours: float = 6.0  # reconcile pings within this window
    reconcile_buffer_minutes: float = 10.0  # don't reconcile pings newer than this (safety margin)
    reconcile_grace_minutes: float = 10.0  # gap between at-place pings before splitting a visit
    reconcile_min_pings: int = 3            # minimum at-place pings to count as a visit
    reconcile_min_dwell_sec: int = 60       # minimum duration (sec) to count as a visit


@dataclass
class WebChatConfig:
    """In-app web chat surface (``[web.chat]``).

    Always-on companion to Talk — there is no per-user opt-out. Knobs cap
    prompt size, attachment size, and the per-user message rate; the poll
    intervals tune the SSE generator cadence and the client polling fallback.
    """
    max_prompt_chars: int = 32000
    max_attachment_mb: int = 25
    attachment_extensions: list[str] = field(default_factory=lambda: [
        "pdf", "png", "jpg", "jpeg", "webp", "gif", "txt", "md",
        "csv", "wav", "mp3", "m4a", "ogg", "docx", "xlsx",
    ])
    rate_limit_messages: int = 30
    rate_limit_window_seconds: int = 300
    sse_poll_interval_ms: int = 200
    client_poll_interval_ms: int = 1500


@dataclass
class WebConfig:
    """Authenticated web interface configuration.

    Auth uses Nextcloud's built-in OAuth2 provider (no extra NC apps required).
    Auth-only flow: code exchange → identity check via OCS → discard token.
    """
    enabled: bool = False
    port: int = 8766
    # `oauth2_provider` is the user-facing NC URL — what the browser hits to
    # authorize. `oauth2_token_endpoint` and `oauth2_userinfo_endpoint` are
    # server-to-server; in Docker they typically point at the internal
    # service URL while `oauth2_provider` points at the host-mapped URL.
    # Empty endpoint overrides default to derivations from `oauth2_provider`.
    oauth2_provider: str = ""
    oauth2_client_id: str = ""
    oauth2_client_secret: str = ""
    oauth2_token_endpoint: str = ""
    oauth2_userinfo_endpoint: str = ""
    oauth2_redirect_uri: str = ""       # explicit override; otherwise derived from request
    token_storage: str = "ephemeral"    # self-documenting; only "ephemeral" supported today
    session_secret_key: str = ""
    chat: WebChatConfig = field(default_factory=WebChatConfig)


@dataclass
class SiteConfig:
    """Static website hosting configuration."""
    enabled: bool = False
    hostname: str = ""        # e.g. "istota.example.com"
    base_path: str = ""       # e.g. "/srv/app/istota/html"


@dataclass
class NetworkConfig:
    """Network isolation via CONNECT proxy (requires sandbox)."""
    enabled: bool = True  # --unshare-net + proxy; false keeps current open-network behavior
    allow_pypi: bool = True  # add pypi.org + files.pythonhosted.org to allowlist
    extra_hosts: list[str] = field(default_factory=list)  # operator-specific additions


@dataclass
class SecurityConfig:
    """Security hardening configuration."""
    sandbox_enabled: bool = True  # bwrap filesystem isolation per user
    sandbox_admin_db_write: bool = False  # allow admin DB writes in sandbox
    skill_proxy_enabled: bool = True  # proxy skill CLI calls via Unix socket
    skill_proxy_timeout: int = 300  # timeout for proxied skill commands (seconds)
    passthrough_env_vars: list[str] = field(default_factory=lambda: [
        "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    ])
    sandbox_ro_paths: list[str] = field(default_factory=lambda: ["/srv/app"])
    network: NetworkConfig = field(default_factory=NetworkConfig)


@dataclass
class GoogleWorkspaceConfig:
    """Google Workspace CLI integration (OAuth-based)."""
    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = field(default_factory=lambda: [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/documents.readonly",
    ])


@dataclass
class MoneymanConfig:
    """Instance-level Moneyman service configuration."""
    cli_path: str = ""  # e.g. "/srv/app/moneyman/app/.venv/bin/moneyman"
    config_path: str = ""  # e.g. "/srv/app/moneyman/app/config.toml"
    api_url: str = ""  # e.g. "http://localhost:8090"
    api_key: str = ""


@dataclass
class SkillsConfig:
    """Skill routing configuration."""
    semantic_routing: bool = True  # enable LLM-based Pass 2 skill classification
    semantic_routing_model: str = "fast"  # role alias — resolves to HAIKU by default; operator-overridable
    semantic_routing_timeout: float = 3.0  # seconds, falls back to Pass 1 on timeout


@dataclass
class NativeBrainConfig:
    """Settings for the native harness (``brain.kind = "native"``).

    The native brain runs istota's own agent loop in-process against an
    ``LLMProvider``. ``provider`` selects the backend; the rest configure it.

    - ``provider`` — ``"openai_compat"``: any OpenAI chat-completions endpoint
      (Anthropic, OpenRouter, Ollama, …). The only provider; the field stays so
      the layer can grow new backends without a config break.
    - ``model`` — explicit model id (``openai_compat`` does no aliasing).
    - ``base_url`` / ``api_key`` / ``extra_headers`` — for ``openai_compat``.
      ``api_key`` is populated from the ``ISTOTA_BRAIN_NATIVE_API_KEY`` env
      override (kept out of the TOML file).
    - ``context_window`` — 0 resolves from the bundled model catalog; set to
      override per deployment.
    - ``max_turns`` — hard cap on assistant turns per task (loop backstop).
    - ``max_tokens`` — per-completion output cap.
    """

    provider: str = "openai_compat"  # only "openai_compat"
    model: str = ""
    effort: str = ""  # native-brain default effort: low/medium/high/xhigh/max (empty = none)
    base_url: str = "https://api.anthropic.com/v1"
    api_key: str = ""  # from ISTOTA_BRAIN_NATIVE_API_KEY at load time
    extra_headers: dict = field(default_factory=dict)
    context_window: int = 0  # 0 = resolve from istota.llm.catalog
    max_turns: int = 100
    max_tokens: int = 16384
    # Opt-in cache_control breakpoints (Anthropic/OpenRouter). Tri-state: ``None``
    # (the operator set no explicit value) derives the default from base_url in
    # make_provider — on for api.anthropic.com, off elsewhere. An explicit
    # ``True``/``False`` always wins, whether it came from the TOML or was set
    # directly on the dataclass.
    prompt_caching: bool | None = None


@dataclass
class BrainConfig:
    """Selects which brain implementation handles model invocation.

    ``"claude_code"`` (default) wraps the ``claude`` CLI subprocess.
    ``"native"`` runs istota's own agent loop in-process; its settings live in
    the nested ``native`` block (``[brain.native]`` in TOML).

    ``source_type_overrides`` maps a task ``source_type`` (``scheduled``,
    ``heartbeat``, ``talk``, …) to a brain kind, overriding ``kind`` for
    matching tasks. This is the gradual-rollout knob: move cron/heartbeat to
    the native brain while interactive tasks stay on ``claude_code``. Set in
    TOML as ``[brain.source_type_overrides]``.
    """
    kind: str = "claude_code"
    native: NativeBrainConfig = field(default_factory=NativeBrainConfig)
    source_type_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class BriefingDefaultsConfig:
    """Admin-level defaults for briefing components (expanded when user sets boolean)."""
    markets: dict = field(default_factory=dict)
    news: dict = field(default_factory=dict)
    headlines: dict = field(default_factory=dict)


@dataclass
class ModelsConfig:
    """Operator-controlled model role aliases.

    The default role mapping (``fast``→Haiku, ``general``→Sonnet,
    ``smart``→Opus for ``ClaudeCodeBrain``) lives on the active brain in
    ``brain.claude_code.DEFAULT_ROLE_TARGETS``. Operators set
    ``[models.roles]`` in TOML to rebind any role to a different canonical
    ID or provider alias — e.g., a deployment that wants to stay on Opus
    4.6 in prod can write ``smart = "opus-46-high"`` here and every call
    site that reads ``smart`` follows. Role names beyond the three
    defaults are accepted, so operators can introduce custom roles like
    ``deep`` or ``cheap``.
    """

    roles: dict[str, str] = field(default_factory=dict)


@dataclass
class ExperimentalConfig:
    """Operator-scoped feature flags. See ``src/istota/experimental.py``."""

    features: list[str] = field(default_factory=list)

    def is_enabled(self, feature: str) -> bool:
        return feature in self.features


@dataclass
class Config:
    namespace: str = "istota"  # Install namespace (drives /etc/{namespace}/, /srv/app/{namespace}/, etc.)
    bot_name: str = "Istota"  # User-facing name (used in chat, emails, folder names)
    emissaries_enabled: bool = True  # Include config/emissaries.md in system prompt
    model: str = ""  # Model ID or alias; pin to a versioned ID (e.g. "claude-opus-4-8") rather than a floating alias so upgrades are explicit. Empty = brain default
    effort: str = ""  # Effort level: low, medium, high, xhigh, max. Empty = model default. Support varies by model
    max_memory_chars: int = 0  # cap total memory in prompts (0 = unlimited)
    max_knowledge_facts: int = 50  # cap knowledge graph facts per prompt (0 = unlimited)
    db_path: Path = field(default_factory=lambda: Path("data/istota.db"))
    nextcloud: NextcloudConfig = field(default_factory=NextcloudConfig)
    talk: TalkConfig = field(default_factory=TalkConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    conversation: ConversationConfig = field(default_factory=ConversationConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    devbox: DevboxConfig = field(default_factory=DevboxConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    briefing_defaults: BriefingDefaultsConfig = field(default_factory=BriefingDefaultsConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    memory_search: MemorySearchConfig = field(default_factory=MemorySearchConfig)
    sleep_cycle: SleepCycleConfig = field(default_factory=SleepCycleConfig)
    channel_sleep_cycle: ChannelSleepCycleConfig = field(default_factory=ChannelSleepCycleConfig)
    developer: DeveloperConfig = field(default_factory=DeveloperConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    site: SiteConfig = field(default_factory=SiteConfig)
    location: LocationReceiverConfig = field(default_factory=LocationReceiverConfig)
    moneyman: MoneymanConfig = field(default_factory=MoneymanConfig)
    google_workspace: GoogleWorkspaceConfig = field(default_factory=GoogleWorkspaceConfig)
    web: WebConfig = field(default_factory=WebConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    experimental: ExperimentalConfig = field(default_factory=ExperimentalConfig)
    users: dict[str, UserConfig] = field(default_factory=dict)  # nc_username -> UserConfig
    admin_users: set[str] = field(default_factory=set)  # users with full system access
    rclone_remote: str = "nextcloud"  # rclone remote name
    nextcloud_mount_path: Path | None = None  # If set, use mount instead of rclone CLI
    skills_dir: Path = field(default_factory=lambda: Path("config/skills"))
    bundled_skills_dir: Path | None = None  # Override bundled skills dir (for testing)
    disabled_skills: list[str] = field(default_factory=list)  # instance-wide skills to exclude
    custom_system_prompt: bool = False  # Use config/system-prompt.md instead of Claude Code's default
    temp_dir: Path = field(default_factory=lambda: Path("/tmp/istota"))
    config_path: Path | None = None  # Set by load_config() to the file actually loaded

    @property
    def bot_dir_name(self) -> str:
        """Lowercase bot name used for Nextcloud folder names.

        Spaces replaced with underscores, non-ASCII/non-alphanumeric chars stripped.
        e.g. "Mister Jones" -> "mister_jones", "My-Bot 2" -> "my-bot_2"
        """
        import re
        name = self.bot_name.lower().strip()
        name = re.sub(r'\s+', '_', name)
        name = re.sub(r'[^a-z0-9_\-]', '', name)
        return name or "istota"

    @property
    def use_mount(self) -> bool:
        """Whether to use local mount instead of rclone CLI."""
        return self.nextcloud_mount_path is not None

    def get_user(self, nc_username: str) -> UserConfig | None:
        """Get user config by Nextcloud username. Returns None if user not configured."""
        return self.users.get(nc_username)

    def find_user_by_email(self, email_address: str) -> str | None:
        """Find user_id by email address. Returns None if not found."""
        email_lower = email_address.lower()
        for user_id, user_config in self.users.items():
            if email_lower in [e.lower() for e in user_config.email_addresses]:
                return user_id
        return None

    def is_trusted_email_sender(
        self, user_id: str, sender_email: str, conn: "sqlite3.Connection | None" = None,
    ) -> bool:
        """Check if sender is trusted for the given user.

        Trusted = user's own email addresses OR matches trusted_email_senders
        config patterns OR exists in runtime trusted_email_senders DB table.
        """
        from fnmatch import fnmatch

        user = self.users.get(user_id)
        if not user:
            return False

        sender_lower = sender_email.lower()

        if sender_lower in [e.lower() for e in user.email_addresses]:
            return True

        for pattern in user.trusted_email_senders:
            if fnmatch(sender_lower, pattern.lower()):
                return True

        # Check runtime-managed trusted senders in DB
        if conn is not None:
            from . import db
            if db.is_sender_trusted_in_db(conn, user_id, sender_lower):
                return True

        return False

    def email_reply_routing_for(self, user_id: str) -> str:
        """Per-user mirror policy for email replies to messages we sent.

        One of ``origin+thread`` (default — deliver to the origin surface AND
        continue the email thread), ``origin`` (origin surface only), or
        ``thread`` (email only). An unrecognized stored value falls back to the
        default and logs a warning.
        """
        valid = ("origin+thread", "origin", "thread")
        user = self.users.get(user_id)
        value = (getattr(user, "email_reply_routing", "") or "").strip() if user else ""
        if not value:
            return "origin+thread"
        if value not in valid:
            logger.warning(
                "Unknown email_reply_routing %r for user %s; using 'origin+thread'",
                value, user_id,
            )
            return "origin+thread"
        return value

    @property
    def caldav_url(self) -> str:
        """CalDAV base URL derived from Nextcloud URL."""
        if not self.nextcloud.url:
            return ""
        base = self.nextcloud.url.rstrip("/")
        return f"{base}/remote.php/dav"

    @property
    def caldav_username(self) -> str:
        """CalDAV username (same as Nextcloud username)."""
        return self.nextcloud.username

    @property
    def caldav_password(self) -> str:
        """CalDAV password (same as Nextcloud app password)."""
        return self.nextcloud.app_password

    def effective_user_max_fg_workers(self, user_id: str) -> int:
        """Effective max fg workers for a user (per-user override > global default)."""
        uc = self.get_user(user_id)
        if uc and uc.max_foreground_workers > 0:
            return uc.max_foreground_workers
        return self.scheduler.user_max_foreground_workers

    def effective_user_max_bg_workers(self, user_id: str) -> int:
        """Effective max bg workers for a user (per-user override > global default)."""
        uc = self.get_user(user_id)
        if uc and uc.max_background_workers > 0:
            return uc.max_background_workers
        return self.scheduler.user_max_background_workers

    def is_module_enabled(
        self,
        user_id: str,
        module: str,
        *,
        conn: "sqlite3.Connection | None" = None,
    ) -> bool:
        """Check whether a module is enabled for a user.

        Modules are on by default. Returns False only when the user has an
        explicit ``disabled_modules`` entry for this module. Unknown users
        default to True so docker auto-seeding doesn't block first-login
        access.

        Reads from the ``user_profiles`` DB row when a DB is configured so
        that web / scheduler / skill subprocesses all see the same value
        without waiting for a config reload. Falls back to the in-memory
        ``UserConfig`` for the init / test paths where the DB may not exist
        yet, or when the row hasn't been seeded.

        Pass ``conn`` to reuse an existing framework-DB connection — hot
        per-tick loops in the scheduler already hold one and would
        otherwise open a fresh sqlite connection per call (the FD churn
        that produced "unable to open database file" / EMFILE).
        """
        from .modules import EXPERIMENTAL_MODULES, MODULE_NAMES
        if module not in MODULE_NAMES:
            logger.debug("is_module_enabled: unknown module %r", module)
            return False

        # Experimental modules stay dark until the operator opts in via
        # `[experimental] features = ["module_<name>"]`. This runs before
        # the per-user opt-out check so a disabled experimental module
        # short-circuits without a DB lookup.
        flag = EXPERIMENTAL_MODULES.get(module)
        if flag and not self.experimental.is_enabled(flag):
            return False

        if self.db_path is not None and Path(self.db_path).exists():
            try:
                from . import user_profiles as _up
                profile = _up.get_profile(Path(self.db_path), user_id, conn=conn)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("is_module_enabled DB read failed: %s", e)
                profile = None
            if profile is not None:
                return module not in (profile.disabled_modules or [])

        uc = self.users.get(user_id)
        if uc is None:
            return True
        return module not in (uc.disabled_modules or [])

    def resolve_user_timezone(
        self,
        user_id: str,
        *,
        conn: "sqlite3.Connection | None" = None,
    ) -> str:
        """Return the user's timezone string (IANA name), never empty.

        Prefers the live ``user_profiles`` DB row over the in-memory
        ``UserConfig`` so web-UI timezone edits take effect on the next task
        without a scheduler restart (ISSUE-099). Falls back to the in-memory
        config, then to ``"UTC"``. Mirrors ``is_module_enabled``'s DB-read
        pattern, including the optional ``conn`` for hot loops that already
        hold a framework-DB connection (avoids per-call FD churn on the
        FUSE-backed mount).

        Does NOT validate the zone name — callers that need a ``tzinfo`` wrap
        the result in ``ZoneInfo`` and own the invalid-name fallback, so this
        helper stays usable by code that only needs the string.
        """
        tz_str = ""
        if self.db_path is not None and Path(self.db_path).exists():
            try:
                from . import user_profiles as _up
                profile = _up.get_profile(Path(self.db_path), user_id, conn=conn)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("resolve_user_timezone DB read failed: %s", e)
                profile = None
            if profile is not None:
                tz_str = profile.timezone or ""

        if not tz_str:
            uc = self.users.get(user_id)
            tz_str = (uc.timezone if uc else "") or "UTC"
        return tz_str

    def is_admin(self, user_id: str) -> bool:
        """Check if user has admin privileges.

        Returns True if no admins file exists (empty set = all users are admin
        for backward compatibility), or if user_id is in the admin set.
        """
        if not self.admin_users:
            return True
        return user_id in self.admin_users


def load_admin_users(path: str | None = None) -> set[str]:
    """Load admin user IDs from a plain-text file.

    File format: one user ID per line, # comments, blank lines ignored.
    Returns empty set if file doesn't exist. Empty-set semantics are
    asymmetric: Config.is_admin treats empty as "all users admin" for
    legacy back-compat, while the web admin dashboard fails closed.

    Args:
        path: Override file path. If None, checks ISTOTA_ADMINS_FILE env var,
              then falls back to /etc/istota/admins. The default path is
              wrong for renamed-namespace installs (e.g. /etc/zorg/admins);
              such deploys must set ISTOTA_ADMINS_FILE in every entry-point
              systemd unit. A WARNING is logged when the resolved path is
              missing so silent fail-closed admin in the web UI is visible
              in the journal.
    """
    explicit_path = path is not None
    env_var_set = "ISTOTA_ADMINS_FILE" in os.environ
    if path is None:
        path = os.environ.get("ISTOTA_ADMINS_FILE", "/etc/istota/admins")
    admins_path = Path(path)
    if not admins_path.exists():
        if not explicit_path:
            if env_var_set:
                logger.warning(
                    "admins_file_missing path=%s (ISTOTA_ADMINS_FILE set but file absent — "
                    "web admin dashboard will fail closed)",
                    path,
                )
            else:
                # DEBUG, not INFO: fires on every subprocess config load
                # (feeds/money facades call load_config()) where
                # ISTOTA_ADMINS_FILE isn't propagated. The env-var-set-but-
                # missing case above stays WARNING — that's a real misconfig.
                logger.debug(
                    "admins_file_default_missing path=%s (ISTOTA_ADMINS_FILE not set; "
                    "no web admins will be recognized)",
                    path,
                )
        return set()
    admins = set()
    for line in admins_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            admins.add(line)
    return admins


def _parse_user_data(user_data: dict, user_id: str) -> UserConfig:
    """Parse a user data dict (from main config or per-user file) into UserConfig."""
    # Parse briefings
    briefings = []
    for b in user_data.get("briefings", []):
        briefings.append(BriefingConfig(
            name=b.get("name", ""),
            cron=b.get("cron", ""),
            conversation_token=b.get("conversation_token", ""),
            output=b.get("output", "talk"),
            components=b.get("components", {}),
        ))

    # Parse resources
    _resource_known_keys = {"type", "path", "name", "permissions", "base_url", "api_key"}
    resources = []
    for r in user_data.get("resources", []):
        extra = {k: v for k, v in r.items() if k not in _resource_known_keys}
        resources.append(ResourceConfig(
            type=r.get("type", ""),
            path=r.get("path", ""),
            name=r.get("name", ""),
            permissions=r.get("permissions", "read"),
            base_url=r.get("base_url", ""),
            api_key=r.get("api_key", ""),
            extra=extra,
            _allow_obsolete=True,
        ))

    # Backward-compat: migrate reminders_file string to a resource
    reminders_file = user_data.get("reminders_file", "")
    if reminders_file:
        resources.append(ResourceConfig(
            type="reminders_file",
            path=reminders_file,
            name="Reminders",
            permissions="read",
        ))

    # Parse credential sections as resources (server-side only, not synced to Nextcloud)
    monarch_data = user_data.get("monarch", {})
    if monarch_data.get("session_token"):
        resources.append(ResourceConfig(
            type="monarch",
            name="Monarch Money",
            extra={k: v for k, v in monarch_data.items()},
            _allow_obsolete=True,
        ))

    return UserConfig(
        display_name=user_data.get("display_name", user_id),
        email_addresses=user_data.get("email_addresses", []),
        timezone=user_data.get("timezone", "UTC"),
        briefings=briefings,
        resources=resources,
        log_channel=user_data.get("log_channel", ""),
        alerts_channel=user_data.get("alerts_channel", ""),
        site_enabled=user_data.get("site_enabled", False),
        max_foreground_workers=user_data.get("max_foreground_workers", 0),
        max_background_workers=user_data.get("max_background_workers", 0),
        disabled_skills=user_data.get("disabled_skills", []),
        trusted_email_senders=user_data.get("trusted_email_senders", []),
        disabled_modules=user_data.get("disabled_modules", []),
        routing=dict(user_data.get("routing", {}) or {}),
        default_destination=user_data.get("default_destination", "talk") or "talk",
    )


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from TOML file."""
    if config_path is None:
        # `ISTOTA_CONFIG_PATH` lets a parent process (e.g. the scheduler)
        # propagate its loaded config to subprocesses whose cwd no longer
        # contains the relative `config/config.toml` candidate.
        env_path = os.environ.get("ISTOTA_CONFIG_PATH")
        if env_path:
            candidate = Path(env_path)
            if candidate.exists():
                config_path = candidate

    if config_path is None:
        # Look for config in standard locations
        candidates = [
            Path("config/config.toml"),
            Path.home() / "src/config/config.toml",
            Path.home() / ".config/istota/config.toml",
            Path("/etc/istota/config.toml"),
        ]
        for candidate in candidates:
            try:
                if candidate.exists():
                    config_path = candidate
                    break
            except PermissionError:
                continue

    if config_path is None or not config_path.exists():
        # Return default config
        return Config()

    with open(config_path, "rb") as f:
        data = tomli.load(f)

    config = Config()
    config.config_path = config_path

    if "namespace" in data:
        config.namespace = data["namespace"]

    if "bot_name" in data:
        config.bot_name = data["bot_name"]

    if "emissaries_enabled" in data:
        config.emissaries_enabled = data["emissaries_enabled"]

    if "model" in data:
        config.model = data["model"]

    if "effort" in data:
        config.effort = data["effort"]

    if "max_memory_chars" in data:
        config.max_memory_chars = data["max_memory_chars"]
    if "max_knowledge_facts" in data:
        config.max_knowledge_facts = data["max_knowledge_facts"]

    if "db_path" in data:
        config.db_path = Path(data["db_path"])

    if "rclone_remote" in data:
        config.rclone_remote = data["rclone_remote"]

    if "nextcloud_mount_path" in data:
        config.nextcloud_mount_path = Path(data["nextcloud_mount_path"])

    if "skills_dir" in data:
        config.skills_dir = Path(data["skills_dir"])

    if "disabled_skills" in data:
        config.disabled_skills = data["disabled_skills"]

    if "custom_system_prompt" in data:
        config.custom_system_prompt = data["custom_system_prompt"]

    if "temp_dir" in data:
        config.temp_dir = Path(data["temp_dir"])

    if "nextcloud" in data:
        nc = data["nextcloud"]
        config.nextcloud = NextcloudConfig(
            url=nc.get("url", ""),
            username=nc.get("username", ""),
            app_password=nc.get("app_password", ""),
        )

    if "talk" in data:
        talk = data["talk"]
        config.talk = TalkConfig(
            enabled=talk.get("enabled", True),
            bot_username=talk.get("bot_username", "istota"),
        )

    if "users" in data:
        for nc_username, user_data in data["users"].items():
            config.users[nc_username] = _parse_user_data(user_data, nc_username)

    if "email" in data:
        email = data["email"]
        config.email = EmailConfig(
            enabled=email.get("enabled", False),
            imap_host=email.get("imap_host", ""),
            imap_port=email.get("imap_port", 993),
            imap_user=email.get("imap_user", ""),
            imap_password=email.get("imap_password", ""),
            smtp_host=email.get("smtp_host", ""),
            smtp_port=email.get("smtp_port", 587),
            smtp_user=email.get("smtp_user", ""),
            smtp_password=email.get("smtp_password", ""),
            poll_folder=email.get("poll_folder", "INBOX"),
            bot_email=email.get("bot_email", ""),
        )

    if "conversation" in data:
        conv = data["conversation"]
        config.conversation = ConversationConfig(
            enabled=conv.get("enabled", True),
            lookback_count=conv.get("lookback_count", 10),
            selection_model=conv.get("selection_model", "fast"),
            selection_timeout=conv.get("selection_timeout", 30.0),
            skip_selection_threshold=conv.get("skip_selection_threshold", 3),
            use_selection=conv.get("use_selection", True),
            always_include_recent=conv.get("always_include_recent", 5),
            context_truncation=conv.get("context_truncation", 0),
            context_recency_hours=conv.get("context_recency_hours", 0),
            context_min_messages=conv.get("context_min_messages", 10),
            previous_tasks_count=conv.get("previous_tasks_count", 3),
            talk_context_limit=conv.get("talk_context_limit", 100),
        )

    if "scheduler" in data:
        sched = data["scheduler"]
        config.scheduler = SchedulerConfig(
            poll_interval=sched.get("poll_interval", 5),
            dispatch_interval=sched.get("dispatch_interval", 0.5),
            email_poll_interval=sched.get("email_poll_interval", 60),
            briefing_check_interval=sched.get("briefing_check_interval", 60),
            tasks_file_poll_interval=sched.get("tasks_file_poll_interval", sched.get("istota_file_poll_interval", 30)),
            shared_file_check_interval=sched.get("shared_file_check_interval", 120),
            heartbeat_check_interval=sched.get("heartbeat_check_interval", 60),
            db_health_check_interval=sched.get("db_health_check_interval", 86400),
            scheduler_stats_interval=sched.get("scheduler_stats_interval", 60),
            talk_poll_interval=sched.get("talk_poll_interval", 10),
            talk_poll_timeout=sched.get("talk_poll_timeout", 30),
            talk_poll_wait=sched.get("talk_poll_wait", 2.0),
            progress_updates=sched.get("progress_updates", True),
            progress_show_tool_use=sched.get("progress_show_tool_use", True),
            progress_show_text=sched.get("progress_show_text", False),
            event_log_enabled=sched.get("event_log_enabled", True),
            stream_text_gate_chars=sched.get("stream_text_gate_chars", 200),
            push_notification_threshold_seconds=sched.get("push_notification_threshold_seconds", 30),
            push_notification_sources=sched.get("push_notification_sources", []),
            task_timeout_minutes=sched.get("task_timeout_minutes", 30),
            confirmation_timeout_minutes=sched.get("confirmation_timeout_minutes", 120),
            stale_pending_warn_minutes=sched.get("stale_pending_warn_minutes", 30),
            stale_pending_fail_hours=sched.get("stale_pending_fail_hours", 2),
            max_retry_age_minutes=sched.get("max_retry_age_minutes", 60),
            worker_heartbeat_seconds=sched.get("worker_heartbeat_seconds", 60),
            worker_stuck_minutes=sched.get("worker_stuck_minutes", 10),
            task_retention_days=sched.get("task_retention_days", 7),
            email_retention_days=sched.get("email_retention_days", 7),
            temp_file_retention_days=sched.get("temp_file_retention_days", 7),
            worker_idle_timeout=sched.get("worker_idle_timeout", 10),
            worker_idle_poll_interval=sched.get("worker_idle_poll_interval", 0.5),
            scheduled_job_max_consecutive_failures=sched.get("scheduled_job_max_consecutive_failures", 5),
            cron_max_staleness_minutes=sched.get("cron_max_staleness_minutes", 60),
            max_foreground_workers=sched.get("max_foreground_workers", 5),
            max_background_workers=sched.get("max_background_workers", 3),
            user_max_foreground_workers=sched.get("user_max_foreground_workers", 2),
            user_max_background_workers=sched.get("user_max_background_workers", 1),
        )

    if "browser" in data:
        br = data["browser"]
        config.browser = BrowserConfig(
            enabled=br.get("enabled", False),
            api_url=br.get("api_url", "http://localhost:9223"),
            vnc_url=br.get("vnc_url", ""),
        )

    if "devbox" in data:
        dx = data["devbox"]
        config.devbox = DevboxConfig(
            enabled=dx.get("enabled", False),
            container_prefix=dx.get("container_prefix", "devbox-"),
            docker_cli=dx.get("docker_cli", "/usr/bin/docker"),
            docker_socket=dx.get("docker_socket", "/var/run/docker.sock"),
            exec_timeout_seconds=dx.get("exec_timeout_seconds", 300),
            max_output_bytes=dx.get("max_output_bytes", 102_400),
        )

    if "ntfy" in data:
        logger.warning(
            "[ntfy] block in config.toml is no longer used — ntfy is now per-user "
            "and configured via the secrets table (web settings or `istota secret`)."
        )

    if "skills" in data:
        sk = data["skills"]
        config.skills = SkillsConfig(
            semantic_routing=sk.get("semantic_routing", True),
            semantic_routing_model=sk.get("semantic_routing_model", "fast"),
            semantic_routing_timeout=sk.get("semantic_routing_timeout", 3.0),
        )

    if "brain" in data:
        br = data["brain"]
        native_raw = br.get("native", {})
        if not isinstance(native_raw, dict):
            native_raw = {}
        native = NativeBrainConfig(
            provider=native_raw.get("provider", "openai_compat"),
            model=native_raw.get("model", ""),
            effort=native_raw.get("effort", ""),
            base_url=native_raw.get("base_url", "https://api.anthropic.com/v1"),
            api_key=native_raw.get("api_key", ""),
            extra_headers=dict(native_raw.get("extra_headers", {}) or {}),
            context_window=int(native_raw.get("context_window", 0)),
            max_turns=int(native_raw.get("max_turns", 100)),
            max_tokens=int(native_raw.get("max_tokens", 16384)),
            # Absent key → None (derive from base_url); present → explicit bool.
            prompt_caching=(
                bool(native_raw["prompt_caching"])
                if "prompt_caching" in native_raw
                else None
            ),
        )
        overrides_raw = br.get("source_type_overrides", {})
        if not isinstance(overrides_raw, dict):
            overrides_raw = {}
        config.brain = BrainConfig(
            kind=br.get("kind", "claude_code"),
            native=native,
            source_type_overrides={
                str(k): str(v) for k, v in overrides_raw.items()
            },
        )

    # [models] table — operator-controlled role aliases. The mapping is
    # parsed here, then applied globally below via brain._roles.set_role_overrides
    # after every other config layer has settled. Each brain consults the
    # global override table inside its own resolve_alias() call.
    if "models" in data:
        models_section = data["models"]
        roles = models_section.get("roles", {}) if isinstance(models_section, dict) else {}
        if not isinstance(roles, dict):
            roles = {}
        config.models = ModelsConfig(roles={str(k): str(v) for k, v in roles.items()})

    if "experimental" in data:
        exp = data["experimental"]
        feats = exp.get("features", []) if isinstance(exp, dict) else []
        if not isinstance(feats, list):
            feats = []
        config.experimental = ExperimentalConfig(features=[str(f) for f in feats])
        from .experimental import KNOWN_FEATURES
        for f in config.experimental.features:
            if f not in KNOWN_FEATURES:
                logger.warning(
                    "[experimental] unknown feature %r — typo or stale flag", f,
                )

    if "briefing_defaults" in data:
        bd = data["briefing_defaults"]
        config.briefing_defaults = BriefingDefaultsConfig(
            markets=bd.get("markets", {}),
            news=bd.get("news", {}),
            headlines=bd.get("headlines", {}),
        )

    if "logging" in data:
        log = data["logging"]
        config.logging = LoggingConfig(
            level=log.get("level", "INFO"),
            output=log.get("output", "console"),
            file=log.get("file", ""),
            rotate=log.get("rotate", True),
            max_size_mb=log.get("max_size_mb", 10),
            backup_count=log.get("backup_count", 5),
        )

    if "memory_search" in data:
        ms = data["memory_search"]
        config.memory_search = MemorySearchConfig(
            enabled=ms.get("enabled", True),
            auto_index_conversations=ms.get("auto_index_conversations", True),
            auto_index_memory_files=ms.get("auto_index_memory_files", True),
            auto_recall=ms.get("auto_recall", False),
            auto_recall_limit=ms.get("auto_recall_limit", 5),
        )

    if "sleep_cycle" in data:
        sc = data["sleep_cycle"]
        config.sleep_cycle = SleepCycleConfig(
            enabled=sc.get("enabled", False),
            cron=sc.get("cron", "0 2 * * *"),
            memory_retention_days=sc.get("memory_retention_days", 0),
            lookback_hours=sc.get("lookback_hours", 24),
            auto_load_dated_days=sc.get("auto_load_dated_days", 3),
            curate_user_memory=sc.get("curate_user_memory", False),
            curation_log_summary=sc.get("curation_log_summary", True),
        )

    if "channel_sleep_cycle" in data:
        csc = data["channel_sleep_cycle"]
        config.channel_sleep_cycle = ChannelSleepCycleConfig(
            enabled=csc.get("enabled", True),
            cron=csc.get("cron", "0 3 * * *"),
            lookback_hours=csc.get("lookback_hours", 24),
            memory_retention_days=csc.get("memory_retention_days", 0),
        )

    if "site" in data:
        s = data["site"]
        config.site = SiteConfig(
            enabled=s.get("enabled", False),
            hostname=s.get("hostname", ""),
            base_path=s.get("base_path", ""),
        )

    if "location" in data:
        loc = data["location"]
        config.location = LocationReceiverConfig(
            enabled=loc.get("enabled", False),
            webhooks_port=loc.get("webhooks_port", 8765),
            accuracy_threshold_m=loc.get("accuracy_threshold_m", 100.0),
            visit_exit_minutes=loc.get("visit_exit_minutes", 5.0),
            reconcile_enabled=loc.get("reconcile_enabled", True),
            reconcile_lookback_hours=loc.get("reconcile_lookback_hours", 6.0),
            reconcile_buffer_minutes=loc.get("reconcile_buffer_minutes", 10.0),
            reconcile_grace_minutes=loc.get("reconcile_grace_minutes", 10.0),
            reconcile_min_pings=loc.get("reconcile_min_pings", 3),
            reconcile_min_dwell_sec=loc.get("reconcile_min_dwell_sec", 60),
        )

    if "moneyman" in data:
        mm = data["moneyman"]
        config.moneyman = MoneymanConfig(
            cli_path=mm.get("cli_path", ""),
            config_path=mm.get("config_path", ""),
            api_url=mm.get("api_url", ""),
            api_key=mm.get("api_key", ""),
        )

    if "google_workspace" in data:
        gw = data["google_workspace"]
        config.google_workspace = GoogleWorkspaceConfig(
            enabled=gw.get("enabled", False),
            client_id=gw.get("client_id", ""),
            client_secret=gw.get("client_secret", ""),
            scopes=gw.get("scopes", GoogleWorkspaceConfig().scopes),
        )

    if "web" in data:
        w = data["web"]
        chat_data = w.get("chat", {})
        _chat_defaults = WebChatConfig()
        web_chat = WebChatConfig(
            max_prompt_chars=chat_data.get("max_prompt_chars", _chat_defaults.max_prompt_chars),
            max_attachment_mb=chat_data.get("max_attachment_mb", _chat_defaults.max_attachment_mb),
            attachment_extensions=chat_data.get(
                "attachment_extensions", _chat_defaults.attachment_extensions
            ),
            rate_limit_messages=chat_data.get("rate_limit_messages", _chat_defaults.rate_limit_messages),
            rate_limit_window_seconds=chat_data.get(
                "rate_limit_window_seconds", _chat_defaults.rate_limit_window_seconds
            ),
            sse_poll_interval_ms=chat_data.get("sse_poll_interval_ms", _chat_defaults.sse_poll_interval_ms),
            client_poll_interval_ms=chat_data.get(
                "client_poll_interval_ms", _chat_defaults.client_poll_interval_ms
            ),
        )
        config.web = WebConfig(
            enabled=w.get("enabled", False),
            port=w.get("port", 8766),
            oauth2_provider=w.get("oauth2_provider", ""),
            oauth2_client_id=w.get("oauth2_client_id", ""),
            oauth2_client_secret=w.get("oauth2_client_secret", ""),
            oauth2_token_endpoint=w.get("oauth2_token_endpoint", ""),
            oauth2_userinfo_endpoint=w.get("oauth2_userinfo_endpoint", ""),
            oauth2_redirect_uri=w.get("oauth2_redirect_uri", ""),
            token_storage=w.get("token_storage", "ephemeral"),
            session_secret_key=w.get("session_secret_key", ""),
            chat=web_chat,
        )

    if "developer" in data:
        dev = data["developer"]
        extra = {}
        if "gitlab_api_allowlist" in dev:
            extra["gitlab_api_allowlist"] = dev["gitlab_api_allowlist"]
        if "github_api_allowlist" in dev:
            extra["github_api_allowlist"] = dev["github_api_allowlist"]
        config.developer = DeveloperConfig(
            enabled=dev.get("enabled", False),
            repos_dir=dev.get("repos_dir", ""),
            gitlab_url=dev.get("gitlab_url", "https://gitlab.com"),
            gitlab_token=dev.get("gitlab_token", ""),
            gitlab_username=dev.get("gitlab_username", ""),
            gitlab_default_namespace=dev.get("gitlab_default_namespace", ""),
            gitlab_reviewer_id=dev.get("gitlab_reviewer_id", ""),
            github_url=dev.get("github_url", "https://github.com"),
            github_token=dev.get("github_token", ""),
            github_username=dev.get("github_username", ""),
            github_default_owner=dev.get("github_default_owner", ""),
            github_reviewer=dev.get("github_reviewer", ""),
            api_timeout_seconds=dev.get("api_timeout_seconds", 30),
            devbox_proxy_enabled=dev.get("devbox_proxy_enabled", True),
            devbox_proxy_socket_dir=dev.get("devbox_proxy_socket_dir", "/var/run/istota"),
            devbox_proxy_audit_log=dev.get("devbox_proxy_audit_log", ""),
            **extra,
        )

    if "security" in data:
        sec = data["security"]
        net_data = sec.get("network", {})
        network_config = NetworkConfig(
            enabled=net_data.get("enabled", True),
            allow_pypi=net_data.get("allow_pypi", True),
            extra_hosts=net_data.get("extra_hosts", []),
        )
        config.security = SecurityConfig(
            sandbox_enabled=sec.get("sandbox_enabled", True),
            sandbox_admin_db_write=sec.get("sandbox_admin_db_write", False),
            skill_proxy_enabled=sec.get("skill_proxy_enabled", True),
            skill_proxy_timeout=sec.get("skill_proxy_timeout", 300),
            network=network_config,
            **({
                "passthrough_env_vars": sec["passthrough_env_vars"]
            } if "passthrough_env_vars" in sec else {}),
        )

    config.admin_users = load_admin_users()

    # Environment variable overrides for secrets (allows EnvironmentFile= usage).
    # Naming convention: ISTOTA_<SECTION>_<FIELD>, matching the config dataclass
    # path. Same convention as docker-compose env vars — single source of truth
    # for "where does this credential come from" across all deploy paths.
    _env_secret_overrides = [
        ("ISTOTA_NEXTCLOUD_APP_PASSWORD", "nextcloud", "app_password"),
        ("ISTOTA_EMAIL_IMAP_PASSWORD", "email", "imap_password"),
        ("ISTOTA_EMAIL_SMTP_PASSWORD", "email", "smtp_password"),
        ("ISTOTA_DEVELOPER_GITLAB_TOKEN", "developer", "gitlab_token"),
        ("ISTOTA_DEVELOPER_GITHUB_TOKEN", "developer", "github_token"),
        ("ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET", "google_workspace", "client_secret"),
        ("ISTOTA_WEB_OAUTH2_CLIENT_SECRET", "web", "oauth2_client_secret"),
        ("ISTOTA_WEB_SESSION_SECRET_KEY", "web", "session_secret_key"),
    ]
    for env_var, section, field_name in _env_secret_overrides:
        val = os.environ.get(env_var)
        if val:
            setattr(getattr(config, section), field_name, val)

    # Native-brain API key lives two levels deep (brain.native.api_key), so it
    # doesn't fit the flat section/field table above.
    _native_key = os.environ.get("ISTOTA_BRAIN_NATIVE_API_KEY")
    if _native_key:
        config.brain.native.api_key = _native_key

    # Phase 6: overlay profile fields from the user_profiles table.
    # DB rows replace the matching scalar fields on TOML-loaded UserConfig
    # entries; briefings stay TOML-owned. Users that exist only
    # in the DB (no TOML entry) get a synthesised UserConfig.
    _apply_user_profiles(config)

    # Phase 7a: overlay user_resources rows onto config.users[*].resources.
    # DB rows win over TOML for matching (type, path); distinct (type, path)
    # pairs coexist. Existing call sites (executor merge, webhook_receiver,
    # money/feeds loaders, secrets_store import) keep reading
    # ``config.users[uid].resources`` unchanged.
    _apply_user_resources(config)

    # Modules refactor: absorb credentials from `[[resources]]` blocks for
    # types that have been retired (karakeep base_url, overland.ingest_token,
    # etc.) into the secrets table, then drop those rows from user_resources
    # and from the in-memory ``uc.resources`` lists so the rest of the load
    # cycle sees the post-cleanup state.
    _migrate_obsolete_resources(config)

    # Phase 7b: overlay briefing_configs rows onto config.users[*].briefings.
    # DB rows replace TOML rows of the same ``name``; distinct names coexist.
    # ``check_briefings`` and ``get_briefings_for_user`` keep reading
    # ``user_config.briefings`` unchanged.
    _apply_user_briefings(config)

    # Apply operator role-alias overrides globally so every downstream call
    # to ``brain.resolve_model_name`` / ``brain.resolve_alias`` picks up the
    # operator's mapping. Done last so it sees any TOML edits.
    #
    # Per-entry semantic validation is delegated to the active brain (it
    # knows its own provider alias namespace) so operators see typos
    # surfaced at startup rather than at task time.
    from .brain import make_brain, set_role_overrides
    if config.models.roles:
        _logger = logging.getLogger("istota.config")
        _brain = make_brain(config.brain)
        for _role, _target in config.models.roles.items():
            for _msg in _brain.validate_role_override(_role, _target):
                _logger.warning("[models.roles] %s", _msg)
    set_role_overrides(config.models.roles)

    return config


def _apply_user_profiles(config: "Config") -> None:
    """Merge ``user_profiles`` rows into ``config.users``.

    Best-effort: a missing/unreadable DB does not fail config loading
    (callers like ``istota init`` run before the DB exists). The DB wins for
    profile-shaped fields; TOML keeps resources and briefings.
    """
    try:
        from . import user_profiles as _up  # avoid import cycles at module load
    except Exception:  # pragma: no cover - defensive
        return

    db_path = config.db_path
    if db_path is None or not Path(db_path).exists():
        return

    try:
        rows = _up.list_profiles(Path(db_path))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("user_profiles overlay skipped: %s", e)
        return

    for user_id, profile in rows.items():
        existing = config.users.get(user_id)
        if existing is None:
            existing = UserConfig(display_name=profile.display_name or user_id)
            config.users[user_id] = existing
        _up.merge_into_user_config(profile, existing)


def _apply_user_resources(config: "Config") -> None:
    """Merge ``user_resources`` rows into ``config.users[*].resources``.

    DB rows are appended as ``ResourceConfig`` entries so every existing call
    site that walks ``user_config.resources`` (executor merge,
    webhook_receiver, money/feeds loaders, secrets_store import) sees
    DB-managed resources transparently. Dedup key is ``(type, path)`` — DB
    wins, matching the user_profiles precedence rule.

    Best-effort: a missing DB does not fail config loading.
    """
    try:
        from . import db as _db
    except Exception:  # pragma: no cover - defensive
        return

    db_path = config.db_path
    if db_path is None or not Path(db_path).exists():
        return

    user_ids: set[str] = set(config.users.keys())
    try:
        with _db.get_db(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT user_id FROM user_resources"
            ).fetchall()
            user_ids.update(r["user_id"] for r in rows)

            for user_id in user_ids:
                db_resources = _db.get_user_resources(conn, user_id)
                if not db_resources:
                    continue
                user_config = config.users.get(user_id)
                if user_config is None:
                    user_config = UserConfig(display_name=user_id)
                    config.users[user_id] = user_config

                # Drop TOML rows that the DB also owns (same type+path).
                db_keys = {(r.resource_type, r.resource_path) for r in db_resources}
                user_config.resources = [
                    rc for rc in user_config.resources
                    if (rc.type, rc.path) not in db_keys
                ]

                # Append DB rows as ResourceConfig entries. Pull credentials
                # the loader normally splits out (base_url, api_key) into
                # the dataclass's flat fields so secrets_store._IMPORT_MAP
                # and Karakeep's loader keep working unchanged.
                # _allow_obsolete: a stale obsolete-type row may still exist
                # on first startup after the modules refactor; the next
                # _migrate_obsolete_resources pass absorbs and deletes it.
                for r in db_resources:
                    extras = dict(r.extras or {})
                    rc = ResourceConfig(
                        type=r.resource_type,
                        path=r.resource_path,
                        name=r.display_name or "",
                        permissions=r.permissions or "read",
                        base_url=str(extras.pop("base_url", "")) or "",
                        api_key=str(extras.pop("api_key", "")) or "",
                        extra=extras,
                        _allow_obsolete=True,
                    )
                    rc.from_db = True
                    user_config.resources.append(rc)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("user_resources overlay skipped: %s", e)


def _migrate_obsolete_resources(config: "Config") -> None:
    """Absorb obsolete resource credentials into secrets, then drop the rows.

    Sequence:

    1. ``secrets_store.import_from_user_configs`` — copies credentials out of
       ``[[resources]]`` extras for the retired types (karakeep base_url,
       overland.ingest_token, monarch session_token, etc.) into the
       encrypted secrets table. Idempotent; rows already in the table are
       not overwritten.
    2. ``db.cleanup_obsolete_resources`` — deletes the matching rows from
       the ``user_resources`` DB table so they stop being merged into
       ``uc.resources`` on future loads.
    3. Filter ``uc.resources`` in memory so the rest of this load cycle
       sees the post-cleanup state (the executor merge, scheduler hooks,
       etc. all read this list).

    Best-effort: a missing/unreadable DB or unset ``ISTOTA_SECRET_KEY`` is
    not fatal — startup continues and the operator sees the warning.
    """
    try:
        from . import db as _db  # noqa: PLC0415
        from . import secrets_store as _ss  # noqa: PLC0415
    except Exception:  # pragma: no cover - defensive
        return

    db_path = config.db_path
    if db_path is None or not Path(db_path).exists():
        return

    try:
        _ss.import_from_user_configs(db_path, config.users)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("secrets import failed: %s", e)

    try:
        removed = _db.cleanup_obsolete_resources(db_path)
        if removed:
            logger.info(
                "dropped %d obsolete resource row(s) (types: %s)",
                removed, ", ".join(_db._OBSOLETE_RESOURCE_TYPES),
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("obsolete resource cleanup failed: %s", e)

    obsolete = set(_db._OBSOLETE_RESOURCE_TYPES)
    for uc in config.users.values():
        uc.resources = [rc for rc in uc.resources if rc.type not in obsolete]


def _apply_user_briefings(config: "Config") -> None:
    """Merge ``briefing_configs`` rows into ``config.users[*].briefings``.

    DB rows replace TOML rows of the same ``name``; distinct names coexist.
    Disabled DB rows (enabled=0) drop the matching TOML name without adding
    a replacement, so an operator can switch a TOML-templated briefing off
    via the web UI without re-templating.

    Best-effort: a missing DB does not fail config loading.
    """
    try:
        from . import user_briefings as _ub  # avoid import cycles at module load
    except Exception:  # pragma: no cover - defensive
        return

    db_path = config.db_path
    if db_path is None or not Path(db_path).exists():
        return

    try:
        rows = _ub.list_briefings(Path(db_path))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("user_briefings overlay skipped: %s", e)
        return

    by_user: dict[str, list] = {}
    for row in rows:
        by_user.setdefault(row.user_id, []).append(row)

    for user_id, db_rows in by_user.items():
        user_config = config.users.get(user_id)
        if user_config is None:
            user_config = UserConfig(display_name=user_id)
            config.users[user_id] = user_config

        db_names = {r.name for r in db_rows}
        # Drop TOML briefings whose names are claimed by DB rows.
        user_config.briefings = [
            b for b in user_config.briefings if b.name not in db_names
        ]
        # Append enabled DB rows as BriefingConfig entries.
        for r in db_rows:
            if not r.enabled:
                continue
            bc = BriefingConfig(
                name=r.name,
                cron=r.cron,
                conversation_token=r.conversation_token,
                output=r.output,
                components=dict(r.components),
            )
            bc.from_db = True
            user_config.briefings.append(bc)
