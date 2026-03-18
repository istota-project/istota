# Scheduler & DB Internals

## Scheduler Functions

### `run_daemon()`
```python
def run_daemon(config: Config) -> None
```
1. Acquire flock on `/tmp/istota-scheduler-daemon.lock`
2. Set SIGTERM/SIGINT handlers
3. Hydrate user configs from Nextcloud API
4. Ensure user directories
5. Start Talk polling in daemon thread
6. Create `WorkerPool`
7. Main loop (while not `_shutdown_requested`):
   - Check briefings (every `briefing_check_interval`)
   - Check scheduled jobs (every `briefing_check_interval`)
   - Check sleep cycles (every `briefing_check_interval`)
   - Check channel sleep cycles (every `briefing_check_interval`)
   - Poll emails (every `email_poll_interval`)
   - Organize shared files (every `shared_file_check_interval`)
   - Poll TASKS.md files (every `tasks_file_poll_interval`)
   - Run cleanup checks (every `briefing_check_interval`)
   - Check heartbeats (every `heartbeat_check_interval`)
   - Check invoice schedules (every `briefing_check_interval`)
   - `pool.dispatch()`
   - Sleep `poll_interval`
8. Shutdown workers, release lock

### `run_scheduler()`
```python
def run_scheduler(config: Config, max_tasks: int | None = None, dry_run: bool = False) -> int
```
Single-pass mode: runs all checks once, then processes tasks until none remain or `max_tasks` hit.

### `process_one_task()`
```python
def process_one_task(config: Config, dry_run: bool = False, user_id: str | None = None) -> tuple[int, bool] | None
```
1. `claim_task()` with user_id filter → None if nothing
2. Update to `running`
3. Get user resources, send Talk ack, download attachments
4. `execute_task()` → (success, result, actions_taken)
5. **Success path**:
   - Check confirmation request (regex `CONFIRMATION_PATTERN`)
   - Update to `completed` with `actions_taken`
   - Index conversation for memory search
   - Handle heartbeat results / silent scheduled jobs
   - Deliver results
   - Reset scheduled job failures
6. **Failure path**:
   - Check cancellation
   - Retry with backoff if attempts remain (1, 4, 16 min)
   - Mark failed permanently
   - Track scheduled job failures, auto-disable after threshold
7. Deliver results (Talk/email) outside DB context

## Deferred DB Operations

After successful task completion (not confirmation, not failure), the scheduler processes deferred JSON files from the user temp dir:

**`_process_deferred_subtasks(config, task, user_temp_dir)`**:
- Reads `task_{id}_subtasks.json` — array of `{prompt, conversation_token?, priority?}`
- Admin-only: non-admin files are ignored and deleted
- Creates tasks via `db.create_task()` with `source_type="subtask"`, inherits `queue` from parent
- Deletes file after processing

**`_process_deferred_tracking(config, task, user_temp_dir)`**:
- Reads `task_{id}_tracked_transactions.json` — `{monarch_synced: [...], csv_imported: [...], monarch_recategorized: [...]}`
- Calls `db.track_monarch_transactions_batch()`, `db.track_csv_transactions_batch()`, `db.mark_monarch_transaction_recategorized()`
- Deletes file after processing

**`_process_deferred_sent_emails(config, task, user_temp_dir)`**:
- Reads `task_{id}_sent_emails.json` — array of `{message_id, to_addr, subject, thread_id, ...}`
- Records outbound emails in `sent_emails` table for emissary thread matching
- Enables reply routing: when external contacts reply, References headers match against sent_emails
- Deletes file after processing

**Why deferred**: With bubblewrap sandbox, DB is RO inside the sandbox. Claude and skill CLIs write JSON to the always-RW temp dir; the scheduler (unsandboxed) processes them.

