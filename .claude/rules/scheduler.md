# Scheduler & DB Internals

## Scheduler Functions

### `run_daemon()`
```python
def run_daemon(config: Config, *, install_signal_handlers: bool = True,
               ready_event: threading.Event | None = None) -> None
```
The `install_signal_handlers` / `ready_event` kwargs support the combined
`istota serve` launcher (local install — see AGENTS.md "Local single-user
install"). Defaults reproduce the standalone-daemon behaviour exactly. `serve`
runs this on a worker thread with `install_signal_handlers=False` (signal
handlers are main-thread-only) and owns SIGINT/SIGTERM via uvicorn; it drives
shutdown through `scheduler.request_shutdown()` (sets the shared
`_shutdown_requested` flag). `run_daemon` clears that flag at start, sets
`ready_event` once the pool + pollers are up (right before the loop), and on
flock contention raises `_DaemonAlreadyRunning` (not `return`) so `serve` can
report "already running" — the standalone `main()` catches it → clean `SystemExit(1)`.
The lock path is the module constant `DAEMON_LOCK_PATH` (default
`/tmp/istota-scheduler-daemon.lock`; overridable, notably in tests).

1. Acquire flock on `DAEMON_LOCK_PATH`
2. Set SIGTERM/SIGINT handlers (skipped when `install_signal_handlers=False`)
3. Hydrate user configs from Nextcloud API
4. Ensure user directories
4a. `recover_orphaned_tasks_on_startup(config)` — reclaim tasks left `running`/`locked` by a dead prior instance (see "Startup orphan recovery" below)
4b. Start the persistent `AsyncRuntime` (`async_runtime.get_async_runtime()`) — see below
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
   - Sweep SQLite DBs (framework + per-user feeds/health/location/money, all local now) with `PRAGMA quick_check` + self-healing `REINDEX` (every `db_health_check_interval`, default 24h; runs immediately on the first tick of the daemon so a fresh deploy surfaces latent index corruption without waiting a day)
   - Snapshot local DBs to `{mount}/istota-db-backups/<date>/…` (dated dirs, retention + collapse guard) via the SQLite online-backup API (every `db_backup_interval`, default 24h; off-host durability now that module DBs are local — clock starts at boot, first snapshot after one interval); alerts the operator on any errored/suspect DB and on backup staleness
   - Emit the `scheduler_stats` health line (every `scheduler_stats_interval`, default 60s; first emit after one full interval; `0` disables)
   - Check invoice schedules (every `briefing_check_interval`)
   - `pool.dispatch()`
   - Sleep `poll_interval`
8. Shutdown workers (`pool.shutdown()`), stop the persistent runtime (`runtime.stop(timeout=10)`), release lock

## Persistent asyncio runtime (`async_runtime.py`)

All Nextcloud Talk I/O runs on **one** long-lived asyncio loop on a dedicated
daemon thread, against **one** pooled `httpx.AsyncClient`, instead of a fresh
`asyncio.run` loop + fresh client per call. This gives TCP/TLS connection reuse
to Nextcloud and removes the per-call loop-teardown leak surface.

- **`AsyncRuntime`** — owns the loop thread. `submit(coro, *, timeout=None)`
  bridges sync→async via `run_coroutine_threadsafe(...).result(timeout)`
  (`timeout=None` = wait forever, matching `asyncio.run`; on timeout it cancels
  the coroutine and raises `TimeoutError`). Calling `submit` from the loop's own
  thread raises (the reentry guard) instead of deadlocking. `stop(timeout=10)`
  cancels in-flight coroutines first, then runs registered cleanup hooks
  (closing the shared client), then stops the loop — cancel-before-aclose so a
  cleanup hook can't close the client out from under a live request. `start()`
  clears stale cleanup hooks so an in-process restart doesn't accumulate them.
- **`run_coro(coro, *, timeout=None)`** — the workhorse every sync Talk call site
  uses: `run_coro(post_result_to_talk(...))`, `run_coro(edit_talk_message(...))`,
  `run_coro(poll_talk_conversations(config))`, etc. Lazily starts the process-global
  runtime on first use (convenience for CLI/tests; `run_daemon` starts it
  explicitly).
- **`get_talk_client(config)`** — process-global persistent `TalkClient`
  singleton. Every Talk delivery path pulls from it (the `TalkTransport` seam,
  the event consumers, `notifications._send_talk`, the inbound poller,
  `commands.dispatch`, `_resolve_channel_name`, `_finalize_log_channel`) so they
  share one connection pool. It is a **synchronous, reentry-safe accessor**: it
  must not call `run_coro` (it's invoked from inside Talk coroutines already on
  the loop), so the underlying httpx pool opens lazily on the first awaited
  method call — which always runs on the persistent loop because every call site
  goes through `run_coro`. `get_async_runtime()` is called so the registered
  `aclose` cleanup hook fires on `stop()`. `reset_async_runtime()` /
  `reset_talk_client()` are test-teardown helpers.

**Invariant:** every `TalkClient` method invocation must end up on the persistent
loop (via `run_coro`), because the methods issue requests on the loop-bound
`self._client`. There are no transient `TalkClient(config)` constructions left in
daemon Talk paths. Email delivery stays on `asyncio.run` (sync SMTP, not httpx).
Single-pass `run_scheduler` shares `process_one_task` (which uses `run_coro`), so
it lazily uses the same persistent runtime; it calls `reset_async_runtime()`
before returning (as does the `istota run` CLI) so the shared client's `aclose`
runs a clean shutdown instead of connections being dropped on process exit.
`run_cleanup_checks` is synchronous — its rare Talk notices go
through `send_notification` (→ `run_coro`), keeping its blocking DB/IMAP/fs
cleanup off the persistent loop.

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
- `_process_deferred_garmin_import` — `task_{id}_garmin_import.json` from `istota-skill location import-garmin-tracks` when sandboxed. Runs `istota.location.garmin_import.import_tracks` in-process (the daemon holds `ISTOTA_SECRET_KEY`, stripped in the sandbox), gated on the location module, then pushes the result to the user via `send_notification(purpose="notification")`. This is the chat-accessible path health `garmin-sync` lacks (its enqueue hits the sandbox's read-only framework DB; the deferred dir is writable, so this doesn't).
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
- When the queue empties, the worker lingers in `_worker_idle_wait` instead of exiting at once: it re-checks for new work every `worker_idle_poll_interval` (default 0.5s) until `worker_idle_timeout` (default 10s) of *continuous* emptiness elapses, then exits. A follow-up task arriving mid-linger is claimed within ~one idle poll (the "quick follow-up" case `dispatch()` alone can't help, because the parked worker still holds the per-user slot). A cheap `count_claimable_tasks_for_user_queue` pre-check gates the expensive `claim_task` so idle polling costs no more than a `dispatch()` scan. The deadline tracks continuous emptiness — a lost claim race does not reset it, so two idle workers can't keep each other alive forever. `worker_idle_poll_interval` ≤ 0 or ≥ `worker_idle_timeout` restores the legacy single coarse-wait + single-recheck behaviour (an interruptible `stop_event.wait`, exact pre-phase-2 parity). The fine-cadence path mirrors `_dispatch_sleep` (slice-sleep + stop/shutdown checks), so per-worker stop and global shutdown are honoured within one idle poll.
- Both the idle pre-check and `dispatch()`'s spawn count use `db.count_claimable_tasks_for_user_queue` (not the raw `count_pending_tasks_for_user_queue`), which mirrors `claim_task`'s per-channel single-active gate via the shared `_CLAIM_CHANNEL_GATE_SQL`. A follow-up gated behind an active task in the *same* room therefore counts as 0 — `dispatch()` won't spawn a doomed extra worker for it and an idle worker keeps sleeping cheaply — while a task in a *different* room still counts. Raw `count_pending_*` survives only for status/observability (the daemon's pending-backlog status file).
- During the linger both this worker and `dispatch()` may scan the same user; the overlap is harmless — `claim_task` is atomic (`UPDATE … RETURNING`), so at most one wins.
- After exit, `dispatch()` re-spawns a worker on the next pending task (phase-1 sub-tick cadence, ~0.5s).
- Each worker creates fresh DB connections and `asyncio.run()` event loops

## Poller Integrations

| Poller | Function | Interval Config | State Table |
|---|---|---|---|
| Talk | `_talk_poll_loop()` | `talk_poll_interval` | `talk_poll_state` |
| Email | `poll_emails()` (transport/email/inbound.py) | `email_poll_interval` | `processed_emails` |
| TASKS.md | `poll_all_tasks_files()` (tasks_file_poller.py) | `tasks_file_poll_interval` | `istota_file_tasks` |
| Heartbeat | `check_heartbeats()` (heartbeat.py) | `heartbeat_check_interval` | `heartbeat_state` |
| DB health | `check_db_health()` → `db_health.check_and_repair()` | `db_health_check_interval` | — (logs only) |
| DB backup | `db_backup.backup_databases()` (SQLite online-backup → `{mount}/istota-db-backups/<date>`; retention prune + collapse guard + `_alert_backup_problems` + staleness alert) | `db_backup_interval` | — (logs + operator alerts) |
| Scheduler stats | `_emit_scheduler_stats()` (daemon-only) | `scheduler_stats_interval` | — (logs only) |
| Shared files | `discover_and_organize_shared_files()` (shared_file_organizer.py) | `shared_file_check_interval` | `user_resources` |
| Briefings | `check_briefings()` | `briefing_check_interval` | `briefing_state` |
| Scheduled jobs | `check_scheduled_jobs()` | `briefing_check_interval` | `scheduled_jobs` |
| Sleep cycle | `check_sleep_cycles()` (memory/sleep_cycle.py) | `briefing_check_interval` | `sleep_cycle_state` |
| Channel sleep | `check_channel_sleep_cycles()` (memory/sleep_cycle.py) | `briefing_check_interval` | `channel_sleep_cycle_state` |

When `playbooks.enabled`, `process_user_sleep_cycle` also distills **learned playbooks** (Part B): the extraction prompt gains a `PLAYBOOKS:` section (gated on `playbooks.min_tool_calls` tool calls + success + reusability). The section instructs the model to copy commands/paths *verbatim from the per-task `Tools (N):` line* rather than reconstruct a plausible path, and to write a **thin router** (trigger + exact verified command + one gotcha) instead of re-narrating a single-script trajectory's internals (ISSUE-174 Concern 1/4). `_parse_structured_extraction` returns a 4th `playbooks` list, and `_process_extracted_playbooks` writes each to `{workspace}/Users/<uid>/<bot_dir>/playbooks/<slug>.md` (dedup by slug → update in place), indexing it into `memory_chunks` with `source_type="playbook"`. A file marked `pinned: true` in frontmatter (a human correction) keeps its on-disk content but is **re-indexed from that content** — recall serves `memory_chunks`, not the file, so the correction must reach the index without the sleep cycle clobbering the human edit (`_playbook_is_pinned`; Concern 1 correction path). `cleanup_old_playbooks(conn=…)` age-prunes by `playbooks.retention_days` (default 90; 0 = keep) and **deletes the pruned file's `memory_chunks` too** (`playbook` isn't in `EPHEMERAL_SOURCE_TYPES`, so an unlinked-but-indexed file would otherwise still be recalled). Age is **last-use mtime**: `_recall_playbooks` stamps the file's mtime on every recall hit (Concern 3), so the prune targets idle guidance, not merely un-re-derived guidance; a **grandfather one-shot** (`.retention_initialized` sentinel) refreshes existing files on the first post-upgrade run so nothing is pruned on stale write-mtime, and a `pinned` file is never pruned. Markdown-only, never executed; recalled via `executor._recall_playbooks`.

## Cleanup (`run_cleanup_checks`)
1. Expire stale confirmations → notify user via Talk
2. Log warnings for stale pending tasks
3. Fail ancient pending tasks → notify user **only for user-submitted source types**. The notice ("A task you submitted was cancelled…") is suppressed for `_AUTOMATED_SOURCE_TYPES` (`scheduled`/`briefing`/`heartbeat`/`subtask`): those pile up on their own when the queue wedges, so notifying their output channel turns one stuck worker into a per-minute "task cancelled" flood (and the message isn't true for them).
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
| `dispatch_interval` | 0.5s | Sub-tick `pool.dispatch()` cadence inside `_dispatch_sleep` — bounds cold pending-task pickup latency without re-running the interval-gated checks. 0 or ≥ `poll_interval` = legacy one-dispatch-per-tick |
| `talk_poll_interval` | 10s | Talk poller |
| `talk_poll_timeout` | 30s | Talk long-poll |
| `email_poll_interval` | 60s | Email poller |
| `briefing_check_interval` | 60s | Briefings, jobs, sleep, cleanup, invoices |
| `tasks_file_poll_interval` | 30s | TASKS.md poller |
| `shared_file_check_interval` | 120s | Shared file organizer |
| `heartbeat_check_interval` | 60s | Heartbeat checks |
| `db_health_check_interval` | 86400s (24h) | SQLite `quick_check` + `REINDEX` self-heal across framework + per-user feeds/health/location/money DBs. Backstop now that module DBs are local (was for FUSE-mount corruption); enumerates via `Config.module_db_path`, no longer mount-gated |
| `main_loop_read_timeout_ms` | 2000ms | `busy_timeout` for the dispatch scan + idle pre-check (read-only). A lock past this raises `OperationalError` → the loop skips the tick (re-dispatches ~0.5s later) instead of blocking 30s and tripping the stall watchdog. Passed as `db.get_db(..., busy_timeout_ms=)`. 0 = keep the 30s connect timeout. Defense-in-depth on top of WAL |
| `db_backup_enabled` / `db_backup_interval` / `db_backup_dir` / `db_backup_retention` | true / 86400s / "" / 7 | `db_backup.backup_databases(config, today=None)` snapshots the framework DB + every per-user module DB to `db_backup_dir/<YYYY-MM-DD>/…` (default `{nextcloud_mount}/istota-db-backups`) via SQLite's online-backup API — off-host durability now that module DBs left the Nextcloud-synced workspaces. **Dated dirs, not a single overwritten slot** (ISSUE-159): a corrupted/emptied live DB can't clobber the last good copy. `db_backup_retention` keeps the N newest dated dirs (0 = keep all) but never prunes a dir holding the newest *good* copy of any DB (`_prune_old_snapshots` protects it). A **collapse guard** (`_apply_collapse_guard` → `db_relocate._data_row_count`) quarantines a fresh snapshot as `*.suspect` (status `suspect`) when a DB that previously held data comes back empty/unreadable; exact-zero only (framework `tasks` legitimately shrinks under retention cleanup). Backup tree is `0700`/files `0600`. Cold copies are forced to DELETE journal mode (a WAL header would SIGBUS on the FUSE mount if ever opened in place). **Mount-liveness guard** (`_destination_is_durable`): a mount-derived destination is written only when `os.path.ismount` is true — a down rclone FUSE mount reverts to a local dir, and a naive `mkdir` would silently write the "backup" to local disk under the stale mountpoint; the run is skipped instead and the clock left stale. An explicit `db_backup_dir` is trusted without the check. The clock is **persisted** to `{db_path.parent}/.db_backup_last_run` and seeded at boot via `db_backup.last_backup_time`, so it survives restarts. It advances **only when ≥1 DB snapshotted OK** — a fully-errored (or mount-down) run leaves it stale so the staleness alert can fire. The scheduler alerts the operator on any errored/suspect DB (`_alert_backup_problems`) and on **staleness** (`_maybe_alert_backup_stale`: persisted last-run older than `2 × db_backup_interval`; re-armable, gated on a prior successful run so a fresh deploy doesn't false-alarm). Both go through `_send_operator_alert`, which runs `send_notification` on a short-lived daemon thread with a join timeout so a wedged Talk can't stall the dispatch loop (ISSUE-143 class). Force an immediate backup with `python -m istota.db_backup` (ignores the interval; closes the first-run gap after a deploy). Restore via `db_restore` (`python -m istota.db_restore --all`) — copies the newest good cold copy back (or `--date`), clears stale `-wal`/`-shm` sidecars, refuses an empty snapshot without `--force`, and refuses to run while the daemon holds its flock (`_daemon_running`) since a copy over a live WAL DB corrupts it; then `init_db` re-flips WAL |
| `scheduler_stats_interval` | 60s | One `scheduler_stats threads=… fds=… rss_mb=… tasks_running=… workers_active=…` INFO line per interval on logger `istota.scheduler.stats`, daemon-only. Surfaces resource leaks (ISSUE-101 class) in minutes via `journalctl … \| grep scheduler_stats`. psutil-derived fields (`fds`/`rss_mb`) omitted with a one-time WARN when psutil is unavailable; DB hiccup → `tasks_running=?`. First emit after one full interval. `0` disables. |
| `loop_stall_alert_seconds` | 180s | Defense-in-depth (ISSUE-143). `LoopWatchdog` runs on its own daemon thread, watches a last-tick timestamp the main loop bumps each iteration (`watchdog.tick()`), and logs an ERROR + fires one operator alert (`send_notification(purpose="alert")` to the first admin/user via `_operator_alert_user`) when the loop hasn't ticked in this long. Re-arms on recovery so a transient stall pages once. `0` disables. |
| `worker_idle_timeout` | 10s | Cumulative-idle linger before a worker exits. The worker re-checks for work on a fine cadence for up to this long (continuous emptiness) before exiting; resets whenever a task is claimed. (Pre-phase-2 this was effectively capped to ~one `poll_interval` with a single recheck — the knob is now honoured.) |
| `worker_idle_poll_interval` | 0.5s | Idle re-check cadence inside `_worker_idle_wait`. A follow-up task is claimed within ~one interval instead of waiting a `poll_interval`. A cheap `count_claimable_tasks_for_user_queue` pre-check gates the `claim_task`. 0 or ≥ `worker_idle_timeout` = legacy single coarse-wait + single-recheck. |
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
| `stream_text_gate_chars` | 280 | Narration gate / substance classifier for streamed answer text on stream surfaces (web/REPL). A text run emits no `text_delta` until it crosses this many chars without an intervening tool call. At a tool boundary (`executor._settle_deltas_at_tool_boundary`): a short lead-in that stayed under the gate is dropped; a substantial block that crossed it is flushed and kept (the web client renders kept intermediate blocks as their own prose group). Never loses text (a short final answer still arrives via `result`). 0 disables. Executor logs `stream_gate:` per flush/discard — tune against those. |

## Other Scheduler Functions

| Function | Purpose |
|---|---|
| `get_worker_id()` | `{hostname}-{pid}[-{user_id}]` |
| Event streaming (task-event-streaming spec) | `process_one_task` builds an `EventWriter` (`istota/events.py`) per brain-path task and subscribes `TalkEventSubscriber` / `LogChannelSubscriber` / `PushNotificationSubscriber` (`istota/consumers/`). It passes the writer to `execute_task(event_writer=…)`, then emits terminal events (`confirmation`/`result`/`cancelled`/`error` + `done`) and calls `writer.finish()` once the task reaches a non-retry terminal state. On a retry-eligible failure it instead emits a `progress_text` "⏳ Attempt failed — retrying in N min…" notice (no terminal frame) and keeps the event log — the next attempt's `EventWriter` resumes `seq` from `db.get_max_task_event_seq` so it stays monotonic across attempts (no `UNIQUE(task_id, seq)` collision) and a watching web client survives the retry instead of hanging on "Working…". (The log used to be wiped via `db.delete_task_events` here; that broke the client's resume cursor. `_purge_deferred_files_for_retry` still runs.) The SSE/snapshot terminal backstop (`web_app._synthetic_terminal_events`) covers any remaining terminal-without-`done` gap (e.g. a crash that skipped `finish()`). The old `_make_talk_progress_callback` / `_make_log_channel_callback` / `_composite_callback` and `progress_style` are gone; `_finalize_log_channel(config, task, log_dests, …)` posts the log-channel footer to every resolved destination (`notifications.effective_log_destinations` — opt-in `routing["log"]` > legacy `log_channel`), reading `all_descriptions` / `delivery_state` (per-destination message ids) off `LogChannelSubscriber`. Delivery is capability-keyed: edit-capable surfaces edit their streamed message into the final state, non-edit surfaces (email/ntfy) get one final-summary delivery. |
| `post_result_to_talk()` | Send result to Talk conversation. Optional `target_token` overrides `task.conversation_token` for the actual post — used when the task's stored token isn't a real Talk room. |
| `_talk_target_for_delivery()` | Resolve the Talk room for a task's notifications. Returns `task.talk_delivery_token` when set (real Talk room, populated at task creation in `transport/email/inbound.py` and inherited by subtasks). Falls back to `task.conversation_token` for talk-source tasks. Legacy email tasks that pre-date the `talk_delivery_token` column may carry only a synthetic 16-char hex hash in `conversation_token`; for those the helper falls back to `resolve_conversation_token` (alerts → briefing → auto-DM). Synthetic-token detection is shared with `email_support.is_synthetic_email_thread_token`. |
| `_execute_command_task()` | Run a shell-command task in a subprocess (cwd = `config.temp_dir`, env from `build_stripped_env()` + propagated `ISTOTA_*` + manifest-derived credential / connection vars). `ISTOTA_EXPERIMENTAL_FEATURES` is always injected as CSV of `config.experimental.features` so `@requires_feature`-gated subcommands behave consistently with the LLM path. `NC_*`, `CALDAV_*`, etc. come from `build_skill_env(list(skill_index), …)` over an `EnvContext` populated by `discover_calendars_for_task(task, config)`, so the gates the LLM path honors (notably `gate_has_discovered_calendars`) apply here too — no parallel hardcoded list. `dispatch_setup_env_hooks` runs after `build_skill_env` so vars declared `from: "setup_env"` (notably `LOCATION_DB_PATH`, `HEALTH_DB_PATH`) reach the subprocess; hook values use `env.update(...)` and overwrite any ambient daemon env because a stray systemd-leaked value would point at the wrong user's DB (ISSUE-097). Success criterion: `returncode == 0`, with one exception — when stdout starts with `{` and parses as a JSON dict containing `"status": "error"`, the task is marked failed using `parsed["error"]` as the message. This catches the silent-failure pattern where module-skill facades (feeds, money) print `{"status":"error","error":"…"}` to stdout while exiting 0 — and the same envelope is what `@requires_feature` emits on a gated-off call, so gating refusals surface as task failures with the human-readable message intact (the money facade additionally unwraps the inner envelope via `_unwrap_inner_error` before re-emitting it). Non-JSON stdout and malformed JSON are unaffected. The subprocess runs via `_run_capture` (not `subprocess.run`) so a timeout kills the whole process *group* — see below. |
| `_execute_skill_task()` | Run an auto-seeded skill subprocess (`python -m istota.skills.<skill>`). Same trusted env resolution as `_execute_command_task`: `build_skill_env` over the **full** skill index (so co-declared vars like `NC_URL`, declared on both `files` and `nextcloud`, reach the subprocess regardless of which skill the row names) plus `discover_calendars_for_task` on the `EnvContext` and `dispatch_setup_env_hooks` for setup_env-resolved vars. `ISTOTA_EXPERIMENTAL_FEATURES` is also injected. No proxy split — skill-tasks run a trusted CLI. Same JSON-error-envelope detection (with money-facade unwrap) as command-tasks. **Special case (ISSUE-098):** `skill="health"` + `skill_args[0]=="garmin-sync"` is short-circuited to `_run_garmin_sync_inprocess` *before* the subprocess spawn. The Garmin engine reads + writes encrypted secrets multiple times per sync (oauth blob, rotated SDK tokens, error flag, last_sync) and the subprocess path strips `ISTOTA_SECRET_KEY` by design — so the engine runs on the daemon thread (where the key is in scope) instead. Mirrors the in-process call the web `/garmin/sync` endpoint already makes. Same `(success, result_text)` shape; scheduler delivery / failure tracking unchanged. |
| `_run_garmin_sync_inprocess()` | Garmin-sync dispatch helper. Parses `--days-back` from `skill_args[1:]` (default 2), resolves `HealthContext` via `health.resolve_for_user`, picks up the user's timezone from `UserConfig`, calls `garmin_sync.sync_garmin(...)` on the daemon thread. Returns a JSON payload shaped `{"status": "ok"\|"error", "inserted": …, "skipped": …, "days_processed": …, "auth_error": bool, "error"?: "token_expired"}` — same shape `_execute_skill_task` would have surfaced if the subprocess path worked. Broad `Exception` catch so an SDK crash maps to a `"garmin sync: <exc>"` failure rather than crashing the worker. |
| `_run_capture()` / `_kill_process_group()` | Subprocess runner for both `_execute_command_task` and `_execute_skill_task`. Runs the child under `subprocess.Popen(start_new_session=True)` and, on `TimeoutExpired`, `os.killpg(SIGKILL)`s the whole process group (then a bounded second `communicate`) before re-raising. `subprocess.run(timeout=…)` only SIGKILLs the *direct* child and then blocks forever in its post-kill `communicate()` when an orphaned grandchild still holds the stdout/stderr pipe — a CRON `command:` that backgrounds a child (or a skill CLI that shells out) wedged its worker past the timeout that way, and since the per-task heartbeat thread keeps pinging, the liveness-based stuck-running reaper never reclaimed it (a per-minute job held its only background slot 6.5h). Returns a `CompletedProcess` so callers keep using `.returncode`/`.stdout`/`.stderr`; re-raises `TimeoutExpired` so their existing except branches are unchanged. |
| `discover_calendars_for_task()` | Best-effort CalDAV discovery (lives in `executor.py`, re-exported via scheduler imports). Returns `[]` when CalDAV is unconfigured / unreachable / the user owns no calendars. Used by all three subprocess paths (LLM `execute_task`, `_execute_skill_task`, `_execute_command_task`) so the `gate_has_discovered_calendars` env-spec gate fires consistently. |
| `_parse_email_output()` | Parse JSON email response (legacy fallback) |
| `_load_deferred_email_output()` | Load structured email output from deferred file |
| `_process_deferred_sent_emails()` | Record outbound emails for emissary thread matching |
| `post_result_to_email()` | Send email reply with threading |
| `check_briefings()` | Cron-based briefing scheduling. Creates each due briefing as a `source_type="briefing"` background task carrying only the briefing identity (`briefing_name`) + a placeholder prompt — it does **no** network prefetch on the dispatch thread (ISSUE-143). The slow prompt build (`build_briefing_prompt`: news/yfinance/FinViz/IMAP) is deferred to `executor.build_deferred_briefing_prompt`, run by the background worker that picks the task up, so a slow upstream can't stall `pool.dispatch()` for every room. `check_briefing_triggers` (NC-app trigger files) does the same. |
| `check_scheduled_jobs()` | Cron-based job scheduling. **Overlap guard:** before enqueuing a fire it skips when `db.count_inflight_tasks_for_scheduled_job(job.id) > 0` (a prior run still `pending`/`locked`/`running`/`pending_confirmation`), *without* advancing `last_run_at` — so the job fires the next tick once the in-flight run clears (correct for sparse jobs; advancing would push the next fire out a full interval). Stops a `* * * * *` job behind a wedged single background worker from stacking one row/minute. Composes with `cron_max_staleness_minutes` (a far-past `next_run` after a long run then trips staleness suppression). |
| `cleanup_old_temp_files()` | Remove old temp files |

---

# DB Module (db.py)

## All Tables

| Table | Dataclass | Key Columns |
|---|---|---|
| `tasks` | `Task` | id, status, source_type, user_id, prompt, conversation_token, talk_delivery_token, priority, attempt_count, max_attempts, cancel_requested, worker_pid, last_heartbeat, locked_at/by, scheduled_for, output_target, talk_message_id, reply_to_talk_id, heartbeat_silent, scheduled_job_id, briefing_name, actions_taken, execution_trace, model, effort |
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
| `task_events` | `TaskEvent` (in `events.py`) | id, task_id, seq, kind, payload (JSON), created_at; `UNIQUE(task_id, seq)`. Task-event-streaming log. `seq` monotonic per task (writer-assigned). Read via `db.get_task_events(task_id, since_seq)`; `seq` resumed across retries via `db.get_max_task_event_seq` (log kept, not wiped on retry); cleared via `db.delete_task_events(task_id)` only in `cleanup_old_tasks` (retention). Cascade clause decorative — hand-deleted. |
| `memory_chunks` | — | (from memory_search.py schema) |
| `user_profiles` | — | user_id, display_name, timezone, log_channel, alerts_channel, max_foreground_workers, max_background_workers, email_addresses (JSON), disabled_skills (JSON), disabled_modules (JSON), trusted_email_senders (JSON) |
| `secrets` | — | user_id, service, key, value (Fernet ciphertext), created_at, updated_at, last_accessed_at |
| `knowledge_facts` | — | id, user_id, subject, predicate, object, source, source_task_id, valid_from, valid_to, created_at |
| `knowledge_facts_audit` | — | id, fact_id, user_id, op, payload (JSON), source_task_id, created_at |
| `google_oauth_tokens` | — | user_id, access_token, refresh_token, expires_at, scopes |
| `geocode_cache` / `reverse_geocode_cache` | — | global Nominatim caches (forward + reverse geocoding); shared across users so per-user splitting would lose dedup |
| (`location_pings` / `places` / `visits` / `location_state` / `dismissed_clusters`) | — | per-user `location.db` only; not in framework `istota.db`. See `src/istota/location/db.py` and AGENTS.md "GPS Location" |
| `talk_messages` / `talk_poll_state` | — | Talk poller state + message cache |
| `istota_kv` | — | user_id, namespace, key, value (JSON) — backs the `kv` skill |
| `trusted_email_senders` | — | user_id, pattern (fnmatch) — email-gate allowlist |
| `web_chat_rooms` | `WebChatRoom` | id, user_id, token (channel id), name, archived, created_at, updated_at — backs the web chat surface; the frontend's integer room id. `UNIQUE(user_id, token)` (NOT globally unique on token): a shared Talk room has one handle row per participant so it surfaces in each member's list (ISSUE-134) |
| `room_members` | — | room_token, user_id, created_at; `PRIMARY KEY (room_token, user_id)` — per-user membership of a shared room (ISSUE-134). A room is shared (one token, one transcript) but visibility is resolved through membership (`list_member_rooms`), not the single-owner `rooms.user_id`. Populated by `register_room` / inbound senders / the `room_members_v1` backfill |
| `web_chat_messages` | `WebChatMessage` | id, user_id, token, role, title, text, created_at — bot-delivered (unsolicited) room messages: alerts / logs / notifications routed to the `web` surface via `WebTransport.deliver`. Distinct from task-backed turns; merged into room history by time |

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

Steps 3–5 (and the standalone `fail_stuck_locked_running_tasks()` maintenance
pass) share `_STUCK_RUNNING_PREDICATE` to decide "stuck" by **worker liveness**,
not raw runtime (ISSUE-112). A `running` task is stuck when its `last_heartbeat`
has been silent longer than `worker_stuck_minutes` (default 10); when no heartbeat
was ever recorded it falls back to `started_at` older than `task_timeout_minutes`
+ grace (`scheduler._stuck_running_minutes`). The running worker pings
`last_heartbeat` every `worker_heartbeat_seconds` via the `_task_heartbeat`
context manager (`db.touch_task_heartbeat`), so a slow-but-alive worker — notably
the in-process native brain, which has no killable PID — is never reclaimed,
while a crashed worker is recovered in minutes. (Distinct from the health-check
heartbeat system in `heartbeat.py`.)

### Startup orphan recovery (`recover_orphaned_tasks_on_startup`)
The time-based stuck-reclaim *infers* a dead worker from heartbeat silence — fine
for the rare worker-died-but-daemon-survived case, but slow (≤ `worker_stuck_minutes`)
for the common one: a **scheduler restart** that kills a worker mid-task and leaves
its row `running`. A restart is deterministic, not a guess — the daemon holds a
singleton flock, so the instant a fresh instance boots, every `running`/`locked`
row is definitionally an orphan of the dead instance. `run_daemon` calls
`recover_orphaned_tasks_on_startup(config)` once under the flock, **before any
worker spawns** (step 4a), so there's no live owner to race. `db.recover_orphaned_tasks`
resolves each orphan in priority order: `cancel_requested` → `cancelled` (no
re-run); retries-exhausted / older than `max_retry_age_minutes` / inline-only
source (`INLINE_ONLY_SOURCE_TYPES` — REPL is never daemon-claimed, so releasing
would strand it `pending`) → `failed`; otherwise → `pending` with `attempt_count`
bumped and every liveness column cleared (so the stuck predicate can't re-fire /
a second claimer can't re-steal). For the cancelled/failed cases the scheduler
emits a terminal event frame via a subscriber-less `EventWriter` (seq resumed
above the dead attempt's partial deltas) so a watching web/SSE client gets
immediate closure instead of a hung spinner; released orphans emit nothing — the
re-run streams its own `task_started` and the client resumes from its cursor (the
retry-continuity path). `attempt_count` is the same supersession token the
`process_one_task` ownership guard keys on, so the two compose. `pending_confirmation`
is left untouched (legitimately awaiting the user).

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
