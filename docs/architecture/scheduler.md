# Scheduler

The scheduler is the central coordinator. It runs a main loop that checks every subsystem on configurable intervals and dispatches worker threads to process tasks.

## Modes

**Daemon mode** (`run_daemon`): Long-running process with a `WorkerPool`. Acquires a file lock on `/tmp/istota-scheduler-daemon.lock` to prevent duplicate instances. Handles SIGTERM/SIGINT for graceful shutdown. It also starts the persistent `AsyncRuntime` (see below) explicitly and stops it on shutdown.

**Single-pass mode** (`run_scheduler`): Runs all checks once, processes tasks until none remain or a `max_tasks` limit is hit, then exits. Used for testing and one-off runs. It shares `process_one_task` (which uses `run_coro`), so it lazily starts the same persistent runtime, then calls `reset_async_runtime()` before returning for a clean shutdown.

## Persistent asyncio runtime

All Nextcloud Talk I/O runs on **one** long-lived asyncio loop on a dedicated daemon thread, against **one** pooled `httpx.AsyncClient`, instead of a fresh `asyncio.run` loop + fresh client per call (`src/istota/async_runtime.py`). This gives TCP/TLS connection reuse to Nextcloud and removes the per-call loop-teardown leak surface.

- **`AsyncRuntime`** owns the loop thread; `submit(coro, *, timeout=None)` bridges sync→async via `run_coroutine_threadsafe`. `stop(timeout=10)` cancels in-flight coroutines, runs cleanup hooks (closing the shared client), then stops the loop — cancel-before-aclose so a hook can't close the client out from under a live request.
- **`run_coro(coro, *, timeout=None)`** is the workhorse every sync Talk call site uses (`run_coro(post_result_to_talk(...))`, `run_coro(poll_talk_conversations(config))`, …). It lazily starts the process-global runtime on first use.
- **`get_talk_client(config)`** is a process-global persistent `TalkClient` singleton; every Talk delivery path pulls from it so they share one connection pool.

**Invariant:** every `TalkClient` method invocation must end up on the persistent loop (via `run_coro`), because the methods issue requests on the loop-bound client. There are no transient `TalkClient(config)` constructions left in daemon Talk paths. Email delivery stays on `asyncio.run` (sync SMTP, not httpx).

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

### Stuck-running detection by worker liveness (ISSUE-112)

Steps 3–5 (and the standalone `fail_stuck_locked_running_tasks()` maintenance pass) share `_STUCK_RUNNING_PREDICATE`, which decides "stuck" by **worker liveness**, not raw runtime. A `running` task is stuck when its `last_heartbeat` has been silent longer than `worker_stuck_minutes` (default 5); when no heartbeat was ever recorded it falls back to `started_at` older than `task_timeout_minutes` + grace. The running worker pings `last_heartbeat` every `worker_heartbeat_seconds` (default 60) via the `_task_heartbeat` context manager (`db.touch_task_heartbeat`), so a slow-but-alive worker — notably the in-process native brain, which has no killable PID — is never reclaimed, while a crashed worker is recovered in minutes. (This is distinct from the health-check heartbeat system in `heartbeat.py`.)

`claim_task` and every other `Task`-returning helper (`get_task`, `get_pending_confirmation*`, `get_reply_parent_task`, `get_stale_pending_tasks`, `get_completed_*_since`) route their SELECT/RETURNING through a single `_TASK_COLUMNS` constant in `db.py`. Adding a column means editing one place; missing columns now raise `IndexError` rather than silently returning `None` (the failure mode that masked the brief 027eb1a regression where `task.skill` came back unset and module-poller rows fell through to the LLM path with an empty prompt).

## Task dispatch

`process_one_task` decides between three execution paths based on the task's columns:

| Task shape | Dispatcher | Notes |
|---|---|---|
| `task.skill` set | `_execute_skill_task()` | `python -m istota.skills.<skill>` subprocess. Trusted env via `build_skill_env(list(skill_index), skill_index, ctx)` over the **full** index, so co-declared vars (e.g. `NC_URL` declared on both `files` and `nextcloud`) reach the subprocess. No proxy split. |
| `task.command` set | `_execute_command_task()` | Shell command. Admin-gated (non-admin tasks refused at runtime + dropped at sync time). Same trusted env resolver as skill-tasks. JSON `{"status":"error","error":"…"}` envelopes on stdout are detected and surfaced as failures even when returncode is 0. |
| neither | LLM path via the brain | Default; runs through `execute_task` |

Auto-seeded `_module.feeds.run_scheduled` / `_module.money.run_scheduled` rows dispatch as skill-tasks. `_purge_obsolete_skill_jobs` removes rows whose skill name is no longer in the index.

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

## Task event streaming

One persistent, typed event stream per task feeds every output surface. `process_one_task` builds an `EventWriter` (`events.py`) per brain-path task and subscribes the in-process consumers (`TalkEventSubscriber`, `LogChannelSubscriber`, `PushNotificationSubscriber`) before passing the writer to `execute_task(event_writer=…)`. The executor adapts the brain's `StreamEvent` stream into `TaskEvent`s, persisted to the `task_events` table (WAL, shared scheduler ⇄ web). When the task reaches a non-retry terminal state the scheduler emits the terminal event (`confirmation` / `result` / `cancelled` / `error` + `done`) and calls `writer.finish()`.