## WorkerPool
```python
class WorkerPool:
    def __init__(self, config: Config)
    def dispatch(self) -> None        # Two-phase: fg first (fg cap), then bg (bg cap)
    def _on_worker_exit(self, user_id: str, queue_type: str, slot: int) -> None
    def shutdown(self) -> None         # request_stop + join(10s)
    @property active_count -> int
```
- Thread-safe via `threading.Lock` on `_workers` dict
- Workers keyed by `(user_id, queue_type, slot)` 3-tuple — allows multiple workers per user per queue
- Foreground cap: `max_foreground_workers` (default 5)
- Background cap: `max_background_workers` (default 3)
- Per-user caps: `effective_user_max_fg_workers(user_id)` / `effective_user_max_bg_workers(user_id)` (global default via `user_max_foreground_workers`/`user_max_background_workers`, overridable per user)
- Workers only spawned up to `min(per_user_cap, pending_task_count)` to avoid idle workers

## UserWorker
```python
class UserWorker(threading.Thread):
    def __init__(self, user_id: str, config: Config, pool: WorkerPool, queue_type: str, slot: int)
    def run(self) -> None
    def request_stop(self) -> None
```
- Loops calling `process_one_task(config, user_id=user_id)`
- Exits after `worker_idle_timeout` seconds of no tasks
- Each worker creates fresh DB connections and `asyncio.run()` event loops

## Poller Integrations

| Poller | Function | Interval Config | State Table |
|---|---|---|---|
| Talk | `_talk_poll_loop()` | `talk_poll_interval` | `talk_poll_state` |
| Email | `poll_emails()` (email_poller.py) | `email_poll_interval` | `processed_emails` |
| TASKS.md | `poll_all_tasks_files()` (tasks_file_poller.py) | `tasks_file_poll_interval` | `istota_file_tasks` |
| Heartbeat | `check_heartbeats()` (heartbeat.py) | `heartbeat_check_interval` | `heartbeat_state` |
| Shared files | `discover_and_organize_shared_files()` (shared_file_organizer.py) | `shared_file_check_interval` | `user_resources` |
| Briefings | `check_briefings()` | `briefing_check_interval` | `briefing_state` |
| Scheduled jobs | `check_scheduled_jobs()` | `briefing_check_interval` | `scheduled_jobs` |
| Sleep cycle | `check_sleep_cycles()` (sleep_cycle.py) | `briefing_check_interval` | `sleep_cycle_state` |
| Channel sleep | `check_channel_sleep_cycles()` (sleep_cycle.py) | `briefing_check_interval` | `channel_sleep_cycle_state` |
| Invoice sched | `check_scheduled_invoices()` (invoice_scheduler.py) | `briefing_check_interval` | `invoice_schedule_state` |

## Cleanup (`run_cleanup_checks`)
1. Expire stale confirmations → notify user via Talk
2. Log warnings for stale pending tasks
3. Fail ancient pending tasks → notify user
4. Clean old completed tasks (`task_retention_days`)
5. Clean old emails from IMAP (`email_retention_days`)
6. Clean old temp files (`temp_file_retention_days`)

## Memory Search Integration
After task completion, if enabled + `auto_index_conversations`:
- Index under `user_id`
- Also index under `channel:{conversation_token}` if in channel

## Config Intervals (SchedulerConfig defaults)

| Param | Default | Used By |
|---|---|---|
| `poll_interval` | 2s | Main loop sleep, worker poll |
| `talk_poll_interval` | 10s | Talk poller |
| `talk_poll_timeout` | 30s | Talk long-poll |
| `email_poll_interval` | 60s | Email poller |
| `briefing_check_interval` | 60s | Briefings, jobs, sleep, cleanup, invoices |
| `tasks_file_poll_interval` | 30s | TASKS.md poller |
| `shared_file_check_interval` | 120s | Shared file organizer |
| `heartbeat_check_interval` | 60s | Heartbeat checks |
| `worker_idle_timeout` | 30s | Worker thread exit |
| `max_foreground_workers` | 5 | Instance-level fg worker cap |
| `max_background_workers` | 3 | Instance-level bg worker cap |
| `user_max_foreground_workers` | 2 | Global per-user fg default |
| `user_max_background_workers` | 1 | Global per-user bg default |
| `task_timeout_minutes` | 30 | Claude Code timeout |
| `confirmation_timeout_minutes` | 120 | Confirmation expiry |
| `max_retry_age_minutes` | 60 | Max age for retry |
| `stale_pending_fail_hours` | 2 | Ancient task auto-fail |
| `task_retention_days` | 7 | Task cleanup |
| `scheduled_job_max_consecutive_failures` | 5 | Auto-disable threshold |

