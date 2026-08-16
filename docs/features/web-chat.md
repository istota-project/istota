# Web chat

An always-on, in-app chat surface in the web UI. It is a full-page console at `/chat` — the first nav tab, before Feeds — with Discord/Slack-style rooms in a sidebar. It complements Nextcloud Talk rather than replacing it: an in-app companion for talking to the bot without leaving the dashboard.

## Rooms

Each room is a persistent conversation backed by its own per-surface channel token, stored in the `web_chat_rooms` table. A room gets its own `CHANNEL.md` and its own channel sleep-cycle handling, exactly like a Talk channel.

- **Create / select** — rooms live in the sidebar; selecting one loads its history.
- **Per-room settings** — a kebab (⋮) on each room opens a settings modal that renames the room (the token stays the same), copies its token (to paste into a `web:<token>` output route), promotes a web-origin room to a real Nextcloud Talk conversation and binds the two, and hard-deletes the room behind a GitHub-style type-the-name confirm. A room with a task still running can't be deleted until it finishes.
- **Deep link** — `/chat?room=<token>` selects a room on load, silently falling back if the token is unknown or belongs to another user.

Deleting a room is a hard, token-scoped cascade across `task_events`, `tasks`, `web_chat_messages`, and `channel_sleep_cycle_state`, plus a best-effort removal of the `Channels/<token>/` workspace folder. (Channel `memory_chunks` are a documented residual.)

## Sending a message

**Enter** sends and **Shift+Enter** writes a newline; **Cmd/Ctrl+Enter** still sends, and so does the send button. On a phone or tablet the return key keeps inserting a newline — there is no cheap Shift there and the send button is already under your thumb. The key does nothing while a turn is running, while a voice message is recording, or while an attachment is still uploading, and never sends the Enter that commits an input-method candidate.

A sent message becomes a `source_type="web"` task with `output_target="web"`. It is an interactive task — it loads conversation context, the room's `CHANNEL.md`, and the `guidelines/web.md` channel guidelines. Because `web` is a stream surface, the result and progress are not pushed anywhere; they live in the `task_events` log, which the `/api/chat/tasks/{id}/stream` SSE endpoint tails.

The live view streams:

- **Tool use** — a single activity chip showing the active tool while it runs and a "✓ N tool calls" summary when done; expand it for the full list.
- **Real reasoning** — the model's thinking surfaces as its own activity-chip segment.
- **Answer text** — streamed token-by-token (both the native brain and, as of the latest release, the Claude Code brain via `--include-partial-messages`). Short lead-in narration ("Let me check…") is held back by the narration gate (`scheduler.stream_text_gate_chars`) so it can't leak into the answer area.

If the SSE stream falls back to polling, the client recovers without flashing an error; a terminally-failed task surfaces a terminal frame instead of hanging on "Working…".

## Message actions

Hovering a message (or tapping it, on a touch screen) reveals a row of actions under it: **copy**, **star**, **reply**, **delete**.

- **Copy** puts the whole message on the clipboard as its original markdown — headings, lists and fenced code come across ready to paste, and the activity chips and tool traces are left out. It is unavailable while a reply is still being written, since half an answer is not an answer.
- **Star** marks the message so it shows up in the Starred view. Unlike copy it works mid-reply, because it marks the message rather than its text. A starred message keeps its star visible without hovering.
- **Reply** answers one specific message rather than the room in general — see below. It is absent in the aggregate views (All / Unread), which have no composer for a staged reply to go to.
- **Delete** asks first, then removes the message for good — from the room, from the aggregate views, and from the conversation the bot remembers. In a room that is also open in Nextcloud Talk the confirmation says so, and the message is removed there too where the server allows it. A turn that is still running can't be deleted until it finishes.

Deletion is not private to you: rooms are shared, so a message you delete is gone for everyone in the room. Anyone with an open tab sees it disappear straight away.

## Replying to a message

