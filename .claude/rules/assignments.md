---
paths:
  - "src/istota/assignments/**"
  - "src/istota/skills/assignment/**"
---

# Assignments

An assignment is a goal Istota holds across days until it is met, abandoned or out of budget. It works on the goal in short task runs called wakes, keeps a scratchpad in the user's bot directory, hands steps to the user, and stops on its own. In chat people say quest, errand or mission; code, tables, CLI, files, notices and logs say **assignment** and nothing else. The step handed to the user is a hand-off (`assignment_actions.kind = 'handoff'`), never "handoff" on its own, since `config_editor/handoff.py` has that name.

The feature is admin-only and gated by one experimental flag, `skill_assignment` (`experimental.KNOWN_FEATURES`). The same flag gates skill selection, the CLI, the scheduler's gate and the dispatch lane.

## Where it lives

A core package, `src/istota/assignments/`, not a module in `MODULE_NAMES`: wakes need a source type, a queue and columns on `tasks` and `task_usage`, which only the framework DB holds. `__init__.py` stays empty.

| Module | Holds |
|---|---|
| `store.py` | Row access for every assignment table. Plain functions on the caller's connection; commits nothing, decides nothing. |
| `charter.py` | Validating charter fields (`CharterError(code, message)`), the slug, `render`, `charter_digest`, the assignment directory with its seed `SCRATCHPAD.md`, and the `CHARTER.md` writer. |
| `validators.py` | The validator menu (`file_parses`, `csv_columns`, literal `contains`), `parse_spec`, and `run_item`, which never raises. |
| `lifecycle.py` | The one writer of `assignments.state`: one function per transition, each inside `relay.requests.write_transaction` with the source state re-read and checked (`TransitionError(code, message, state)`), each appending an `assignment_events` row. Also the user's other writes: `edit_charter`, notes, item confirmation, hand-off reports, `topup`, `close`. |
| `caller.py` | `classify(conn, task_id=, user_id=)`: `UserTurn`, `Wake(assignment_id, wake_id)` or `Other`, from the task row. |
| `budget.py` | The ledger (`totals`), `remaining`, `is_spent`, `topped_up` and the `max_active_per_user` count. |
| `wakes.py` | `dispatch_due` (the `assignments` gate's body and the one creator of wakes), `start_wake` (pickup), `reap_orphans`, and `finish_wake`. |
| `scratchpad.py` | Reading and repairing `SCRATCHPAD.md` by descriptor, the snapshots, diffs, `begin` (user-edit detection, repair, the `before` snapshot) and `end` (user lines kept, the `after` snapshot). |
| `prompt.py` | The wake prompt: the system half's `## Assignment` section and the user half. |
| `skills/assignment/` | The CLI: the user-turn verbs, `activate`, `list` / `show`, and the wake verbs. |

The bell resolver is added by a later stage of the same work.

## Wakes and the lane

- **One creator.** The scheduler's `assignments` gate (on `briefing_check_interval`, a fifth known mismatch beside the four in scheduler.md; `enabled` by the flag; daemon only, not `one_shot`) runs `wakes.reap_orphans` then `wakes.dispatch_due`. The user verbs that wake an assignment (`wake`, `resume`, `topup`, `close --reject`) only make it due. Each assignment is decided and woken in its own `write_transaction` that re-reads the row, then committed, so two ticks cannot both wake one and one bad row is logged and skipped.
- **The rules**, first match wins: a passed deadline with no `deadline_wake_done` event carrying that deadline (so an extended deadline fires again); a sleeping assignment whose `next_wake_at` has come; a sleeping one with an unconsumed `PULL_FORWARD_KINDS` event, held to `min_wake_interval_minutes` after the last wake ended (else `next_wake_at` moves to that floor); a waiting one with an unconsumed event whose kind ends its wait (`WAIT_WAKERS`: `user` is ended by `user_note`, `handoff_done`, `charter_change`; `completion` by nothing, since `close` answers it). `budget_low` and `user_edit` never pull a wake forward: the host writes them around a wake.
- **Budget first.** `budget.check_before_wake` runs before anything is created; a spent budget is `lifecycle.pause_for_budget`, no task.
- **The wake task** is `source_type` and `queue` `assignment`, `max_attempts = 1`, no conversation token, no output target, `skip_log_channel`, prompt `[assignment wake]`, created only when due (never `scheduled_for`). Model and effort resolve through `scheduler.resolve_pinned_model_effort`, the cron job's resolver; a brain pin survives only if `[brain] room_selectable` lists it. The `assignment_wakes` row and the `active` transition (`lifecycle.wake_created`) are in the same transaction.
- **The lane** is `WorkerPool.dispatch`'s third phase (`_spawn_capped_workers`, shared with the background phase): `[assignments] max_workers` and `user_max_workers`, behind the same admission gate, no channel gate. It is not gated on the flag, so a wake queued before the flag went off still reaches pickup, which turns it away. `_claimable_backlog` and the status file (`pending_assignment`) count the queue.
- **Nothing is delivered.** `transport.registry.NO_DELIVERY_SOURCE_TYPES` makes `resolve_delivery_plan` answer an empty plan for a wake whatever its `output_target`, and `_surface_for_source_type` answer `none` rather than fall through to `talk`. `tests/test_assignments_wakes.py` holds every source-type collection's decision for `assignment` in one table: it is automated (`scheduler._AUTOMATED_SOURCE_TYPES`, `session.result`, `sleep_cycle`, the web admin split) and in no interactive, retry, run-now, job-failure or transcript set.
- **Orphans.** `reap_orphans` finishes an open wake whose task is gone, or terminal for `ORPHAN_GRACE_SECONDS`, so the worker that ran it gets there first. `finish_wake` ends a wake once (a second call is a no-op) and never raises; a finish that throws ends the wake `failed` with a `finish_failed` event, so it is not retried every tick. Today it records the end and returns an `active` assignment to `sleeping` at its cadence, keeping a pause or abandon the user set meanwhile; the full sequence (scratchpad, validators, signals, `next`, pauses) is the finishing stage's.

## Pickup and the wake prompt

- **Pickup** is `wakes.start_wake`, called by `execute_task` for a `source_type = 'assignment'` task at the point a briefing builds its deferred prompt. A task whose assignment is not `active`, whose wake is not open, which is not the user's, or which runs with the flag off is turned away: `finish_wake(config, task, "not_active")` ends the wake (not a failure; an `active` assignment goes back to `sleeping`, due now) and the task returns `NOT_ACTIVE_ERROR` without reaching the brain. Otherwise one `write_transaction` runs `scratchpad.begin`, claims every unconsumed inbox event for the wake (`consume_inbox`), sets `started_at` and writes a `wake_started` event. A re-run of a started wake rebuilds the prompt from its `before` snapshot and the events it claimed, writing nothing again. A dry run writes nothing and renders the unconsumed inbox.
- **Limits.** The brain's `timeout_seconds` is `per_wake_minutes`, clamped to `scheduler.task_timeout_minutes`; `BrainRequest.max_turns` is `per_wake_turns`, which `NativeBrain` applies as `min(config.max_turns, max_turns)` and the Claude Code brains ignore. `ISTOTA_ASSIGNMENT_ID` (`caller.ASSIGNMENT_ID_VAR`) goes into the skill proxy's base env only, never the model's.
- **Memory.** A wake loads `USER.md`, knowledge facts and playbooks as a private task does: `_recall_playbooks` exempts the `assignment` source type from its automated-task skip, and the recall query (memories, facts, playbooks) is the charter's goal, not the placeholder prompt.
- **The scratchpad.** `begin` compares the file with the last `after` snapshot, or the seed (`charter.SCRATCHPAD_SEED`, written as a `seed` snapshot the first time it is needed). A difference is a trusted `user_edit` inbox event carrying a unified diff (cut at 8,000 characters) and the lines it added, plus a `user_edit` audit row by the user. A missing, empty, non-UTF-8, over-1-MiB or non-regular file is restored from the newest whole `after`/`seed` snapshot (a link at the name is removed, never followed), with a `scratchpad_restored` row; an emptied file also records a `user_edit` row. A directory that cannot be opened is `scratchpad_unavailable`, and nothing is written. Snapshots over 512 KiB are stored truncated with the whole text's hash. `end` (called from `finish_wake` by the finishing stage) appends the user's lines that stood in the `before` snapshot and are gone, under `## From the user (kept by Istota, wake N)`, and stores `after`.
- **The prompt** (`prompt.py`). System half: `## Assignment` after the persona, in place of `## Response format`: `config/guidelines/assignment.md` (loaded by `load_channel_guidelines`), the assignment id and directory, and `charter.render` under `### Charter (set by the user)` with every heading line demoted three levels, so a line of the goal cannot open a system heading. Nothing there points at the user half; a compacted wake reads its state back with `assignment show`. User half, after the memory blocks: `## Wake` (status line and host notes: final wake, refused completion, thrash), `## Done when`, `## Open hand-offs`, `## Inbox` (a `trusted = 0` event fenced with `frame_untrusted`), `## Attempts` (30 most recent targets), `## Scratchpad` (capped at `scratchpad_prompt_chars`, keeping the head and `## Now`, inside a code fence longer than any in the file), `## Earlier wakes` (the last summary whole, older ones to `history_prompt_chars`), and a fixed `## User's request`. Golden: `source_assignment`.

## Activation

Activation is the one approval, and it rides the relay request ledger (`whatsapp_skill_requests`, `.claude/rules/relay.md`) as kind `assignment_activation`, following `location_share`. No second hold table.

- **Hold.** `lifecycle.hold_activation`, from `activate` in a user turn, refuses anything but a draft (`bad_state`), a passed deadline (`deadline_passed`), the active ceiling (`too_many_active`) and a second held activation of the same assignment in another task (`confirmation_pending`). It stores the request through `relay.requests._store_request` with `preview` the fixed `ACTIVATION_PREAMBLE` plus `charter.render`, and `destination = {"assignment_id", "charter_digest"}`, the digest taken in draft. The key is `assignment-<id>-<n>`, numbered per task, so a turn whose hold a charter change cancelled can hold again. A call from a task already holding or approved for this assignment returns that row, so the confirmed re-run calling `activate` again answers `approved`.
- **Park.** The scheduler's authorization branch (`scheduler._AUTHORIZATION_KINDS`) parks the task on the stored preview, not the model's answer, with the bell title `ASSIGNMENT_ACTIVATION_TITLE`. Like `location_share` it is in `_STOPPABLE_AUTHORIZATION_KINDS`: the park re-reads the request under the writer lock and cancels the task when it closed in between. No clean-turn release.
- **Approve.** `relay.requests.approve_request` calls `lifecycle.activate_from_request` after matching task, request and preview digest. It recomputes `charter_digest` and re-checks draft, deadline and ceiling; any miss closes the request `failed` with that code (`charter_changed`, `bad_state`, `deadline_passed`, `too_many_active`, `not_found`, `assignment_unavailable` with no config) and cancels the parked task, returned rather than raised so the approval savepoint keeps the close. Success is draft → sleeping, `next_wake_at` and `activated_at` now, an `activated` event, the request `approved`. The task then re-runs as every approved authorization does.
- **Cancel.** `edit_charter` calls `cancel_held_activation` on every charter change, which closes the held request and its parked task through `relay.relays.close_task_questions`. Abandon does not cancel it; the approval then fails `bad_state`.
- **Known gap.** `CHARTER.md` still shows the state line `draft` after approval until the next charter change rewrites it: the approval runs inside the confirmation's write transaction, where no file is written.
- **Migration.** The `kind` CHECK is widened by `db._rebuild_skill_request_kinds(conn, "assignment_activation")`; `_SKILL_REQUESTS_DDL` is held equal to `schema.sql` by `tests/test_whatsapp_requests.py`. A table rebuild, so the upgrade tier (`scripts/test-upgrade.sh`) is owed before a release.

## Who may write

- **A user turn** (`caller.UserTurn`) is a running task of the user from `web`, `talk` or `cli`, with no guest, no other author on its stored turn (`rooms.scopes.written_by_someone_else`), no parent, scheduled job, command or skill, not a group chat, and, when it has a conversation token, a registered room only that user reads (`db.is_private_room_of` on the canonical token; an unregistered token is refused). Every verb that changes the charter, the state or the budget, or writes a trusted inbox event, needs one. A wake trying one gets `not_a_user_turn`.
- **A wake** (`caller.Wake`, with its wake row) may use only the wake verbs, and only on its own assignment: anyone else gets `not_a_wake`, a wake naming another assignment `wrong_assignment`. `--assignment` defaults to `ISTOTA_ASSIGNMENT_ID`, then the task row's assignment, and is checked against the task row either way. Each write (`lifecycle.request_next`, `add_handoff`, `add_report`, `add_attempt`, `request_complete`) re-reads the wake in its transaction and refuses `not_a_wake` once it has ended. They go straight to the DB, not through deferred files, because a failed task's deferred files are purged and a failing wake's `next`, hand-offs and attempts must survive.
- **A wake's verbs request, they do not decide.** `next` stores `{mode, requested, wake_at, wait, clamped, recorded_at}` in `assignment_wakes.next_request` (a later call replaces it, `--progress` with it), `wake_at` already clamped below by `min_wake_interval_minutes` from the call and above by the deadline; `complete` stores the evidence in `complete_request` and appends a `complete_requested` event. `finish_wake` applies both. `handoff` writes an open `assignment_actions` row with the wake id and a `handoff_created` event; `report` is a `report` event (actor `assignment`, payload `{text}`); those two events are what the notices hang off, and both verbs are refused on a completed or abandoned assignment. `attempt` writes an `assignment_attempts` row with the target's whitespace collapsed, so repeats count.
- **`handoff` is two verbs by shape**: `handoff done <id> <action-id>` as three separate words is the user's; anything else, including a quoted `"done 4 7"`, is a wake's step.
- **The CLI's gates come first**: the `skill_assignment` flag (`feature_disabled`), admin (`not_admin`), `_guard_skill`, and a refusal when the calling task has scopes withheld, since an assignment is the user's personal record. Then every lookup is scoped by `ISTOTA_USER_ID`.
- **Refusals are raised, not printed.** Handlers raise `Refusal`, `TransitionError` or `CharterError`, and `run_skill_cli`'s `on_exception` turns them into `{"status": "error", "error": code, "message": ...}`, so a refused write rolls back with the connection. A successful write is committed before `CHARTER.md` is rewritten.
- **A draft's charter edits write no inbox event**; after activation every edit that moves the render writes a trusted `charter_change` with both renders. `edit_charter` is the one place a charter changes, so it is where a held activation is cancelled.

## The ledger

`budget.totals` sums `task_usage.cost_usd` by `assignment_id`. That column is `NOT NULL DEFAULT 0.0`, so "unmeasured" is read elsewhere: a finished wake (outcome other than `not_active`) is unmeasured when no usage row for its task has `has_totals = 1`, which covers a brain that writes no row and a run that ended before a result frame. Its cost counts as zero; `max_wakes` still binds. `task_usage` is pruned at `scheduler.usage_retention_days` (180), so an assignment older than that reads a smaller ledger.

## The tables

Nine tables in `schema.sql`, all new, so no `_run_migrations` block creates them:

- `assignments`: the charter's scalar fields (title, goal, autonomy clause, budget JSON, deadline, cadence, `auto_complete`, model, effort, brain) and the state. `UNIQUE (user_id, slug)`.
- `assignment_items`: done-when items, `mechanical` (with a host-written validator spec) or `confirmed`.
- `assignment_frozen`: frozen commitments, `UNIQUE (assignment_id, name)`.
- `assignment_wakes`: one row per wake. `seq` counts per assignment and is assigned inside the INSERT, the same way `task_usage.attempt_seq` is. `task_id` may dangle after task retention; nothing joins on it after the wake has finished.
- `assignment_snapshots`: the scratchpad's text before and after each wake, plus the seed. They are in the DB rather than beside the file because a wake can write anything in its own directory, and the history is the audit trail.
- `assignment_inbox`: events waiting for the next wake. `trusted` defaults to 0; only host code and the user's own turns write a trusted event.
- `assignment_actions`, `assignment_attempts`, `assignment_events` (append-only audit).

Every timestamp in these tables is ISO-Z (`db.iso_utc_now()`), like `task_usage`. `tasks` uses the `datetime('now')` format, and `' '` sorts below `'T'`, so convert explicitly before comparing the two. The `ON DELETE CASCADE` clauses are documentation: istota never sets `PRAGMA foreign_keys=ON`.

## Columns on existing tables

- `tasks.assignment_id`: the assignment a wake belongs to, set only on a `source_type = 'assignment'` task. It goes through all six places a task column needs (`schema.sql`, `_add_columns`, `_TASK_COLUMNS`, `_row_to_task`, `Task`, `create_task`).
- `task_usage.assignment_id`: copied from the task by `executor.persist_brain_usage`, so an assignment's ledger survives the 7-day task retention. The ledger is `SUM(cost_usd) WHERE assignment_id = ?`, a column the model cannot write.
- `create_task(max_attempts=...)`: a wake is created with `max_attempts = 1`, since the assignment's next wake is its retry. `None` leaves the schema default.

## Rules the store holds

- **One writer of `assignments.state`.** `store.set_state` writes `state`, `state_reason` and `waiting_on` together, and only `lifecycle` calls it. `store.update_assignment` refuses `state` and every identity column with `ValueError`, so a stray state write fails loudly instead of skipping the transition checks.
- **Lookups by id can be scoped by owner.** `get_assignment(conn, id, user_id=...)` returns `None` for another user's assignment. Every CLI path must pass the user, so that another user's id reads as not found.
- **No policy in the store.** Which transitions are legal, who may change the charter and when a wake is due are decided in `lifecycle`, `caller` and `wakes`.

## The charter and its files

- **The rows are the charter; `CHARTER.md` is a view.** `charter.render` is the one renderer, used for the activation preview, `CHARTER.md` and the wake's system section, so what the user approves is the text they read. `charter_digest` is the sha256 of that render, which includes the state line, so compute it the same way at hold time and at approval. The host writes `CHARTER.md` (`rewrite_charter_file`) after every charter change and never reads it.
- **A date-only deadline is the end of that day in UTC** (`T23:59:59.999Z`), and renders back as the bare date. A time with no offset is read in the timezone the caller passes.
- **The directory is `{bot_dir}/assignments/<slug>/`**, made with `confined.open_or_create_dirs` from `Config.workspace_root(user_id)` and written by descriptor through `storage.write_regular_file`. A link at any component is refused. A failed write leaves the DB as it is and is logged.
- **Validators read files a wake wrote, in the daemon.** Every open goes through `confined` from the workspace root with no link followed, under a 1 MiB cap. `contains` is a literal substring, never a regex, because `re` has no timeout. `parse_spec` runs again on the stored spec before each check, so a row written some other way cannot widen what a check opens. Details are host text and never quote the file.

## Config

`[assignments]` (`config.AssignmentsConfig`): the lane's caps (`max_workers`, `user_max_workers`), the deployment ceilings every charter is held to (`max_active_per_user`, `max_cost_usd_per_assignment`, `max_wakes_per_assignment`), the wake floor (`min_wake_interval_minutes`), the pause thresholds (`stall_wakes`, `failure_wakes`, `budget_low_fraction`) and the prompt caps (`scratchpad_prompt_chars`, `history_prompt_chars`). Documented as a commented block in `config/config.example.toml`.
