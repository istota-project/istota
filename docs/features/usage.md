# Token usage and cost

Every model call the daemon makes writes a usage record: token counts, cost where cost is a real number, and enough identity to answer "who spent this, on what, through which brain". The record is operator-facing. It is read from the shell with `istota usage` and from the admin dashboard; no user-facing surface shows anyone's spend.

## What is recorded

One row per brain *attempt*, in the framework DB's `task_usage` table, with a `task_usage_models` child row per model where the brain reports a per-model split.

An attempt is not a task. A task that was retried, or that failed over from the primary brain to the fallback, writes a row per attempt, each carrying its own `attempt_seq` and an `is_fallback` flag. Summing them gives what the task really cost rather than what its last attempt cost.

| Column group | Contents |
|---|---|
| Identity | `user_id`, `task_id` (nullable), `attempt_seq`, `origin`, `source_type`, `brain_kind`, `is_fallback` |
| Model | `model` (the largest cost share), `effort`, `stop_reason`, `success` |
| Tokens | `billed_input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, guarded by `has_totals` |
| Cost | `cost_usd`, `cost_basis` |
| Shape | `turns`, `model_requests`, `subagent_requests`, `compacted_requests` |
| Context | `initial_context_tokens`, `peak_context_tokens`, `context_window` — NULL, never 0, when unmeasured |
| Timing | `duration_ms`, `duration_api_ms`, `service_tier`, `session_id` |

`has_totals` matters. A run killed before its result frame arrives has real context columns and meaningless zeroes in the token columns, so every token aggregate filters on it. The rows that report nothing are counted and named rather than averaged in as zero — `istota usage` prints how many tasks in the window recorded no usage, and the dashboard shows the same count.

The context columns are NULL rather than 0 when unmeasured, because SQL `AVG` skips NULL and a zero would halve a mixed-brain mean. The native brain does not track per-request prompt sizes, so its rows are unmeasured here by design.

## Where a call came from

`origin` names the thing that made the call. Most calls are `task` — work someone asked for, with a `task_id` behind it. The rest have no task at all and were invisible before this table existed:

| Origin | Call |
|---|---|
| `task` | A user's task |
| `sleep_cycle` | The nightly memory pass |
| `shared_blocks` | Generating a module-owned shared briefing block |
| `code_review` | The `code_review` skill's reviewers |
| `health_ocr` | Reading an uploaded health document |
| `health_encounter_ocr` | Reading an uploaded encounter document |
| `health_immunization_ocr` | Reading an uploaded immunization record |
| `health_explainer` | Explaining a biomarker or an immunization |

Non-task rows carry `user_id = "__system__"` where the work belongs to no one user, and an empty `source_type`. They are a real share of a month's spend, so a surprising month can be traced to the thing that caused it rather than assumed to be user traffic.

## Cost basis

A currency figure is shown only where it is real money. `cost_basis` records which kind of number `cost_usd` holds:

| Basis | Meaning |
|---|---|
| `api` | The provider reported a charged cost. Real money. |
| `subscription` | What the same usage would have cost at list price. Not money. |
| `estimated` | Derived from the model catalog's per-mtok prices. The catalog prices an unknown model at zero, so a 0.0 here is not a claim of zero spend. |
| `unknown` | No basis could be determined. |

Both the CLI and the dashboard render anything other than `api` as a dash with the basis named beside it, and never sum across bases. A group holding rows of two kinds shows the real figure plus the names of the others, not one added-up number. Usage in those groups is read through the token counts instead.

Sub-cent figures render at four decimals. A 24-hour per-user cost is routinely below a cent, and `$0.00` cannot be told apart from a genuine zero.

## Reading it back

```bash
istota usage                                  # last 30 days, one total
istota usage --days 7 --by day                # grouped by day
istota usage --by user                        # per user
istota usage --by model                       # per model, from the per-model split
istota usage --by origin                      # task vs the daemon's own calls
istota usage --by brain                       # per brain kind
istota usage --by source                      # per task source type
istota usage --since 2026-08-01 --until 2026-08-14
istota usage -u alice --brain native --json
```

`--until` is inclusive: the given date is expanded to the following midnight. `--user`, `--brain`, `--source`, `--model` and `--origin` are filters and combine with any grouping. `--json` emits the same figures for a script.

The command is operator-facing and runs from the operator's shell, so `--user` is a convenience filter rather than a boundary.

The admin dashboard carries the same figures. A **Token usage** card shows 24-hour and 30-day totals, cache hit rate and the two context averages, then per-model, per-brain and per-origin breakdowns. Per-user tokens and cost sit on the Users rows beside that user's task counts, rather than being repeated in the usage card where the two copies could disagree.

## Retention

Usage rows are pruned at `scheduler.usage_retention_days`, default **180**. That is deliberately far above `task_retention_days` (7): the point of a separate table is that spend outlives the task it came from. `task_id` dangles once the task is deleted, and the denormalized identity columns keep every row self-sufficient, so no aggregate ever joins `tasks`. `tasks.id` is `AUTOINCREMENT`, so a dangling id can never be reassigned to a different task.

Six months rather than a year because `db_backup` snapshots the framework DB into dated directories and keeps several of them, so every row is already duplicated on the backup target.

## Related

- [Configuration reference](../configuration/reference.md#scheduler) — `usage_retention_days`
- [Database](../architecture/database.md#usage) — the table definitions
- [Native brain](../configuration/native-brain.md) — how each brain reports its figures
- [CLI reference](../reference/cli.md#usage-and-cost)
