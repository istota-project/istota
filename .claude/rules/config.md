# Config Module (`src/istota/config.py`)

## Dataclass Definitions

### `LoggingConfig` (L12-20)
```
level: str = "INFO"          output: str = "console"     file: str = ""
rotate: bool = True          max_size_mb: int = 10       backup_count: int = 5
```

### `NextcloudConfig` (L23-27)
```
url: str = ""                username: str = ""          app_password: str = ""
```

### `TalkConfig` (L30-33)
```
enabled: bool = True         bot_username: str = "istota"
```

### `EmailConfig` (L36-60)
```
enabled: bool = False        imap_host/port/user/password    poll_folder: str = "INBOX"
smtp_host/port/user/password                                 bot_email: str = ""
```
Properties: `effective_smtp_user` (L53), `effective_smtp_password` (L57) — fall back to imap creds

### `NtfyConfig` (L62-72)
```
enabled: bool = False        server_url: str = "https://ntfy.sh"
topic: str = ""              token: str = ""
username: str = ""           password: str = ""
priority: int = 3
```

### `BrowserConfig` (L74-79)
```
enabled: bool = False        api_url: str = "http://localhost:9223"    vnc_url: str = ""
```

### `ConversationConfig` (L70-79)
```
enabled: bool = True                lookback_count: int = 25
selection_model: str = "haiku"      selection_timeout: float = 30.0
skip_selection_threshold: int = 3   use_selection: bool = True
always_include_recent: int = 5      context_truncation: int = 0
previous_tasks_count: int = 3
```

### `SchedulerConfig` (L82-111)
See `memory/scheduler.md` for full table of fields and defaults.

### `SleepCycleConfig` (L114-121)
```
enabled: bool = False        cron: str = "0 2 * * *"
memory_retention_days: int = 0     lookback_hours: int = 24
```

### `ChannelSleepCycleConfig` (L124-130)
```
enabled: bool = False        cron: str = "0 3 * * *"
lookback_hours: int = 24     memory_retention_days: int = 0
```

### `SecurityConfig`
```
mode: str = "permissive"     # "permissive" or "restricted"
passthrough_env_vars: list[str] = ["LANG", "LC_ALL", "LC_CTYPE", "TZ"]
```

### `BriefingConfig` (L133-140)
```
name: str                    cron: str                   conversation_token: str = ""
output: str = "talk"         components: dict = {}
```

### `ResourceConfig` (L143-149)
```
type: str                    path: str                   name: str = ""
permissions: str = "read"
```

### `UserConfig` (L155-167)
```
display_name: str = ""                    email_addresses: list[str] = []
timezone: str = "UTC"                     briefings: list[BriefingConfig] = []
resources: list[ResourceConfig] = []
invoicing_notifications: str = ""         invoicing_conversation_token: str = ""
ntfy_topic: str = ""
max_foreground_workers: int = 0           max_background_workers: int = 0  # 0 = use global default
```

### `MemorySearchConfig` (L165-170)
```
enabled: bool = False        auto_index_conversations: bool = True
auto_index_memory_files: bool = True
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

### `BriefingDefaultsConfig` (L173-177)
```
markets: dict = {}           news: dict = {}
```

### `Config` (L180-234)
```
db_path: Path = Path("data/istota.db")
nextcloud: NextcloudConfig          talk: TalkConfig
email: EmailConfig                  conversation: ConversationConfig
scheduler: SchedulerConfig          browser: BrowserConfig
ntfy: NtfyConfig                    logging: LoggingConfig
briefing_defaults: BriefingDefaultsConfig   security: SecurityConfig
memory_search: MemorySearchConfig   sleep_cycle: SleepCycleConfig
channel_sleep_cycle: ChannelSleepCycleConfig
developer: DeveloperConfig
users: dict[str, UserConfig] = {}
admin_users: set[str] = set()      # from /etc/istota/admins (empty = all admin)
rclone_remote: str = "nextcloud"
nextcloud_mount_path: Path | None = None
skills_dir: Path = Path("config/skills")
temp_dir: Path = Path("/tmp/istota")
users_dir: Path | None = None
```
Properties:
- `use_mount` (L200): `bool` — True if `nextcloud_mount_path` set
- `caldav_url` (L217): derived from `nextcloud.url + /remote.php/dav`
- `caldav_username` (L225): `nextcloud.username`
- `caldav_password` (L229): `nextcloud.app_password`
Methods:
- `get_user(nc_username) -> UserConfig | None` (L205)
- `is_admin(user_id) -> bool` — True if `admin_users` empty or user in set
- `find_user_by_email(email_address) -> str | None` (L209)

## Config Loading

### `load_config()` (L320-488)
Search order: `config/config.toml` → `~/src/config/config.toml` → `~/.config/istota/config.toml` → `/etc/istota/config.toml`

1. Parse TOML file
2. Build each sub-config from sections: `[logging]`, `[nextcloud]`, `[talk]`, `[email]`, `[browser]`, `[conversation]`, `[scheduler]`, `[memory_search]`, `[channel_sleep_cycle]`, `[briefing_defaults]`
3. Parse `[users.*]` section → `_parse_user_data()` for each
4. Set `users_dir = config_dir / "users"` if exists
5. Load per-user configs via `load_user_configs()`
6. **Merge**: per-user files override main config `[users]` section
7. Parse `[security]` section → `SecurityConfig`
8. Call `load_admin_users()` → `config.admin_users`
9. Apply env var overrides for secrets (`ISTOTA_NC_APP_PASSWORD` → `nextcloud.app_password`, etc.)
10. Return `Config`

**Secret env var overrides** (applied after TOML, enables `EnvironmentFile=`):
| Env Var | Config Field |
|---|---|
| `ISTOTA_NC_APP_PASSWORD` | `nextcloud.app_password` |
| `ISTOTA_IMAP_PASSWORD` | `email.imap_password` |
| `ISTOTA_SMTP_PASSWORD` | `email.smtp_password` |
| `ISTOTA_GITLAB_TOKEN` | `developer.gitlab_token` |
| `ISTOTA_GITHUB_TOKEN` | `developer.github_token` |
| `ISTOTA_NTFY_TOKEN` | `ntfy.token` |
| `ISTOTA_NTFY_PASSWORD` | `ntfy.password` |

### `load_admin_users(path=None) -> set[str]`
Loads admin user IDs from plain text file (one per line, `#` comments, blank lines ignored).
- Check `ISTOTA_ADMINS_FILE` env var, then default `/etc/istota/admins`
- Returns empty set if file missing (all users = admin for backward compat)

### `_parse_user_data()` (L236-290)
Parses user dict → `UserConfig`:
- Parses `[[briefings]]` → `BriefingConfig` list
- Parses `[sleep_cycle]` → `SleepCycleConfig`
- Parses `[[resources]]` → `ResourceConfig` list
- Backward compat: migrates `reminders_file` string to `ResourceConfig(type="reminders_file")`

### `load_user_configs()` (L293-317)
Loads `config/users/*.toml` (skips `*.example.toml`):
- Filename = user_id (without `.toml`)
- Returns `dict[user_id, UserConfig]`

## UserResource (DB Model, in db.py L44-52)
```python
@dataclass
class UserResource:
    id: int
    user_id: str
    resource_type: str      # "calendar", "folder", "todo_file", "email_folder",
                            # "notes_file", "reminders_file", "shared_file",
                            # "ledger", "invoicing"
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
3. It loads from `[users.NAME.field]` in main config or per-user TOML
4. Update `config/users/alice.example.toml`

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
5. In skill `_index.toml`, add `resource_types = ["my_data"]` to relevant skill
6. Document in skill markdown file
