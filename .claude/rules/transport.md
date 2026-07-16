# Transport abstraction (`src/istota/transport/`)

A uniform seam over Istota's messaging surfaces. Inbound, a `Transport`
normalizes a surface's messages into `IncomingMessage`; `ingest_message` turns
those into tasks. Outbound, `deliver` / `edit` push a task's result to a
resolved channel. `TransportRegistry` holds the enabled transports and resolves
one per task.

Six concrete transports ship: `TalkTransport`, `EmailTransport`,
`NtfyTransport`, `IstotaFileTransport`, `ReplTransport`, and `WebTransport`.
**Matrix is the designed-for next consumer** — adding one is a new `Transport`
subclass plus a line in `make_registry`, not a patch across the scheduler, the
consumers, and the notification dispatcher.

Transports split into two `surface_class`es (`TransportCapabilities.surface_class`):
- **push** (`talk`, `email`, `ntfy`, `istota_file`, future Matrix) — the daemon
  actively delivers via `Transport.deliver()` to a resolved channel.
- **stream** (`repl`, `web`) — an *interactive* task's own result is the
  `task_events` log the client tails. The web chat surface uses
  `source_type="web"` / `output_target="web"`; `web` is in
  `routing._STREAM_SURFACES` so the planner short-circuits a web task's result
  to a stream destination (no push) and the `/api/chat/*` SSE endpoint tails
  `task_events`. `ReplTransport.deliver` is a genuine no-op (the terminal has no
  persistent store). `WebTransport.deliver`, by contrast, is a real write
  (ISSUE-121): web is a *user-routable* delivery surface, so alerts / the
  verbose execution log / any notification routed to `web` append an unsolicited
  system message to the user's room (`web_chat_messages` table), rendered merged
  into room history and surfaced live in an open room by an idle poll. The two
  meanings of `web` — interactive stream vs. notification sink — don't collide:
  the stream path never calls `deliver`, and `deliver` never runs for a
  `source_type="web"` task's own result (the planner already routed it to
  stream).

`conversation_token` keeps its name and stays opaque at every consumer (it is
the per-surface channel id); `source_type` stays the routing key. Neither was
renamed. (Folding ntfy + istota_file into transports and adding the REPL
stream surface superseded the original "ntfy/istota_file are side channels"
design — see "Outbound delivery routing" below.)

## Layout
```
transport/
├── __init__.py   # re-exports + the public surface
├── _types.py     # IncomingMessage, TransportCapabilities, DeliveryOptions, Transport protocol
├── registry.py   # TransportRegistry, make_registry, _surface_for_source_type
├── routing.py    # Destination, parse_output_target, resolve_delivery_plan, plan_has_surface
├── ingest.py     # ingest_message(conn, config, msg) -> int
├── talk/         # Nextcloud Talk surface (push)
│   ├── __init__.py  # TalkTransport (seam: deliver/edit/resolve + poll entry)
│   └── inbound.py   # poll_talk_conversations + filtering/dispatch + module caches
├── email/        # IMAP/SMTP surface (push)
│   ├── __init__.py  # EmailTransport (seam: poll/deliver/resolve)
│   ├── inbound.py   # poll_emails + routing precedence + confirmation gate
│   └── outbound.py  # deliver_email_result + structured-output parse + sent-email record
├── ntfy/         # ntfy push surface (push) — NtfyTransport + send_ntfy_async (the single ntfy POST)
├── istota_file/  # TASKS.md result write-back (push) — IstotaFileTransport
├── repl/         # terminal REPL (stream) — ReplTransport (deliver is a no-op; outbound is task_events)
└── web/          # web chat delivery surface (stream, user_routable) — WebTransport + default_web_room_token; deliver appends a web_chat_messages row
```

Both surfaces are subpackages because both directions live together. For Talk:
`TalkTransport` (the seam) in `__init__.py`, the inbound poll body in
`inbound.py`. For email: `EmailTransport` in `__init__.py`, the inbound poll
body in `inbound.py`, the send body in `outbound.py`. The low-level clients stay
shared and outside the seam — Talk's HTTP/OCS `TalkClient` in `istota.talk`,
email's IMAP/SMTP client in `istota.skills.email`.

Email's genuinely-shared, non-transport plumbing (`get_email_config`,
`is_synthetic_email_thread_token`, `normalize_subject`, `compute_thread_id`,
`cleanup_old_emails`) lives in `istota.email_support` — used by both transport
halves and by non-transport callers (the briefing skill, the notification
dispatcher, the TASKS.md poller, the scheduler's delivery-routing / cleanup
paths). It is the only email code outside `transport/email/`.

