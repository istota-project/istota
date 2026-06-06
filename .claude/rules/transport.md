# Transport abstraction (`src/istota/transport/`)

A uniform seam over Istota's messaging surfaces. Inbound, a `Transport`
normalizes a surface's messages into `IncomingMessage`; `ingest_message` turns
those into tasks. Outbound, `deliver` / `edit` push a task's result to a
resolved channel. `TransportRegistry` holds the enabled transports and resolves
one per task.

Two concrete transports ship: `TalkTransport` and `EmailTransport`. **Matrix
and web chat are the designed-for next consumers** — adding one is a new
`Transport` subclass plus a line in `make_registry`, not a patch across the
scheduler, the consumers, and the notification dispatcher.

This is a pure-refactor seam: no DB migration, no config change. `conversation_token`
keeps its name and stays opaque at every consumer (it is the per-surface
channel id); `source_type` stays the routing key. Neither was renamed.

## Layout
```
transport/
├── __init__.py   # re-exports + the public surface
├── _types.py     # IncomingMessage, TransportCapabilities, Transport protocol
├── registry.py   # TransportRegistry, make_registry, _surface_for_source_type
├── ingest.py     # ingest_message(conn, config, msg) -> int
├── talk.py       # TalkTransport
└── email.py      # EmailTransport
```

## Core types (`_types.py`)

- **`IncomingMessage`** — a surface-normalized inbound message. Field→column
  contract that `ingest_message` relies on: `channel_token` →
  `Task.conversation_token`, `delivery_token` → `Task.talk_delivery_token`,
  `platform_message_id` → `Task.talk_message_id`, `reply_to_message_id` →
  `Task.reply_to_talk_id`; plus `user_id`, `text`, `source_type`, `surface`,
  `attachments`, `is_group_chat`, `output_target`, `model`/`effort`, `raw`.
- **`TransportCapabilities`** (frozen) — `supports_edit`, `supports_threading`,
  `supports_progress_ack`, `supports_typing`, `max_message_length`. Drives
  capability-gated wiring in the scheduler instead of `source_type ==` checks.
- **`Transport`** (`@runtime_checkable` Protocol) — `name`, `capabilities`, and
  `async poll() -> list[IncomingMessage]`, `async deliver(target, text, *, task,
  reply_to, reference_id, threaded) -> int | None`, `async edit(target,
  message_id, text)`, `async download_attachment(remote_ref, local_path)`,
  `resolve_target(task) -> str | None`.

