---
name: briefings_config
triggers: [briefing config, briefing schedule, briefing setup, configure briefing, set up briefing, change briefing, edit briefing, briefing time]
description: Read and change a user's briefing schedule (time, room, delivery surface)
cli: false
---
# Briefing schedule configuration

A briefing is a scheduled summary delivered to a Talk room, an email address or a push. This skill is about **when it runs and where it goes**. What goes *in* it is a separate thing — see below.

## Where the schedule lives

In the database, as `briefing_configs` rows. Two surfaces write it:

- The web UI, under Briefings → Settings.
- `istota briefing ensure`, which the operator runs.

Operator-set briefings can also come from `[[briefings]]` blocks in the instance `config.toml`. A database row of the same name replaces the TOML one, so a user's own change always wins over the operator default.

**There is no file to edit.** A user's workspace used to hold `{BOT_DIR}/config/BRIEFINGS.md` and that file is retired — nothing reads it. If you find one, it is inert; say so rather than editing it, and point at the settings page. Writing to it does nothing and reports success, which is the failure this retirement fixes.

## Reading the current schedule

The user's briefings are in the config you were given. Read them from there rather than looking for a file.

## Changing it

You cannot write `briefing_configs` yourself. When a user asks to move a briefing, change its room, or add or remove one:

1. Tell them what the current schedule is.
2. Point them at Briefings → Settings in the web UI, which is where they change it.

Do not offer to edit a file, and do not claim the change is made.

## Schedule format

Standard 5-field cron: `minute hour day-of-month month day-of-week`, evaluated in the user's configured timezone.

- `0 7 * * 1-5` — 7am weekdays
- `0 18 * * *` — 6pm every day
- `30 8 * * 1` — 8:30am Mondays
- `0 */6 * * *` — every 6 hours

## Delivery

`output` names where the briefing goes: `talk` (needs a `conversation_token`), `email`, `ntfy`, or a comma-separated list such as `talk,email` to deliver to several at once.

## Content is separate

What appears in a briefing is built from **content blocks**, edited in the briefings editor in the web UI — not from this schedule. An older format had per-briefing component toggles for markets, news, calendar and so on; those are gone. If a user asks for different content in a briefing, that is the block editor, not the schedule.