## Core types (`_types.py`)

- **`IncomingMessage`** — a surface-normalized inbound message. Field→column
  contract that `ingest_message` relies on: `channel_token` →
  `Task.conversation_token`, `delivery_token` → `Task.talk_delivery_token`,
  `platform_message_id` → `Task.talk_message_id`, `reply_to_message_id` →
  `Task.reply_to_talk_id`; plus `user_id`, `text`, `source_type`, `surface`,
  `attachments`, `is_group_chat`, `output_target`, `model`/`effort`, `raw`.
- **`TransportCapabilities`** (frozen) — `supports_edit`, `supports_threading`,
  `supports_progress_ack`, `supports_typing`, `max_message_length`,
  `surface_class` (`"push"` | `"stream"`), `user_routable` (default `True`).
  Drives capability-gated wiring in the scheduler instead of `source_type ==`
  checks; the delivery planner reads `surface_class` to decide push-vs-stream.
  `user_routable` marks a surface a user can deliberately point traffic at (a
  briefing output, a default destination, an alert route). The self-routing
  surfaces are `False` — `istota_file` only ever delivers back to the TASKS.md
  line a task came from, and `repl`/stream is the inline terminal the daemon
  never delivers to. `registry.routable_names()` filters on it, and the web UI
  (`web_app._registered_delivery_surfaces`, the briefing `outputs` list)
  offers only those; the grammar still validates the self-routing surfaces on
  the wire (`_validate_descriptor_surfaces`), so programmatic / CLI descriptors
  keep working — `user_routable` only governs what the UI *offers*.
- **`DeliveryOptions`** (frozen) — optional per-delivery metadata passed
  alongside `deliver(target, text, *, options=…)`: `title` / `priority` /
  `tags`. `NtfyTransport.deliver` reads them; surfaces that don't use them
  ignore them. A typed object rather than untyped `**extra`.
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
build one on demand. Talk is registered when `talk.enabled` and email when
`email.enabled`; `ntfy`, `istota_file`, `repl`, and `web` are registered
unconditionally (per-user / per-task gating happens in their `resolve_target` /
`deliver`, not at construction).

`_surface_for_source_type` (the *inbound* source_type → primary surface map):
`email` → `"email"`; `repl` → `"repl"`; `web` → `"web"` (a stream surface with
no push transport, so `for_task` resolves it to `None` — the `task_events` log
is the delivery, exactly as for REPL); everything else (talk, briefing,
scheduled, subtask, heartbeat, cli, istota_file, unknown) → `"talk"`, the
existing default. `registry.for_task(task)` uses it to resolve the primary
delivery transport (the one consumer, the progress-ack gate, already no-ops on
`None`). Outbound fan-out (a task delivering to several surfaces) is the
delivery planner's job, not this map — see below.

## Inbound

**Both surfaces self-create their tasks inside `poll`** — for the same class of
reason: the `create_task` must share the surface's `db.get_db` transaction with
the inbound side effects, so a create failure rolls the whole batch back and the
messages are re-polled rather than silently lost.

