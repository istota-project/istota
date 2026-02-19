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

    @property
    def effective_smtp_user(self) -> str:
        return self.smtp_user or self.imap_user

    @property
    def effective_smtp_password(self) -> str:
        return self.smtp_password or self.imap_password


@dataclass
class NtfyConfig:
    """ntfy push notification configuration."""
    enabled: bool = False
    server_url: str = "https://ntfy.sh"
    topic: str = ""
    token: str = ""       # bearer token auth
    username: str = ""     # basic auth (alternative to token)
    password: str = ""
    priority: int = 3


@dataclass
class BrowserConfig:
    """Browser container configuration."""
    enabled: bool = False
    api_url: str = "http://localhost:9223"
    vnc_url: str = ""  # external noVNC URL for user access


@dataclass
class ConversationConfig:
    enabled: bool = True
    lookback_count: int = 25
    selection_model: str = "haiku"  # Haiku sufficient for relevance matching
    selection_timeout: float = 30.0
    skip_selection_threshold: int = 3  # Include all messages if history ≤ this
    use_selection: bool = True  # If False, include all messages without LLM selection
    always_include_recent: int = 5  # Always include this many recent messages without selection
    context_truncation: int = 0  # Max chars per bot response in context (0 to disable)


@dataclass
class SchedulerConfig:
    poll_interval: int = 2  # seconds between task queue checks
    email_poll_interval: int = 60  # seconds between email polls
    briefing_check_interval: int = 60  # seconds between briefing checks
    tasks_file_poll_interval: int = 30  # seconds between TASKS.md file polls
    shared_file_check_interval: int = 120  # seconds between shared file organization checks
    heartbeat_check_interval: int = 60  # seconds between heartbeat checks
    talk_poll_interval: int = 10  # seconds between Talk polls
    talk_poll_timeout: int = 30  # long-poll timeout for Talk API
    talk_poll_wait: float = 2.0  # max seconds to wait for all rooms before processing available results
    # Progress updates (Talk only)
    progress_updates: bool = True          # master toggle
    progress_min_interval: int = 8         # min seconds between progress messages
    progress_max_messages: int = 5         # max progress messages per task
    progress_show_tool_use: bool = True    # show "Reading file.txt", "Running script..."
    progress_show_text: bool = False       # show intermediate assistant text (noisy)
    task_timeout_minutes: int = 30  # kill task execution after this
    # Robustness settings
    confirmation_timeout_minutes: int = 120  # auto-cancel pending_confirmation after this
    stale_pending_warn_minutes: int = 30  # log warning for tasks pending longer than this
    stale_pending_fail_hours: int = 2  # auto-fail tasks pending longer than this
    max_retry_age_minutes: int = 60  # don't retry stuck tasks older than this
    task_retention_days: int = 7  # delete completed/failed/cancelled tasks older than this
    email_retention_days: int = 7  # delete emails older than N days from IMAP, 0 to disable
    temp_file_retention_days: int = 7  # delete temp files older than N days, 0 to disable
    worker_idle_timeout: int = 30    # seconds before idle worker exits
    max_foreground_workers: int = 5  # instance-level foreground (interactive) worker cap
    max_background_workers: int = 3  # instance-level background (scheduled/briefing) worker cap
    user_max_foreground_workers: int = 1  # global per-user fg worker default
    user_max_background_workers: int = 1  # global per-user bg worker default
    scheduled_job_max_consecutive_failures: int = 5  # auto-disable after N failures (0 = never)
    feed_check_interval: int = 300  # seconds between feed polls
    feed_item_retention_days: int = 30  # delete feed items older than this


@dataclass
class SleepCycleConfig:
    """Sleep cycle (nightly memory extraction) configuration."""
    enabled: bool = False
    cron: str = "0 2 * * *"  # 2am in user's timezone
    memory_retention_days: int = 0  # 0 = unlimited retention
    lookback_hours: int = 24


@dataclass
class ChannelSleepCycleConfig:
    """Channel-level sleep cycle (memory extraction from shared conversations)."""
    enabled: bool = False
    cron: str = "0 3 * * *"  # UTC (after user sleep cycles)
    lookback_hours: int = 24
    memory_retention_days: int = 0  # 0 = unlimited retention


@dataclass
class BriefingConfig:
    """Briefing configuration."""
    name: str
    cron: str  # cron expression, evaluated in user's timezone
    conversation_token: str = ""  # Talk room to post to
    output: str = "talk"  # "talk", "email", or "both"
    components: dict = field(default_factory=dict)


