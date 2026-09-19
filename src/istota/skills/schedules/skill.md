---
name: schedules
triggers: [schedule, scheduled, recurring, cron, every day, every week, every morning, every evening, periodic, nightly, daily, weekly]
description: Scheduled recurring job management
---
**Always use CRON.md for all scheduled jobs.** Never use session-scoped cron tools (like CronCreate) — they don't survive restarts. CRON.md is persistent and the only supported way to schedule recurring work.

CRON.md is for running tasks and commands on a schedule. For monitoring conditions and alerting on failures, use HEARTBEAT.md instead.

**A scheduled job is not a way to get an answer back.** Jobs fire on minute boundaries and deliver to a room, an inbox or shared content — never to the task that registered them. If you need a result *now*, use a skill CLI: `istota-skill` runs with credentials and network access the task itself does not have, and returns the value synchronously. Registering a `once = true` job and then waiting for it costs at least a minute of a held worker slot before the job can even start. When you genuinely have to check on a job's run afterwards, `istota-skill tasks recent --source-type scheduled --since 10m` and `istota-skill tasks status <id>` are the read surface (admin-only).

You can manage recurring scheduled jobs by editing the user's `{BOT_DIR}/config/CRON.md` file. The scheduler reads this file automatically — changes take effect within ~60 seconds.

**File location:** `$NEXTCLOUD_MOUNT_PATH/Users/$ISTOTA_USER_ID/{BOT_DIR}/config/CRON.md`

## Format

The file uses a TOML code block inside markdown:

```markdown
# Scheduled Jobs

\`\`\`toml
[[jobs]]
name = "daily-report"
cron = "0 9 * * *"
prompt = "Generate my daily report"
target = "talk"
room = "ROOM_TOKEN"

[[jobs]]
name = "weekly-cleanup"
cron = "0 18 * * 0"
prompt = "Review and clean up completed tasks"
target = "email"
silent_unless_action = true

[[jobs]]
name = "memory-stats"
cron = "0 6 * * *"
command = "istota-skill memory_search stats"
target = "talk"
room = "ROOM_TOKEN"
\`\`\`
```

## Fields

These are TOML basic strings: a backslash or a double quote inside one must be escaped (`\\` and `\"`). An unescaped one makes the whole block unreadable, and every job in the file stops running until it is repaired. Prefer `prompt_file` for anything holding a regex, a Windows path, or quoted text.

- `name`: Unique per user, short identifier (e.g., `daily-report`, `weekly-cleanup`)
- `cron`: Standard 5-field cron (minute hour day month weekday). Evaluated in the user's configured timezone
- `prompt`: The full prompt text that will be executed as a task (via Claude Code). Mutually exclusive with `command` and `prompt_file`
- `prompt_file`: Path to a file containing the prompt text (relative to your workspace root, `$NEXTCLOUD_MOUNT_PATH`, e.g. `/Users/alice/scripts/prompts/my-job.txt`). The file contents are read at load time and used as the prompt. Mutually exclusive with `prompt` and `command`. Useful for long prompts that would clutter CRON.md
- `command`: A shell command to run directly via subprocess (not Claude Code). Mutually exclusive with `prompt` and `prompt_file`. Each job must have exactly one of `prompt`, `prompt_file`, or `command`
- `target`: Where to deliver results. See "Delivering into a room" below — `"email"`, `"ntfy"`, a room descriptor, a comma-separated list of those, or omit for no delivery
- `room`: The conversation the job runs in. See "Delivering into a room" — it is a *room* token and not always a Talk one
- `enabled`: Set to `false` to pause the job (default: true). Use `!cron disable/enable` for runtime control
- `once`: When `true`, the job auto-deletes from both the DB and CRON.md after successful execution. Failed jobs are kept for retry. Used by the reminders skill for one-shot fire-and-forget entries
- `silent_unless_action`: When `true`, only posts output if response starts with `ACTION:`. Useful for monitoring jobs
- `skip_log_channel`: When `true`, this job's run is not mirrored to the verbose execution log channel. Useful for noisy, frequent jobs
- `model`: Per-job model override (canonical id, provider alias like `opus-high`, or a role alias like `fast`/`general`/`smart`). Empty = the instance default
- `effort`: Per-job effort override (`low`/`medium`/`high`/`xhigh`/`max`). Empty = the model default
- `brain`: Per-job brain kind (`claude_code`/`native`/`tmux_claude`). Empty = whatever the deployment routes this job to. **Admin-only**, and the operator has to have allowlisted the kind — for a non-admin the field is dropped on every sync and the job runs the configured brain, so do not write it unless the user is an admin and asked for it. A job that pins a brain gets no failover: if that brain is unavailable the run fails rather than being answered by another one
- `publish_shared_kv`: Publish this job's result text into shared curated content that every user's briefings can read (see "Publishing shared briefing content" below). **Admin-only.**
- `publish_shared_kv_trusted`: When `true`, the published content is marked trusted (rendered un-wrapped, not treated as untrusted web content). Only use for injection-safe content such as pure numeric tables — never for free-text/web-derived content. Default `false`

## Delivering into a room

