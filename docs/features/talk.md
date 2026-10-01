# Nextcloud Talk

Istota communicates through Nextcloud Talk using the regular user API (not the bot API). The bot authenticates as an ordinary Nextcloud user, polling conversations it's a member of.

## Polling

The talk poller runs in a background daemon thread and drives all its Nextcloud I/O onto the daemon's persistent asyncio runtime (one long-lived loop + one pooled httpx client, via `run_coro`). It long-polls each conversation the bot participates in. First poll initializes state; subsequent polls use `lookIntoFuture=1` for real-time message delivery. Talk is one surface behind the [Transport abstraction](../architecture/overview.md) — inbound it normalizes messages into `IncomingMessage`/`ingest_message`, outbound it delivers through `TalkTransport`.

Fast rooms (with new messages) are processed immediately without waiting for slow (quiet) rooms. The `talk_poll_wait` setting (default 2s) controls the maximum wait time before processing available results.

## Signaling instead of polling

Where the deployment runs Nextcloud's standalone signaling server (the high-performance backend), `[talk.signaling] enabled = true` takes inbound messages over a WebSocket instead. The poll loop is then not started at all — one driver, never two — and `room_sync_interval` (default 300s) is what bounds a gap if the event stream drops, by comparing each room's latest message id against the stored cursor and fetching only the rooms that are behind.

It is off by default, needs the `signaling` extra, and refuses to boot rather than falling back to the poller when the HPB is unregistered or the `websockets` library is missing. See [`[talk.signaling]`](../configuration/reference.md#talksignaling) for the fields and for why there is no credential to set.

## Multi-user rooms

A group conversation is a [shared room](shared-rooms.md). Every message in it is recorded, including those from guests, Nextcloud users who are not Istota users, and other bots, so the bot knows what was said when it is asked. By default it answers only when @mentioned; `[speech_gate] mode` changes that. Two-person rooms behave like DMs. Participant counts are cached (5 min TTL).

The bot's own @mention is stripped from the prompt in every conversation, a DM included: a DM that is only `@bot` is ignored, `@bot !help` runs the command, and `@bot yes` answers a waiting confirmation. Other mentions are resolved to `@DisplayName`.

A guest who mentions the bot is answered as the room's host, on the host's authority and with nothing private of the host's (see [hosts and guests](shared-rooms.md#hosts-and-guests)). Anyone in the room can switch the bot off with `!<name> off` ([switching the bot off](room-veto.md)).

Final responses in group chats use `reply_to` on the original message, and prepend `@{user_id}` only when a member asked on Talk: an answer to a guest or to a web turn is threaded without a mention. Intermediate messages (ack, progress) are sent without reply threading to avoid noise; for a guest's message the ack carries no progress and is removed when the turn is held or cancelled.

## Progress updates

While the brain works, the bot sends real-time updates to Talk showing what's happening. Progress is driven by the [task event stream](../architecture/scheduler.md#task-event-streaming): the `TalkEventSubscriber` consumer edits the initial ack message in place, showing the latest tool action and elapsed time. One message, updated as work progresses — no separate progress spam. `progress_show_tool_use` and `progress_show_text` gate which event kinds appear.

### Log channel

Per-user verbose logging of every tool action, with a `[task_id #channel]` prefix and status emoji, for full observability without cluttering the user's chat. This is driven by the `LogChannelSubscriber` and is no longer Talk-only — it routes to any user-routable surface via the `log` routing purpose (`routing["log"]` > the legacy `log_channel` Talk shorthand > off). Edit-capable surfaces (Talk) get the live in-place edited stream; non-edit surfaces (email, ntfy) get a single final-summary delivery. See [delivery routing](../configuration/per-user.md#delivery-routing).

## Message handling

- Messages split at 4000 chars
- File attachments downloaded to `/Users/{user_id}/inbox/`
- Audio attachments pre-transcribed before skill selection (so the transcript is available to selection and the model)
- Confirmation flow: regex-detected confirmation requests prompt user for yes/no reply
- Alerts channel (`alerts_channel` per-user config): dedicated Talk room for confirmations, email gate prompts, and security alerts. Falls back to briefing token, then auto-detected 1:1 DM with the bot
- `!trust`/`!untrust` commands for runtime management of trusted email senders
- Multi-line tool output is collapsed to the first line in progress updates
- The ack message id is carried on the task so the `TalkEventSubscriber` edits the same message as work progresses
- The Talk progress consumer and the log-channel consumer both subscribe to the one per-task event stream — independent consumers, not chained callbacks
- Background tasks (briefings, scheduled jobs) suppress error notifications to avoid noise -- failures are logged to the DB and log channel only

## Configuration

| Setting | Default | Section |
|---|---|---|
| `enabled` | `true` | `[talk]` |
| `bot_username` | `"istota"` | `[talk]` |
| `talk_poll_interval` | 10s | `[scheduler]` |
| `talk_poll_timeout` | 30s | `[scheduler]` |
| `talk_poll_wait` | 2.0s | `[scheduler]` |
| `talk_poll_full_sweep_interval` | 300s | `[scheduler]` |
| `progress_updates` | `true` | `[scheduler]` |
| `progress_show_tool_use` | `true` | `[scheduler]` |
| `progress_show_text` | `false` | `[scheduler]` |
| `talk_cache_max_per_conversation` | 200 | `[scheduler]` |
