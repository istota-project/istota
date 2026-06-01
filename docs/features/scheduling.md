# Scheduling

Istota supports cron-based scheduling through CRON.md files and natural-language reminders.

## CRON.md

Each user can define scheduled jobs in `/Users/{user_id}/{bot_dir}/config/CRON.md`. The file uses markdown with an embedded TOML block:

````markdown
# Scheduled jobs

```toml
[[jobs]]
name = "daily-check"
cron = "0 9 * * *"
prompt = "Check my calendar for today and summarize what's coming up"
conversation_token = "room123"

[[jobs]]
name = "backup-reminder"
cron = "0 10 * * 1"
prompt = "Remind me to check the backup logs"
output_target = "talk"
silent_unless_action = true

[[jobs]]
name = "disk-check"
cron = "0 */6 * * *"
type = "command"
command = "df -h / | tail -1"
```
````

CRON.md is the source of truth. `cron_loader.py` reads it and syncs job definitions to the `scheduled_jobs` DB table.

## Job types

**Prompt jobs** (default): Run through Claude Code like any other task. The prompt is sent to the executor with full skill and context support.

**Prompt file jobs**: Like prompt jobs, but the prompt is loaded from an external file. Paths are relative to the Nextcloud mount root.

**Command jobs**: Run shell commands directly via `subprocess.run()`. No Claude invocation. Output captured and optionally posted to Talk.

All job types go through the same task queue with retry logic, `!stop` support, failure tracking, and auto-disable.

## Configuration options

| Field | Required | Description |
|---|---|---|
| `name` | yes | Job identifier |
| `cron` | yes | Cron expression (evaluated in user's timezone) |
| `prompt` | for prompt jobs | The prompt to send to Claude |
| `prompt_file` | for prompt_file jobs | Path to prompt file (relative to mount root) |
| `command` | for command jobs | Shell command to execute |
| `type` | no | `"prompt"` (default), `"prompt_file"`, or `"command"` |
| `conversation_token` | no | Talk room for output |
| `output_target` | no | `"talk"` or `"email"` |
| `once` | no | Auto-delete after successful execution |
| `silent_unless_action` | no | Suppress output unless response has `ACTION:` prefix |
| `skip_log_channel` | no | Suppress log channel output for frequent jobs |
| `model` | no | Claude model override for this job (e.g. `"claude-sonnet-4-6"`) |
| `effort` | no | Effort override: `low`, `medium`, `high`, `xhigh`, or `max` |

## Failure handling

Jobs auto-disable after 5 consecutive failures (`scheduled_job_max_consecutive_failures`). Failures reset on success. Disabled jobs can be re-enabled via `!cron enable <name>` in Talk.

## Catch-up suppression after outages

When the daemon returns from a long outage, it does not fire every missed cron instance at once. If a job or briefing's computed next fire time is more than `cron_max_staleness_minutes` (default 60) behind the current time, the fire is skipped and `last_run_at` is bumped so the schedule resumes from the next future fire. This prevents a thundering herd of stale tasks from flooding the queue after a restart.

Set `cron_max_staleness_minutes = 0` in `[scheduler]` to restore the prior behavior (unconditional catch-up of all missed fires).

## Context isolation

Scheduled job results are excluded from interactive conversation context. This prevents cron output from cluttering a user's chat history.

## Reminders

Natural-language reminders are implemented as one-shot cron entries (`once = true`). When a user asks "remind me to do X tomorrow at 9am", the bot creates a CRON.md entry that fires once and auto-deletes.

## Managing jobs

In Talk, use `!cron` to list, enable, or disable scheduled jobs:

```
!cron              # List all jobs with status
!cron enable NAME  # Re-enable a disabled job
!cron disable NAME # Disable a job
```
