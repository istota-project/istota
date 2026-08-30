# Notifications and the inbox

A bell in the app bar shows what is waiting on you, from every page rather than only from the chat.

Two things could be held for your answer — an email from an unknown sender, and a reply the bot drafted but has not sent — and both used to be visible only if you happened to open the chat. A message held while you were on the money pages sat there until you did. The bell replaces that with a durable list: every item that needs you gets a row, the row survives a failed push, and answering anywhere closes it everywhere.

## Two things with similar names

This page is about the **inbox**: `notification_store.py`, the `notifications` table, and the bell. It is not `notifications.py`, which is **delivery** — the dispatcher that sends a message out over Talk, email or ntfy.

Raising a notification does both, and they are separate: a row is written to the table, and separately the text fans out through the delivery layer. A user with no alerts channel loses nothing, because the bell is always there. A push that failed to send leaves the row untouched.

## The bell and the panel

The badge is the count of **open rows** — one number, no dot, no second concept. Every open row means the same thing: something is waiting on you, whether it is waiting to be done or waiting to be looked at. Having seen an item is not the same as having dealt with it, so a held task you have looked at and not answered still counts.

Clicking the bell opens a panel with two tabs:

- **All** — everything open.
- **Needs action** — only the items with a button to press.

The panel opens on **All** every time, and the filter deliberately does not survive a close. Some items are informational and carry no action; under a sticky "Needs action" tab those would never render, never be seen, never resolve, and the badge would climb forever for anyone who lived in that filter.

Tab counts come from the list itself rather than from the badge, so "Needs action (3)" can never sit above a visibly shorter list. The footer reads "Showing N of M open" on both tabs — on "Needs action" it will say something like "Showing 4 of 60 open", which is what that tab means.

A row shows a severity dot, the title, how long ago it last changed, and `×N` when the same thing has happened more than once. Opening a row gives the full text, any status note, and every action. Unseen rows differ by weight only, so they read as *new* rather than *important* — severity is the dot's job.

**Dismiss appears only on a row with no actions.** Where there are actions, answering is how the item closes, and offering Dismiss beside Confirm and Discard would ask you to tell apart two pairs of buttons that look alike and are not.

## What raises a row

Six sources ship.

| Source | What it is | What you can do | Closes when |
|---|---|---|---|
| `confirmation` | A task parked waiting for your approval — a gated email from an unknown sender, or a question the model asked mid-run | Confirm, Discard | The task leaves `pending_confirmation` |
| `outbound_draft` | A reply the bot composed and held at the delivery gate | Send, Discard | The draft is sent or discarded |
| `cron_job` | A scheduled job the scheduler switched off after five consecutive failures | Nothing in-app; the status note names `!cron enable <name>` | The job's failure counter returns to zero |
| `connected_service` | A stored credential the remote rejected (Garmin today) | Reconnect, which links to Settings | The service reports connected again |
| `health_panel` | A bloodwork panel left in draft after OCR | Review, which links to the bloodwork page | The panel leaves draft |
| `task_alert` | One-shot alerts: a task that failed, a mail throttle notice, a confirmation that timed out, a DMARC warning, a result that reached nobody | Nothing; it clears itself once you have seen it | You open the panel with it visible |

The first five are **object-backed**: something outside the table changes and the row closes. `task_alert` is **fire-and-forget** — nothing will ever close it, so it closes when you see it.

Three of these are worth spelling out because the obvious reading is wrong.

**A disabled cron job is tracked by its failure counter, not by whether it is enabled.** `CRON.md` is authoritative for the enabled flag and the scheduler re-syncs it every tick, so a job auto-disabled after five failures is switched back on within a tick. A row watching the flag would go stale minutes after it was raised, leaving you a push about something the panel then denied all knowledge of. Every path that really ends the condition — a successful run, `!cron enable`, the module-job rescue — zeroes the counter instead.

**A module job (`_module.*`) raises nothing here at all.** Those are re-enabled hourly whether or not anything was fixed, so a row would be raised, marked stale by the rescue, and reopened an hour later — and a reopen delivers. That turns a permanently broken module job into an hourly push with no command to run against it. A module job failing for a reason you can act on has a source of its own: a dead Garmin credential raises `connected_service`.

**A `task_alert` the model wrote itself comes in three grades, and only two of them interrupt you.** `security` and `action_needed` are written to the panel *and* pushed. `note` is written and never pushed: info severity, no action chip, closed on the first render. It is also the fallback, so the two loud grades have to be named — an unrecognised type, or an alert with no `type` key at all, is a `note`. That direction is deliberate. It used to collapse onto `security`, and the example in the bot's own email guideline wrote the file with no `type`, so the shape a model copies landed on the loudest grade in the system: an ordinary polite refusal to a stranger arrived as "Action needed", and their follow-up as "Security alert" at danger severity. A grade that interrupts somebody was reachable by saying nothing.

