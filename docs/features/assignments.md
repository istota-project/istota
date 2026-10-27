# Assignments

An assignment is a goal the bot keeps working on across days until it is met, abandoned or out of budget. It works in short runs called wakes, keeps notes in a scratchpad you can read, hands steps to you, and stops on its own. In chat you can call it a quest, an errand or a mission; commands, files and notices always say assignment.

A single task already has compaction, `!steer`, `!stop`, retries and a turn limit. An assignment adds what a single task cannot hold: state that lives outside any one context window, a lifetime of days with no worker held while it waits, a budget across all its runs, steps that belong to you rather than the bot, and progress judged by the host from evidence the model cannot write.

## Who can use it

Assignments are on by default and for admins only. A non-admin gets `not_admin` from every command. An admin who loses admin keeps any running assignment running, the same as a scheduled job, and can no longer change it.

## Starting one

You describe the goal in your private web or Talk chat, and the bot turns it into a charter with the `assignment` skill. The charter is:

- a title and a goal;
- what the bot may do without asking (the autonomy clause), stated as actions and recipients, such as "may email the installers on the approved list about quotes";
- done-when items, each either mechanical (a check the host runs on a file) or confirmed (you tick it);
- frozen commitments, values only you can change, such as a price ceiling;
- a budget in list-price dollars and a number of wakes, plus per-wake limits in minutes and turns;
- an optional deadline and a check-in interval (the cadence, 6 hours by default).

After each change the bot shows you the charter as rendered text. When you are happy, say so; the bot runs `activate`, and you are asked to approve that exact text. Approval is the one approval an assignment needs: spending starts there. Any change to the charter while the approval is waiting cancels it, so what you approve is always what you read.

After activation, charter changes you make in chat apply at once, and the next wake is told what changed. Raising the budget is a top-up.

Only your own turn in your private chat can change the charter, activate, pause, resume, wake, top up, abandon, close, leave a note or report a step done. Email, SMS, WhatsApp, scheduled jobs, shared rooms and the wakes themselves cannot.

### Mechanical checks

A mechanical item names a file in the assignment's folder and one of three checks:

| Check | Passes when |
|---|---|
| `file_parses` | The file is a regular file of at most 1 MiB and parses as CSV, JSON, TOML or non-empty UTF-8 markdown |
| `csv_columns` | The CSV has every named column, no blank value in them, and at least the minimum number of data rows |
| `contains` | The file contains a literal piece of text, or does not when the item says so |

The host runs every check after every wake. A link in the folder is not followed, so a check never reads outside it.

## What happens while it runs

The scheduler wakes an assignment when its next wake is due, when you leave it a note, report a step done or confirm an item, and once at the deadline. A completion it proposed holds the deadline wake back until you answer it: confirming closes the assignment, and rejecting it lets the deadline wake run, if the deadline has not had one. A wake is an ordinary task on its own worker lane: its prompt carries the charter, the done-when board, open hand-offs, what arrived since the last wake, the attempt log, the scratchpad and the last wakes' summaries. Its answer is not posted anywhere; it is kept as the wake's summary.

A wake ends by asking for the next one: in some hours, at a time, or not until you act. It can hand you a step ("photograph the furnace label", with what to report back and a due date), record what it tried and how it went, report a milestone, and propose completion. Two wakes are at least 15 minutes apart by default.

### The folder and the scratchpad

Each assignment has a folder at `<bot folder>/assignments/<slug>/` in your files. `CHARTER.md` there is generated from the record; editing it changes nothing. `SCRATCHPAD.md` holds the bot's notes, with sections for what it is doing now, what is done, its thinking, what it rejected, problems and findings.

To steer an assignment, leave it a note (`assignment note`), which the next wake reads as your words. Editing the scratchpad does not steer it. Any of your tasks can write that file, including one started by an email, a scheduled job or a turn in a shared room, so the bot cannot tell your change from theirs. The next wake is shown a change made outside a wake as untrusted material with its source unknown, never as your instruction, and a wake is free to remove those lines. A scratchpad that is missing, emptied or unreadable is restored from the last good copy. Every version is kept in the database, so a bad line can be traced to the wake that wrote it.

## Progress, pauses and completion

The host decides whether a wake made progress, from things the model cannot fake: a check that newly passes, a confirmed item, input from you, a step you reported done, an attempt with an outcome not seen before, or a deliverable that changed and passes its checks. Scratchpad edits alone never count.

The host pauses an assignment, and tells you why, when:

- the budget is spent, before a wake starts or after one ends;
- three wakes in a row made no progress (stalled);
- three wakes in a row failed;
- one step failed four times in a row (after three, the next wake is told to try another way or hand it to you);
- the deadline wake ended without completing it.

A pause you set yourself waits for you to resume it. A wake running when you pause or abandon finishes, and your state stands.

When a wake proposes completion, the host runs every check first. If any mechanical item fails, the proposal is refused and the next wake is told which items. Otherwise the assignment completes on its own if the charter allows that and nothing is left to confirm; if not, it waits for you to confirm it or say what is missing.

You can also finish it without a wake. Confirming the last item you were asked to confirm completes the assignment when every mechanical check passes at that moment, since your confirmation is the answer a proposal would ask for. `close --completed` does the same when nothing has been proposed, from a sleeping assignment or one waiting on you, and names the failing items if any check fails.

## Notices

What needs you reaches the [notifications](notifications.md) bell, under the push category "Assignments". Routine wakes post nothing.