@dataclass
class ResourceConfig:
    """User resource configuration (defined in per-user TOML files)."""
    type: str           # calendar, folder, todo_file, email_folder, notes_file, shared_file, reminders_file, karakeep
    path: str = ""
    name: str = ""
    permissions: str = "read"
    # Service-specific credentials (e.g. karakeep)
    base_url: str = ""
    api_key: str = ""
    # Arbitrary extra fields for plugin skills (unrecognized keys go here)
    extra: dict = field(default_factory=dict)


@dataclass
class UserConfig:
    """Per-user configuration."""
    display_name: str = ""  # friendly name for prompts
    email_addresses: list[str] = field(default_factory=list)  # for email-to-user mapping
    timezone: str = "UTC"  # user's timezone for briefing scheduling
    briefings: list[BriefingConfig] = field(default_factory=list)
    resources: list[ResourceConfig] = field(default_factory=list)
    invoicing_notifications: str = ""  # default surface for invoice notifications
    invoicing_conversation_token: str = ""  # Talk room for invoice notifications
    ntfy_topic: str = ""  # per-user ntfy topic override
    site_enabled: bool = False  # static website hosting at /~user/
    max_foreground_workers: int = 0  # per-user fg worker override (0 = use global default)
    max_background_workers: int = 0  # per-user bg worker override (0 = use global default)


