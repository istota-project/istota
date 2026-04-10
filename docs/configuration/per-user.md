# Per-user configuration

Each user can be configured at three levels (later overrides earlier):

1. `[users.alice]` in main `config/config.toml`
2. `config/users/alice.toml` (per-user TOML file)
3. User workspace files in Nextcloud (PERSONA.md, BRIEFINGS.md, CRON.md, etc.)

## Per-user TOML settings

```toml
# config/users/alice.toml
display_name = "Alice"
email_addresses = ["alice@example.com", "alice.work@company.com"]
timezone = "America/New_York"

# Per-user worker limits (0 = use global default)
max_foreground_workers = 2
max_background_workers = 1

# Skills to exclude for this user
disabled_skills = ["markets"]

# Verbose tool logging to a dedicated Talk room
log_channel = "room456"

# Talk room for confirmations and security alerts
alerts_channel = "room789"

# Trusted email senders (bypass confirmation gate, supports fnmatch patterns)
trusted_email_senders = ["*@company.com", "boss@other.com"]

# Static website at /~alice/
site_enabled = true

# ntfy topic override
ntfy_topic = "alice-alerts"
```

### Resources

Resources define what data the bot can access for this user:

```toml
[[resources]]
type = "reminders_file"
path = "/Users/alice/shared/Notes/_REMINDERS.md"
name = "Reminders"

[[resources]]
type = "miniflux"
name = "Feeds"
extra = { base_url = "https://rss.example.com", api_key = "..." }

[[resources]]
type = "moneyman"
name = "Accounting"
extra = { user_key = "alice" }

[[resources]]
type = "overland"
name = "GPS"
extra = { ingest_token = "secret", default_radius = 100 }
```

Resource types: `calendar`, `folder`, `todo_file`, `email_folder`, `shared_file`, `reminders_file`, `notes_folder`, `ledger`, `karakeep`, `monarch`, `miniflux`, `moneyman`, `overland`.

CalDAV calendars are auto-discovered from Nextcloud and don't need to be configured as resources.

### Briefings

```toml
[[briefings]]
name = "morning"
cron = "0 6 * * *"
conversation_token = "room123"
output = "both"

[briefings.components]
calendar = true
todos = true
markets = true
news = true
```

See [briefings](../features/briefings.md) for details.

## User workspace files

These files live in the user's Nextcloud folder at `/Users/{user_id}/{bot_dir}/config/` and can be edited through the Nextcloud web UI:

| File | Purpose | See |
|---|---|---|
| `USER.md` | Persistent memory (auto-loaded into prompts) | [Memory](../features/memory.md) |
| `TASKS.md` | File-based task queue with status markers | [Scheduling](../features/scheduling.md) |
| `PERSONA.md` | Personality customization (overrides global) | [Persona](persona.md) |
| `BRIEFINGS.md` | Briefing schedule (overrides TOML config) | [Briefings](../features/briefings.md) |
| `CRON.md` | Scheduled jobs (markdown + TOML) | [Scheduling](../features/scheduling.md) |
| `HEARTBEAT.md` | Health monitoring checks | [Heartbeat](../features/heartbeat.md) |

### TASKS.md format

```markdown
# Tasks
- [ ] Send email to john about the meeting tomorrow
- [~] Checking calendar for tomorrow's schedule...
- [x] 2025-01-26 12:34 | Summarized report | Result: Summary saved to exports/
- [!] 2025-01-26 12:35 | Failed task | Error: timeout (attempt 2/3)
```

Status markers: `[ ]` pending, `[~]` in progress, `[x]` completed, `[!]` failed.

## Admin vs non-admin

Admin users are listed in `/etc/istota/admins`. Empty file = all users are admin.

Non-admin restrictions:

- Scoped mount path (`/Users/{user_id}` only)
- No DB access (no `ISTOTA_DB_PATH`, no sqlite3 tool)
- No subtask creation
- `admin_only` skills filtered out (e.g., tasks, schedules)
