---
paths:
  - "src/istota/scheduler.py"
  - "src/istota/scheduler_deferred.py"
  - "src/istota/db.py"
---

# Scheduler & DB Internals

## Scheduler Functions

### `run_daemon()`
`run_daemon(config, *, install_signal_handlers=True, ready_event=None)`. `istota serve` runs it on a thread with `install_signal_handlers=False` (main-thread-only) and stops it via `scheduler.request_shutdown()`. `ready_event` is set before the loop; flock contention on `DAEMON_LOCK_PATH` raises `_DaemonAlreadyRunning`.

1. flock; 2. signal handlers; 3. hydrate user configs; 4. user dirs; 4a. `recover_orphaned_tasks_on_startup`; 4b. start `AsyncRuntime`; 5. Talk poll thread; 6. `WorkerPool`.
7. Loop: `pool.dispatch()`, `_tick_interval_gates(...)`, `_dispatch_sleep`. No check is written into the loop: `build_interval_gates` is the authoritative list, pinned by `tests/test_scheduler_interval_gates.py::EXPECTED_BINDINGS`. Notes:
   - DB health and the `host_pressure` breadcrumb run on the first tick. The backup clock is seeded from the persisted stamp, never reset at boot (a host deploying daily otherwise never backed up).
   - `_check_host_pressure` feeds `pool.update_pressure()`; on a `snapshot_trigger` crossing it writes one `host_pressure_snapshot` plus one alert per cooldown.
8. `pool.shutdown()`, `runtime.stop(timeout=10)`, release lock.

### The interval gate table (F33)

`build_interval_gates(config, *, pool, background_checks, doctor_state, pressure_state, backup_state)` returns `IntervalGate` rows in loop order; `seed_interval_clocks` builds the clocks. Readers: `_tick_interval_gates` (daemon; `background` rows go through `_spawn_background_check`, clock advances at spawn) and `_run_interval_gates_once` (`run_scheduler`; the nine `one_shot` rows, synchronously, no clocks). Load-bearing order: `shared-files` before `tasks-file-poll`; `backup-stale-alert` right after `db-backup`. A grep guard fails if a loop restates a gate.

- `field` is authoritative, `interval()` derives from it. `None` = not a config field (`travel-timezone`, `status-write` = 60, `backup-stale-alert`). `__post_init__` refuses both or neither, since neither would silently mean "every tick".
- A non-positive interval bypasses the clock (`backup-stale-alert`), so a backwards NTP step cannot skip it.
- `enabled` carries `bool(interval)` so 0 means off.
- `shared-blocks`, `scheduled-jobs`, `sleep-cycles`, `cleanup` read `briefing_check_interval` though not briefings. Known; own keys are an operator-visible config change.
- `on_error` / `one_shot_on_error` differ because the paths differed: `check_briefings` / `check_scheduled_jobs` failures abort the one-shot pass; the daemon logs past them.
- `phone-room-backfill` (literal 60 seconds, `background`) replays each minted SMS or WhatsApp room's pre-mint history and writes the `_room_backfill` marker once nothing on the room's aliases is unfinished. A marked room costs one indexed read per pass; a WhatsApp group room or a shared phone room is never marked and is re-checked every pass. Mechanism in `transport.md` ("Phone rooms").

### Off-thread periodic checks (`_spawn_background_check`, ISSUE-144)

Long checks run on `bgcheck-<name>` daemon threads so they never block dispatch (DB pair = Tier 1, sleep cycles = Tier 2, email poll = ISSUE-250, heartbeat sweep since `self-check` runs doctor). No `LoopWatchdog.suspended()` call site remains.

- `_spawn_background_check(name, fn, inflight, *, overlap_expected=False)` skips a tick while the previous run lives; exceptions log `background_check_failed`. `overlap_expected` demotes the skip log to DEBUG.
- `_run_db_backup` = backup + `_alert_backup_problems`. `_run_email_poll` and `_run_sleep_cycles` (`check_sleep_cycles` then `check_channel_sleep_cycles`, one thread, each half try/excepted on its own connection) are also the synchronous `run_scheduler` bodies.
- Clocks advance at spawn; the staleness alert reads the persisted clock. Threads die with the process (dated snapshots; a killed sleep cycle re-runs).
- `check_heartbeats` commits per check, else its first write held the lock across the sweep and `send_heartbeat_alert`'s own connection.
- `_run_sleep_cycle_brain` commits any pending transaction before each brain call, so no write lock is held across a model call.

## Persistent asyncio runtime (`async_runtime.py`)

