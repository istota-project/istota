# Talk integration

Istota communicates through Nextcloud Talk using the regular user API (not the bot API). The bot runs as an ordinary Nextcloud user, polling conversations it's a member of.

## Polling

The talk poller runs in a background daemon thread with its own asyncio event loop. It long-polls each conversation the bot participates in. First poll initializes state; subsequent polls use `lookIntoFuture=1` for real-time message delivery.

Fast rooms (with new messages) are processed immediately without waiting for slow (quiet) rooms. The `talk_poll_wait` setting (default 2s) controls the maximum wait time before processing available results.

## Multi-user rooms

In rooms with 3+ participants, the bot only responds when @mentioned. Two-person rooms behave like DMs. Participant counts are cached (5 min TTL). The bot's own @mention is stripped from the prompt; other mentions are resolved to `@DisplayName`.

Final responses in group chats use `reply_to` on the original message and prepend `@{user_id}` for notification. Intermediate messages (ack, progress) are sent without reply threading to avoid noise.

## Progress updates

While Claude works, the bot sends real-time updates to Talk showing what's happening.

**Replace mode** (default): Edits the initial ack message in-place, showing the latest tool action + elapsed time. One message, updated as work progresses.

**Full mode**: Appends all tool actions as separate messages.

**None mode**: Silent, no progress updates.

Rate-limited: minimum `progress_min_interval` (8s) between updates, capped at `progress_max_messages` (5) per task.

### Log channel

Per-user verbose logging to a dedicated Talk conversation. When `log_channel` is set in per-user config, every tool action is posted to that room with a `[task_id #channel]` prefix and status emoji. This provides full observability without cluttering the user's chat.

## Message handling

- Messages split at 4000 chars
- File attachments downloaded to `/Users/{user_id}/inbox/`
- Audio attachments pre-transcribed before skill selection (so keyword matching works on voice memos)
- Confirmation flow: regex-detected confirmation requests prompt user for yes/no reply
- Multi-line tool output is collapsed to the first line in progress updates
- Progress callbacks carry the Talk message ID from the ack so subsequent updates edit the same message
- Log channel callbacks compose with progress callbacks -- both fire on each event
- Background tasks (briefings, scheduled jobs) suppress error notifications to avoid noise -- failures are logged to the DB and log channel only

## Configuration

| Setting | Default | Section |
|---|---|---|
| `enabled` | `true` | `[talk]` |
| `bot_username` | `"istota"` | `[talk]` |
| `talk_poll_interval` | 10s | `[scheduler]` |
| `talk_poll_timeout` | 30s | `[scheduler]` |
| `talk_poll_wait` | 2.0s | `[scheduler]` |
| `progress_style` | `"replace"` | `[scheduler]` |
| `progress_min_interval` | 8s | `[scheduler]` |
| `progress_max_messages` | 5 | `[scheduler]` |
| `talk_cache_max_per_conversation` | 200 | `[scheduler]` |