| Notice | When | Clears when |
|---|---|---|
| A step for you | A wake hands you a step | You report it done |
| Paused | The host pauses it (budget, stalled, failures, a repeated failure, deadline) | It is resumed, topped up or abandoned |
| Says it is done | A wake proposes completion and it waits for you | You confirm it or say what is missing |
| Budget running low | 75% of the dollar or wake budget is spent | It is topped up or ends |
| Progress report | A wake reports a milestone | You see it |
| Completed | It completes | You see it |

Each notice in the bell starts with the assignment's number and slug (`Assignment 1 · heat-pump-quotes`), so you can name it in chat. It has two links: Open in chat goes to the room you activated the assignment from, or your default room when that one is gone or you have left it, and Open folder opens the assignment's folder on the Files page.

A notice's text may quote what a wake wrote, such as the step or the report, and that text stays in the bell. A push names the assignment and points to the bell. When you confirm completion yourself in chat, the completed notice goes to the bell without a push.

## Budget and cost

The dollar budget is counted from the usage records of the assignment's wakes, which the model cannot write. A wake whose cost could not be measured counts as zero; `show` says how many there were, and the wake cap still binds. Tokens are recorded but not limited. The budget can be overshot by at most one wake, since a running wake is not stopped for cost. To keep that rare, the host does not start a wake when less is left than the cheapest measured wake of this assignment has cost; the assignment pauses for budget instead, and a top-up must leave at least that much to resume it; `topup` says so when it does not. Before any wake has been measured there is no such floor.

Each wake's time limit is the charter's per-wake minutes, never past `task_timeout_minutes`. The per-wake turn limit applies on the native brain only.

## Commands

All verbs are `istota-skill assignment <verb>`, normally run by the bot for you. `<id>` is the number or the slug.

From your own turn: `create`, `set`, `item add|remove|confirm`, `frozen add|remove`, `activate`, `pause`, `resume`, `abandon`, `wake`, `topup --usd N | --wakes N`, `note`, `handoff done <id> <action-id> --report`, and `close --completed | --reject "what is missing"`. From any task of yours: `list` and `show`. From a wake, on its own assignment only: `next`, `handoff`, `report`, `attempt` and `complete`.

`show` and the replies of the other verbs include `wake_due`: why the scheduler will wake the assignment on its next pass (`inbox`, `time` or `deadline`), or empty when it will not. An assignment waiting on you that shows `inbox` has already been answered and is about to wake.

Errors are `{"status": "error", "error": "<code>", "message": "..."}`:

| Code | Meaning |
|---|---|
| `feature_disabled` | The `assignment` skill is disabled for this user or unavailable on this deployment |
| `not_admin` | The caller is not an admin |
| `not_found` | No such assignment, item, hand-off or frozen commitment for this user |
| `not_a_user_turn` | The verb needs your own turn in your private chat, or the task has scopes withheld |
| `not_a_wake` | A wake-only verb from something other than a running wake |
| `wrong_assignment` | A wake named another assignment |
| `bad_state` | Not allowed in the current state, which the error names |
| `not_met` | `close --completed` with nothing proposed, while a mechanical check fails; the message names the items |
| `items_changed` | The assignment or its done-when list changed while `close` was checking it; run it again |
| `over_ceiling` | A budget above the deployment's ceiling |
| `too_many_active` | The user is at the limit of running assignments |
| `invalid_charter` | A charter field is missing or not valid |
| `invalid_validator` | A mechanical check is not valid |
| `invalid_duration` | A duration is not `Nm`, `Nh` or `Nd` |
| `invalid_input` | Another argument is not valid |
| `deadline_passed` | The deadline has already passed |
| `confirmation_pending` | An approval for this assignment, or for this turn, is already waiting |
| `task_unavailable` | The calling task is not running |

An approval that cannot start the assignment closes with one of `charter_changed` (the charter changed since it was shown), `bad_state`, `deadline_passed`, `too_many_active`, `not_found` or `assignment_unavailable`, and the waiting task is cancelled.

## Configuration

Deployment limits, in `[assignments]`:

| Key | Default | What it sets |
|---|---|---|
| `max_workers` | 2 | Assignment wakes running at once across the instance |
| `user_max_workers` | 1 | Assignment wakes one user may run at once |
| `max_active_per_user` | 3 | Assignments past draft and not yet closed, per user |
| `max_cost_usd_per_assignment` | 50.0 | Ceiling on any charter's dollar budget |
| `max_wakes_per_assignment` | 500 | Ceiling on any charter's wake budget |
| `min_wake_interval_minutes` | 15 | Shortest gap between two wakes of one assignment |
| `stall_wakes` | 3 | Wakes in a row with no progress before a pause |
| `failure_wakes` | 3 | Failed wakes in a row before a pause |
| `budget_low_fraction` | 0.75 | Share of the budget spent that raises the low-budget notice |
| `scratchpad_prompt_chars` | 24000 | Scratchpad cap in a wake's prompt |
| `history_prompt_chars` | 12000 | Cap on earlier wake summaries in a wake's prompt |

## Limits

- One owner per assignment. No shared or group assignments.
- No assignment room, questions, watchers, detached jobs or child tasks yet; you steer with notes and the commands above.
- A wake uses the existing skills under their existing gates. Held outbound mail, relay approval and Wallet policy apply as they do to any task.
- A save to the scratchpad while a wake is running is attributed to the wake.
- Nothing deletes an assignment. A completed or abandoned one keeps its rows, files and ledger.

## Upgrading

The first start adds the assignment tables and two columns, and rebuilds the request ledger table to allow the activation request. The rebuild is a migration covered by the upgrade tier (`scripts/test-upgrade.sh`).
