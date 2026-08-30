# Keeping the old developer workflow

The `developer` skill used to order one development routine on every deployment: a worktree per task, three change tiers, a full test suite at the end of the work, a review before landing, a merge request rather than a merge. ISSUE-337 turned those orders into defaults that yield to whatever the user has written in `USER.md`, or in a project room's `CHANNEL.md`.

Some of them changed as well as losing their mandate. The verification pass narrowed to the tests covering the change plus the repository's linters and type checker over the whole tree; no tier escalates to a full suite any more; and the report block gained a `Workflow:` line naming whose rules the task ran under. Everything else kept its old default.

Deployment mechanics did not move. The forge boundary and its refused verbs, the network allowlist, the ceiling on a single command, the credential rules, where builds and tests run, the pre-submission checks, and every delete path are still stated as facts, and nothing written in either file overrides them.

**If you want the shipped defaults, paste nothing.** They apply as written when neither file says anything about development workflow. This page is for the other case.

## Restoring the pre-337 routine

The usual home is the `developer` skill's per-user overlay, `config/skills/developer.md`, which is read only when that skill loads — exactly when a workflow decision applies, and at no cost on the tasks that will never write code. Write it as a file — paste the whole block in, prose and code blocks included — and then check it took:

```bash
istota-skill skills overlays    # the file should show binds: true
```

Or paste the block into `config/USER.md` to have it on every task, or into a project room's `CHANNEL.md` to scope it to that project. Where `USER.md` and a room's `CHANNEL.md` disagree, `CHANNEL.md` wins for a task from that room. The overlay's own label says it outranks the skill's defaults and claims nothing about the other two files, so write each decision in one place rather than in two expecting a defined winner.

Nothing else is needed. `USER.md` is in the system prompt of every task, and a room's `CHANNEL.md` is in the system prompt of every task from that room; neither is ever truncated. That difference in reach is the thing to weigh — a workflow written only in a room's `CHANNEL.md` reaches nothing from the 1:1, from a cron job, or from another room, and one written only in the overlay reaches nothing on a task where the `developer` skill did not load.

**That last point decides where the machine-specific rules go.** An admin task is handed the repositories tree whenever the developer feature is on, whether or not the `developer` skill was selected — so a task can reach a checkout and start a test run with no overlay loaded. A rule about what this host can afford ("the suite takes over an hour here, never run it in a foreground task") belongs in `USER.md` for that reason. The workflow decisions below are inert on such a task and belong in the overlay.

One warning before you paste it. The full-suite line is the rule ISSUE-337 was filed about: a suite that cannot finish inside a single command is killed partway and produces no coverage at all, which is worse than a narrow pass that completed. Keep it only if the suite finishes on the host the task runs on, and expect it to be run detached rather than under a timeout.

````markdown
## Development workflow

These rules outrank the `developer` skill's defaults.

- Worktree per task, always, however small the change.
- Change tiers as the skill describes them: Fast, Standard, Full, and Full for any boundary surface.
- When a test gets written: first for a reported bug, for tricky pure logic and for any Full-tier change; alongside the implementation everywhere else.
- When tests run, and which: the whole test suite once, at the end of the work, plus lint and typecheck. Run it detached if it will outlast a single command, and report the exit status the run wrote to disk.
- Commit granularity: coherent steps rather than one lump, and commit before the review reads the range.
- Whether a review runs: at Standard and Full tier, after the commits exist and before the branch is pushed.
- An MR or PR rather than a merge: push the branch and open one. Merge to the default branch only when I ask for it.
- Report shape: the block the skill gives, with the `Workflow:` line naming this file.
````

## Trimming it

Length here is not free, and the cost lands where you will not see it. `USER.md` itself is never truncated, but the memory block around it is capped: past `max_memory_chars` the assembler drops recalled memories first, then knowledge-graph facts, then dated memories, then playbooks. A long block is paid for out of what the bot remembers, on every task rather than only coding ones, and `memory/sleep_cycle.py` warns separately once the file passes about 8 KB.

So treat the block as a menu rather than a set: drop the lines you do not care about, and each decision you leave out keeps the skill's default. A user who only wants the full suite back writes one line.

The seeded `examples/WORKFLOW.md` in a user's bot folder is the same vocabulary written for the user rather than for this repository, and it names the same three homes. It is the file to point someone at who wants to write their own workflow instead of restoring this one.
