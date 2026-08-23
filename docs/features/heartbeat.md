# Heartbeat monitoring

User-defined health checks evaluated on a schedule. Configure checks in `/Users/{user_id}/{bot_dir}/config/HEARTBEAT.md`.

Heartbeat needs the Nextcloud mount — the config file lives on it, so `load_heartbeat_config` returns nothing and the whole subsystem is inert on a deployment without one.

## Check types

| Type | What it does |
|---|---|
| `file-watch` | Monitors file age and existence |
| `shell-command` | Runs a command and evaluates a condition (**admin-only**) |
| `url-health` | Checks HTTP status codes |
| `calendar-conflicts` | Detects overlapping calendar events |
| `task-deadline` | Finds overdue items in TASKS.md |
| `self-check` | System diagnostics: Claude binary, bwrap, DB connectivity, task failure rate, and a live sandboxed execution test |

`shell-command` checks run with `build_stripped_env()`, but arbitrary shell is still arbitrary shell — a non-admin's check returns unhealthy with "shell-command checks are admin-only" rather than running.

`shell-command` runs under `bash -o pipefail -c`, so a probe ending in a pipe reports the first stage that failed rather than the last command. Two things follow. A non-final stage that exits non-zero to *report* something — `grep` with no match, `diff` — now makes the check unhealthy; append `|| true` where that is not what you mean. And a probe ending in `head` or `grep -q` reports status 141, which is the pipe being closed early rather than a failure; the alert message says so. Note also that these run under bash rather than `/bin/sh`, so `echo` no longer expands backslash escapes — if a check compares output against a `condition`, confirm it still matches.

The `self-check` execution test actually spawns a sandboxed `claude` and asks it to echo a known string, which is the only step that proves the whole path works end to end. It is on by default; set `execution_test = false` on the check to skip it.

## Per-check controls

| Setting | Purpose |
|---|---|
| `interval_minutes` | Run expensive checks less frequently |
| `cooldown_minutes` | Prevent alert fatigue (minimum time between alerts) |
| `channel` | Deliver this check's alerts to an explicit surface, overriding the user's routing |

## Global settings

Under `[settings]` in HEARTBEAT.md:

| Setting | Purpose |
|---|---|
| `quiet_hours` | Suppress alerts during off-hours (cross-midnight supported). Global, not per-check |
| `default_cooldown_minutes` | Cooldown for checks that don't set their own (default 60) |
| `conversation_token` | Default room for this user's heartbeat alerts |

State is tracked in the `heartbeat_state` table: last check time, last alert time, last healthy time, last error time, and consecutive error count.

## Alerts

When a check fails, the alert is routed through `surface_for_purpose(config, user_id, "alert")` — the user's purpose-keyed routing table, or the check's own `channel` when it sets one. See [per-user delivery routing](../configuration/per-user.md#delivery-routing).

If the resolved channel isn't configured, the alert is skipped with a warning and the consecutive-error count is *not* incremented — an undeliverable alert is a deployment gap, not a failing check.

The `!check` command in Talk runs an immediate inline health check (the same diagnostics as `self-check`, independent of the heartbeat schedule).

## Configuration

Heartbeat checks are defined in HEARTBEAT.md as markdown with embedded TOML, similar to CRON.md. The scheduler evaluates checks every `heartbeat_check_interval` (default 60s).
