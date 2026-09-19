---
name: reminders
triggers: [remind, reminder, reminders, remind me, alert me, notify me, don't forget, don't let me forget, in an hour, in 30 minutes, at 3pm, tomorrow morning]
description: Set and manage time-based reminders via CRON.md
---
Reminders are one-shot scheduled jobs in CRON.md. There is no separate reminder system — you MUST write an entry to CRON.md for the reminder to actually fire. Saying "I'll remind you" without writing the file does nothing.

## Setting a reminder

1. **Parse the time.** The user's timezone is in the prompt header. Convert relative times ("in 2 hours", "tomorrow at 9am") to an absolute datetime in that timezone.
2. **Build a cron expression.** Pin to the exact date and time: `{minute} {hour} {day} {month} *`. Examples:
   - "in 30 minutes" at 14:00 UTC → `30 14 17 2 *` (for Feb 17)
   - "tomorrow at 9am" → `0 9 18 2 *` (for Feb 18)
   - "next Friday at 3pm" → `0 15 21 2 *` (for Feb 21)
3. **Read the user's CRON.md** at `$NEXTCLOUD_MOUNT_PATH/Users/$ISTOTA_USER_ID/{BOT_DIR}/config/CRON.md`.
4. **Append a `[[jobs]]` entry** inside the TOML code block:
   ```toml
   [[jobs]]
   name = "reminder-{unix_timestamp}"
   cron = "{minute} {hour} {day} {month} *"
   prompt = "Reply with ONLY this text, nothing else:\n\n@{user_id} Reminder: {message}"
   target = "{room_target}"
   room = "{room_token}"
   once = true
   ```
   - `name`: Use `reminder-` prefix + unix timestamp for uniqueness
   - `prompt`: MUST use "Reply with ONLY this text, nothing else:" followed by the message starting with `@{user_id}`. The `@` mention notifies the user on the chat surface so they actually get alerted. Without the `@` mention, the reminder fires silently. Do NOT use phrasing like "Send this exact message" — it causes the bot to output reasoning before the message
   - `target` and `room`: **copy both from the prompt header's `Room:` line**, which gives the descriptor for the room this task is in. A room is one conversation bound to several surfaces, so the descriptor is `talk:<token>` for a Talk room, `web:<token>` for a web chat room, and `web:<token>,talk:<talk_token>` for a web room that is also open in Talk. `istota-skill rooms list` answers the same for any other room. **`target = "talk"` with a web room's token posts nowhere** — the Talk API is handed a token naming no conversation, the job reports success and the reminder never arrives. `room:<token>` also works and means "whichever surfaces this room is on when the job runs"; prefer the header's descriptor, which says which surfaces the reminder was written for
   - `once = true`: The job is automatically removed from DB and CRON.md after it fires successfully. No manual cleanup needed
   - For email delivery, use `target = "email"` instead
5. **Write the updated CRON.md.**
6. **Confirm to the user** what was set and when it will fire in human-readable form (e.g., "I'll remind you to call the dentist at 4:30 PM today").

## Critical rule

NEVER tell the user you set a reminder without actually writing to CRON.md. If you cannot determine the time, ask. If you don't have write access to the file, tell the user.

## Cleanup

One-time jobs (`once = true`) are automatically removed after successful execution — no manual cleanup needed.

If a user asks to cancel a reminder, remove its entry from CRON.md manually.

## Listing reminders

To show pending reminders, read CRON.md and filter for entries whose name starts with `reminder-`. Show the reminder message and the scheduled time in human-readable form.

## Recurring reminders

For recurring requests like "remind me every day at 9am", use a standard recurring cron expression (e.g., `0 9 * * *`) instead of pinning to a specific date. Use a descriptive name like `reminder-daily-standup` instead of a timestamp. Do NOT set `once = true` for recurring reminders.