**Reply** stages the message you picked as a chip above the composer, showing the first 200 characters of it. Send, and the turn is recorded as a reply: the transcript renders the quoted excerpt above your message, and the bot is given the parent's text as the thing you are responding to — so a bare "yes, do that" lands on the right referent. The quote is read from the stored message server-side; nothing the browser sends can put words in it.

- **The chip is part of the unsent message.** It rides in the draft, so leaving the room and coming back keeps it. Clearing the composer drops it, Escape dismisses it (after any open autocomplete menu has had the key first), and a `!command` clears it on send, since a command returns inline with no turn for a citation to attach to.
- **The rendered quote is a link back.** Clicking it jumps to the message being replied to, when that message is loaded in the current view.
- **A deleted parent still reads as a reply** — the quote renders as "Original message deleted" rather than the turn silently becoming an ordinary message.
- **Replying to a message that is already gone fails the send**, and your text and attachments go back into the composer with the dead citation dropped. This is the one failure that repopulates the box: retrying would only re-send the same missing parent, and a reply delivered without its referent is not the message you wrote.
- **Talk works both ways.** In a room bound to a Nextcloud Talk conversation, a web reply posts as a real Talk reply (falling back to a plain post if the parent never reached Talk), and a reply made in Talk shows up as a reply in web chat.

## Commands and model override

`!commands` and the `!model <alias> <prompt>` prefix work identically in web chat and in Nextcloud Talk — both route through `commands.dispatch(..., surface=...)`. On a stream surface like web the handler result is returned inline (`inline_result`) and rendered as a text card, rather than delivered as a separate push message. The per-user rate limit counts `source_type='web'` rows.

## Confirmations and attachments

- **Confirmations** — an action that needs approval parks correctly and renders a Confirm/Cancel card; staged side effects wait until you confirm.
- **Attachments** — drag, paste, or use the `+` button. In a browser that opens the file picker; in the iOS app it opens a menu offering your photo library, the camera, or a file. A message can only reference files you uploaded.
- **Voice messages** — the microphone button in the composer records, shows the elapsed time, and lets you discard or keep the take. The recording is attached like any other file and transcribed on arrival. It needs a secure connection to reach the microphone at all, so the button is simply absent over plain http, and transcription needs the optional speech extra.
- **Sending only a file** — a message with attachments and no text is accepted; the transcript shows what was sent in place of the missing text.
- **Chips** — each attachment appears as a chip that survives leaving and re-opening the room, and links to the file itself, served from inside your own session. A file the browser cannot serve you — one attached in Talk, or another member's upload in a shared room — stays a plain label rather than a link that would fail.

Size and type limits are the server's: the browser checks against the numbers the server publishes and refuses early with the real figure, and the server rejects anything that gets past it. See **Configuration** below.

## Held outbound mail

