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

The modules for the scratchpad, budget, lifecycle, wakes, prompt and caller, the `assignment` skill and the bell resolver are added by later stages of the same work, each named here as it lands.

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
