# Scheduler

The scheduler is the central coordinator. It runs a main loop that checks every subsystem on configurable intervals and dispatches worker threads to process tasks.

## Modes

**Daemon mode** (`run_daemon`): Long-running process with a `WorkerPool`. Acquires a file lock on `/tmp/istota-scheduler-daemon.lock` to prevent duplicate instances. Handles SIGTERM/SIGINT for graceful shutdown.

**Single-pass mode** (`run_scheduler`): Runs all checks once, processes tasks until none remain or a `max_tasks` limit is hit, then exits. Used for testing and one-off runs.

## Main loop

```python
while not shutdown_requested:
    check_briefings()           # every briefing_check_interval (60s)
    check_scheduled_jobs()      # every briefing_check_interval
    check_sleep_cycles()        # every briefing_check_interval
    check_channel_sleep_cycles()# every briefing_check_interval
    poll_emails()               # every email_poll_interval (60s)
    organize_shared_files()     # every shared_file_check_interval (120s)
    poll_tasks_files()          # every tasks_file_poll_interval (30s)
    run_cleanup_checks()        # every briefing_check_interval
    check_heartbeats()          # every heartbeat_check_interval (60s)
    check_invoice_schedules()   # every briefing_check_interval
    pool.dispatch()             # spawn workers for users with pending tasks
    sleep(poll_interval)        # 2s
```

Talk polling runs in a separate daemon thread, started at scheduler launch.

## Worker pool

`WorkerPool` manages concurrent `UserWorker` threads with three-tier concurrency control:

**Instance-level caps**: `max_foreground_workers` (default 5) and `max_background_workers` (default 3) limit total concurrent workers by queue type. Dispatch is two-phase: foreground first, then background.

**Per-user limits**: `user_max_foreground_workers` (default 2) and `user_max_background_workers` (default 1) cap how many workers a single user can have. Individual users can override these in their config.

**Per-channel gate**: Before creating a task, the Talk poller checks if an active foreground task exists for the conversation. If so, it queues the message but sends "Still working on a previous request" as an immediate response.

Workers are keyed by `(user_id, queue_type, slot)`. Each `UserWorker` is a thread that processes tasks serially, exiting after `worker_idle_timeout` (30s) of no tasks. Thread safety: fresh DB connections per call, new `asyncio.run()` event loop per worker, `threading.Lock` on the workers dict.

## Task claiming

`claim_task()` uses atomic `UPDATE...RETURNING` with stale lock detection:

1. Fail old stale locked tasks (created > `max_retry_age`, locked > 30 min)
2. Release recent stale locks for retry
3. Fail old stuck running tasks
4. Release recent stuck running tasks for retry
5. Claim next pending: `ORDER BY priority DESC, created_at ASC`

The claim sets `status='locked', locked_at=now, locked_by=worker_id` atomically.

## Task processing

`process_one_task()` handles the full lifecycle:

1. Claim a task (with optional `user_id` filter)
2. Update status to `running`
3. Get user resources, send Talk acknowledgment, download attachments
4. Call `execute_task()` -> `(success, result, actions_taken, execution_trace)`
5. On success:
    - Check for malformed output (leaked tool-call XML) -> reclassify as failure
    - Check for confirmation request (regex pattern)
    - Update to `completed`
    - Index conversation for memory search
    - Deliver results
6. On failure:
    - Check cancellation
    - Retry with exponential backoff if attempts remain (1, 4, 16 min delays)
    - Mark permanently failed after max attempts

## Retry logic

Failed tasks retry with exponential backoff: 1 min, 4 min, 16 min (up to `max_attempts`, default 3). Transient API errors (5xx, 429) get 3 fast retries with 5s delay before counting against task attempts.

## Deferred DB operations

With the bubblewrap sandbox, the DB is read-only inside the subprocess. Claude and skill CLIs write JSON files to a writable temp dir. The scheduler processes these after successful completion:

- `task_{id}_subtasks.json` -- subtask creation (admin-only)
- `task_{id}_tracked_transactions.json` -- transaction dedup tracking
- `task_{id}_sent_emails.json` -- outbound email tracking for emissary thread matching

Identity fields (`user_id`, `conversation_token`) always come from the task, not from the JSON, to prevent spoofing.

## Cleanup

Runs every `briefing_check_interval`:

- Cancel stale confirmations after 120 min, notify user
- Log warnings for tasks pending longer than 30 min
- Auto-fail tasks pending longer than 24 hours
- Delete completed tasks older than `task_retention_days` (7)
- Delete processed emails from IMAP older than `email_retention_days` (7)
- Delete old temp files

## Poller intervals

| Poller | Default interval | Config key |
|---|---|---|
| Task queue | 2s | `poll_interval` |
| Talk conversations | 10s | `talk_poll_interval` |
| Email (IMAP) | 60s | `email_poll_interval` |
| Briefings/jobs/sleep/cleanup | 60s | `briefing_check_interval` |
| TASKS.md files | 30s | `tasks_file_poll_interval` |
| Shared files | 120s | `shared_file_check_interval` |
| Heartbeats | 60s | `heartbeat_check_interval` |
