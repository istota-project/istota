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
   prompt = "Send this exact message: @{user_id} Reminder: {message}"
   target = "talk"
   room = "{current_conversation_token}"
   ```
   - `name`: Use `reminder-` prefix + unix timestamp for uniqueness
   - `prompt`: MUST instruct the bot to start the response with `@{user_id}` (the Nextcloud username, e.g. `@alice`). This triggers a Nextcloud Talk mention notification so the user actually gets alerted. Without the `@` mention, the reminder fires silently
   - `room`: Use the conversation token from the current task context
   - For email delivery, use `target = "email"` instead
5. **Write the updated CRON.md.**
6. **Confirm to the user** what was set and when it will fire in human-readable form (e.g., "I'll remind you to call the dentist at 4:30 PM today").

## Critical rule

NEVER tell the user you set a reminder without actually writing to CRON.md. If you cannot determine the time, ask. If you don't have write access to the file, tell the user.

## Cleanup

When you are delivering a reminder (i.e., you were invoked by a scheduled job with a `reminder-` name), delete the spent `[[jobs]]` entry from CRON.md afterward. Read the file, remove the entry that fired, and write it back. This keeps the file clean.

Same for cancelled reminders — if a user asks to cancel a reminder, remove its entry from CRON.md.

## Listing reminders

To show pending reminders, read CRON.md and filter for entries whose name starts with `reminder-`. Show the reminder message and the scheduled time in human-readable form.

## Recurring reminders

For recurring requests like "remind me every day at 9am", use a standard recurring cron expression (e.g., `0 9 * * *`) instead of pinning to a specific date. Use a descriptive name like `reminder-daily-standup` instead of a timestamp.