One loop thread, one pooled `httpx.AsyncClient` for all Talk I/O.
- `AsyncRuntime.submit(coro, *, timeout=None)`; on timeout cancels and raises `TimeoutError`; from the loop thread it raises rather than deadlocks. `stop(timeout=10)` cancels in-flight work, then runs cleanup hooks (client close), then stops the loop. `start()` clears stale hooks.
- `run_coro(coro, *, timeout=None)` is the sync entry for every Talk call; lazily starts the runtime.
- `get_talk_client(config)`: the shared `TalkClient`. Must not call `run_coro` (called from coroutines on the loop); the pool opens on the first awaited call. `reset_async_runtime()` / `reset_talk_client()` for tests.

**Invariant:** every `TalkClient` method runs on the persistent loop via `run_coro`; no transient `TalkClient(config)` in daemon Talk paths. Email stays on `asyncio.run`. `run_scheduler` and `istota run` call `reset_async_runtime()` before returning. `run_cleanup_checks` buffers its notices until its transaction closes, since a `web` route writes the same DB.

### `run_scheduler()`
`run_scheduler(config, max_tasks=None, dry_run=False) -> int`: runs the checks once, then tasks until none remain or `max_tasks`.

### `process_one_task()`
`process_one_task(config, dry_run=False, user_id=None) -> tuple[int, bool] | None`: claim, `running`, resources/ack/attachments, `execute_task()`.

**Success:** `detect_malformed_result()` can reclassify as failure; `CONFIRMATION_PATTERN`; `completed`; index; deliver; reset job failures and close the `cron_job` row.
- `once = true`: row deleted, CRON.md removal buffered (ISSUE-387; the FUSE write held the lock). `_remove_once_job_from_cron_md` runs after `deliver_pending`, never raises (post-commit), and re-deletes the row after writing, since `_sync_cron_files` could re-insert it in between.

**Failure:** the row branch and the terminal-event block classify through one pair, `retry_flags(task, result, *, success)` and `decide_retry(...)` (F4); `retry_flags` returns `decide_retry`'s keyword names so no site can disagree on one flag (the event block once omitted `is_sigpipe`, latent). `RetryDecision.reason` is diagnostic only; do not refactor sites onto it (reasons collapse onto one else-arm; the event block tests `is_requeued` before `is_cancelled`). `decide_retry` owns the `1 << (attempt_count * 2)` backoff and `attempt_count < max_attempts - 1` budget; `tests/test_scheduler_retry_decision.py` enumerates all 64 combinations and greps for a copy. `run_task_inline` keeps its own `is_cancelled`.
- Cancelled: no retry, `result` = `task.partial_result` (ISSUE-372), nothing posted.
- Policy refusal (`_is_policy_refusal`): failed, `_post_policy_refusal_alert()`, no retry.
- Shutdown collateral (`_is_shutdown_collateral`: `_shutdown_requested` and `is_signal_termination`): `db.release_task_for_restart`, no attempt charged, no backoff, deferred files purged (ISSUE-191, restart SIGTERMs the cgroup). The unit's `KillMode=mixed` routes this to orphan recovery; this branch is the half shipped by auto-update. Bounded by `fail_ancient_pending_tasks`. Event block emits "Scheduler restarting" `progress_text`.
- Permanent provider error (`is_api_error_banner` and `is_permanent_api_error`): no retry (ISSUE-212). Banner-gated so an answer discussing a 400 still retries.
- Else retry at 1, 4, 16 min (not OOM), else fail with `result` = `partial_result`, appended to both notices by `_with_partial_work` below the error line, uncapped, terminal failures only.
- Job auto-disable (here, policy refusal, `_record_publish_failure`): `db.suspend_scheduled_job` writes `auto_disabled_at`, never `enabled` (the CRON.md sync rewrites `enabled` every tick), and a `cron_job` notification is buffered for `deliver_pending`. `!cron disable` uses `db.disable_scheduled_job` since it also writes the file. Resolver closes on `auto_disabled_at IS NULL`.

Deliver results outside the DB context.

## Deferred DB Operations

Sandboxed tasks have no DB, so they and skill CLIs write JSON to the temp dir and the scheduler replays it after success only. Handlers live in `scheduler_deferred.py`. Identity always comes from the task, never the JSON.