Mail the [outbound approval gate](email.md#the-outbound-approval-gate) holds appears as a card under the assistant turn that composed it, showing the recipients, the subject, the whole drafted body verbatim, and anything else that task did — so declining does not quietly leave a calendar event behind. Send, edit the body, or discard from the card; recipients and threading are not editable. A draft whose task has no room of its own (a cron job mailing an external address), or whose turn has scrolled out of the loaded history, falls to a list above the transcript, so nothing is reachable only from a room you never open.

A draft stuck in `sending` is shown with no action offered — nobody can tell from outside whether the message went out, and one of the actions would send it twice. Answering the same draft from Talk (`!drafts`) or another device removes the card here on the next stream frame.

## Turns from outside the room

An email from an external contact that lands in a thread you started is mirrored into the room as a turn, marked with its origin and sender so a bot answer never appears without the question above it. How much of the body shows inline is a per-user setting, `external_turn_display`:

| Value | Inline body |
|---|---|
| `full` | the whole message |
| `collapsed` (default) | a single clipped line, expandable in place |
| `hidden` | none; header, origin marker and sender still render |

Set it with `istota user ensure --external-turn-display <value>`. Every field on such a turn is attacker-supplied and renders as plain text, never markdown.

## Web chat as a delivery surface

`web` is also a *routable delivery surface* (`WebTransport`). Alerts, the verbose execution log, and any notification routed to `web` are appended to a room as unsolicited system messages — `role='system'` rows in the canonical `messages` store, distinct from task-backed turns — merged into room history by time and pushed to an open client by the room stream (below). Because it is user-routable, web appears automatically in every routing selector (default destination, alert route, briefing output) alongside Talk, email, and ntfy. Route to it with a bare `web` (the user's general room) or `web:<token>` for a specific room. See [per-user delivery routing](../configuration/per-user.md#delivery-routing).

## The live room stream

The per-task stream above only ever covers a task the client itself started. Everything else — a turn that arrived from Talk, a routed alert, an unread badge, a room renamed on another device — comes over a second SSE endpoint, `GET /istota/api/chat/stream`.

One connection per open tab carries **every room you are a member of**, so room switching is a client-side filter and background rooms get real content rather than a periodically refetched count. It tails the canonical `messages` store, cursored on `messages.id`: because a turn writes its rows whether or not anyone is watching, a Talk turn that starts and finishes in a fraction of a second is still delivered — timing stops mattering.

Frames are `message` (one history-shaped row plus its room), `gap` (the delta was too large to replay — reload instead), `room` (a rename / model / effort change, or a room appearing or disappearing), `message_deleted`, and a periodic keepalive comment.

Deletions need a cursor of their own. The stream is cursored on `messages.id`, and a deleted row is gone — it cannot carry a frame — so removals are recorded in a small ledger with its own monotonic id, and the `message_deleted` frame carries that cursor in its payload rather than as the SSE id. The client passes it back as `since_deletion_id`, on the polling fallback as well as the stream; without it, reconnecting after a delete would quietly bring the message back. Ledger rows are pruned after 30 days.

Recovery is split between the two ends, because neither can see the other's variable: the server decides on **cost** (a row cap and a byte budget — an assistant row carries its full tool trace, so row count alone measures the wrong thing), the client on **age** (past about a minute of silence it has probably missed state the stream does not carry, such as a star toggled elsewhere). Both converge on the same routine — reload the room list and the open room, then adopt the server's cursor.

If SSE is unavailable (a buffering proxy, say), the client falls back to polling `GET /istota/api/chat/events`, the same snapshot-endpoint pattern the task stream uses, and periodically re-probes the stream. The admin dashboard reports the number of live room-stream connections.

## Configuration

The surface is always enabled when the web UI is on. Tune limits and streaming cadence under `[web.chat]`:

`max_attachment_mb` has one non-obvious property: on an Ansible deployment it is **also** where nginx's `client_max_body_size` comes from, and the role ships 100 rather than the application default of 25. Set them apart and nginx refuses the upload with its own HTML error page, which the browser client cannot parse into a message. Docker has no such variable — nginx is given a generous ceiling there and the application's own 25 MB is the binding limit, so raise it in `config.toml` by hand.

```toml
[web.chat]
max_prompt_chars = 32000
max_attachment_mb = 25          # application default; the Ansible role sets 100
# attachment_extensions defaults to images (incl. heic), documents, text and audio
rate_limit_messages = 30
rate_limit_window_seconds = 300
sse_poll_interval_ms = 200
client_poll_interval_ms = 1500
# Live room stream
room_stream_poll_interval_ms = 1000
room_stream_keepalive_seconds = 20
room_stream_max_batch = 500
room_stream_max_bytes = 2000000
room_stream_room_check_seconds = 10
```

See the [configuration reference](../configuration/reference.md#webchat) for the full table.

## Related

- [Web interface](web-interface.md) — auth, pages, deployment.
- [Talk](talk.md) — the other interactive messaging surface.
- Transport abstraction — `.claude/rules/transport.md` (`WebTransport`, the stream surface class, delivery routing).
