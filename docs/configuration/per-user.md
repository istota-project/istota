# Per-user configuration

Each user can be configured at four levels (later overrides earlier):

1. `[users.alice]` in main `config/config.toml`
2. `config/users/alice.toml` (per-user TOML file — still authoritative for **resources** and **briefings**)
3. `user_profiles` row in the istota DB — owns profile fields (display_name, timezone, channels, ntfy_topic, worker overrides, email lists, trusted senders, disabled skills)
4. User workspace files in Nextcloud (PERSONA.md, BRIEFINGS.md, CRON.md, etc.)

The DB row is populated three ways:

- **Ansible**: `istota user ensure --name alice --display-name "Alice" --tz "America/Los_Angeles" --email alice@example.com` (idempotent partial update).
- **Web UI**: `/istota/settings` lets each user edit their profile directly.
- **Auto-seed**: on first OAuth login the row is created from the Nextcloud display_name and any TOML profile fields the operator already supplied. Subsequent logins do not overwrite values the user has edited.

Migration from existing TOML installs: the scheduler imports any TOML profile fields into the DB on startup, but only for users without a row yet. Existing per-user TOML files keep working — they just stop being the source of truth for profile fields once a DB row exists.

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
type = "feeds"
name = "Feeds"
extra = { tumblr_api_key = "..." }   # optional; TUMBLR_API_KEY env is the fallback

[[resources]]
type = "moneyman"
name = "Accounting"
extra = { user_key = "alice" }

[[resources]]
type = "overland"
name = "GPS"
extra = { ingest_token = "secret", default_radius = 100 }
```

Resource types: `calendar`, `folder`, `todo_file`, `email_folder`, `shared_file`, `reminders_file`, `notes_folder`, `ledger`, `karakeep`, `monarch`, `feeds`, `money`, `overland`.

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
