CRON.md is for running tasks and commands on a schedule. For monitoring conditions and alerting on failures, use HEARTBEAT.md instead.

You can manage recurring scheduled jobs by editing the user's `{BOT_DIR}/config/CRON.md` file. The scheduler reads this file automatically — changes take effect within ~60 seconds.

**File location:** `$NEXTCLOUD_MOUNT_PATH/Users/$ISTOTA_USER_ID/{BOT_DIR}/config/CRON.md`

## Format

The file uses a TOML code block inside markdown:

```markdown
# Scheduled Jobs

\`\`\`toml
[[jobs]]
name = "daily-report"
cron = "0 9 * * *"
prompt = "Generate my daily report"
target = "talk"
room = "ROOM_TOKEN"

[[jobs]]
name = "weekly-cleanup"
cron = "0 18 * * 0"
prompt = "Review and clean up completed tasks"
target = "email"
silent_unless_action = true

[[jobs]]
name = "memory-stats"
cron = "0 6 * * *"
command = "python -m istota.skills.memory_search stats"
target = "talk"
room = "ROOM_TOKEN"
\`\`\`
```

## Fields

- `name`: Unique per user, short identifier (e.g., `daily-report`, `weekly-cleanup`)
- `cron`: Standard 5-field cron (minute hour day month weekday). Evaluated in the user's configured timezone
- `prompt`: The full prompt text that will be executed as a task (via Claude Code). Mutually exclusive with `command`
- `command`: A shell command to run directly via subprocess (not Claude Code). Mutually exclusive with `prompt`. Each job must have exactly one of `prompt` or `command`
- `target`: Where to deliver results — `"talk"` (post to room), `"email"` (send to user's email), or omit for no delivery
- `room`: Talk conversation token (required when `target` is `"talk"`)
- `enabled`: Set to `false` to pause the job (default: true). Use `!cron disable/enable` for runtime control
- `silent_unless_action`: When `true`, only posts output if response starts with `ACTION:`. Useful for monitoring jobs

## Cron examples

- `0 9 * * *` — every day at 9:00 AM
- `0 9 * * 1-5` — weekdays at 9:00 AM
- `30 18 * * 0` — Sundays at 6:30 PM
- `0 */6 * * *` — every 6 hours
- `0 8 1 * *` — first of every month at 8:00 AM

## Operations

To add a job: append a new `[[jobs]]` entry to the TOML block in the file.
To remove a job: delete its `[[jobs]]` entry from the file.
To modify a job: edit the relevant fields in the file.
To temporarily disable: set `enabled = false` in the file, or use the `!cron disable <name>` command.

When creating a job with Talk output, use the conversation token from the current task context for the `room` field.