@dataclass
class MemorySearchConfig:
    """Memory search configuration."""
    enabled: bool = False
    auto_index_conversations: bool = True
    auto_index_memory_files: bool = True


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
    gitlab_api_allowlist: list[str] = field(default_factory=lambda: [
        "GET /api/v4/projects/*",
        "GET /api/v4/groups/*",
        "GET /api/v4/users*",
        "POST /api/v4/projects/*/merge_requests",
        "POST /api/v4/projects/*/merge_requests/*/notes",
        "POST /api/v4/projects/*/issues",
        "POST /api/v4/projects/*/issues/*/notes",
        "PUT /api/v4/projects/*/merge_requests/*/merge",
    ])
    github_url: str = "https://github.com"
    github_token: str = ""        # Personal access token (repo scope recommended)
    github_username: str = ""     # GitHub username for HTTPS auth (defaults to x-access-token if empty)
    github_default_owner: str = ""  # Default org/user for resolving short repo names
    github_reviewer: str = ""     # GitHub username to request as PR reviewer
    author_credit: str = ""       # Appended to every commit message (e.g., "Co-Authored-By: Name <email>")
    github_api_allowlist: list[str] = field(default_factory=lambda: [
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


@dataclass
class SiteConfig:
    """Static website hosting configuration."""
    enabled: bool = False
    hostname: str = ""        # e.g. "istota.example.com"
    base_path: str = ""       # e.g. "/srv/app/istota/html"


@dataclass
class SecurityConfig:
    """Security hardening configuration."""
    mode: str = "permissive"  # "permissive" or "restricted"
    sandbox_enabled: bool = False  # bwrap filesystem isolation per user
    sandbox_admin_db_write: bool = False  # allow admin DB writes in sandbox
    passthrough_env_vars: list[str] = field(default_factory=lambda: [
        "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    ])


@dataclass
class BriefingDefaultsConfig:
    """Admin-level defaults for briefing components (expanded when user sets boolean)."""
    markets: dict = field(default_factory=dict)
    news: dict = field(default_factory=dict)


@dataclass
class Config:
    bot_name: str = "Istota"  # User-facing name (used in chat, emails, folder names)
    model: str = ""  # Claude model to use (e.g. "sonnet", "opus"); empty = CLI default
    db_path: Path = field(default_factory=lambda: Path("data/istota.db"))
    nextcloud: NextcloudConfig = field(default_factory=NextcloudConfig)
    talk: TalkConfig = field(default_factory=TalkConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    conversation: ConversationConfig = field(default_factory=ConversationConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    briefing_defaults: BriefingDefaultsConfig = field(default_factory=BriefingDefaultsConfig)
    memory_search: MemorySearchConfig = field(default_factory=MemorySearchConfig)
    sleep_cycle: SleepCycleConfig = field(default_factory=SleepCycleConfig)
    channel_sleep_cycle: ChannelSleepCycleConfig = field(default_factory=ChannelSleepCycleConfig)
    developer: DeveloperConfig = field(default_factory=DeveloperConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    site: SiteConfig = field(default_factory=SiteConfig)
    users: dict[str, UserConfig] = field(default_factory=dict)  # nc_username -> UserConfig
    admin_users: set[str] = field(default_factory=set)  # users with full system access
    rclone_remote: str = "nextcloud"  # rclone remote name
    nextcloud_mount_path: Path | None = None  # If set, use mount instead of rclone CLI
    skills_dir: Path = field(default_factory=lambda: Path("config/skills"))
    bundled_skills_dir: Path | None = None  # Override bundled skills dir (for testing)
    temp_dir: Path = field(default_factory=lambda: Path("/tmp/istota"))
    users_dir: Path | None = None  # config/users/ directory for per-user TOML files

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
    Returns empty set if file doesn't exist (backward compat: all users = admin).

    Args:
        path: Override file path. If None, checks ISTOTA_ADMINS_FILE env var,
              then falls back to /etc/istota/admins.
    """
    if path is None:
        path = os.environ.get("ISTOTA_ADMINS_FILE", "/etc/istota/admins")
    admins_path = Path(path)
    if not admins_path.exists():
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

    return UserConfig(
        display_name=user_data.get("display_name", user_id),
        email_addresses=user_data.get("email_addresses", []),
        timezone=user_data.get("timezone", "UTC"),
        briefings=briefings,
        resources=resources,
        invoicing_notifications=user_data.get("invoicing_notifications", ""),
        invoicing_conversation_token=user_data.get("invoicing_conversation_token", ""),
        ntfy_topic=user_data.get("ntfy_topic", ""),
        site_enabled=user_data.get("site_enabled", False),
        max_foreground_workers=user_data.get("max_foreground_workers", 1),
        max_background_workers=user_data.get("max_background_workers", 1),
    )


def load_user_configs(users_dir: Path) -> dict[str, UserConfig]:
    """
    Load per-user config files from a directory.

    Each .toml file in the directory represents one user.
    Filename (without .toml) = user_id.
    """
    users = {}
    if not users_dir.is_dir():
        return users

    for toml_file in sorted(users_dir.glob("*.toml")):
        # Skip example files (e.g., alice.example.toml)
        if ".example" in toml_file.stem:
            continue
        user_id = toml_file.stem
        try:
            with open(toml_file, "rb") as f:
                user_data = tomli.load(f)
            users[user_id] = _parse_user_data(user_data, user_id)
            logger.debug("Loaded per-user config for %s from %s", user_id, toml_file)
        except Exception as e:
            logger.error("Error loading user config %s: %s", toml_file, e)

    return users


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from TOML file."""
    if config_path is None:
        # Look for config in standard locations
        candidates = [
            Path("config/config.toml"),
            Path.home() / ".config/istota/config.toml",
            Path("/etc/istota/config.toml"),
        ]
        for candidate in candidates:
            if candidate.exists():
                config_path = candidate
                break

    if config_path is None or not config_path.exists():
        # Return default config
        return Config()

    with open(config_path, "rb") as f:
        data = tomli.load(f)

    config = Config()

    if "bot_name" in data:
        config.bot_name = data["bot_name"]

    if "model" in data:
        config.model = data["model"]

    if "db_path" in data:
        config.db_path = Path(data["db_path"])

    if "rclone_remote" in data:
        config.rclone_remote = data["rclone_remote"]

    if "nextcloud_mount_path" in data:
        config.nextcloud_mount_path = Path(data["nextcloud_mount_path"])

    if "skills_dir" in data:
        config.skills_dir = Path(data["skills_dir"])

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

    # Load per-user config files from users/ directory (sibling to config file)
    users_dir = config_path.parent / "users"
    if users_dir.is_dir():
        config.users_dir = users_dir
        per_user_configs = load_user_configs(users_dir)
        # Per-user files take precedence over [users] section in main config
        config.users.update(per_user_configs)

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
            selection_model=conv.get("selection_model", "haiku"),
            selection_timeout=conv.get("selection_timeout", 30.0),
            skip_selection_threshold=conv.get("skip_selection_threshold", 3),
            use_selection=conv.get("use_selection", True),
            always_include_recent=conv.get("always_include_recent", 5),
            context_truncation=conv.get("context_truncation", 0),
        )

    if "scheduler" in data:
        sched = data["scheduler"]
        config.scheduler = SchedulerConfig(
            poll_interval=sched.get("poll_interval", 5),
            email_poll_interval=sched.get("email_poll_interval", 60),
            briefing_check_interval=sched.get("briefing_check_interval", 60),
            tasks_file_poll_interval=sched.get("tasks_file_poll_interval", sched.get("istota_file_poll_interval", 30)),
            shared_file_check_interval=sched.get("shared_file_check_interval", 120),
            heartbeat_check_interval=sched.get("heartbeat_check_interval", 60),
            talk_poll_interval=sched.get("talk_poll_interval", 10),
            talk_poll_timeout=sched.get("talk_poll_timeout", 30),
            talk_poll_wait=sched.get("talk_poll_wait", 2.0),
            progress_updates=sched.get("progress_updates", True),
            progress_min_interval=sched.get("progress_min_interval", 8),
            progress_max_messages=sched.get("progress_max_messages", 5),
            progress_show_tool_use=sched.get("progress_show_tool_use", True),
            progress_show_text=sched.get("progress_show_text", False),
            task_timeout_minutes=sched.get("task_timeout_minutes", 30),
            confirmation_timeout_minutes=sched.get("confirmation_timeout_minutes", 120),
            stale_pending_warn_minutes=sched.get("stale_pending_warn_minutes", 30),
            stale_pending_fail_hours=sched.get("stale_pending_fail_hours", 2),
            max_retry_age_minutes=sched.get("max_retry_age_minutes", 60),
            task_retention_days=sched.get("task_retention_days", 7),
            email_retention_days=sched.get("email_retention_days", 7),
            temp_file_retention_days=sched.get("temp_file_retention_days", 7),
            worker_idle_timeout=sched.get("worker_idle_timeout", 30),
            scheduled_job_max_consecutive_failures=sched.get("scheduled_job_max_consecutive_failures", 5),
            feed_check_interval=sched.get("feed_check_interval", 300),
            max_foreground_workers=sched.get("max_foreground_workers", 5),
            max_background_workers=sched.get("max_background_workers", 3),
            user_max_foreground_workers=sched.get("user_max_foreground_workers", 1),
            user_max_background_workers=sched.get("user_max_background_workers", 1),
        )

    if "browser" in data:
        br = data["browser"]
        config.browser = BrowserConfig(
            enabled=br.get("enabled", False),
            api_url=br.get("api_url", "http://localhost:9223"),
            vnc_url=br.get("vnc_url", ""),
        )

    if "ntfy" in data:
        n = data["ntfy"]
        config.ntfy = NtfyConfig(
            enabled=n.get("enabled", False),
            server_url=n.get("server_url", "https://ntfy.sh"),
            topic=n.get("topic", ""),
            token=n.get("token", ""),
            username=n.get("username", ""),
            password=n.get("password", ""),
            priority=n.get("priority", 3),
        )

    if "briefing_defaults" in data:
        bd = data["briefing_defaults"]
        config.briefing_defaults = BriefingDefaultsConfig(
            markets=bd.get("markets", {}),
            news=bd.get("news", {}),
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
            enabled=ms.get("enabled", False),
            auto_index_conversations=ms.get("auto_index_conversations", True),
            auto_index_memory_files=ms.get("auto_index_memory_files", True),
        )

    if "sleep_cycle" in data:
        sc = data["sleep_cycle"]
        config.sleep_cycle = SleepCycleConfig(
            enabled=sc.get("enabled", False),
            cron=sc.get("cron", "0 2 * * *"),
            memory_retention_days=sc.get("memory_retention_days", 0),
            lookback_hours=sc.get("lookback_hours", 24),
        )

    if "channel_sleep_cycle" in data:
        csc = data["channel_sleep_cycle"]
        config.channel_sleep_cycle = ChannelSleepCycleConfig(
            enabled=csc.get("enabled", False),
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
            **extra,
        )

    if "security" in data:
        sec = data["security"]
        config.security = SecurityConfig(
            mode=sec.get("mode", "permissive"),
            sandbox_enabled=sec.get("sandbox_enabled", False),
            sandbox_admin_db_write=sec.get("sandbox_admin_db_write", False),
            **({
                "passthrough_env_vars": sec["passthrough_env_vars"]
            } if "passthrough_env_vars" in sec else {}),
        )

    config.admin_users = load_admin_users()

    # Environment variable overrides for secrets (allows EnvironmentFile= usage)
    _env_secret_overrides = [
        ("ISTOTA_NC_APP_PASSWORD", "nextcloud", "app_password"),
        ("ISTOTA_IMAP_PASSWORD", "email", "imap_password"),
        ("ISTOTA_SMTP_PASSWORD", "email", "smtp_password"),
        ("ISTOTA_GITLAB_TOKEN", "developer", "gitlab_token"),
        ("ISTOTA_GITHUB_TOKEN", "developer", "github_token"),
        ("ISTOTA_NTFY_TOKEN", "ntfy", "token"),
        ("ISTOTA_NTFY_PASSWORD", "ntfy", "password"),
    ]
    for env_var, section, field_name in _env_secret_overrides:
        val = os.environ.get(env_var)
        if val:
            setattr(getattr(config, section), field_name, val)

    return config
