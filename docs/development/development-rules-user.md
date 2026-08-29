# Keeping the old developer workflow

The `developer` skill used to order one development routine on every deployment: a worktree per task, three change tiers, a full test suite at the end of the work, a review before landing, a merge request rather than a merge. ISSUE-337 turned those orders into defaults that yield to whatever the user has written in `USER.md`, or in a project room's `CHANNEL.md`.

Two of the defaults also changed. The verification pass is now the tests covering the change plus the repository's linters and type checker over the whole tree, and no tier escalates to a full suite any more. Everything else kept its old default and lost only its mandate.

Deployment mechanics did not move. The forge boundary and its refused verbs, the network allowlist, the ceiling on a single command, the credential rules, where builds and tests run, and every delete path are still stated as facts, and nothing written in either file overrides them.

**If you want the shipped defaults, paste nothing.** They apply as written when neither file says anything about development workflow. This page is for the other case.

## Restoring the pre-337 routine

Paste the block below into your `USER.md` — the file at `config/USER.md` inside your bot folder — to get the old routine back on every coding task. Paste it into a project room's `CHANNEL.md` instead to scope it to that project. Where both files carry a workflow and they disagree, `CHANNEL.md` wins for a task from that room.

Nothing else is needed. Both files are already in the system prompt at the start of every task, and neither is ever truncated.

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

`USER.md` is loaded into every task, not only coding ones, and `memory/sleep_cycle.py` warns once it passes about 8 KB. The block is a menu rather than a set: drop the lines you do not care about, and each decision you leave out keeps the skill's default. A user who only wants the full suite back writes one line.

The seeded `examples/WORKFLOW.md` in a user's bot folder is the same vocabulary written for the user rather than for this repository. It is the file to point someone at who wants to write their own workflow instead of restoring this one.