**Retry continuity:** on a retry-eligible failure the event log is kept (not wiped). The retry branch emits a `progress_text` "⏳ Attempt failed — retrying in N min…" notice, and the next attempt's `EventWriter` resumes `seq` from `db.get_max_task_event_seq` so it stays monotonic across attempts and a watching web client survives the retry instead of hanging on "Working…". The SSE / snapshot endpoints synthesize a terminal frame from the task row (`web_app._synthetic_terminal_events`) for any terminal-without-`done` gap (e.g. a crash that skipped `finish()`).

Config under `[scheduler]`: `progress_show_tool_use`, `progress_show_text`, `event_log_enabled`, `stream_text_gate_chars`, `push_notification_threshold_seconds`, `push_notification_sources`.

## Delivery routing

Where a task's result goes is resolved by `transport.routing.resolve_delivery_plan(config, task, registry)`, which turns a task into an ordered, deduplicated, channel-resolved list of destinations. Precedence: explicit `output_target` > reply-to-origin (interactive source types) > source-type default > drop. `process_one_task` builds the plan once and fans out to every push destination; `stream` destinations (REPL, web) contribute no push work — the `task_events` log is the delivery. Separately, a per-user **purpose-keyed routing table** (`UserConfig.routing`, purposes `reply`/`alert`/`log`/`briefing`/`notification`) routes *notifications* via `notifications.send_notification(..., purpose=…)`. See the [Transport abstraction](overview.md) and `.claude/rules/transport.md`.

## Deferred DB operations

With the bubblewrap sandbox, the DB is read-only inside the subprocess. Claude and skill CLIs write JSON files to a writable temp dir. The scheduler processes these after successful completion. The handlers and the file envelope helper live in `scheduler_deferred.py` (extracted from `scheduler.py` for size and testability; `scheduler.py` keeps a re-export shim so `from istota.scheduler import _process_deferred_*` still works).

| File | Handler | Purpose |
|---|---|---|
| `task_{id}_subtasks.json` | `_process_deferred_subtasks` | Subtask creation (admin-only, depth- and rate-capped) |
| `task_{id}_tracked_transactions.json` | `_process_deferred_tracking` | Transaction dedup tracking |
| `task_{id}_sent_emails.json` | `_process_deferred_sent_emails` | Outbound email tracking for emissary thread matching |
| `task_{id}_kv_ops.json` | `_process_deferred_kv_ops` | KV store set/delete operations |
| `task_{id}_kg_ops.json` | `_process_deferred_kg_ops` | Knowledge-graph fact add/invalidate/delete (per-op commit) |
| `task_{id}_user_alerts.json` | `_process_deferred_user_alerts` | Alerts/notifications for the user's alerts channel |
| `task_{id}_email_output.json` | `_load_deferred_email_output` | Structured email reply (preferred over the legacy stdout-JSON parser) |

`_load_deferred_json(user_temp_dir, task_id, suffix, expected_type=...)` is the shared envelope helper: builds the path, exists-checks, parses JSON, validates the top-level shape (`list` or `dict`), and warns + unlinks on a malformed file. Each handler then runs its own business logic and unlinks at the call site so per-handler invariants (admin gate, depth gate, KG per-op commit) read cleanly.

`_purge_deferred_files_for_retry` clears the slate when a task is set back to `pending_retry`, so a non-idempotent op like a KG `invalidate` isn't replayed twice across attempts. `_warn_unconsumed_deferred_files` scans the user temp dir after the drain phase and logs WARN for files missing the `task_` prefix or carrying an unknown suffix; the misnamed file is left on disk for inspection.

Identity fields (`user_id`, `conversation_token`) always come from the task, not from the JSON, to prevent spoofing.

## Cleanup

Runs every `briefing_check_interval`:

- Cancel stale confirmations after 120 min, notify user
- Log warnings for tasks pending longer than 30 min
- Auto-fail tasks pending longer than `stale_pending_fail_hours` (2)
- Delete completed tasks older than `task_retention_days` (7)
- Delete processed emails from IMAP older than `email_retention_days` (7)
- Delete old temp files

## Poller intervals

| Poller | Default interval | Config key |
|---|---|---|
| Task queue | 2s | `poll_interval` |
| Pending-task dispatch sub-tick | 0.5s | `dispatch_interval` |
| Talk conversations | 10s | `talk_poll_interval` |
| Email (IMAP) | 60s | `email_poll_interval` |
| Briefings/jobs/sleep/cleanup | 60s | `briefing_check_interval` |
| TASKS.md files | 30s | `tasks_file_poll_interval` |
| Shared files | 120s | `shared_file_check_interval` |
| Heartbeats | 60s | `heartbeat_check_interval` |
| SQLite health (`quick_check` + self-heal `REINDEX`) | 86400s (24h) | `db_health_check_interval` |

`dispatch_interval` decouples cold pending-task pickup latency from the interval-gated checks: the main loop runs `pool.dispatch()` on this sub-tick cadence without re-running the per-subsystem checks (0 or ≥ `poll_interval` = legacy one-dispatch-per-tick). `cron_max_staleness_minutes` (default 60) is the insertion-time staleness gate for `check_scheduled_jobs` / `check_briefings` — after a long outage it skips the catch-up insert and resumes from the next future fire, suppressing thundering-herd catch-up.