- `_process_deferred_subtasks`: admin-only (others deleted), `source_type="subtask"`, inherits `queue`.
- `_process_retired_deferred_files`: deletes `_RETIRED_DEFERRED_SUFFIXES` files (`tracked_transactions`, ISSUE-427, fed dead framework tables). Runs first; must precede `_warn_unconsumed_deferred_files`. The suffix stays in `_KNOWN_DEFERRED_SUFFIXES` (purge still clears it) but not in the `expected name:` hint.
- `_process_deferred_sent_emails`: `sent_emails` rows for emissary reply matching.
- `_process_deferred_kg_ops`: commits per op.
- `_process_deferred_kv_ops`: set ops re-read the value so they compose; `set-trim` skipped on a missing row. Non-dict entries skipped (ISSUE-451; `_load_deferred_json` checks only the outer list); same in sent_emails.
- `_process_deferred_user_alerts`; `_load_deferred_email_output` (preferred over stdout-JSON).
- `_process_deferred_health_ops`: `source_path` is attacker-influenced and the daemon is unsandboxed, so `_resolved_source_path` resolves symlinks, confines (`_source_path_allowed`) to the deferred dir or `{mount}/Users/{uid}` (email attachments land in `inbox/`) and returns the path to read. `attach_document`, `register_upload` and `import_csv` (ISSUE-447) all use it; a new source-path op must too. `attach_document` uses `_health_max_document_bytes`, validates its target first, and resolves `encounter_ref` via a separate `encounter_refs` dict (unresolved raises). The op loop catches `Exception` (ISSUE-451), `conn.rollback()`s the failed op (the next per-op commit would sweep it in) and writes the failure sidecar. `_load_deferred_json` also catches `RecursionError` and `UnicodeDecodeError`.
- `_process_deferred_garmin_import`: runs in-process (daemon holds `ISTOTA_SECRET_KEY`), then notifies.

**Retry replay safety (ISSUE-074).** Files are keyed by `task.id`, which requeues keep, so `process_one_task` calls `_purge_deferred_files_for_retry()` after the claim when `attempt_count > 0`, covering all four requeue paths. The retry and shutdown-collateral branches also purge (the latter charges no attempt). Confirmation re-runs are exempt (`task.confirmation_prompt is None`): held ops wait for the answer and `confirm_task` keeps `attempt_count`; the stale-`email_output` cleanup prevents a double-send.

Unconsumed files are warned about and left on disk (ISSUE-073). All reads and writes name `utf-8`.

**The drain is guarded per handler (ISSUE-469)**; no `Exception` escapes `_drain_deferred_ops`.
- Per handler, not one `try`, which would skip every later handler. The table is built inside the function so tests can patch handlers.
- The task stays `completed` (a retry would re-execute a correct answer). `_record_deferred_handler_failure` logs ERROR with `exc_info` and a `task_logs` row with the exception type only (messages can carry model content and host paths; cross-user admin surface), on a connection at `main_loop_read_timeout_ms` so a locked DB does not delay delivery 30s per handler.
- Delivery stays after the drain.
- A `BaseException` (e.g. `CancelledError` from `run_coro` during `AsyncRuntime.stop`) is recorded and re-raised.
- Handler-level guards still protect later ops in the same file; `_process_deferred_user_alerts`'s except recovers by sending directly, so do not remove it.

## WorkerPool
`dispatch()` runs the admission gate, then the fg cap, then the bg cap. `_workers` is locked and keyed `(user_id, queue_type, slot)`; per-user caps via `effective_user_max_fg_workers` / `_bg_`; spawns up to `min(per_user_cap, claimable_pending)`. `shutdown()` = request_stop + join 10s.

### Long-task slot reclassification (foreground only)

