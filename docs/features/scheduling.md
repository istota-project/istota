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

Deleting a job from the file deletes it. That includes the last one: a file whose TOML block is empty means you have no jobs, and the sync removes the rows to match. Two shapes are not that, and neither deletes anything:

- A file with no TOML block, or one holding only comments, has had no job list written into it. That is what a freshly seeded CRON.md looks like, so if the table holds jobs they are written into the file instead — which is how a workspace whose CRON.md went missing fills back in. Emptying the block by hand but leaving a comment in it lands here too; delete the comment as well if you meant the jobs to go.
- A file that lists jobs the loader cannot use — a typo in a required key, a `prompt_file` it cannot read — is left alone with a warning in the log, and so are the existing rows. Fix the file and the next tick picks it up.

Rewriting CRON.md replaces the first TOML block and nothing else. Notes you keep above or below the fence, a second fenced block, the header you rewrote — all of it survives `!cron disable`, the removal of a `once` job and the rest. What does not survive is anything inside the block that is not a job the loader accepted: comments there, and a job entry it skipped, are both re-rendered away. The one case that still rewrites the whole file is the seeded-template restore above, which has no job list to splice into.

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
| `brain` | no | Brain kind this job runs on: `claude_code`, `native` or `tmux_claude`. Admin-only, and bounded by `[brain] room_selectable`. See below |
| `publish_shared_kv` | no | On success, publish the result text to the shared KV store as `"<namespace>/<key>"` (a bare key means the `briefing_shared_blocks` namespace) |
| `publish_shared_kv_trusted` | no | Mark the published value trusted, so consuming briefings splice it without the untrusted-content wrapper |

The five flags — `enabled`, `once`, `silent_unless_action`, `skip_log_channel` and `publish_shared_kv_trusted` — take a bare TOML boolean, `true` or `false`. A quoted `"false"` is a string and a bare `1` is an integer; either one logs a warning and the field takes its default rather than being read for truthiness. `enabled = "false"` used to leave the job running, and `once = "false"` used to delete it after one run.

## Choosing a brain per job

`brain` pins one job to one brain kind — a nightly summary on `native`, a repo-touching job on `claude_code` — without moving the instance default. It is the same mechanism a chat room's `!brain` uses, bounded by the same operator allowlist, `[brain] room_selectable`. That list is empty by default, so nothing here works until an operator names kinds.

Writing the field is admin-only. CRON.md is a file on the mount and is also written by the model through the `schedules` skill, and a brain kind decides which process holds the agent loop, which credentials it carries and which sandbox profile is built — so it should not be the one enforcement-shaped setting a task can choose for its own next run. A non-admin's `brain` is dropped at each sync, with a warning, and the rest of the job is kept and runs the configured brain. That gate is the multi-user deployment's, as the one on `command` jobs already is: an empty `/etc/istota/admins` means everyone is an admin, so on the single-user install nothing is dropped and a task writing CRON.md can pin a brain.

The sync answers who may write the field, not what they may write. `!cron` shows the stored value, so the listing never shows a pin the author was not allowed to write — but an admin's pin is stored whatever it names, and a kind the operator has not allowlisted is refused later, at dispatch. A kind that is not a real brain kind is warned about when the file is read; an unlisted one is warned about when the job fires. Neither fails the job, and neither is visible in the listing: it falls through to whatever the deployment would have run anyway.

**A pinned job does not fail over.** If the pinned brain is unavailable — a usage limit, a missing binary, a tmux launch failure — the run fails with that brain's own reason instead of being answered by the deployment's backup brain, which for unattended work is the safer of the two. The failure is not silent: the task's retries run out, at 1, 4 and 16 minutes or immediately for an error that cannot be retried, `consecutive_failures` climbs, the job auto-disables at five and raises a notice in the inbox. `!cron enable <name>` restarts it once the brain is back.

`model` is resolved by whichever brain the job will actually run. A portable name like `smart` therefore lands on that brain's own model rather than on the deployment default's, which is what makes a per-job brain and a per-job model usable together.

## Publishing to shared content

`publish_shared_kv` lets an admin's job write its result where every user's briefings can read it — the escape hatch for shared content the built-in shared-block generator can't produce (it runs tool-less, while a scheduled `prompt` job gets the full sandbox and tools). Shared-KV writes are admin-only: the job's user must pass the writer check, and an unauthorized or failed publish fails loudly (error log, operator alert, and a job-failure increment that the success path withholds). An empty result is a clean skip, leaving the last-known-good value in place.

Consume the published value with a `shared_block` source on a briefing block. See [briefings](briefings.md#shared-curated-content).

## Failure handling

Jobs auto-disable after 5 consecutive failures (`scheduled_job_max_consecutive_failures`). Failures reset on success.

A job stopped that way is *suspended*, which is a different thing from the `enabled = false` you write in CRON.md. Suspension is recorded in `scheduled_jobs.auto_disabled_at`, a column the file cannot express and the sync never writes; `enabled` stays whatever the file says, because the user never asked for the job to stop. A job runs only when it is enabled and not suspended. `!cron` and the admin dashboard render the two apart — `DISABLED` for the first, `SUSPENDED` for the second.

Until this split the sync wrote `enabled` back from CRON.md on every tick, roughly once a minute, so auto-disable did nothing at all for a job defined in a file and a job that failed every run kept running every run.

Three things lift a suspension:

- a successful run;
- `!cron enable <name>` in Talk, which lifts the suspension, clears the failure count and writes `enabled = true` back into CRON.md;
- an edit in CRON.md to what the job dispatches — `cron`, `prompt`, `command`, `skill` or `skill_args`. Fixing the thing that was failing is how most people will expect to restart a job, so it counts. Changing `target`, `room`, `model`, `effort`, `brain` or a flag does not.

`brain` is outside that set for the same reason `model` and `effort` are: the set is what the job dispatches, and a rule a reader can hold is worth more than covering one more plausible repair. A job suspended because its pinned brain was down is restarted by `!cron enable` after the brain comes back, not by editing the field.

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
!cron enable NAME  # Re-enable a disabled job, or lift a suspension
!cron disable NAME # Disable a job
```