- **Talk**: `transport.talk.inbound.poll_talk_conversations(config) -> list[int]` owns every
  Talk-specific step (conversation listing + cache, per-room long-poll,
  system/own/unknown/unmentioned filtering, `!model` prefix, `!command` dispatch,
  confirmation-reply handling, the per-channel active-task gate, attachment
  extraction, cancelling superseded confirmations) **and** calls `ingest_message`
  in the same transaction as `set_talk_poll_state` / the command + confirmation
  side effects. If `create_task` raised after the poll cursor advanced (separate
  transactions), the messages would be lost forever (the dedup guard can't help —
  they'd never be re-polled). `TalkTransport.poll` delegates to it and returns an
  empty `IncomingMessage` list. (An earlier design split this into a
  `collect → ingest` step across a transaction boundary; that introduced exactly
  this message-loss window and was reverted.)
- **Email**: `transport.email.inbound.poll_emails(config) -> list[int]` owns
  every email-specific step (IMAP listing, the plus-address → sender → thread
  routing precedence, attachment download + Nextcloud upload, prompt assembly,
  the untrusted-sender confirmation gate) **and**, like Talk, calls
  `ingest_message` in the same `db.get_db` transaction as the confirmation gate /
  `mark_email_processed`. It self-creates because the gate
  (`set_task_confirmation` + the gate message) and the `processed_emails` linkage
  both need the freshly created task id mid-loop. `EmailTransport.poll` delegates
  to it and returns an empty list. The scheduler's email tick imports
  `poll_emails` from `transport.email` and calls it directly.

  **Email-reply origin routing.** A thread-matched reply (recipient replies to a
  mail we sent) routes back to the *surface the original send came from*, not
  unconditionally to Talk. At send time `routing.origin_descriptor(task)` stamps
  `sent_emails.origin_target` — a descriptor (`web:<token>` / `talk:<token>`)
  recovered from the originating task's surface, or, for an email-*continuation*
  task, from its `conversation_token` (a `web-`/`repl-`/synthetic token is
  classified accordingly so multi-round threads keep their origin). On the inbound
  reply, `poll_emails` reads that descriptor and applies the per-user
  `Config.email_reply_routing_for(user_id)` policy (`origin+thread` default |
  `origin` | `thread`) to build `output_target` (e.g. `web:<token>,email`), with
  `conversation_token` set to the origin room so the reply continues that
  conversation. A NULL `origin_target` (pre-migration row, or a non-deliverable
  origin) falls back to the exact legacy `talk,email` behavior + the
  `talk_delivery_token` ladder — which now refuses `web-`/`repl-`-prefixed tokens
  as Talk channels. A *foreign* reply routed into a web room is delivered via
  `WebTransport.deliver` (`process_one_task`'s web-push branch); it does not gate
  confirmations (only own-origin `source_type="web"` tasks do). Policy column lives
  in `user_profiles.email_reply_routing`; set via `istota user ensure
  --email-reply-routing`.

`ingest_message` is the only shared inbound code; it maps an `IncomingMessage`
straight onto `db.create_task` (the duplicate-Talk-message guard returns the
existing id rather than inserting twice). **Both** surfaces route their creates
through it — Talk inside its poll transaction, email inside its poll transaction
— and it is the entry point a future driver-ingested surface (web chat) would
use across its own boundary. `record_inbound` stamps the surface-native message
id into the canonical user row's `external_ids` (Talk ids at ingest) — feeding
both the echo ledger and the Talk→web read-sync cursor cap.

## Post-as-user mirroring + echo prevention (user-scoped OAuth)

When `[web] token_storage = "encrypted"` and `ISTOTA_WEB_TOKEN_KEY` are set
(web unit only — see `istota.web_tokens`), a web send into a Talk-bound room is
posted to Talk *as the user* at ingest time by the web process
(`web_app._mirror_web_turn_as_user`): a short-lived
`TalkClient(config, bearer_token=…, timeout=5)` sends the prompt with
`referenceId = WEBMIRROR_REF_PREFIX + <canonical message id>`
(`transport.WEBMIRROR_REF_PREFIX = "istota:webmirror:"`, defined in
`_types.py`), then stamps the returned Talk id onto the canonical user row.
That stamp doubles as the scheduler's repost-suppression signal
(`db.user_turn_has_external_id(task_id, "talk")` — the mirror branch skips
`_format_mirror_user_repost` when present) and as the echo ledger entry.

Echo prevention is two independent guards:
1. **referenceId fast-path** (`transport/talk/inbound.py`): any polled message
   whose `referenceId` starts with `WEBMIRROR_REF_PREFIX` is skipped before
   dispatch — race-free even when the long-poll beats the stamp write, because
   the marker travels inside the Talk message. The poll cursor still advances
   and the `talk_messages` context cache still keeps the turn.
2. **external-ids ledger** (`record_inbound`): `db.message_has_external_id`
   with `exclude_origin=surface` — catches a referenceId-stripped echo, while a
   row that *originated* on the inbound surface (a re-polled duplicate) is
   excluded so it still reaches `create_task`'s duplicate dedup.

Read-state sync rides the same token: web→Talk is an event-driven
`mark_conversation_read` push (fire-and-forget, only on actual cursor advance);
Talk→web is a throttled per-user pull on the web rooms poll
(`[web.chat] talk_read_sync_interval`, default 60s) that advances the web
cursor of fully-read (`unreadMessages == 0`) Talk-bound rooms up to
`db.room_max_talk_synced_message_id` — never past web-only system messages.
Everything is web-process-only, feature-gated, and degrades to the legacy
behaviour (attributed repost, web-only read state) on any failure.

## Outbound

- **`TalkTransport.deliver` / `.edit`** own Talk message construction. They no
  longer build a `TalkClient` per call — they pull the process-global persistent
  client via `async_runtime.get_talk_client(config)` (one pooled `httpx.AsyncClient`
  reused across the daemon's lifetime; see `.claude/rules/scheduler.md`
  "Persistent asyncio runtime"). `deliver` splits at `max_message_length`, posts
  parts sequentially, and threads + @mentions the first part in group chats when
  `threaded=True`. `scheduler.post_result_to_talk` and `edit_talk_message` are
  thin shims over these (kept so the event consumers and `process_one_task` keep
  their signatures); their sync call sites invoke them via `run_coro` so the
  awaited methods run on the persistent loop. `notifications._send_talk` also
  delegates to `TalkTransport.deliver`.
- **`EmailTransport.deliver`** owns the send body via
  `transport.email.outbound.deliver_email_result` — structured-output parsing
  (deferred file preferred over inline JSON), thread-reply vs fresh-send routing,
  and `record_sent_email` for emissary thread matching. `scheduler.post_result_to_email`
  is a thin shim, mirroring `post_result_to_talk`. The shim calls the
  bool-returning `deliver_email_result` directly (not `EmailTransport.deliver`)
  because its scheduler callers check the success flag, which the
  `Transport.deliver` protocol (`int | None`) discards for a surface with no
  message-id concept.
- **`process_one_task`** gates the progress-ack subscriber on
  `transport.capabilities.supports_progress_ack` (resolved via the registry),
  keeping the `source_type == "talk"` guard so only interactive Talk tasks get
  an editable ack (briefings / scheduled / subtasks that also resolve to the
  Talk surface do not). Result + email delivery still call the
  `post_result_to_*` shims (extensive introspection-test coverage depends on the
  call shape).
- **`LogChannelSubscriber`** delivers the verbose execution log to the user's
  resolved log destinations via the registry (`notifications.effective_log_destinations`
  — opt-in: `routing["log"]` > legacy `log_channel` > disabled). Delivery is
  capability-keyed on `supports_edit`: edit-capable surfaces (Talk) get the live
  in-place edited message stream; non-edit surfaces (email, ntfy) get a single
  final-summary delivery from `scheduler._finalize_log_channel` instead of
  per-tool spam. No longer Talk-only.

## Outbound delivery routing (`routing.py`)

The single source of truth for "where does a task's result go". A **destination**
is `surface[:channel]`; a task's `output_target` column is a comma-separated list
of them.

- **`parse_output_target(spec) -> list[Destination]`** (pure, no I/O) — splits
  on commas, normalizes the legacy compound aliases (`both` → talk+email,
  `all` → talk+email+ntfy), parses each `surface[:channel]` leaf, dedups. `None`
  / empty / `"none"` (whole spec *or* a list leaf) → dropped. Surface validity
  is **not** checked here.
- **`resolve_delivery_plan(config, task, registry) -> list[Destination]`** —
  turns a task into the ordered, deduplicated, channel-resolved destinations the
  scheduler delivers to. Precedence: explicit `output_target` > reply-to-origin
  (interactive source types: `talk` / `email` / `repl`) > source-type default >
  drop. Each destination has its channel filled (Talk via
  `_talk_target_for_delivery`) or is dropped with a WARNING (unregistered
  surface, or a configured surface whose user-level channel resolves to `None`).
  **Never raises** — plan resolution must not abort task finalization. An empty
  post-drop plan for an interactive source type falls back to reply-to-origin so
  a misconfigured `output_target` can't silently eat a reply.
- **`plan_has_surface(plan, surface) -> bool`** — the replacement for the old
  `target in ("talk", "both", "all")` string checks. `process_one_task`
  precomputes `plan_talk` / `plan_email` / `plan_ntfy` / `plan_file` from the
  resolved plan and branches on those.

`process_one_task` builds the plan once (`make_registry(config)` +
`resolve_delivery_plan`) and fans out to every push destination. A confirmation
prompt is eligible only when Talk is in the plan **and** ntfy is not (the `all`
broadcast target is a fan-out notification, not an interactive turn — mirrors
main's deliberate exclusion of `all` from the confirmation gate). `stream`
destinations (REPL) contribute no push work — the `task_events` log is the
delivery.

### Purpose-keyed routing table (`notifications.py`)

Distinct from `resolve_delivery_plan` (which routes task *results* by
`output_target`), the per-user **routing table** routes *notifications* by
*purpose*. `PURPOSES = (reply, alert, log, briefing, notification)`. Each user's
`UserConfig.routing` maps a purpose → an `output_target` descriptor (e.g.
`{"alert": "ntfy"}`), persisted in the `user_profiles.routing` JSON column.

- **`resolve_destinations(config, user_id, purpose) -> list[Destination]`** —
  precedence: `routing[purpose]` descriptor (full comma list) > legacy fields
  (`alerts_channel` → alert, `log_channel` → log, first briefing token →
  briefing) > `default_destination` > `[talk]`.
- **`send_notification(..., surface=None, purpose=None)`** — an explicit
  `surface` wins (e.g. a heartbeat check's own channel, push.py's `ntfy`); else
  `purpose` resolves through the routing table; else bare `talk`. This is what
  makes `routing={"alert": "ntfy"}` actually reroute alerts. Wired purposes:
  heartbeat alerts (`effective_alert_surface` — a check with no explicit
  `channel` defers to `routing["alert"]`), policy-refusal + deferred
  security/action alerts (`alert`), email-sent notices (`notification`).
- Set via `istota user ensure --route purpose=descriptor` (validated against
  `PURPOSES`) or the web `/settings` Preferences card; both go through the same
  `user_profiles.routing` JSON column. The CLI can set any purpose. The web card
  surfaces `default_destination`, the `alert` route, and the `log` route. The
  `log` route is what drives the verbose execution log — it's read by
  `effective_log_destinations` (the log path), not just stored: routing it to
  `email` / `ntfy` actually moves the log there (the "(off)" empty option
  disables it; the legacy `log_channel` field is the back-compat Talk shorthand
  it supersedes). The remaining purposes are still UI-dead — `briefing`
  duplicates each briefing's own `conversation_token`, `reply` is vestigial
  (result delivery routes via `resolve_delivery_plan`/`output_target`, not the
  routing table), and `notification` falls to the default. The web card
  preserves any CLI-set non-surfaced routes on round-trip rather than stripping
  them.

## Deliberate residuals (ISSUE-113, closed)

Three things the transport-abstraction spec's *Deviations* section flagged for a
later sweep were reviewed under ISSUE-113 and kept as-is. They are settled
decisions, not pending debt.

**No direct `TalkClient` construction outside the singleton.** The
Talk-protocol-internal spots that used to build their own `TalkClient`
(`scheduler._resolve_channel_name`, `scheduler._finalize_log_channel`, the
`run_cleanup_checks` stale/ancient-task notices, `commands.dispatch` `!command`
replies, the inbound poller, the confirmation-reply handler) all pull the
persistent `get_talk_client(config)` singleton and run via `run_coro` — swept by
the persistent-asyncio-loop refactor. A repo-wide grep finds exactly one
`TalkClient(...)` construction: the singleton factory in `async_runtime.py`,
which is its canonical home. The CLI shares that singleton too (via
`commands.dispatch` → `get_talk_client`), so the "no direct `TalkClient` outside
the transport" invariant holds by grep.

**The delivery shims stay.** `scheduler.post_result_to_talk`,
`post_result_to_email`, and `edit_talk_message` remain thin named functions over
`TalkTransport.deliver`/`.edit` and `transport.email.outbound.deliver_email_result`
rather than collapsing into bare `registry.get(surface).deliver(...)` calls at
each site. They centralize three genuine impedance-matches that a uniform
`Transport.deliver` can't carry: the Talk `target_token` override (the
email-source-task-replying-into-Talk synthetic-token case), the email
bool-vs-`int|None` mismatch (the protocol returns a message id, but email has no
message-id concept and its two callers branch on a success bool), and the Talk
url/token guard + exception→`False` in `edit`. The event consumers
(`consumers/talk.py`, `consumers/log_channel.py`) call these by name. Collapsing
buys no behavioral change and would smear that logic across ~5 call sites plus
the consumers; the newest surfaces (ntfy, istota_file) already deliver through
`registry.get(surface).deliver(...)` directly, so the shims are Talk/email-only
and won't acquire new callers.

**Talk inbound caches stay module-global.** The conversation/participant/DM
caches remain module-global in `transport/talk/inbound.py` (they back its
`get_dm_token`, which `notifications.resolve_conversation_token` calls) rather
than instance state on `TalkTransport`. Email's shared, non-transport helpers
live in `istota.email_support` (see the layout section). Moving either buys
little and would churn tightly-coupled tests.

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

**Web chat** (`transport/web/`, ISSUE-121): inbound is the `/chat` web POST →
`ingest_message` (so `WebTransport.poll` returns `[]`); an interactive task's
result streams over the SSE `task_events` reader. `WebTransport.deliver` is the
*notification/log/alert* path — it appends a `web_chat_messages` row to the
target room (`default_web_room_token` resolves a bare `web` route to the user's
`general` room). `resolve_target` returns that default token.
**Matrix** (see `Drafts/Matrix messaging surface spec.md`): a `MatrixTransport`
over matrix-nio, with Matrix's bridges (WhatsApp / Signal / Telegram) riding the
same seam.