A running foreground task older than `long_task_threshold_minutes` stops counting against the interactive cap and uses a long allowance. Nothing is preempted or killed. Pure functions `plan_foreground_slots(*, threads, discounted, pending, user_fg_cap, user_max_long_workers)` and `allocate_long_discounts(long_by_user, *, priority, user_cap, instance_cap)`; per tick `db.count_long_running_tasks_by_user(conn, "foreground", threshold)`.
```
discounted  = allocate_long_discounts(...)[user]   # <= user_max_long_workers, <= max_long_workers
interactive = max(0, threads - discounted)
may_spawn   = min(user_fg_cap - interactive, (user_fg_cap + user_max_long_workers) - threads, claimable_pending)
slot from range(user_fg_cap + user_max_long_workers)
```
- `max_long_workers` bounds discounts, not long tasks (they become long while running); excess long tasks count as interactive.
- Per user additive; instance-wide partitioned inside `max_foreground_workers`, which stays the hard ceiling (the spec's Track C text implied raising it; its own rejected alternatives did not). Budget goes over every user with a long task, longest-waiting first.
- `max(0, …)` is required (counts read before `_lock`). NULL `started_at` counts short.
- Only users at their interactive cap get discounts, else users with free slots take the budget.
- `_retire_surplus_foreground_workers` takes the loan back gracefully when interactive count exceeds the cap; also retires workers stranded by a lowered cap.
- Discounts come from `running` rows; a stale row over-grants at most one thread, bounded by the per-user and hard ceilings, until reclaimed. Clamping to threads is inert.
- Same-conversation follow-ups stay blocked by `_CLAIM_CHANNEL_GATE_SQL`, by design; the allowance unblocks other rooms.
- 0 on any knob restores old behaviour and skips the query. Background untouched. The allowance is global-only; a per-user override needs the `allocate_long_discounts` call site changed too.

### Memory admission gate

Below `min_available_memory_mb` of `MemAvailable` or above `host_pressure_psi_threshold` (`memory some avg10`), nothing spawns and the DB scan is skipped.
- The gate covers every claim: `dispatch()` via `_admission_open()` and both worker claim paths via `pool.admission_open()`, because a lingering worker otherwise kept claiming while the task should have stayed `pending` (`tests/test_worker_pool_admission.py::TestLingeringWorkerRespectsTheGate`).
- Daemon only; `istota run` is ungated.
- `admission_open()` is pure (workers poll it constantly); `_admission_open()` stamps `_gate_closed_since` and has one caller, `dispatch()`. `_emit_host_pressure_snapshot` uses the pure one so it does not reset the clock it reads.
- A closed gate keeps the idle deadline, so the pool drains to zero; the legacy coarse branch exits. Checked before `pending_count()`.
- Because of the drain, `scheduler_stats` carries `admission_closed_s` and the "istota is a bystander" alert keys on `_claimable_backlog()`, not worker count.
- Admission only; held tasks stay `pending`. Fails open (disabled, no or unreadable sample, both thresholds 0); `update_pressure(None)` clears. `shmem_unaccounted` fires the snapshot, not the hold. One `dispatch_admission_closed` WARNING per cooldown.

### Per-task cgroups (`task_cgroup.py`)

`execute_task` creates `<unit cgroup>/task-<id>-<attempt>/` (`memory.max`, `pids.max`, `cpu.max`) and releases it from the proxies' `ExitStack` on every exit path. `run_daemon` sweeps leftover `task-*` (an OOM-killed daemon runs no cleanup).
- Attempt in the name, so a retry never shares a surviving runaway's directory.
- `destroy` on `EBUSY` writes `cgroup.kill` (kills escaped descendants, ISSUE-257 shape) and polls `rmdir`; the sweep reports survivors apart from removals.
- `oom_kill` and `pids.events` `max` are logged with the config key before `rmdir`. `pids.max` counts threads and is the likeliest to bind.
- Swap deliberately unbounded (zram absorbs first); revisit if a contained task drives swap I/O.
- Sibling of the daemon's cgroup (v2 forbids processes plus child controllers); `DelegateSubgroup=supervisor`; `resolve_root` takes the last `.service`/`.scope` component (the first may be `user@N.service`).
- The kernel is the probe: a `memory.max` write proves delegation. Failed `memory.max` removes the dir; failed `pids.max`/`cpu.max` keeps it.
- Placement in the child before exec (ISSUE-285): `BrainRequest.task_cgroup` → `task_cgroup.placement(...)` as `preexec_fn`, because membership is inherited at `fork` and `bwrap` forks during setup. The parent opens `cgroup.procs`, the child writes `0` (safe in a threaded daemon). Each spawn then calls `verify_placement(pid, path)`; a miss warns once and is not retried (`tests/test_task_cgroup_placement.py`).
- `place()` only for TmuxClaudeBrain via `on_pid`.
- Fails open, never silent: `create` returns `None` without delegation, logged once per cause. `STARTUP Per-task cgroups:` is a real probe under `task-probe`, since resolving the root succeeds on any systemd host.
- `task_cgroup_enabled = false` disables; `task_cpu_max_percent = 0` writes no `cpu.max`.

## UserWorker
`UserWorker(user_id, config, pool, queue_type, slot)`, `run()`, `request_stop()`.
- Loops on `process_one_task`, each claim gated on `pool.admission_open()`.
- On an empty queue it lingers in `_worker_idle_wait`, re-checking every `worker_idle_poll_interval` until `worker_idle_timeout` of continuous emptiness, so follow-ups are claimed fast (the parked worker holds the slot). A lost claim race does not reset the deadline. Poll ≤ 0 or ≥ timeout = legacy single wait.
- Pre-check and dispatch counts use `count_claimable_tasks_for_user_queue` (`_CLAIM_CHANNEL_GATE_SQL`), so a same-room follow-up counts 0. Raw `count_pending_*` is status-only.
- Overlap with `dispatch()` is harmless: `claim_task` is atomic. Fresh DB connections per worker.

## Poller Integrations

Each poller runs on its same-named interval key: `_talk_poll_loop()`, `poll_emails()`, `poll_all_tasks_files()`, `check_heartbeats()`, `check_db_health()`, `discover_and_organize_shared_files()`, `check_skill_overlay_reindex()`, `check_doctor()` (3600s); briefings, jobs and sleep cycles on `briefing_check_interval`; `check_travel_timezone()` (900s, `location.enabled`).
- `check_worktree_reap()`: gated on `developer.enabled`, `repos_dir`, `worktree_reap_enabled`.
- `check_sandbox_cache_sweep()`: gated on `sandbox_cache_sweep_enabled` and `sandbox_cache_sweep_root(config)`; skips users with a live task.
- `check_avatar_import()`: gated on `storage_is_nextcloud` and `web.avatar_import_from_nextcloud`. Users from `config.users`, never `user_avatars`; no transaction across a fetch; a generated avatar writes a NULL-image probe row with the ETag; stamps `shared_kv` `_avatar_import`/`last_tick` for doctor.

**Learned playbooks** (`playbooks.enabled`, ISSUE-174): sleep-cycle extraction gains a `PLAYBOOKS:` section (gated on `min_tool_calls`) copying commands verbatim from the `Tools (N):` line into a thin router; `_process_extracted_playbooks` writes `playbooks/<slug>.md` indexed as `source_type="playbook"`. `pinned: true` files are re-indexed from their content, never overwritten or pruned. `cleanup_old_playbooks` prunes on last-use mtime (stamped by `_recall_playbooks`) and deletes the chunks; grandfathered once (`.retention_initialized`). See `memory.md` for the full lifecycle.

## Cleanup (`run_cleanup_checks`)
1. Expire stale confirmations; notify via the user's `alert` route (ISSUE-241) after the transaction closes.
2. Warn on stale pending. 3. Fail ancient pending; notify only user-submitted types, never `_AUTOMATED_SOURCE_TYPES` (flood when the queue wedges).
4. `cleanup_old_tasks`. 4a. Prune `processed_emails`. 4b. `prune_old_usage` in its own transaction: steps 1-4a are one long write transaction and a large delete under it stalls readers. Failures log at WARNING.
5. IMAP cleanup. 6. Temp files.

## Memory Search Integration
After completion with `auto_index_conversations`: index under `user_id`, and `channel:{token}` in a channel.

## Config Intervals (SchedulerConfig defaults)

| Param | Default | Notes |
|---|---|---|
| `poll_interval` / `dispatch_interval` | 2s / 0.5s | Sub-tick dispatch; 0 or ≥ poll = once per tick |
| `talk_poll_timeout` | 30s | Server-side hold only, so the wait gate is `talk_poll_timeout + talk_poll_wait`. Refused ≤ 0 (`_positive_int`, ISSUE-399) |
| `talk_poll_full_sweep_interval` | 300s | ISSUE-399. Between sweeps a room whose listing `lastMessage.id` is not past its cursor is skipped; fails toward fetching; a stale (fallback) list ungates. `archive_orphaned_talk_rooms` runs only on a sweep. 0 = always sweep |
| `email_poll_interval` / `email_poll_batch_size` | 60s / 50 | Batch of oldest UIDs above the `email_poll_state` cursor (ISSUE-250); `processed_emails` stays the authority |
| `email_rate_limit_messages` / `email_sender_rate_limit_messages` / `email_rate_limit_window_seconds` | 60 / 20 / 3600 | ISSUE-250. In `poll_emails` before `ingest_message`, after owner resolution and the quiet filter; per sender on the addr-spec, taskless rows excluded. Over-budget mail is filed `throttled` and left in the folder, not dropped. One alert per user per window (`_ThrottleNotice`) |
| `email_task_queue` | `background` | Keeps floods off foreground slots. The channel gate is foreground-only, so email and a live turn in one room may run concurrently; a cross-queue gate was rejected (briefings would block rooms) |
| `email_max_body_chars` | 32000 | `_truncate_body`, marked |
| `email_max_attachment_bytes` / `_per_poll` | 25 MiB / 100 MiB | Whole attachments skipped; the per-poll budget bounds a batch |
| `talk_poll_interval` / `briefing_check_interval` / `tasks_file_poll_interval` / `shared_file_check_interval` / `heartbeat_check_interval` / `db_health_check_interval` | 10s / 60s / 30s / 120s / 60s / 24h | DB health covers module DBs via `Config.module_db_path` |
| `main_loop_read_timeout_ms` | 2000 | Busy timeout for dispatch scan and idle pre-check; a lock skips the tick instead of tripping the watchdog |
| `db_backup_*` | true / 24h / "" / 7 | See below |
| `scheduler_stats_interval` | 60s | `scheduler_stats threads= fds= rss_mb= tasks_running= workers_active= admission_closed_s=`; psutil fields omitted with one WARN |
| `host_pressure_*` | see below | |
| `loop_stall_alert_seconds` | 180s | `LoopWatchdog` (ISSUE-143), one alert via `_operator_alert_user`, re-arms. `suspended()` kept as an escape hatch; prefer `_spawn_background_check` |
| `max_foreground_workers` / `max_background_workers` / `user_max_foreground_workers` / `user_max_background_workers` | 5 / 3 / 2 / 1 | Scans are `ORDER BY MIN(created_at)` (ISSUE-250) because dispatch breaks at the cap and keeps no state |
| `worker_idle_timeout` / `worker_idle_poll_interval`; `long_task_threshold_minutes` / `user_max_long_workers` / `max_long_workers` | 10s / 0.5s; 10 / 1 / 2 | |
| `task_cgroup_enabled` / `task_memory_max_mb` / `task_pids_max` / `task_cpu_max_percent` | true / 2048 / 512 / 200 | 0 on memory/pids writes `max` |
| `task_timeout_minutes` / `confirmation_timeout_minutes` / `max_retry_age_minutes` / `stale_pending_fail_hours` / `task_retention_days` / `scheduled_job_max_consecutive_failures` | 30 / 120 / 60 / 2 / 7 / 5 | |
| `usage_retention_days` | 180 | Outlives tasks on purpose; not a year because backups duplicate the DB 7x. Bounds built as ISO-Z in Python, not `datetime('now', …)` (`' '` < `'T'`) |
| `email_retention_days` / `processed_email_retention_days` | 7 / 90 | See below |
| `skill_overlay_reindex_interval` | 6h | ISSUE-343. Overlays have no CLI write path. Not in the sleep cycle, which bails when disabled or on `primary_brain_unavailable`. Own connection; runs on the first tick; never raises |
| `cron_max_staleness_minutes` | 60 | Past it, skip the insert and bump `last_run_at` (no catch-up herd). 0 = catch up |
| `max_subtasks_per_task` / `max_subtask_depth` / `max_subtask_prompt_chars` | 10 / 3 / 8000 | |
| `stream_text_gate_chars` | 280 | No `text_delta` until a run crosses N chars without a tool call; `settle_at_tool_boundary` drops short lead-ins. Never loses text |

### DB backup
Snapshots framework + module DBs to `db_backup_dir/<date>/` (default `{workspace}/Backups/db/snapshots`) via online backup.
- Dated dirs (ISSUE-159); `_prune_old_snapshots` never prunes the newest good copy of any DB.
- `_apply_collapse_guard` marks `*.suspect` when a DB that held data comes back empty (exact zero).
- `0700`/`0600`; cold copies in DELETE journal mode (WAL on FUSE SIGBUSes).
- `_destination_is_durable`: a destination resolving under `nextcloud_mount_path` is written only if `os.path.ismount`; keyed on the resolved path, not the config branch (ISSUE-480); `user_scope.is_within`; mount resolved once (`ismount` is False on a symlink). Outside the mount is trusted.
- Clock persisted at `{db_path.parent}/.db_backup_last_run`, advances only when ≥1 DB succeeded. `_alert_backup_problems` and `_maybe_alert_backup_stale` (> 2x interval, after a prior success) use `_send_operator_alert` (thread with join timeout, ISSUE-143 class).
- `python -m istota.db_backup` forces a run; `python -m istota.db_restore --all` (or `--date`) clears sidecars, refuses empty snapshots without `--force`, refuses while `_daemon_running`.

### Host pressure
- Breadcrumb on `istota.scheduler.pressure`, unconditional (slow leaks cross no threshold). `shmem_unaccounted_kb` = `Shmem` − Σ tmpfs used (deduped by `st_dev`). Field order is a data format (`test_host_pressure.py`). Gated on `/proc/meminfo`, not PSI (Debian disables PSI by default). Unmeasured = `?`, never 0. Carries `memory_events_high` / `memory_events_oom_kill`.
- `is_under_pressure` gates admission; `snapshot_trigger` adds the residue arm for attribution only. `host_pressure_docker_socket` (root-equivalent, read-only GET, `""` disables) maps containers to pids.
- Sandbox attribution (ISSUE-286): `_emit_host_pressure_snapshot` reads running `(id, worker_pid)` at `main_loop_read_timeout_ms`; failure passes `None` (`sandbox not-queried`). `find_sandboxed_pid` descends past the outer bwrap to a different mount namespace.
- Never raises into the loop; the gate gets its sample before attribution runs.

### IMAP retention
`email_support.cleanup_old_emails` → one `skills.email.delete_emails_before` sweep (IMAP `BEFORE`, batched, one connection; ISSUE-230). Internal date; everything past the cutoff; count logged first.
- `UID EXPUNGE` with UIDPLUS (read post-auth by `_server_capabilities`), else folder-wide `EXPUNGE` with one warning per host. A refused `UID EXPUNGE` rolls back `\Deleted`. `delete_email` shares the path.
- `_MAX_DELETES_PER_SWEEP` (2000) because it runs on the dispatch loop.

### `processed_emails` retention
ISSUE-231, on `processed_at`. Rows referenced by `messages` are never deleted (`_EMAIL_SENDER_SUBQUERY`, ISSUE-226). `_effective_processed_email_retention` floors at `email_retention_days + 1` (re-ingest guard). Key `(uidvalidity, email_id)`. `email_retention_days = 0` disables the prune.

## Other Scheduler Functions

- `get_worker_id()`: `{hostname}-{pid}[-{user_id}]`.
- **Event streaming.** Brain-path tasks get an `EventWriter` (Talk, log-channel, push subscribers); terminal events and `writer.finish()` only on a non-retry terminal state. A retry emits `progress_text` and keeps the log; `seq` resumes via `db.get_max_task_event_seq`. `web_app._synthetic_terminal_events` backstops a missing `done`.
- `post_result_to_talk()` (`target_token` override); `_talk_target_for_delivery()` is a shim over `transport.routing.talk_channel_for_task`, ladder in `.claude/rules/transport.md`.
- `_execute_command_task()`: cwd `config.temp_dir`; env = `build_stripped_env()` + `ISTOTA_*` + `build_skill_env(list(skill_index), …)` with `discover_calendars_for_task`, then `dispatch_setup_env_hooks` (overwrite ambient env, ISSUE-097). `ISTOTA_EXPERIMENTAL_FEATURES` and `ISTOTA_DEFERRED_DIR` (ISSUE-233) injected; deferred writes land only on exit 0.
  - Success is exit 0 unless stdout is a JSON dict with `"status": "error"` (facades and `@requires_feature`); money's inner envelope unwrapped (`_unwrap_inner_error`).
  - Runs via `shell_exec.shell_argv`, not `shell=True` (dash has no `pipefail`, ISSUE-307 twin); `_run_capture` has no `shell` parameter. Without bash, `/bin/sh -c` with one log. Interpreter swap: `echo`/`$0` differ; `$BASH_ENV` stripped by `build_stripped_env` (`_SHELL_STARTUP_ENV_VARS`; `ENV` kept).
  - Upgrade effects: hidden failures now fail and can auto-disable; a 141 gets `SIGPIPE_NOTE` and is non-retryable via `is_sigpipe_failure` gated on `task.command` (the ladder would repeat side effects).
- `_execute_skill_task()`: `python -m istota.skills.<skill>`, same env over the full index, same envelope check. `health garmin-sync` short-circuits to `_run_garmin_sync_inprocess` (ISSUE-098; needs `ISTOTA_SECRET_KEY`), which returns `{"status", "inserted", "skipped", "days_processed", "auth_error", "error"?}`.
- `_run_capture()`: `Popen(start_new_session=True)`; on timeout SIGKILLs the group via `process_group.kill_group_if_live` (reaped guard, F5), then re-raises; `subprocess.run(timeout=)` hung on grandchildren holding the pipe.
- `check_briefings()`: enqueues a background `briefing` task with `briefing_name`; no network on the dispatch thread (ISSUE-143); the prompt is built by `executor.build_deferred_briefing_prompt`. Same for `check_briefing_triggers`.
- `check_scheduled_jobs()`: skips a fire while `db.count_inflight_tasks_for_scheduled_job > 0` without advancing `last_run_at`. `_resolve_job_model_effort` resolves `job.model` against the brain `resolve_brain_kind` picks for the job via `resolve_alias`, keeping effort (ISSUE-419). Never raises.

## WhatsApp request and relay polling

The `whatsapp_requests` gate drains durable requests on the runtime that owns Baileys. Self-sends may leave mid-task; held questions may not. After success `process_one_task` parks a stored question with its exact preview, only to the verified private origin; failures close unsurfaced holds. Approval releases the stored action, not resumed model output. Bounded batches; never bypasses the ledger's no-resend rule. See `whatsapp.md`, `relay.md`.

---

# DB Module (db.py)

## All Tables

| Table | Notes |
|---|---|
| `processed_emails` | `UNIQUE (uidvalidity, email_id)` (ISSUE-250) |
| `scheduled_jobs` | See below |
| `monarch_synced_transactions`, `csv_imported_transactions` | Dead copies (ISSUE-427); live ones are in the money DB |
| `task_usage` | One row per brain attempt; not FK'd to `tasks` so it outlives cleanup; `task_id` NULL for task-less calls (`origin` names them), may dangle. `UNIQUE(task_id, attempt_seq) WHERE task_id IS NOT NULL`. Context fields NULL when unmeasured, never 0. Aggregates filter `has_totals = 1` and `initial_context_tokens IS NOT NULL` independently |
| `task_usage_models` | Per-model split; FK decorative, so `prune_old_usage` deletes children first. Native brain has no split; `--by model` uses the parent's `model` |
| `task_events` | `UNIQUE(task_id, seq)`; kept across retries and confirms (ISSUE-235): `confirmations.approve` relabels `confirmation` to `confirmed` in place and prunes only `done` (ISSUE-592). A park keeps its `text_delta`/`thinking` rows; the re-run's terminal prune takes them, and a declined or expired question keeps them until `cleanup_old_tasks`. Bulk retention in `cleanup_old_tasks`; `db.delete_task_events` has no caller |
| `web_chat_rooms` | `UNIQUE(user_id, token)`, one handle per participant (ISSUE-134) |
| `room_members` | `PRIMARY KEY (room_token, user_id)` (ISSUE-134); visibility via `list_member_rooms` |
| `web_chat_messages` | Legacy, no reader or writer; kept only because `delete_web_chat_room` clears it; dropping needs a migration |

**`scheduled_jobs`.** `enabled` is user intent (CRON.md); `auto_disabled_at` is the scheduler's suspension; fires only when `enabled = 1 AND auto_disabled_at IS NULL`. `disabled_at` marks that `!cron disable` wrote the 0 (ISSUE-392), read by the module sync's rescue arm and the `!cron` listing; the CRON.md sync never writes it from the file but clears it with `enabled = 1`. `brain` (ISSUE-419) is file-owned and admin-only (`cron_loader.fj_brain_or_none`). Only a pin `resolve_brain_kind` admits (listed in `[brain] room_selectable`) reaches `db.create_task(brain=…)`, else NULL: the model is resolved against the kind that runs, so a raw pin reads as a namespace crossing and the model is dropped. NULL, not the fallthrough kind, which would clear `fallback`.

## Key DB Functions

### Task Operations
`create_task(conn, prompt, user_id, source_type="cli", …) -> int`, `claim_task(conn, worker_id, max_retry_age_minutes=60, user_id=None)`, `update_task_status(conn, task_id, status, result=None, …)`, `set_task_pending_retry`, `release_task_for_restart` (attempt_count untouched), confirmation and cancel helpers, `list_tasks`, `get_users_with_pending_*tasks`.
- `create_task` raises `ValueError` for a `user_id` that cannot name its own directory (`user_scope.is_scopable_user_id`, ISSUE-402); covers local entry points (`istota task -u`, `istota repl -u`, `execute_task_interactive`).
- `update_task_status` writes `result` as `COALESCE(?, result)` on `failed`/`cancelled` (ISSUE-372), so re-marking a completed row failed after an email-delivery error keeps the answer (ISSUE-255).

### `claim_task()` Locking Mechanism
1. Fail old stale locks; 2. release recent stale locks; 3. fail old stuck running; 4. release recent stuck running; 5. fail stuck running with retries exhausted; 6. atomic `UPDATE…RETURNING`, `priority DESC, created_at ASC`, sets `locked`/`locked_at`/`locked_by`.

Steps 3-5 and `fail_stuck_locked_running_tasks()` share `_STUCK_RUNNING_PREDICATE` (ISSUE-112): stuck means `last_heartbeat` silent past `worker_stuck_minutes`, else `started_at` past `task_timeout_minutes` + grace. Workers ping via `_task_heartbeat`, so a slow live worker is never reclaimed.

**`worker_pid` invariant.** Cleared on every exit from `running` (`update_task_status`, `set_task_pending_retry`, `release_task_for_restart`, `recover_orphaned_tasks`), because `!stop` and `web_app._chat_cancel_task` signal whatever the row holds (ISSUE-191). Both use `process_group.kill_process_group(pid, SIGTERM)` (ISSUE-257), falling back to the single process when the pid leads no group. The streaming child and tmux panes lead groups; the non-streaming child (pid since ISSUE-550) does not, so its tree is orphaned. Clearing bounds the wider stale-pid hazard; `_chat_cancel_task` also gates on `status IN ('running','locked')`.

### Startup orphan recovery (`recover_orphaned_tasks_on_startup`)
Under the flock every `running`/`locked` row at boot is an orphan; recovered before workers spawn. `db.recover_orphaned_tasks`: `cancel_requested` → `cancelled`; retries exhausted, too old, or `INLINE_ONLY_SOURCE_TYPES` → `failed`; else `pending` with `attempt_count` bumped and liveness cleared. Cancelled/failed get a terminal frame from a subscriber-less `EventWriter`; released orphans emit nothing. `pending_confirmation` untouched.

### Conversation & Context
`get_conversation_history` / `get_previous_tasks` take `user_email_addresses`; `external_email_sender` is pure; `ConversationMessage` carries `external_sender`.

**Email sender attribution (ISSUE-226).** `user_id` is who mail was routed to, not its author. Both readers recover the envelope sender from `processed_emails` by scalar subquery on `task_id` and set `external_sender` when it is not one of that user's addresses (address, not `routing_method`). `user_email_addresses` is a per-user map; omitting it attributes every email turn to its sender. `context._speaker_label` renders `External sender <addr>` as an ASCII dot-atom or `unknown sender`, never the raw header. Also used by `memory/sleep_cycle.speaker_labels` and `index_conversation(..., speaker=…)`.

### Other Key Functions
`reset_scheduled_job_failures` also lifts a suspension; `disable_scheduled_job` is the user's verb (`enabled = 0` + `disabled_at`), `suspend_scheduled_job` the daemon's (`auto_disabled_at`). Also `expire_stale_confirmations`, `fail_ancient_pending_tasks`, `cleanup_old_tasks`, `record_sent_email`, `find_sent_email_by_references`.
