# Web chat

An always-on, in-app chat surface in the web UI. It is a full-page console at `/chat` — the first nav tab, before Feeds — with Discord/Slack-style rooms in a sidebar. It complements Nextcloud Talk rather than replacing it: an in-app companion for talking to the bot without leaving the dashboard.

## Rooms

Each room is a persistent conversation backed by its own per-surface channel token, stored in the `web_chat_rooms` table. A room gets its own `CHANNEL.md` and its own channel sleep-cycle handling, exactly like a Talk channel.

- **Create / select** — rooms live in the sidebar; selecting one loads its history.
- **Per-room settings** — a kebab (⋮) on each room opens a settings modal that renames the room (the token stays the same), copies its token (to paste into a `web:<token>` output route), and hard-deletes the room behind a GitHub-style type-the-name confirm. A room with a task still running can't be deleted until it finishes.
- **Deep link** — `/chat?room=<token>` selects a room on load, silently falling back if the token is unknown or belongs to another user.

Deleting a room is a hard, token-scoped cascade across `task_events`, `tasks`, `web_chat_messages`, and `channel_sleep_cycle_state`, plus a best-effort removal of the `Channels/<token>/` workspace folder. (Channel `memory_chunks` are a documented residual.)

## Sending a message

A sent message becomes a `source_type="web"` task with `output_target="web"`. It is an interactive task — it loads conversation context, the room's `CHANNEL.md`, and the `guidelines/web.md` channel guidelines. Because `web` is a stream surface, the result and progress are not pushed anywhere; they live in the `task_events` log, which the `/api/chat/tasks/{id}/stream` SSE endpoint tails.

The live view streams:

- **Tool use** — a single activity chip showing the active tool while it runs and a "✓ N tool calls" summary when done; expand it for the full list.
- **Real reasoning** — the model's thinking surfaces as its own activity-chip segment.
- **Answer text** — streamed token-by-token (both the native brain and, as of the latest release, the Claude Code brain via `--include-partial-messages`). Short lead-in narration ("Let me check…") is held back by the narration gate (`scheduler.stream_text_gate_chars`) so it can't leak into the answer area.

If the SSE stream falls back to polling, the client recovers without flashing an error; a terminally-failed task surfaces a terminal frame instead of hanging on "Working…".

## Commands and model override

`!commands` and the `!model <alias> <prompt>` prefix work identically in web chat and in Nextcloud Talk — both route through `commands.dispatch(..., surface=...)`. On a stream surface like web the handler result is returned inline (`inline_result`) and rendered as a text card, rather than delivered as a separate push message. The per-user rate limit counts `source_type='web'` rows.

## Confirmations and attachments

- **Confirmations** — an action that needs approval parks correctly and renders a Confirm/Cancel card; staged side effects wait until you confirm.
- **Attachments** — drag-drop or paste files; a message can only reference files the user uploaded.

## Web chat as a delivery surface

`web` is also a *routable delivery surface* (`WebTransport`). Alerts, the verbose execution log, and any notification routed to `web` are appended to a room as unsolicited system messages in the `web_chat_messages` table (distinct from task-backed turns), merged into room history by time and surfaced live in an open room by an idle poll. Because it is user-routable, web appears automatically in every routing selector (default destination, alert route, briefing output) alongside Talk, email, and ntfy. Route to it with a bare `web` (the user's general room) or `web:<token>` for a specific room. See [per-user delivery routing](../configuration/per-user.md#delivery-routing).

## Configuration

The surface is always enabled when the web UI is on. Tune limits and streaming cadence under `[web.chat]`:

```toml
[web.chat]
max_prompt_chars = 32000
max_attachment_mb = 25
rate_limit_messages = 30
rate_limit_window_seconds = 300
sse_poll_interval_ms = 200
client_poll_interval_ms = 1500
```

See the [configuration reference](../configuration/reference.md#webchat) for the full table.

## Related

- [Web interface](web-interface.md) — auth, pages, deployment.
- [Talk](talk.md) — the other interactive messaging surface.
- Transport abstraction — `.claude/rules/transport.md` (`WebTransport`, the stream surface class, delivery routing).
