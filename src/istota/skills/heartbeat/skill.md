HEARTBEAT.md is for monitoring — checking conditions and alerting on failures. For running tasks on a schedule (including AI-powered checks), use CRON.md instead.

The user can configure heartbeat monitoring checks in their `{BOT_DIR}/config/HEARTBEAT.md` file. The system evaluates these checks periodically and sends alerts when conditions fail.

## File Format

The `HEARTBEAT.md` file contains a TOML block with settings and check definitions:

```toml
[settings]
conversation_token = "ROOM_TOKEN"      # Talk room for alerts
quiet_hours = ["22:00-07:00"]          # Suppress alerts during these hours
default_cooldown_minutes = 60          # Default time between repeat alerts

[[checks]]
name = "backup-fresh"
type = "file-watch"
path = "/Users/alice/backups/latest.log"
max_age_hours = 25
cooldown_minutes = 120                 # Override default cooldown
interval_minutes = 15                  # Run this check every 15 min (default: every cycle)

[[checks]]
name = "disk-space"
type = "shell-command"
command = "df -h / | tail -1 | awk '{print $5}' | tr -d '%'"
condition = "< 90"
message = "Disk usage at {value}%"

[[checks]]
name = "api-health"
type = "url-health"
url = "https://api.example.com/health"
expected_status = 200
timeout = 10
```

## Check Types

### file-watch
Check file age or existence.
- `path`: Nextcloud path to the file
- `max_age_hours`: Maximum file age in hours (optional)

### shell-command
Run a command and evaluate the output.
- `command`: Shell command to execute
- `condition`: Comparison expression:
  - `< N` / `> N` — numeric comparison
  - `== value` — exact string match
  - `contains:text` — substring match
  - `not-contains:text` — negative substring match
- `message`: Alert message (use `{value}` placeholder)
- `timeout`: Command timeout in seconds (default: 30)

### url-health
HTTP health check.
- `url`: URL to check
- `expected_status`: Expected HTTP status code (default: 200)
- `timeout`: Request timeout in seconds (default: 10)

### calendar-conflicts
Find overlapping calendar events.
- `lookahead_hours`: Hours to look ahead (default: 24)

### task-deadline
Check for overdue tasks from TASKS.md.
- `source`: Currently only `"file"` is supported
- `warn_hours_before`: Hours before deadline to warn (default: 24)

Tasks should use deadline markers: `@due(2024-01-15)` or `(due: 2024-01-15)`

### self-check
System health diagnostics (same checks as `!check` command). Checks: Claude binary in PATH, bwrap presence (if sandbox enabled), DB connectivity, recent task failure rate, and optional Claude CLI execution test.
- `execution_test`: Whether to run a live Claude invocation test (default: true)

```toml
[[checks]]
name = "system-health"
type = "self-check"
interval_minutes = 30
cooldown_minutes = 60

[checks.config]
execution_test = true
```

## Quiet Hours

Quiet hours suppress alert delivery but checks still run. When quiet hours end, the next failure triggers an immediate alert. Supports both same-day ranges (`09:00-17:00`) and cross-midnight ranges (`22:00-07:00`).

## Cooldown

After an alert is sent, no repeat alerts are sent for that check until the cooldown period expires. This prevents alert fatigue. Use `cooldown_minutes` per-check to override the global `default_cooldown_minutes`.

## Check Interval

By default, all checks run every scheduler cycle (`heartbeat_check_interval`, default 60s). Use `interval_minutes` per-check to run expensive checks less frequently. Checks without `interval_minutes` run every cycle.