## Other Scheduler Functions

| Function | Purpose |
|---|---|
| `get_worker_id()` | `{hostname}-{pid}[-{user_id}]` |
| `_make_talk_progress_callback()` | Rate-limited progress to Talk |
| `post_result_to_talk()` | Send result to Talk conversation |
| `_parse_email_output()` | Parse JSON email response (legacy fallback) |
| `_load_deferred_email_output()` | Load structured email output from deferred file |
| `_process_deferred_sent_emails()` | Record outbound emails for emissary thread matching |
| `post_result_to_email()` | Send email reply with threading |
| `check_briefings()` | Cron-based briefing scheduling |
| `check_scheduled_jobs()` | Cron-based job scheduling |
| `cleanup_old_temp_files()` | Remove old temp files |

---

# DB Module (db.py)

## All Tables

| Table | Dataclass | Key Columns |
|---|---|---|
| `tasks` | `Task` | id, status, source_type, user_id, prompt, conversation_token, priority, attempt_count, max_attempts, cancel_requested, worker_pid, locked_at/by, scheduled_for, output_target, talk_message_id, reply_to_talk_id, heartbeat_silent, scheduled_job_id, actions_taken |
| `user_resources` | `UserResource` | id, user_id, resource_type, resource_path, display_name, permissions |
| `briefing_configs` | `BriefingConfig` | id, user_id, name, cron_expression, conversation_token, components (JSON), enabled |
| `briefing_state` | — | user_id, briefing_name, last_run_at |
| `processed_emails` | `ProcessedEmail` | id, email_id, sender_email, subject, thread_id, message_id, references, user_id, task_id |
| `istota_file_tasks` | `IstotaFileTask` | id, user_id, content_hash, original_line, normalized_content, status, task_id, file_path |
| `scheduled_jobs` | `ScheduledJob` | id, user_id, name, cron_expression, prompt, conversation_token, output_target, enabled, silent_unless_action, consecutive_failures |
| `talk_poll_state` | — | conversation_token, last_known_message_id |
| `sleep_cycle_state` | — | user_id, last_run_at, last_processed_task_id |
| `channel_sleep_cycle_state` | — | conversation_token, last_run_at, last_processed_task_id |
| `heartbeat_state` | `HeartbeatState` | user_id, check_name, last_check_at, last_alert_at, last_healthy_at, consecutive_errors |
| `reminder_state` | `ReminderState` | user_id, queue (JSON), content_hash |
| `monarch_synced_transactions` | — | id, user_id, monarch_transaction_id, amount, merchant, content_hash |
| `csv_imported_transactions` | — | id, user_id, content_hash, source_file |
| `invoice_schedule_state` | `InvoiceScheduleState` | user_id, client_key, last_reminder_at, last_generation_at |
| `invoice_overdue_notified` | — | user_id, invoice_number, notified_at |
| `user_skills_fingerprint` | — | user_id, fingerprint, updated_at |
| `sent_emails` | — | id, user_id, task_id, message_id, to_addr, subject, thread_id, in_reply_to, references, conversation_token, sent_at |
| `task_logs` | — | task_id, level, message, timestamp |
| `memory_chunks` | — | (from memory_search.py schema) |

## Key DB Functions

