# Task lifecycle

A task moves through several stages from creation to completion. This document traces the full path, including which process owns each stage and where data is persisted.

## Status flow

```
pending → locked → running → completed
                           → failed
                           → pending_confirmation → completed (on confirm)
                                                  → cancelled (on deny/timeout)
```

## Creation

Tasks enter the queue from multiple sources:

| Source | Entry point | `source_type` |
|---|---|---|
| Talk message | Talk poller (`talk_poller.py`) | `talk` |
| Email | Email poller (`email_poller.py`) | `email` |
| TASKS.md file | File poller (`tasks_file_poller.py`) | `istota_file` |
| CLI | `istota task` command (`cli.py`) | `cli` |
| Scheduled job | `check_scheduled_jobs()` in scheduler | `scheduled` |
| Briefing | `check_briefings()` in scheduler | `briefing` |
| Subtask | Deferred JSON from a parent task | `subtask` |

All sources call `db.create_task()`, which inserts a row with `status='pending'`.

## Claiming and locking

`claim_task()` runs inside a worker thread. It uses an atomic `UPDATE...RETURNING` to grab the next pending task, setting `status='locked'` with the worker ID and timestamp. Before claiming, it runs stale lock cleanup:

1. Fail tasks locked > 30 min that are too old to retry
2. Release recent stale locks back to `pending`
3. Same for stuck `running` tasks (started > 15 min ago)

Tasks are ordered by `priority DESC, created_at ASC`. Workers filter by `user_id` and `queue` type.

## Execution

After claiming, the worker immediately updates status to `running` and closes the DB connection. Everything from here until result processing happens outside any DB transaction to avoid long locks.

### Command tasks

If the task has a `command` field (shell scheduled jobs), it runs via `_execute_command_task()` — a simple `subprocess.run` with `build_stripped_env()`. No skill selection, no Claude, no prompt assembly.

### Prompt tasks

For all other tasks, `execute_task()` handles the full pipeline:

1. **Skill selection** (two-pass: keyword matching, then LLM routing)
2. **Persist selected skills** to DB via `save_task_selected_skills()`
3. **Load skill docs** and resolve env vars
4. **Context loading** (Talk message cache or email thread)
5. **Memory loading** (USER.md, CHANNEL.md, dated memories, recalled memories)
6. **Prompt assembly** (see [executor docs](executor.md) for section order)
7. **Subprocess launch** (`claude -p <prompt> --output-format stream-json`)
8. **Stream parsing** — tool use and text events forwarded to progress callbacks
9. **Result composition** — handles context management boundaries and terse results

The executor returns `(success, result_text, actions_taken_json, execution_trace_json)`.

### Progress updates

Two independent callback chains run during streaming:

- **Talk progress**: rate-limited updates to the user's conversation (ack message editing)
- **Log channel**: every tool call posted to the operator's log channel (no rate limiting)

When both are active, they're composed into a single callback. Each maintains its own state (message IDs, description lists).

## Result processing

Back in the scheduler, `process_one_task()` handles the result inside a DB transaction:

### Success path

1. **API error guard**: detect API errors masquerading as success (exit 0 with error text)
2. **Malformed output guard**: detect leaked tool-call XML — reclassify as failure
3. **Confirmation check**: regex match for confirmation requests → `pending_confirmation`
4. **Update to `completed`**: stores result, actions_taken, execution_trace
5. **Memory search indexing**: index conversation under user and channel namespaces
6. **Delivery routing**: Talk, email, ntfy, or TASKS.md based on `output_target`

### Failure path

1. Check if task was cancelled by user (`!stop` command)
2. Retry with exponential backoff (1, 4, 16 min) if attempts remain
3. Mark permanently `failed` after `max_attempts` (default 3)
4. Track scheduled job consecutive failures; auto-disable after threshold

## Post-completion

After the DB transaction closes:

1. **Deferred operations**: process JSON files from the sandbox temp dir (subtasks, transaction tracking, sent emails, KV ops, user alerts, email output)
2. **Briefing digest**: save for next-run deduplication
3. **Talk progress finalize**: edit ack message with final summary
4. **Log channel finalize**: edit/post completion message with skills and tool summary
5. **Result delivery**: post to Talk, send email reply, or update TASKS.md

## Log channel messages

When a user has `log_channel` configured, each task gets a log channel entry showing:

```
**[#12345]** ✅ Done (3 actions) - #channel-name
Skills: calendar, email, files, memory, sensitive_actions
📅 Listed calendar events
📧 Sent email reply
📄 Read USER.md
```

The skills line is populated by reading `selected_skills` from the DB after task completion. Controlled by `log_channel_show_skills` (default: true) in the `[scheduler]` config section.

## Data flow gotchas

### Column visibility in get_task()

The `get_task()` function uses an explicit column list in its SELECT, not `SELECT *`. When adding new columns to the tasks table, you must update three places:

1. The `ALTER TABLE` migration in `_run_migrations()`
2. The `_row_to_task()` mapping (with `in row.keys()` fallback)
3. **The SELECT column list in `get_task()`** — easy to forget, and `_row_to_task` silently falls back to `None`

### Skills are saved before execution, read after

`save_task_selected_skills()` runs early in `execute_task()`, before the Claude subprocess launches. The log channel finalize reads them back from the DB after the task completes. Any code path that clears or overwrites the row between those points would lose the skills data.

### DB connections are short-lived

The scheduler opens and closes DB connections for each phase (claim, execute, result processing, finalize). This is intentional — long-held connections would block other workers via SQLite's write lock. Each `with db.get_db()` block is a separate transaction.

### Command tasks skip the executor

Shell command tasks (`task.command` is set) bypass `execute_task()` entirely. They have no skill selection, no prompt, no streaming. Their log channel entries will never show skills.
