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
   - Sweep SQLite DBs (framework + per-user feeds/health/location/money) with `PRAGMA quick_check` + self-healing `REINDEX` (every `db_health_check_interval`, default 24h; runs immediately on the first tick of the daemon so a fresh deploy surfaces latent index corruption without waiting a day)
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
4. `execute_task()` → (success, result, actions_taken, execution_trace)
5. **Success path**:
   - Check malformed output (`detect_malformed_result()`) → reclassify as failure and retry
   - Check confirmation request (regex `CONFIRMATION_PATTERN`)
   - Update to `completed` with `actions_taken` and `execution_trace`
   - Index conversation for memory search
   - Handle heartbeat results / silent scheduled jobs
   - Deliver results
   - Reset scheduled job failures
6. **Failure path**:
   - Check cancellation (`Cancelled by user` → status `cancelled`, no retry)
   - Check policy refusal (`_is_policy_refusal()`: 400 + safety/policy/content/refused/harm/blocked keyword) → mark failed, post alert via `_post_policy_refusal_alert()` (extracts `From:` header for email tasks), no retry
   - Retry with backoff if attempts remain (1, 4, 16 min) — skipped for OOM
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

**Other deferred handlers**:
- `_process_deferred_kg_ops` — `task_{id}_kg_ops.json` from `istota-skill memory_search add-fact|invalidate|delete-fact`. Commits per op so a mid-loop crash can't roll back ops we've already accepted.
- `_process_deferred_kv_ops` — `task_{id}_kv_ops.json` from `istota-skill kv set|delete`.
- `_process_deferred_user_alerts` — `task_{id}_user_alerts.json` for the alerts/notification path.
- `_process_deferred_health_ops` — `task_{id}_health_ops.json` from `istota-skill health log|add-panel|add-biomarker|upload|set|...`. Resolves the user's `HealthContext` and replays insert/update ops against the per-user `health.db`. User id always comes from the task (defense-in-depth); recognized suffix lives in `_KNOWN_DEFERRED_SUFFIXES` so unknown-suffix warnings stay clean.
- `_load_deferred_email_output` — `task_{id}_email_output.json` for structured email replies (preferred over the legacy stdout-JSON parser).

All deferred-op handlers now live in `scheduler_deferred.py` (`_KNOWN_DEFERRED_SUFFIXES`, `_load_deferred_json`, per-handler functions, `_purge_deferred_files_for_retry`, `_warn_unconsumed_deferred_files`). `scheduler.py` calls into it as a thin orchestrator.

**Retry replay safety (ISSUE-074)**: deferred-op producers append to per-task files keyed only by `task.id`. `set_task_pending_retry` keeps the same id, so `_purge_deferred_files_for_retry()` clears the slate in the retry-eligible failure branch — eventual success replays only the final attempt's writes (matters most for non-idempotent KG `invalidate`/`delete`).

**Unconsumed-file warnings (ISSUE-073)**: after the drain phase, `_warn_unconsumed_deferred_files()` scans the user temp dir and logs WARN for files missing the `task_` prefix or carrying an unknown suffix. The misnamed file is left on disk for inspection.

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
| DB health | `check_db_health()` → `db_health.check_and_repair()` | `db_health_check_interval` | — (logs only) |
| Shared files | `discover_and_organize_shared_files()` (shared_file_organizer.py) | `shared_file_check_interval` | `user_resources` |
| Briefings | `check_briefings()` | `briefing_check_interval` | `briefing_state` |
| Scheduled jobs | `check_scheduled_jobs()` | `briefing_check_interval` | `scheduled_jobs` |
| Sleep cycle | `check_sleep_cycles()` (memory/sleep_cycle.py) | `briefing_check_interval` | `sleep_cycle_state` |
| Channel sleep | `check_channel_sleep_cycles()` (memory/sleep_cycle.py) | `briefing_check_interval` | `channel_sleep_cycle_state` |

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
| `db_health_check_interval` | 86400s (24h) | SQLite `quick_check` + `REINDEX` self-heal across framework + per-user feeds/health/location/money DBs (covers Nextcloud-mount FUSE/network induced index corruption) |
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
| `cron_max_staleness_minutes` | 60 | Insertion-time staleness gate for `check_scheduled_jobs` + `check_briefings`. When `now - next_run > N`, skip the queue insert and bump `last_run_at` to now so the schedule resumes from the next future fire. Suppresses thundering-herd catch-up after a long outage. 0 = legacy unconditional catch-up. |
| `max_subtasks_per_task` | 10 | Deferred subtasks created per parent task |
| `max_subtask_depth` | 3 | Subtask chain depth cap (0 = unlimited) |
| `max_subtask_prompt_chars` | 8000 | Skip deferred subtasks with prompts over this size (0 = unlimited) |
| `log_channel_show_skills` | true | Include selected skills in log channel messages |

