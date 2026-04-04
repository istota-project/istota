# Heartbeat monitoring

User-defined health checks evaluated on a schedule. Configure checks in `/Users/{user_id}/{bot_dir}/config/HEARTBEAT.md`.

## Check types

| Type | What it does |
|---|---|
| `file-watch` | Monitors file age and existence |
| `shell-command` | Runs a command and evaluates a condition |
| `url-health` | Checks HTTP status codes |
| `calendar-conflicts` | Detects overlapping calendar events |
| `task-deadline` | Finds overdue items in TASKS.md |
| `self-check` | System diagnostics: Claude binary, bwrap, DB, failure rate |

## Per-check controls

| Setting | Purpose |
|---|---|
| `interval_minutes` | Run expensive checks less frequently |
| `cooldown_minutes` | Prevent alert fatigue (minimum time between alerts) |
| `quiet_hours` | Suppress alerts during off-hours (cross-midnight supported) |

State is tracked in the `heartbeat_state` table: last check time, last alert time, last healthy time, and consecutive error count.

## Alerts

When a check fails, alerts are sent via the configured notification channels (Talk, email, ntfy). The `!check` command in Talk triggers an immediate self-check.

## Configuration

Heartbeat checks are defined in HEARTBEAT.md as markdown with embedded TOML, similar to CRON.md. The scheduler evaluates checks every `heartbeat_check_interval` (default 60s).