### Task Operations
```python
create_task(conn, prompt, user_id, source_type="cli", conversation_token=None,
    parent_task_id=None, is_group_chat=False, attachments=None, priority=5,
    scheduled_for=None, output_target=None, talk_message_id=None,
    reply_to_talk_id=None, reply_to_content=None,
    heartbeat_silent=False, scheduled_job_id=None) -> int

claim_task(conn, worker_id, max_retry_age_minutes=60, user_id=None) -> Task | None
get_task(conn, task_id) -> Task | None
update_task_status(conn, task_id, status, result=None, error=None, actions_taken=None) -> None
set_task_pending_retry(conn, task_id, error, retry_delay_minutes) -> None
set_task_confirmation(conn, task_id, confirmation_prompt) -> None
confirm_task(conn, task_id) -> None
cancel_task(conn, task_id) -> None
cancel_pending_confirmations(conn, conversation_token, user_id) -> int
is_task_cancelled(conn, task_id) -> bool
list_tasks(conn, status=None, user_id=None, limit=50) -> list[Task]
get_users_with_pending_tasks(conn) -> list[str]
get_users_with_pending_interactive_tasks(conn) -> list[str]
get_users_with_pending_background_tasks(conn) -> list[str]
```

### `claim_task()` Locking Mechanism
1. Fail old stale locked tasks (created > max_retry_age, locked > 30min)
2. Release recent stale locks for retry
3. Fail old stuck running tasks
4. Release recent stuck running for retry
5. Fail stuck running if retries exhausted
6. Atomic `UPDATE...RETURNING` to claim next pending
   - Filters by `user_id` if provided
   - Orders by `priority DESC, created_at ASC`
   - Sets `status='locked', locked_at=now, locked_by=worker_id`

### Conversation & Context
```python
get_conversation_history(conn, conversation_token, exclude_task_id=None,
    limit=10, exclude_source_types=None) -> list[ConversationMessage]
get_reply_parent_task(conn, conversation_token, reply_to_talk_id) -> Task | None
```
`ConversationMessage`: `id, prompt, result, created_at, actions_taken`

### Other Key Functions
```python
# Resources
get_user_resources(conn, user_id, resource_type=None) -> list[UserResource]
add_user_resource(conn, user_id, resource_type, resource_path, display_name, permissions="read") -> int

# Briefings
get_briefing_last_run(conn, user_id, briefing_name) -> str | None
set_briefing_last_run(conn, user_id, briefing_name) -> None

# Scheduled jobs
get_enabled_scheduled_jobs(conn) -> list[ScheduledJob]
increment_scheduled_job_failures(conn, job_id, error) -> int
reset_scheduled_job_failures(conn, job_id) -> None
disable_scheduled_job(conn, job_id) -> None

# Cleanup
expire_stale_confirmations(conn, timeout_minutes) -> list[dict]
fail_ancient_pending_tasks(conn, fail_hours) -> list[dict]
cleanup_old_tasks(conn, retention_days) -> int

# Sleep cycle
get_sleep_cycle_last_run(conn, user_id) -> tuple[str | None, int | None]
set_sleep_cycle_last_run(conn, user_id, last_task_id=None) -> None
get_completed_tasks_since(conn, user_id, since, after_task_id) -> list[Task]

# Heartbeat
get_heartbeat_state(conn, user_id, check_name) -> HeartbeatState | None
update_heartbeat_state(conn, user_id, check_name, **kwargs) -> None

# Sent emails (emissary thread tracking)
record_sent_email(conn, user_id, message_id, to_addr, subject=None, task_id=None, thread_id=None, in_reply_to=None, references=None, conversation_token=None) -> int
find_sent_email_by_references(conn, references: list[str]) -> SentEmail | None

# Invoice
get_invoice_schedule_state(conn, user_id, client_key) -> InvoiceScheduleState | None
get_notified_overdue_invoices(conn, user_id) -> set[str]

# Skills fingerprint
get_user_skills_fingerprint(conn, user_id) -> str | None
set_user_skills_fingerprint(conn, user_id, fingerprint) -> None
```
