# Per-user configuration

Per-user data lives in three DB tables and (optionally) the user's Nextcloud workspace:

1. **DB tables** (authoritative)
   - `user_profiles` — display_name, timezone, channels, worker overrides, email lists, trusted senders, disabled_skills, **disabled_modules**, **delivery routing** (`default_destination` + a purpose-keyed `routing` table)
   - `user_resources` — folder mounts (`folder`) and internal `shared_file` organizer state. Only `folder` is declarable after the Resources sunset; the other path-shaped types were retired (calendars are CalDAV-discovered, todo/reminders/notes are workspace-convention files).
   - `briefing_configs` — briefing schedules. `enabled=0` mutes a briefing without deletion.
   - `secrets` — Fernet-encrypted credentials (Karakeep, Monarch, Tumblr, Overland ingest token, ntfy, etc.). See [credentials](credentials.md) for the full per-user inventory.
2. `[users.alice]` block in main `config/config.toml` (the docker entrypoint path) — DB rows still win at config-load time.
3. User workspace files in Nextcloud (`PERSONA.md`, `BRIEFINGS.md`, `CRON.md`, `HEARTBEAT.md`, `TASKS.md`, `USER.md`).

> The legacy `config/users/{user_id}.toml` file (and its `.user.json` overlay) was retired with the OIDC retirement / Phase 7 sweep. The `config/users/` directory is gone, Ansible no longer renders per-user TOML, and `Config.users_dir` / `load_user_configs()` no longer exist.

The DB rows are populated four ways:

- **Ansible**: `istota user|resource|briefing|secret ensure …` — each idempotent and prints `STATE: created|updated|noop` for `changed_when` semantics.
- **Web UI**: `/istota/settings` (Profile + Connected services + module pages) and the per-feature settings under `/istota/{feeds,money,location}/settings`.
- **Auto-seed**: on first OAuth login the profile row is created from the Nextcloud display_name and any `[users.X]` block. Subsequent logins do not overwrite values the user has edited.
- **TOML migration**: on scheduler startup, `import_from_user_configs` (one each for profiles / resources / briefings) seeds DB rows from any remaining `[users.X]` block whose natural key isn't already present.

## Per-user TOML settings

```toml
# main config.toml: [users.alice]
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

# Modules to opt out of (default-on otherwise)
disabled_modules = ["money"]

# Default delivery surface for results/notifications when nothing else applies
default_destination = "talk"   # talk | email | ntfy | web | surface:channel | comma list

# Where replies to inbound email threads are delivered
email_reply_routing = "origin+thread"   # origin+thread (default) | origin | thread

# Purpose-keyed routing table — overrides default_destination per purpose.
# Purposes: reply, alert, log, briefing, notification
[users.alice.routing]
alert = "ntfy"                 # heartbeat + security alerts go to ntfy
log = "web:<room-token>"       # verbose execution log streamed to a web chat room
```

> ntfy push notifications are **not** a profile field. They live in the encrypted `secrets` table — provision via the web UI (`/istota/settings` → Connected services → ntfy push) or `istota secret ensure --user alice --service ntfy --key topic --value …`.

### Resources (folder mounts)

After the Resources sunset, the only declarable resource type is `folder` —
an out-of-workspace path mounted into the sandbox (a cross-user share, an
absolute path elsewhere). In-workspace paths are already covered by the
wholesale user-dir bind, so a `folder` row only does real work for paths
outside `Users/<id>/`. Provision via Ansible (`istota resource ensure
--user alice --type folder --path /shared/Projects --name Projects`) or the
`[[users.X.resources]]` TOML block. Calendars are CalDAV-discovered;
todo/reminders/notes read an explicit path (a briefing-source `path`, or a
deprecated `todo_file`/`reminders_file` resource override) — there is no
convention-default filename, and the `notes/` folder is prompt guidance for
the model only; email folders have no consumer. `calendar`, `notes_folder`,
`email_folder`, and the module/credential types are auto-cleaned from
`user_resources` on scheduler startup; `todo_file`/`reminders_file` are left
in place as deprecated overrides (removed by hand when the user migrates).

```toml
[[users.alice.resources]]
type = "folder"
path = "/shared/Projects"
name = "Projects"
permissions = "write"
```

Resource types: `calendar`, `folder`, `todo_file`, `email_folder`, `shared_file`, `reminders_file`, `notes_folder`.

> **Modules vs resources vs connected services.** The retired `feeds` / `money` / `monarch` / `karakeep` / `overland` resource types were split apart in the modules / connected services refactor:
> - **Modules** (`feeds`, `money`, `location`) are on by default; opt out per user via `disabled_modules`. Module-owned secrets (Tumblr API key, Monarch session, Overland ingest token) live on the per-module settings page.
> - **Connected services** (`karakeep`, `google_workspace`) are external API credentials in the encrypted `secrets` table.
> - The scheduler auto-cleans the obsolete resource types from `user_resources` on startup; their TOML extras are migrated into `secrets` via `secrets_store.import_from_user_configs`.

CalDAV calendars are auto-discovered from Nextcloud and don't need to be configured as resources.

### Briefings

Provision via `istota briefing ensure --user alice --name morning --cron "0 6 * * *" --conversation-token room123 --output both --component calendar=true --component todos=true` or the web UI (`/istota/settings → Briefings`). The `[[users.X.briefings]]` TOML block is a docker-entrypoint shortcut.

```toml
[[users.alice.briefings]]
name = "morning"
cron = "0 6 * * *"
conversation_token = "room123"
output = "both"

[users.alice.briefings.components]
calendar = true
todos = true
markets = true
news = true
```

See [briefings](../features/briefings.md) for details.

### Delivery routing

Each user has a default delivery surface (`default_destination`, defaults to `talk`) plus an optional purpose-keyed `routing` table that overrides it per purpose. The purposes are `reply`, `alert`, `log`, `briefing`, and `notification`; each maps to an `output_target` descriptor (`talk`, `email`, `ntfy`, `web`, `surface:channel`, or a comma list). Routing notifications by purpose (e.g. `alert = "ntfy"`) is what reroutes heartbeat and security alerts off Talk; the `log` purpose drives the verbose per-task execution log to any user-routable surface (it supersedes the legacy `log_channel` shorthand). `web` is a routable delivery surface — alerts, the execution log, and notifications routed to it land in a web chat room as system messages.

Provision via the CLI:

```bash
istota user ensure -u alice \
  --default-destination email \
  --route alert=ntfy \
  --route log=web:<room-token>
```

`--route` is repeatable and validates the purpose against the allowed set. The web Preferences card surfaces `default_destination`, the `alert` route, and the `log` route; CLI-set routes for the other purposes are preserved on round-trip.

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
