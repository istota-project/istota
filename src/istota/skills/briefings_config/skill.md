---
name: briefings_config
triggers: [shared block, shared briefing block, agent block, briefing config, briefing schedule, briefing setup, configure briefing, set up briefing, change briefing, edit briefing, briefing time, remove briefing]
description: Read and change the user's briefing schedule (time, room, delivery surface), and read and re-prompt shared briefing blocks (admins)
cli: true
---
# Briefing schedule and shared blocks

A briefing is a scheduled summary delivered to a Talk room, an email address or a push. This skill is about **when it runs and where it goes**. What goes *in* it is the `briefings` skill (see "Content is separate" below).

Every command prints one JSON object. A refusal is `{"status": "error", "reason": "<code>", "error": "<text>"}` with exit 1; read `reason` to decide what to do, and tell the user the `error` text in your own words. The codes are listed at the end.

## Where the schedule lives

In the database, as `briefing_configs` rows, one per briefing the user owns. This skill reads and writes the user's own rows. The web UI (Briefings → Settings) writes the same rows, and the operator can write them with `istota briefing ensure`.

**There is no file to edit.** A user's workspace used to hold `{BOT_DIR}/config/BRIEFINGS.md`; that file is retired and nothing reads it. If you find one, it is inert: say so rather than editing it, and use the commands below.

## The schedule

```bash
istota-skill briefings_config schedule list
istota-skill briefings_config schedule set --name NAME [--cron CRON] [--title TITLE] [--output OUTPUT] [--conversation-token TOKEN] [--enabled | --disabled]
istota-skill briefings_config schedule remove --name NAME [--confirmed]
```

`schedule list` returns the user's timezone and each briefing with `name`, `title`, `cron`, `timezone`, `output`, `conversation_token`, `enabled`, `held` and `next_run`. Run it before you describe or change a schedule; do not answer from memory.

- `held` is `null`, or `{at, by_label}` when an administrator has paused the briefing. A held briefing does not run, whatever `enabled` says. Only an administrator can lift the hold; the user can still change and save the schedule meanwhile.
- `next_run` is the next time the cron fires, in the user's timezone. It is `null` when the briefing is disabled or held.

`schedule set` creates a briefing or changes one. Flags you leave out keep their stored value, so pass only what the user asked to change. A new name needs `--cron`. `--title ""` clears the title, and the name is shown in its place. The answer has `state` (`created`, `updated` or `noop`) and the briefing as `schedule list` shows it.

The output and the Talk room go through the same checks the settings page applies. A room the user is not in, a room shared with other people, or an unknown surface is refused with `reason: route_refused`; a Talk output with no room is `reason: invalid_definition`. Nothing is saved in either case. A room the briefing already used is not checked again, so the user can still reschedule or disable a briefing whose room has since become shared.

`schedule remove` deletes the schedule. Only the schedule goes: the briefing's content blocks and its archive stay. Without `--confirmed` it changes nothing and answers `reason: confirmation_required` with a `would` object (`name`, `cron`, `output`). Show the user that, ask, and run it again with `--confirmed` only after they agree. An unknown name is `reason: not_found`.

### Schedule format

Standard 5-field cron: `minute hour day-of-month month day-of-week`, evaluated in the user's configured timezone.

- `0 7 * * 1-5`: 7am weekdays
- `0 18 * * *`: 6pm every day
- `30 8 * * 1`: 8:30am Mondays
- `0 */6 * * *`: every 6 hours

### Delivery

`--output` names where the briefing goes: `talk` (needs `--conversation-token`, or a stored one), `email`, `ntfy`, or a comma-separated list such as `talk,email` to deliver to several at once. `talk:<token>` names the room inline.

## Content is separate

What appears in a briefing is built from **content blocks**, changed with the `briefings` skill or in the briefings editor in the web UI, not with this schedule. An older format had per-briefing component toggles for markets, news, calendar and so on; those are gone. If a user asks for different content in a briefing, that is the `briefings` skill.

## Shared briefing blocks

A shared block is one section generated once for every user, such as the world headlines, and read into personal briefings through a `shared_block` source. Its definition lives in the database. An `agent` block's directive is the whole task prompt the block runs.

Only an administrator can use these commands. Anyone else gets `reason: not_a_shared_content_writer`; say that plainly rather than retrying.

```bash
istota-skill briefings_config shared-block show <name>
istota-skill briefings_config shared-block set-prompt <name> --prompt-file <path>
```

`show` prints the definition (cron, timezone, render mode, sources, the whole directive) and the last run: `run.agent` for an agent block's latest task (`task_id`, `captured`, `outcome`, `answered_at`), and the last Run now request and answer. Read it before suggesting a change to a block's prompt, and quote the part you would change. An unknown name is `reason: not_found`.

`set-prompt` replaces the block's directive with the whole contents of a file and changes nothing else. To change a prompt from chat:

1. `show` the block and copy its `directive`.
2. Write the complete new prompt to a file in your temp directory (`$ISTOTA_DEFERRED_DIR`) or the user's workspace. The file replaces the directive, so it must hold the whole prompt, not only the changed part.
3. Show the user what changes, and run `set-prompt` once they agree.

The next run of the block uses the new prompt; nothing is regenerated straight away. An empty file is refused (`reason: empty_prompt`), a path outside your directories is `reason: host_path_refused`, and a prompt that would leave the block invalid is `reason: invalid_definition`. In each case the directive is unchanged.

## Reason codes

| reason | meaning |
|---|---|
| `not_found` | no briefing or shared block of that name for this user |
| `invalid_argument` | a flag is missing or malformed (a new briefing with no `--cron`) |
| `invalid_definition` | the saved result would not be valid; the message says why |
| `route_refused` | the output or Talk room names a destination the user may not deliver to |
| `confirmation_required` | a removal ran without `--confirmed`; nothing changed, `would` says what would go |
| `unavailable` | the deployment's configuration could not be loaded; try again later |
| `not_a_shared_content_writer` | a shared-block command from someone who is not an administrator |
| `empty_prompt` | `set-prompt` was given an empty file |
| `prompt_file_unreadable` | `set-prompt` could not read the file |
| `host_path_refused` | a file argument outside the directories this skill may read |