A room is **one conversation bound to several surfaces**, not a Talk
conversation. A room created in web chat has a `web-…` token and may not be on
Talk at all. `target = "talk"` with such a token posts nowhere: the Talk API is
handed a token naming no conversation, the job reports success, and the user
sees nothing. That is the most common way a scheduled job silently fails.

**Get the descriptor from the room, don't guess it.** The prompt header names
the room this task is in, and `istota-skill rooms list` names every other one.
Both hand back a ready-made `target`:

| the room | `target` |
|---|---|
| created in Talk | `talk:<token>` |
| created in web chat | `web:<token>` |
| web chat, also open in Talk | `web:<token>,talk:<talk_token>` |

The third row is two legs on purpose. The web leg writes the room's own
transcript and pushes nothing to Talk, so naming only the web half leaves the
room's Talk members seeing nothing.

**Set `room` to the same canonical token.** `target` is where the result goes;
`room` is which conversation the job runs in, and it is what makes the result
render as a reply in the room rather than as a standalone system note. It also
gives the job the room's conversation history as context.

```toml
[[jobs]]
name = "weekly-digest"
cron = "0 9 * * 1"
prompt = "Summarise this week's activity"
target = "web:web-alice-3f21c4d90ab7"
room = "web-alice-3f21c4d90ab7"
```

Other values `target` accepts: `"email"`, `"ntfy"`, bare `"web"` (the user's
default `general` room — **not** the job's `room` field), bare `"talk"` (the
room named by `room`, or the user's resolved notification channel), `"none"`,
and a comma-separated list of any of these.

`room:<token>` also works, and means "this room, whichever surfaces it is on at
the time the job runs". The token must be the room's **canonical** one — the
`room` value in the table above, which is what `rooms list` and the prompt
header give you — and not a Talk conversation id copied out of a URL. Prefer the table above: it says which surfaces the job
was written for, so a room later unbound from one of them is legible rather
than silently narrowed. Reach for `room:<token>` when the room may gain a Talk
leg later and you want the job to pick it up without being edited.

**Never create a Talk conversation to post into a room.** A conversation made
with `istota-skill nextcloud talk create` is bound to nothing: it is not the
room, it will not carry the room's transcript, and nobody is watching it. If a
web-only room should also be on Talk, that is the "Also open in Talk" control in
the room's settings in web chat — tell the user where it is.

One caveat: a job with `silent_unless_action = true` delivers to Talk only. A
`web:` target on such a job posts nothing even when the result starts with
`ACTION:`.

## Publishing shared briefing content

Content that is identical for everyone — a world-news digest, a markets snapshot, a curated roundup — should be generated **once** and read by every user's briefing, instead of each user's briefing fetching and summarizing it separately. A scheduled job does the generation; `publish_shared_kv` writes its result where briefings pick it up.

Set `publish_shared_kv` to a shared-content key. A bare key (e.g. `world-headlines`) targets the `briefing_shared_blocks` namespace, so a briefing's **Shared block** source reading that name gets this job's output. Use `<namespace>/<key>` for any other namespace. On each successful run the job's result text is stored; an empty result is skipped (the previous value is kept). A user then adds a `shared_block` (or `kv`) source pointing at the same name to any briefing block.

This is the path for a **rich, agentic** shared block: unlike the built-in shared-block generator (which is tool-less), a scheduled `prompt` job runs with the full sandbox and tools, so it can browse, follow into individual articles, verify, and link them, then publish the digest for everyone.

Guardrails:
- **Admin-only.** Writing shared content is gated on the shared-KV-writer allowlist; a non-admin job that sets `publish_shared_kv` fails loudly (it does not silently no-op) and alerts the operator. Do not add it to a regular user's job. Whether *you* qualify is deployment-specific — run `istota-skill kv shared-status` to confirm `can_write_shared` before wiring the job, rather than assuming admin status implies it (a blank admins file authorizes nobody).
- Leave `publish_shared_kv_trusted` off for anything web- or text-derived (it may carry injected instructions). Set it `true` only for self-formatting, injection-safe data like a numeric quote table.

```markdown
\`\`\`toml
[[jobs]]
name = "world-news-digest"
cron = "40 5,17 * * *"                # ~before the 06:00 / 18:00 briefing windows
prompt = "Browse AP, Reuters and the Guardian world sections. Produce ~8 top world stories, leading with what's new; follow into the linked articles to confirm details and include a source link per story. Neutral wire-service tone. Output only the section body — no title/header line."
publish_shared_kv = "world-headlines"  # briefings reading this Shared block get the digest
\`\`\`
```

## Cron examples

- `0 9 * * *` — every day at 9:00 AM
- `0 9 * * 1-5` — weekdays at 9:00 AM
- `30 18 * * 0` — Sundays at 6:30 PM
- `0 */6 * * *` — every 6 hours
- `0 8 1 * *` — first of every month at 8:00 AM

## Operations

To add a job: append a new `[[jobs]]` entry to the TOML block in the file.
To remove a job: delete its `[[jobs]]` entry from the file.
To modify a job: edit the relevant fields in the file.
To temporarily disable: set `enabled = false` in the file, or use the `!cron disable <name>` command.

To deliver into the room this task is in, copy the `target` and `room` values the prompt header gives you. See "Delivering into a room" above — the conversation token alone is not a `target`, and `target = "talk"` with a web room's token posts nowhere.