`deliver` is **task-aware**: the optional `task` kwarg is ignored by surfaces
that don't need it; Talk uses it for group-chat reply-threading + @mention,
email for the deferred-output / `ProcessedEmail` lookup. (The "task-aware
deliver" decision — keeps the common `(target, text)` case clean without
amputating email's needs.)

## Registry (`registry.py`)

`make_registry(config)` does **no I/O on construction** (`TalkClient.__init__`
only stores credentials), so callers without a registry in scope — notably
`notifications.send_notification`, called from heartbeat / scheduled jobs — can
build one on demand. Only enabled surfaces are registered (`talk.enabled`,
`email.enabled`).

`_surface_for_source_type`: `email` → `"email"`; everything else (talk,
briefing, scheduled, subtask, heartbeat, cli, istota_file, unknown) → `"talk"`,
the existing default. `registry.for_task(task)` uses it to resolve the primary
delivery transport.

ntfy and `istota_file` are **not** transports — ntfy is one-way push, istota_file
writes a file. They stay as fan-out side channels (`notifications` / the file
handler). A task with `output_target="all"` posts to Talk + email via their
transports and pushes ntfy via `notifications`.

## Inbound

- **Talk** splits cleanly: `talk_poller.collect_talk_messages(config) ->
  list[IncomingMessage]` owns every Talk-specific step (conversation listing +
  cache, per-room long-poll, system/own/unknown/unmentioned filtering, `!model`
  prefix, `!command` dispatch, confirmation-reply handling, the per-channel
  active-task gate, attachment extraction, cancelling superseded confirmations)
  and performs their DB side effects inline — it only defers the `create_task`
  step. `TalkTransport.poll` delegates to it. `poll_talk_conversations` is a
  shim (collect + `ingest_message`) kept for the scheduler drivers and tests.
- **Email** can't split: `poll_emails` needs the freshly-created task id
  mid-loop for the untrusted-sender confirmation gate and the `processed_emails`
  linkage, so it self-creates its tasks. `EmailTransport.poll` delegates to
  `poll_emails` and returns an empty `IncomingMessage` list. The scheduler's
  email tick calls `poll_emails` directly.

`ingest_message` is the only shared inbound code; it maps an `IncomingMessage`
straight onto `db.create_task` (the duplicate-Talk-message guard returns the
existing id rather than inserting twice).

## Outbound

- **`TalkTransport.deliver` / `.edit`** own Talk message construction — the one
  place outside the CLI that builds `TalkClient`. `deliver` splits at
  `max_message_length`, posts parts sequentially, and threads + @mentions the
  first part in group chats when `threaded=True`. `scheduler.post_result_to_talk`
  and `edit_talk_message` are thin shims over these (kept so the event consumers
  and `process_one_task` keep their signatures). `notifications._send_talk` also
  delegates to `TalkTransport.deliver`.
- **`process_one_task`** gates the progress-ack subscriber on
  `transport.capabilities.supports_progress_ack` (resolved via the registry),
  keeping the `source_type == "talk"` guard so only interactive Talk tasks get
  an editable ack (briefings / scheduled / subtasks that also resolve to the
  Talk surface do not). Result + email delivery still call the
  `post_result_to_*` shims (extensive introspection-test coverage depends on the
  call shape).
- **`LogChannelSubscriber`** delivers the log-channel message through
  `TalkTransport` (the log channel is always a Talk room today).

## Known residuals (candidates for a later sweep)

`TalkClient` is still constructed directly in a few Talk-protocol-internal spots
that are not part of the surface-delivery seam: `scheduler._resolve_channel_name`
(log-channel name lookup), `scheduler._finalize_log_channel` and the
`run_cleanup_checks` stale/ancient-task notices, and `commands.py` `!command`
replies. The Talk inbound poll body also still lives in `talk_poller.py` (the
transport's `poll` delegates to it) rather than physically inside
`transport/talk.py`, and the conversation/participant/DM caches remain
module-global there (they back `talk_poller.get_dm_token`, which
`notifications.resolve_conversation_token` calls). These are intentional: moving
them buys little and would churn a lot of tightly-coupled tests.

## How to add a transport (e.g. Matrix, web chat)

1. Write `transport/<name>.py` with a class implementing the `Transport`
   protocol: set `name` + `capabilities`, implement `poll` (normalize the
   surface's inbound into `IncomingMessage`), `deliver` / `edit` /
   `download_attachment`, and `resolve_target`.
2. Register it in `make_registry` behind the surface's enabled flag.
3. If the surface introduces a new `source_type`, extend
   `_surface_for_source_type` so `registry.for_task` resolves it.
4. Inbound: the surface's driver calls `transport.poll()` then `ingest_message`
   per result (or self-creates like email if it has a mid-loop dependency).
5. Outbound: a task whose surface resolves to your transport delivers through
   `registry.for_task(task).deliver(...)`; progress acks come for free if your
   `capabilities.supports_progress_ack` is True.
6. Tests: instantiate the transport, mock its transport layer (HTTP / IMAP /
   websocket), and assert `poll` produces the right `IncomingMessage`s and
   `deliver` / `resolve_target` behave. `make_registry` must do no network on
   construction.

**Web chat** (see `Drafts/Web chat surface spec.md`): inbound via a web POST →
`ingest_message`; outbound is already covered by the SSE `task_events` reader,
so a `WebChatTransport` mostly needs `poll` (or a push entry) + `resolve_target`.
**Matrix** (see `Drafts/Matrix messaging surface spec.md`): a `MatrixTransport`
over matrix-nio, with Matrix's bridges (WhatsApp / Signal / Telegram) riding the
same seam.