## Other Scheduler Functions

| Function | Purpose |
|---|---|
| `get_worker_id()` | `{hostname}-{pid}[-{user_id}]` |
| `_make_talk_progress_callback()` | Rate-limited progress to Talk |
| `post_result_to_talk()` | Send result to Talk conversation. Optional `target_token` overrides `task.conversation_token` for the actual post — used when the task's stored token isn't a real Talk room. |
| `_talk_target_for_delivery()` | Resolve the Talk room for a task's notifications. Returns `task.talk_delivery_token` when set (real Talk room, populated at task creation in `email_poller.py` and inherited by subtasks). Falls back to `task.conversation_token` for talk-source tasks. Legacy email tasks that pre-date the `talk_delivery_token` column may carry only a synthetic 16-char hex hash in `conversation_token`; for those the helper falls back to `resolve_conversation_token` (alerts → briefing → auto-DM). Synthetic-token detection is shared with `email_poller.is_synthetic_email_thread_token`. |
| `_execute_command_task()` | Run a shell-command task in a subprocess (cwd = `config.temp_dir`, env from `build_stripped_env()` + propagated `ISTOTA_*` + manifest-derived credential / connection vars). `ISTOTA_EXPERIMENTAL_FEATURES` is always injected as CSV of `config.experimental.features` so `@requires_feature`-gated subcommands behave consistently with the LLM path. `NC_*`, `CALDAV_*`, etc. come from `build_skill_env(list(skill_index), …)` over an `EnvContext` populated by `discover_calendars_for_task(task, config)`, so the gates the LLM path honors (notably `gate_has_discovered_calendars`) apply here too — no parallel hardcoded list. `dispatch_setup_env_hooks` runs after `build_skill_env` so vars declared `from: "setup_env"` (notably `LOCATION_DB_PATH`, `HEALTH_DB_PATH`) reach the subprocess; hook values use `env.update(...)` and overwrite any ambient daemon env because a stray systemd-leaked value would point at the wrong user's DB (ISSUE-097). Success criterion: `returncode == 0`, with one exception — when stdout starts with `{` and parses as a JSON dict containing `"status": "error"`, the task is marked failed using `parsed["error"]` as the message. This catches the silent-failure pattern where module-skill facades (feeds, money) print `{"status":"error","error":"…"}` to stdout while exiting 0 — and the same envelope is what `@requires_feature` emits on a gated-off call, so gating refusals surface as task failures with the human-readable message intact (the money facade additionally unwraps the inner envelope via `_unwrap_inner_error` before re-emitting it). Non-JSON stdout and malformed JSON are unaffected. |
| `_execute_skill_task()` | Run an auto-seeded skill subprocess (`python -m istota.skills.<skill>`). Same trusted env resolution as `_execute_command_task`: `build_skill_env` over the **full** skill index (so co-declared vars like `NC_URL`, declared on both `files` and `nextcloud`, reach the subprocess regardless of which skill the row names) plus `discover_calendars_for_task` on the `EnvContext` and `dispatch_setup_env_hooks` for setup_env-resolved vars. `ISTOTA_EXPERIMENTAL_FEATURES` is also injected. No proxy split — skill-tasks run a trusted CLI. Same JSON-error-envelope detection (with money-facade unwrap) as command-tasks. **Special case (ISSUE-098):** `skill="health"` + `skill_args[0]=="garmin-sync"` is short-circuited to `_run_garmin_sync_inprocess` *before* the subprocess spawn. The Garmin engine reads + writes encrypted secrets multiple times per sync (oauth blob, rotated SDK tokens, error flag, last_sync) and the subprocess path strips `ISTOTA_SECRET_KEY` by design — so the engine runs on the daemon thread (where the key is in scope) instead. Mirrors the in-process call the web `/garmin/sync` endpoint already makes. Same `(success, result_text)` shape; scheduler delivery / failure tracking unchanged. |
| `_run_garmin_sync_inprocess()` | Garmin-sync dispatch helper. Parses `--days-back` from `skill_args[1:]` (default 2), resolves `HealthContext` via `health.resolve_for_user`, picks up the user's timezone from `UserConfig`, calls `garmin_sync.sync_garmin(...)` on the daemon thread. Returns a JSON payload shaped `{"status": "ok"\|"error", "inserted": …, "skipped": …, "days_processed": …, "auth_error": bool, "error"?: "token_expired"}` — same shape `_execute_skill_task` would have surfaced if the subprocess path worked. Broad `Exception` catch so an SDK crash maps to a `"garmin sync: <exc>"` failure rather than crashing the worker. |
| `discover_calendars_for_task()` | Best-effort CalDAV discovery (lives in `executor.py`, re-exported via scheduler imports). Returns `[]` when CalDAV is unconfigured / unreachable / the user owns no calendars. Used by all three subprocess paths (LLM `execute_task`, `_execute_skill_task`, `_execute_command_task`) so the `gate_has_discovered_calendars` env-spec gate fires consistently. |
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
| `tasks` | `Task` | id, status, source_type, user_id, prompt, conversation_token, talk_delivery_token, priority, attempt_count, max_attempts, cancel_requested, worker_pid, locked_at/by, scheduled_for, output_target, talk_message_id, reply_to_talk_id, heartbeat_silent, scheduled_job_id, actions_taken, execution_trace, model, effort |
| `user_resources` | `UserResource` | id, user_id, resource_type, resource_path, display_name, permissions |
| `briefing_configs` | `BriefingConfig` | id, user_id, name, cron_expression, conversation_token, components (JSON), enabled |
| `briefing_state` | — | user_id, briefing_name, last_run_at |
| `processed_emails` | `ProcessedEmail` | id, email_id, sender_email, subject, thread_id, message_id, references, user_id, task_id, routing_method |
| `istota_file_tasks` | `IstotaFileTask` | id, user_id, content_hash, original_line, normalized_content, status, task_id, file_path |
| `scheduled_jobs` | `ScheduledJob` | id, user_id, name, cron_expression, prompt, conversation_token, output_target, enabled, silent_unless_action, consecutive_failures, model, effort |
| `talk_poll_state` | — | conversation_token, last_known_message_id |
| `sleep_cycle_state` | — | user_id, last_run_at, last_processed_task_id |
| `channel_sleep_cycle_state` | — | conversation_token, last_run_at, last_processed_task_id |
| `heartbeat_state` | `HeartbeatState` | user_id, check_name, last_check_at, last_alert_at, last_healthy_at, consecutive_errors |
| `reminder_state` | `ReminderState` | user_id, queue (JSON), content_hash |
| `monarch_synced_transactions` | — | id, user_id, monarch_transaction_id, amount, merchant, content_hash |
| `csv_imported_transactions` | — | id, user_id, content_hash, source_file |
| `user_skills_fingerprint` | — | user_id, fingerprint, updated_at |
| `sent_emails` | — | id, user_id, task_id, message_id, to_addr, subject, thread_id, in_reply_to, references, conversation_token, talk_delivery_token, sent_at |
| `task_logs` | — | task_id, level, message, timestamp |
| `memory_chunks` | — | (from memory_search.py schema) |
| `user_profiles` | — | user_id, display_name, timezone, timezone_follow_nextcloud, log_channel, alerts_channel, site_enabled, max_foreground_workers, max_background_workers, email_addresses (JSON), disabled_skills (JSON), disabled_modules (JSON), trusted_email_senders (JSON) |
| `secrets` | — | user_id, service, key, value (Fernet ciphertext), created_at, updated_at, last_accessed_at |
| `knowledge_facts` | — | id, user_id, subject, predicate, object, source, source_task_id, valid_from, valid_to, created_at |
| `knowledge_facts_audit` | — | id, fact_id, user_id, op, payload (JSON), source_task_id, created_at |
| `google_oauth_tokens` | — | user_id, access_token, refresh_token, expires_at, scopes |
| `geocode_cache` / `reverse_geocode_cache` | — | global Nominatim caches (forward + reverse geocoding); shared across users so per-user splitting would lose dedup |
| (`location_pings` / `places` / `visits` / `location_state` / `dismissed_clusters`) | — | per-user `location.db` only; not in framework `istota.db`. See `src/istota/location/db.py` and AGENTS.md "GPS Location" |
| `talk_messages` / `talk_poll_state` | — | Talk poller state + message cache |
| `istota_kv` | — | user_id, namespace, key, value (JSON) — backs the `kv` skill |
| `trusted_email_senders` | — | user_id, pattern (fnmatch) — email-gate allowlist |

## Key DB Functions

### Task Operations
```python
create_task(conn, prompt, user_id, source_type="cli", conversation_token=None,
    parent_task_id=None, is_group_chat=False, attachments=None, priority=5,
    scheduled_for=None, output_target=None, talk_message_id=None,
    reply_to_talk_id=None, reply_to_content=None,
    heartbeat_silent=False, scheduled_job_id=None,
    talk_delivery_token=None) -> int

claim_task(conn, worker_id, max_retry_age_minutes=60, user_id=None) -> Task | None
get_task(conn, task_id) -> Task | None
update_task_status(conn, task_id, status, result=None, error=None, actions_taken=None, execution_trace=None) -> None
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
record_sent_email(conn, user_id, message_id, to_addr, subject=None, task_id=None, thread_id=None, in_reply_to=None, references=None, conversation_token=None, talk_delivery_token=None) -> int
find_sent_email_by_references(conn, references: list[str]) -> SentEmail | None

# Skills fingerprint
get_user_skills_fingerprint(conn, user_id) -> str | None
set_user_skills_fingerprint(conn, user_id, fingerprint) -> None
```
