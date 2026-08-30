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
room = "room123"

[[jobs]]
name = "backup-reminder"
cron = "0 10 * * 1"
prompt = "Remind me to check the backup logs"
target = "talk"
silent_unless_action = true

[[jobs]]
name = "disk-check"
cron = "0 */6 * * *"
command = "df -h / | tail -1"
```
````

**Shell semantics for `command` jobs.** The command runs under `bash -o pipefail -c`, so a pipeline reports the first stage that failed rather than the last command — `<runner> … | tail` no longer comes back clean on a run that failed. Three consequences worth knowing when you write one:

- A non-final stage that exits non-zero to *report* something rather than to fail now fails the whole job. `grep` finding no match is the common one; `diff` and `git diff --quiet` behave the same way. Put those on their own line, or append `|| true` where the status genuinely does not matter.
- A pipeline ending in `head` or `grep -q` closes the pipe early and reports status 141. That is SIGPIPE rather than a failure, the job's `last_error` says so, and it is failed without retrying — the retry would re-run a command whose first stage already did its work.
- The interpreter is bash, not `/bin/sh`. If a job was written against dash, note that `echo` no longer expands backslash escapes such as `\t`.

CRON.md is the source of truth. `cron_loader.py` reads it and syncs job definitions to the `scheduled_jobs` DB table.

## Job types

There is no `type` field. The loader infers the kind from which of `prompt`, `prompt_file`, or `command` a job sets, and a job that sets more than one is skipped with a warning rather than guessed at.

**Prompt jobs** (default): Run through the brain like any other task. The prompt is sent to the executor with full skill and context support.

**Prompt file jobs**: Like prompt jobs, but the prompt is loaded from an external file. Paths are relative to the Nextcloud mount root. `prompt_file` cannot be combined with `prompt` or `command`.

A multiline `prompt` becomes one of these the first time anything rewrites the file — `!cron enable` / `!cron disable`, the removal of a `once` job, or a schedule migration (ISSUE-330). The text is written to `{bot_dir}/scripts/prompts/<job-name>.txt` and the job is left pointing at it. This is not tidiness: the rewriter used to re-serialize a multiline prompt as a TOML block, where a single stray backslash made *every* job in the file fail to load. A `prompt_file` you set yourself is never touched, and an existing file is only reused when its contents already match.

**Command jobs**: Run shell commands in a subprocess (via `_run_capture`, which kills the whole process group on timeout). No brain invocation. Output captured and optionally posted to Talk.

All job types go through the same task queue with retry logic, `!stop` support, failure tracking, and auto-disable.

## Configuration options

| Field | Required | Description |
|---|---|---|
| `name` | yes | Job identifier |
| `cron` | yes | Cron expression (evaluated in user's timezone) |
| `prompt` | for prompt jobs | The prompt to send to Claude |
| `prompt_file` | for prompt_file jobs | Path to prompt file (relative to mount root) |
| `command` | for command jobs | Shell command to execute |
| `room` | no | Talk room token for output |
| `target` | no | `talk`, `email`, `ntfy`, `both`, `all`, or a `surface:channel` / comma-list descriptor |
| `enabled` | no | `false` parks a job without deleting it (default `true`) |
| `once` | no | Auto-delete after successful execution |
| `silent_unless_action` | no | Suppress output unless response has `ACTION:` prefix |
| `skip_log_channel` | no | Suppress log channel output for frequent jobs |
| `model` | no | Claude model override for this job (e.g. `"claude-sonnet-4-6"`) |
| `effort` | no | Effort override: `low`, `medium`, `high`, `xhigh`, or `max` |
| `publish_shared_kv` | no | On success, publish the result text to the shared KV store as `"<namespace>/<key>"` (a bare key means the `briefing_shared_blocks` namespace) |
| `publish_shared_kv_trusted` | no | Mark the published value trusted, so consuming briefings splice it without the untrusted-content wrapper |

## Publishing to shared content

`publish_shared_kv` lets an admin's job write its result where every user's briefings can read it — the escape hatch for shared content the built-in shared-block generator can't produce (it runs tool-less, while a scheduled `prompt` job gets the full sandbox and tools). Shared-KV writes are admin-only: the job's user must pass the writer check, and an unauthorized or failed publish fails loudly (error log, operator alert, and a job-failure increment that the success path withholds). An empty result is a clean skip, leaving the last-known-good value in place.

Consume the published value with a `shared_block` source on a briefing block. See [briefings](briefings.md#shared-curated-content).

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