## How an item closes

Five ways, and the first is the one that normally happens:

1. **The producer closes it** when it closes the object. Approving a confirmation, sending a draft, reconnecting a service, reviewing a panel.
2. **The liveness backstop.** Every time the panel opens, each open row is re-checked against the thing it is about. An item answered over Talk, by email, or by timeout is found already handled and marked stale. This is why a question answered on another surface can never sit in the panel looking unanswered.
3. **Seeing it**, for fire-and-forget rows.
4. **Dismiss**, on a row with no actions.
5. **Age**, for fire-and-forget rows nobody ever rendered — see retention below.

The backstop is a backstop. Every producer closes its own rows; the re-check exists so that nothing depends on a producer having remembered to.

## Repeats collapse

The same thing happening again bumps one row rather than adding another. A nightly job failing four nights running is one entry marked four times, not four entries. Fifty forged messages in one burst leave a single entry, counted fifty times, naming the senders it has room for.

This is the deduplication a chat channel structurally cannot do — in Talk, four failures are four messages.

A **repeat does not re-notify**; a **reopen does**. If you dismissed an item and the same thing happens again, that is a second thing to hear about, so the row reopens and pushes. Dismissing means "not now", not "never again".

## Retention

Two windows, neither configurable:

- **Fire-and-forget rows nobody read are closed after 14 days**, so the count cannot climb forever.
- **Closed rows are deleted after 30 days.**

**Open object-backed rows are never deleted at any age.** They are held items, and the thing holding them is still there. A held draft still waiting after a year is still waiting.

## Delivery, and the three postures

The inbox does not change how anything is routed. A row is written; separately, the text goes out through the same Talk / email / ntfy dispatcher as before. Which destinations that reaches is unchanged — see [per-user configuration](../configuration/per-user.md).

What varies is whether a producer sends at all, and that is decided per producer rather than per source:

- **Write and deliver** — most sources. A dead Garmin credential delivers because the wiped credential takes the sync job with it, so nothing would ever notice again.
- **Write, never deliver** — a draft bloodwork panel. The producer is the upload handler and you are looking at the review screen it just returned you to, so pushing "lab results are waiting" at that moment is a notice about something you are in the middle of doing.
- **The producer keeps its own send** — the DMARC canary, the mail throttle, the expired-confirmation notice. Each has its own delivery window already and stamps the row rather than sending twice.

## Configuration

**There are none.** No config fields, no environment variables, no CLI verbs. The retention windows above, the 500-row scan ceiling and the panel's 50-row page are constants in the code.

The bell polls every 30 seconds from whatever page you are on, backing off after repeated failures. On a page that already holds a room's event stream open, the count also rides that stream, so an item parked while you are reading a room lights the bell in about a second rather than up to thirty. That fast path is an optimization, not the contract: turning the room stream's tick off (`room_stream_room_check_seconds = 0`) leaves the 30-second poll doing the job.

## Safety notes

Titles and bodies carry text somebody else wrote — an email subject, a sender's name, a value parsed out of a mail header. Two rules follow, and both are enforced rather than assumed:

- **Every field renders as text, never as markup.** There is no HTML injection point anywhere in the feature. Formatting characters are stripped from titles; bodies keep their line breaks and every character that cannot make a link, a code span, raw HTML or a table, because deleting more corrupts what the text says.
- **Every link and every action target is checked against an allowlist** — a relative in-app path and nothing else — at the moment it is rendered, on both the server and the client. A resolver that builds one path wrongly has its whole view downgraded rather than just the bad field.

Keys are bounded on every axis they are built from, so a value an attacker chooses cannot mint one durable row per value, each firing a push.

## Upgrading

Items already held when you upgrade are carried over. The first run seeds the inbox from the two queues that already existed — tasks parked for confirmation and drafts held at the gate — so nothing waiting is lost. That seeding writes rows and delivers nothing, so an upgrade does not fan your whole backlog out to Talk at once.

One scope note: a draft that was already mid-send at upgrade time is not seeded, and stays invisible to the bell.

## Related

- [Web interface](web-interface.md) — the app bar, the pages, the API routes
- [Web chat](web-chat.md) — the in-transcript confirmation and draft cards, which are unchanged
- [Email](email.md) — the inbound confirmation gate and the outbound approval gate, the two biggest producers
- [Scheduling](scheduling.md) — cron jobs and the five-failure auto-disable
- [Database](../architecture/database.md#notifications) — the table
