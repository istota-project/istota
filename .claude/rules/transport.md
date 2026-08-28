---
paths:
  - "src/istota/transport/**"
  - "src/istota/email_support.py"
  - "src/istota/notifications.py"
---

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
  `task_events`. `_STREAM_SURFACES` governs *that* short-circuit and nothing
  else — the room fan-out asks `room_view` instead. `ReplTransport.deliver` is a genuine no-op (the terminal has no
  persistent store). `WebTransport.deliver`, by contrast, is a real write
  (ISSUE-121): web is a *user-routable* delivery surface, so alerts / the
  verbose execution log / any notification routed to `web` append an unsolicited
  system message to the user's room (a `role='system'` row in the canonical
  `messages` store), rendered merged
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
└── web/          # web chat delivery surface (stream, user_routable) — WebTransport + default_web_room_token; deliver appends a role='system' messages row
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
  `sender_address` is the message's *own* sender when that differs from
  `user_id` — today only email's envelope sender, which names who wrote the mail
  rather than the istota user it was routed to. Raw and untrusted;
  `record_inbound` sanitizes it through `db.external_email_sender` before it can
  reach `messages.author_label`, so no reader ever sees a raw `From:`.
  `queue` names the worker queue the resulting task lands on — a property of the
  *surface's* latency contract, not of the message, which is why it defaults to
  `foreground` and interactive surfaces never set it. Email passes
  `[scheduler] email_task_queue` (default `background`), so a flood at the
  public `bot+user@` address cannot take the slots a live chat turn needs
  (ISSUE-250). It threads `IncomingMessage.queue` → `record_inbound(queue=…)` →
  `db.create_task(queue=…)`.
- **`TransportCapabilities`** (frozen) — `supports_edit`, `supports_threading`,
  `supports_progress_ack`, `supports_typing`, `max_message_length`,
  `surface_class` (`"push"` | `"stream"`), `user_routable` (default `True`),
  `room_view` (`"external"` | `"canonical"` | `None`, default `None`).
  Drives capability-gated wiring in the scheduler instead of `source_type ==`
  checks; the delivery planner reads `surface_class` to decide push-vs-stream.
  `room_view` is the **second, orthogonal** routing axis and the one the room
  fan-out reads: whether the surface is a view of a shared room, and if so where
  that view's transcript is stored. `talk` → `"external"` (Nextcloud owns the
  store, so a room fan-out must push), `web` → `"canonical"` (the store *is*
  `messages`, so writing the row is the delivery and a push would double-post),
  everything else → `None` (a delivery target, not a room view). The two axes
  agree today only because web happens to be both the only stream room surface
  and the only canonical-transcript one; `repl` is the counterexample that keeps
  them separate (`"stream"`, no room view). See `_expand_room_destinations`.
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
  `tags` / `markdown`. `NtfyTransport.deliver` reads them; surfaces that don't
  use them ignore them. A typed object rather than untyped `**extra`.
  `markdown` asks the surface to render the body as markup rather than literal
  text (ntfy: a `Markdown: yes` header). **Opt-in** — a plain-text body
  routinely carries `*`, `_` and `#` a renderer would eat, so default-on would
  silently rewrite every existing notification. The ntfy transport sets the
  header outside the `encode_header_value` path: it is a fixed ASCII literal, so
  the ASCII-only retry has nothing to flatten and must not downgrade the render
  mode and deliver raw markup as prose.
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

  **Email confirmation gate.** One `needs_confirmation` decision per email,
  resolved *before* `ingest_message` because it also sets
  `IncomingMessage.suppress_transcript_mirror` — the mirror commits in the task's
  transaction, so a gated turn must not publish attacker text into the room before
  the user has answered (`db.cancel_task` on a decline touches only `tasks`). One
  rule per routing method:

  - `plus_address` / `sender_match` — gated unless `is_trusted_email_sender`,
    called with `include_own_addresses=_own_address_claim_counts(config,
    auth_result)`, which is the whole of the three-state `confirm_sender_match`
    (ISSUE-249 Gap 3). **One rule, both routes** — see below.
  - `thread_match` — gated unless `email_ownership.thread_reply_from_correspondent`
    says the envelope sender is one of the addresses the bot actually wrote to on
    the matched `sent_emails` row, and then by the same `is_trusted_email_sender`
    call as the other two routes. **One rule, all three routes** — see below.

  **Why the thread route joined the gate (ISSUE-234).** It used to be exempt on
  the argument that possession of a `Message-ID` we issued is the routing
  evidence. That is sound about *which thread* and says nothing about *who*: the
  id is a bearer token, not a secret, disclosed to everyone Cc'd, everyone the
  thread is forwarded to (most clients preserve `References`), every relay and
  backup in the path, and to a public archive if the bot ever mails a list. There
  is no retention on `sent_emails`, so every id stays valid indefinitely, and one
  leak bought a permanent bidirectional agent channel scoped to a user — the
  reply goes to `processed_emails.sender_email`, i.e. to whoever wrote in, which
  then mints them an id of their own. One piece of evidence was answering two
  questions with different populations.

  The second question now reads a second piece of evidence. The envelope sender
  is weak on its own — unauthenticated, same as everywhere else on this path —
  but it is the one the forwarded / leaked / hijacked case fails, and it costs
  the ordinary emissary reply nothing, since that sender is the correspondent by
  construction. Everything else about the route is unchanged: it still resolves
  the user, still recovers `origin_target`, still runs the quiet-sender filter.

  **Exact address, never the domain**, and compared against the matched row
  alone. A domain match reads well and is wrong twice: the population a leaked id
  reaches first is the correspondent's own colleagues and shared mailboxes, which
  is exactly what it waves through, and a correspondent on a large provider would
  extend the credential to every account there. Widening to every `sent_emails`
  row in the `References` chain is the other tempting relaxation, and it lets a
  sender who legitimately holds one thread's id add a leaked id from another and
  inherit that thread's `conversation_token` and `origin_target` — the routing
  payload being the thing worth stealing, the sender is checked against the row
  that supplies it. A genuine third party on the thread therefore meets one
  prompt, and `yes trust` settles it.

  Side effect worth knowing: `!trust` and `trusted_email_senders` now mean
  something on this route, where the trust check was previously never consulted.
  There is no matching distrust — the trust list is allow-only, so `!untrust`
  removes a grant rather than adding a block, on this route as on the others.

  Four residuals, each considered and accepted rather than missed:

  - **`to_addr` is the To line only.** No writer records Cc — the skill's
    `reply-all` sends to a Cc list and stores `orig.sender`, `outbound_drafts`
    joins `to_addrs` while holding `cc_addrs` beside it — so a party the bot
    genuinely wrote to on Cc reads as a third party and meets one prompt.
    Recording the full recipient set is a schema change; the miss costs one
    confirmation that `yes trust` settles.
  - **Approving once makes the sender a correspondent.** `deliver_email_result`
    replies to `processed_emails.sender_email` and `_record_sent_email` writes a
    row naming it, so a plain `yes` plus a bot reply mints exactly the evidence
    this predicate reads, and later mail from that address is ungated with no
    trust row for `!untrust` to remove. Consistent with what the address now is
    — the bot did write to it — but wider than the prompt's "process this
    message" wording implies. Pre-existing in shape: the same `yes` had the same
    effect when the whole route was ungated.
  - **No age bound.** `sent_emails` still has no retention, so an address the
    bot wrote to once stays a correspondent indefinitely — a recycled mailbox at
    a former employer keeps the route. Bounding *who* was the fix; bounding *how
    long* is the retention policy ISSUE-234 named as its pairing and this change
    did not take.
  - **The evidence is still an unauthenticated `From:`.** A spoofer who forges
    the correspondent's address is past the gate, and the DMARC canary does not
    watch this route — it is scoped to the self-claim, and `claims_to_be_user`
    is structurally False here. Widening it would alert on every reply from a
    domain that publishes no DMARC policy, i.e. on ordinary mail. ISSUE-249's
    authserv-id scoping made the verdict trustworthy but left it a detector; a
    gate that reads it is still unbuilt.

  What this route now shares with a gated `sender_match` reply is that the held
  task sits under a **real room token** (it inherits `sent_emails.
  conversation_token`, not the synthetic thread hash), so
  `cancel_pending_confirmations` discards it on the room's next message.
  **It no longer parks that room's foreground queue**: `_CLAIM_CHANNEL_GATE_SQL`
  gates only foreground tasks, and inbound mail moved to the background queue
  under ISSUE-250 (`[scheduler] email_task_queue`). Under
  `email_task_queue = "foreground"` the park is back, exactly as described here.
  Losing it cuts both ways and both halves are consequences of that one change:
  an unanswered gate no longer wedges its thread for the full
  `confirmation_timeout_minutes`, and an email turn is no longer serialized
  against a live Talk or web turn in the same room — they are on different
  queues, so the per-room single-active rule does not see across them. Not introduced here — a gated `sender_match` reply that also
  matched a thread has always landed there, which is the case `web_app`'s cancel
  comment describes and the user-scoped notification inbox mitigates —
  but this route widens who reaches it. Pinned by an assertion on
  `conversation_token` in `TestThreadMatchConfirmationGate`.

  **The inbound volume budget (ISSUE-250, consequence 1).** The gate answers
  *who*; this answers *how much*. `bot+{user_id}@domain` is public by
  construction — it is the `From:` on every mail the bot sends on a user's
  behalf — so the set of parties holding a working ingest address is everyone
  the user has ever corresponded with through the bot, plus anyone who saw one
  of those messages. `thread_match` is ungated by design, so every external
  contact holds a permanent ungated route. Nothing bounded how many of those
  became paid model invocations.

  Two counts, both in `poll_emails`, checked **after** owner resolution and the
  quiet-sender filter and **before** `ingest_message`: `email_rate_limit_
  messages` per user and the tighter `email_sender_rate_limit_messages` per
  `(user, sender)` under it, over `email_rate_limit_window_seconds`. The sender
  count is checked first so the log and the alert name the specific reason.
  Placement is deliberate on both sides — quiet mail creates no task and must
  not spend an allowance real mail needs, and an unrouted message has no budget
  to charge.

  **Over-budget mail is filed, not dropped**: `routing_method="throttled"`, no
  task, the message left in the folder where `email from-senders` still reaches
  it. This is the quiet-sender behaviour applied automatically, and it is the
  same file-don't-drop shape as the `read_error` path. A budget that discarded
  would recreate the silent mail loss the poll-cursor pass fixed, with a config
  knob on it. The ISSUE-250 entry named `ingest.py` as where the budget "has to
  bite" because that is where `create_task` is; that is the one place it cannot
  go, because the budget is inseparable from what happens to the mail that
  exceeds it and the shared ingest path has neither a ledger to write nor a
  mailbox to leave it in.

  **The prompts collapse too.** The gate turned a spam flood into a
  *notification* flood — one undeduplicated prompt per held message, each
  answerable alone, plus a `!confirm` backlog to clear by hand or wait out at
  `confirmation_timeout_minutes`. Past `_MAX_PROMPTS_PER_SENDER_WINDOW` (3) per
  `(user, sender)` per window the individual prompt is suppressed and the
  sender's held mail is summarized in the same notice the throttle uses. The
  **task is untouched**: still held, still withheld from the room, still
  addressable by `!confirm <id>`. This is deliberately the cheap half — the full
  version, one prompt resolving onto a *set* of task ids, needs `confirmations`
  to answer several tasks at once and is its own change.

  Two module dicts carry it, both in-process and unpersisted for the same reason
  `_dmarc_alerted` is: `_throttle_alerted` (one alert per user per window) and
  `_prompt_counts`. The DB carries the budget itself, so a restart cannot hand
  an attacker a fresh allowance. One accepted consequence of sharing a window
  between the two: a user already alerted about throttling in this window is not
  alerted again when prompts start collapsing. One alert per window is the
  contract, and the alternative is the flood.

  **What `confirm_sender_match` actually switches (ISSUE-227).** It turns off the
  *own-address branch* of the trust check — the branch that reads "the `From:`
  names one of this user's addresses, therefore it is the user". SMTP `From:` is
  unauthenticated, so that is a claim the sender makes about itself, and with the
  flag on it stops counting as evidence. Everything else about the gate is
  unchanged, which is why one keyword argument carries the whole feature.

  The flag was unreachable before. `sender_match` is *defined* by the own-address
  match, and the plain trust check returns True on its first branch for exactly
  that set, so the gate evaluated `not True` on every reachable path and the
  config surface advertised a control that did nothing. Excluding the branch is
  what makes the question non-circular.

  It applies to **both** sender-trust routes, not only the one ISSUE-227 names —
  the issue named `sender_match` because that is where the dead branch was, not
  because the exposure stops there. Routing resolves by recipient first, and the
  plus-address is public (it is the `From:` on every mail the bot sends on the
  user's behalf), so a spoofer who knows the address the gate is about also knows
  how to route around a `sender_match`-only gate: `From: <user>` + `Cc:
  bot+<user>@…` resolves as `plus_address`, where the own-address branch would
  wave through the identical claim. Both reviewers of the fix found that bypass
  independently; the single expression is the fix. An *external* sender's trust
  answer is untouched by the flag — only the self-claim changes.

  Default **off**, and the flag is best read as a *declaration about the inbound
  mail path* rather than a safety switch. `false` asserts that something upstream
  already authenticated the header — normally DMARC enforcement at the receiving
  MTA, which rejects a forged `From:` at SMTP time so it never reaches
  `poll_folder`; `true` says nothing does, so ask. Upstream is strictly the
  better place: silent, no per-message cost, and not defeatable by a human
  approving a prompt out of habit. This gate is the fallback for paths that can't,
  and it is noisy by construction because nothing in a plain SMTP message
  separates the user from someone claiming to be them. Two supporting reasons for
  the default: the branch was dead since it shipped, so `false` is the behaviour
  every deployment already has; and an unanswered confirmation is auto-cancelled
  at `confirmation_timeout_minutes`, so defaulting it on would have silently
  dropped inbound mail wherever Talk isn't watched. Ansible knob
  `istota_email_confirm_sender_match`. What the default *assumes* about the mail
  path — and the follow-up that detects the assumption breaking (ISSUE-228) — is
  in `docs/features/email.md`.

  **The prompt is route-aware.** `yes trust` writes the sending address into the
  runtime trusted list, which on a self-claim would exempt the user's own address
  — and therefore the spoofer, since the address is all either party presents.
  Offering it as one of three equal options steers the user into disabling the
  control on its first message, so a self-claim gets a plain yes/no and an
  external sender keeps the shortcut. The affordance is hidden, not the verb:
  `handle_confirmation_reply` still matches `yes trust` on the reply text with no
  knowledge of which prompt variant was sent, so a user who types it anyway gets
  it, alongside `!trust` and `trusted_email_senders`. All three stay reachable as
  deliberate acts; the point is not to nudge.

  **The gate reads the verdict under `verify` (ISSUE-249 Gap 3).** A signal the
  sender cannot forge is what lets it distinguish the user from a spoofer and
  stop asking about legitimate mail, and `[email] authserv_id` is what makes the
  verdict trustworthy enough to act on. `_own_address_claim_counts(config,
  auth_result)` is the whole policy and feeds `include_own_addresses`: `off`
  returns True (the header is proof), `gate` returns False (it never is),
  `verify` returns True only for `_AuthResult.verdict == "pass"`. **Fails
  closed** — a `None` result, which is what the thread route passes since
  `claims_to_be_user` is structurally False there, is held. `verify` is refused
  at config load without `authserv_id`, because an unscoped verdict is read off
  whichever header arrived on top and gating on a sender-written value reads as
  protection while being none. `_own_address_claim_counts` re-asserts the
  authserv-id itself rather than resting on that validator, so the guarantee is
  local to the decision; `_sender_match_policy` is the reader and normalises case
  and the legacy booleans for anything that builds an `EmailConfig` directly.

  **Authserv-id discovery, and why it splits in two.** While `authserv_id` is
  blank the poller helps the operator find it, but the value can only be read off
  a header — so what it does depends on whether that header is credible.
  `_note_observed_authserv_id` names the observed id **only on a `pass`**, where
  the header authenticated the sender's domain and is therefore good evidence of
  a real MTA, and only in the log, keyed on `user_id` alone. A canary alert
  instead carries `_AUTHSERV_ID_ADVICE`, which names **no id at all**: a failing
  verdict means the top header is the one in doubt, and a spoofer can raise that
  alert on demand, so recommending the authserv-id off their own forged header
  would let them nominate the id we then trust — silencing the canary and, under
  `verify`, turning every forged message into a `pass`. Keying the dedup on the
  observed id would also be an unbounded axis (attacker-chosen in exactly this
  state, so one log line and one permanent entry per message), which is the flood
  `_DMARC_RESULTS` buckets unregistered tokens to avoid. Neither half raises a
  notification of its own, so a healthy path stays as quiet on the alert channel
  as it was before, and both stop once the id is set.

  **A `verify` hold always says why.** `_check_dmarc_canary`'s WARNING is the
  usual explanation, but it sits behind `dmarc_canary` and, for an `unevaluated`
  verdict, `dmarc_canary_warn_on_missing` — both of which the gate is
  deliberately independent of. In either state every self-addressed message would
  be held with nothing saying why, and an unanswered hold is cancelled at
  `confirmation_timeout_minutes`, so the failure mode is mail quietly going
  missing. `poll_emails` logs the hold separately, per message and undeduped.

  **The canary alert describes the policy, never the message's fate.** What
  actually happened is not knowable where the alert is composed: the hold is
  decided much later and also turns on `is_trusted_email_sender`, and the
  quiet-sender and rate-limit branches can drop the message before a task exists.
  A draft that inferred the outcome from the policy string was wrong both ways —
  it told a `gate` deployment nothing was blocked when everything is, and told a
  `verify` deployment a trusted sender's message was held when it ran.

  **The DMARC canary (ISSUE-228)** is the monitoring for the assumption the
  `off` default encodes. `_check_dmarc_canary` in
  `transport/email/inbound.py` warns — and alerts, `purpose="alert"` — when a
  self-claim arrives without a `dmarc=pass` from the receiving MTA.
  `_check_dmarc_canary` is still purely a *detector* and never blocks, holds or
  reroutes, which is why `dmarc_canary` is safe on by default. **The verdict it
  reports is no longer only a detector**, though: under `confirm_sender_match =
  "verify"` the same `_AuthResult` decides whether the message runs (ISSUE-249
  Gap 3). So the verdict is computed by the *caller* (`poll_emails`) and passed
  in, rather than derived inside the canary behind the `dmarc_canary` switch —
  the gate needs an answer whether or not the operator wants the warnings, and
  one shared computation is what stops the two from disagreeing about one
  message. Anything claiming the canary is "not a gate" should be read as being
  about `dmarc_canary`, not about the verdict. It does not verify DKIM
  in-process; if the MTA already rejects, re-implementing the check buys nothing.
  An attacker who forges a header the check accepts silences it, which is
  acceptable because the MTA is the boundary — it catches misconfiguration and
  drift, and should never be described as a control. What `[email] authserv_id`
  changes is which forgery works: unscoped, any sender-written top header does,
  including in the drift case the canary is watching for; scoped, the forgery has
  to name the operator's own MTA.

  Three things about it that are load-bearing and easy to break:

  - **Which headers are read is `[email] authserv_id`'s decision** (ISSUE-249).
    `Email.authentication_results_all` carries every `Authentication-Results` in
    wire order (`_header_all`) and `authentication_results_headers` is the
    accessor; `authentication_results` stays the topmost, and the accessor falls
    back to it for the several places that build an `Email` by hand. Blank
    `authserv_id` reads element 0 alone, which is what the check always did:
    each hop prepends, so while the MTA stamps, element 0 is its stamp. Set,
    `_our_headers` keeps every header whose `_authserv_id` matches and discards
    the rest, so a sender can neither quieten the check with an injected
    `dmarc=pass` nor make it noisy with an injected `dmarc=fail`. Several stamps
    of ours is legitimate (one header per method), so all matching headers are
    read and the non-pass-wins rule applies across them as well as within one.
    A quoted authserv-id reads as empty and matches nothing — the loud
    direction, per the parser's standing rule.
  - **"No stamp of ours" is loud on its own** when `authserv_id` is set
    (`unstamped`), without `dmarc_canary_warn_on_missing`. Configuring the id is
    the operator's assertion that their MTA stamps, so a message contradicting
    it is a finding; the flag keeps its narrower meaning, our stamp present with
    no DMARC verdict in it (`unevaluated`). Unscoped, absence stays
    `unevaluated` and stays behind the flag, exactly as before. The alert counts
    the headers it rejected and never quotes them — in that branch every header
    present is one the sender wrote.
  - **A pass is checked for alignment, and this rung runs whether or not
    `authserv_id` is set** — so it is the one part of ISSUE-249 that changes
    behaviour for a deployment on the default config. `_dmarc_header_from`
    returns **every** `header.from` attributed to a DMARC methodspec in a header,
    and the caller warns (`misaligned`) if *any* fails `_domains_align` against
    `_address_domain(sender)`. Returning the first would put the check back on
    first-match-wins, which is exactly the rule `_dmarc_result` was fixed to stop
    using — a sender appending a second `header.from` naming the right domain
    would mask the wrong one ahead of it.
  - **Absent and unreadable are different answers, and only absent is quiet.**
    `_dmarc_header_from` returns a second flag for "the property is there and no
    domain came out of it" — a quoted value (`_split_methodspecs` blanks quoted
    strings), a value the next methodspec's `;` truncated away, a bare root dot.
    Each is an ambiguous read and resolves loudly, matching `_authserv_id`'s rule
    in the same file; `_HEADER_FROM_PROPERTY` captures with `*` rather than `+`
    so an empty value reaches that branch instead of looking like absence.
    Genuine absence stays silent because many MTAs never emit the property.
    Known limit: a property wholly inside a comment is blanked with nothing left
    to notice and reads as absent.
  - **Alignment is relaxed to a label boundary** (`_domains_align`): exact, or a
    parent/child relationship either way. DMARC's own relaxed mode aligns on the
    organizational domain and an MTA may record the domain it evaluated, so a
    strict compare warns on every message from a subdomain sender — the noise
    `dmarc_canary_warn_on_missing` is off by default to avoid. Deliberately not a
    public-suffix lookup; both domains come from the same message, so what it
    admits beyond a true org match is parent/child pairs, not unrelated
    registrations under a shared suffix.
  - **`dkim=`/`spf=` are alert detail only** (`_method_result`). They skip the
    read-completeness cross-check because nothing load-bearing rests on them, and
    they are truncated at `_ECHOED_VALUE_MAX` like every other header-derived
    value reaching a log line or an alert — `[a-z]+` bounds the alphabet, not the
    length, and the WARNING is emitted per message and never deduped.
  - **Scoped to the self-claim on both routes**, gated on `claims_to_be_user and
    routing_method in ("plus_address", "sender_match")` — the same set the
    confirmation gate covers, for the same ISSUE-227 reason. Watching only
    `sender_match` reopens that bypass: the plus-address is public, so `From:
    <user>` + `Cc: bot+<user>@…` carries the identical claim on a route a
    sender-match-only check never sees. `claims_to_be_user` is computed once
    above both consumers.
  - **`dmarc=none` is a failure, not an absence.** It means the domain publishes
    no policy — the "DMARC record was edited away" drift case, which is the whole
    point. Only a *missing* verdict (no header, or a header reporting other
    methods) is the silent-by-default `unevaluated` class, behind
    `dmarc_canary_warn_on_missing`.

  The parser earns its length, and every rule in it closes a way to make the
  canary go **quiet** — the only failure that matters, since a noisy canary is
  merely annoying. `_dmarc_result` anchors `dmarc=` to the start of a methodspec
  (so a `header.dmarc=` property does not match), splits on `;` only at paren
  depth zero and outside quotes, and drops comment and quoted-string contents —
  a reporting MTA echoes the envelope sender into the SPF comment and into
  `smtp.mailfrom=`, so a bare `split(";")` lets the sender promote their own text
  to the start of a segment. Comment nesting is depth-tracked because RFC 5322
  comments nest and a `\([^)]*\)` strip stops at the first `)`. Three further
  rules, each pinned by its own test:

  - **Any non-pass beats a pass**, rather than first-match-wins, so an injected
    `dmarc=pass` cannot mask the genuine `dmarc=fail` in the same header.
  - **A read that looks incomplete yields `malformed`**, never `pass` and never
    `None` — the two quiet answers. Incomplete means either the header ended
    mid-quote/mid-comment, or `_DMARC_RAW` counts more `dmarc=` tokens in the raw
    header than the parse attributed to methodspecs. **The count is the
    load-bearing half**, and the reason dropping quoted and commented text is not
    sufficient alone: an *unbalanced* delimiter is noticeable, but a sender who
    plants a **matched pair** straddling the verdict hides it with nothing
    unbalanced left to see — two stray quotes echoed into `header.d=` and
    `smtp.mailfrom=` — and the answer would otherwise be `None` (silent by
    default) or a `pass` appended after. That was a must-fix in the second review
    round; the first round's unbalanced-only rule missed it.
  - **An unregistered result token buckets to `other`.** It reaches the dedup
    key, and where this canary matters most the sender chose it; left open it is
    an unbounded key axis, i.e. one alert per message.

  Consequence worth knowing before "fixing" it: a header carrying a `dmarc=` in a
  `reason="…"` or a comment reports `malformed` and warns, even though no
  legitimate verdict is in doubt. That is the count firing, and it is the
  intended trade — the parser cannot tell an MTA that quoted the word from a
  sender who planted it, so it declines to call the header clean.

  Alert dedup is an in-process dict keyed `(user_id, sender.lower(), verdict)`
  with a 24h window; the WARNING log is never deduped, so the per-message record
  survives the throttle. Not persisted on purpose — a restart re-alerting is
  harmless and it needs no schema. The window opens only on a *delivered* alert:
  `send_notification` reports "no destination configured" by returning False
  rather than raising, and stamping at decision time let one silent failure
  swallow the next 24 hours. Delivery itself (`_deliver_dmarc_alerts`) runs
  **after the whole batch**, outside every per-message transaction — an alert can
  route to the web surface, which opens a second connection to the same DB and
  would otherwise block on the poller's own lock until the busy timeout. Ansible knobs `istota_email_dmarc_canary` /
  `istota_email_dmarc_canary_warn_on_missing`.

  **Three pre-existing interactions the flag made reachable**, noted under
  ISSUE-227 and swept with ISSUE-241: a gated task parks its room via
  `_CLAIM_CHANNEL_GATE_SQL` and only the Talk poller called
  `cancel_pending_confirmations`, so a gated turn in a *web* room froze it until
  the timeout — `_chat_create_web_task` now cancels the room's own pending
  confirmations on a new send, exactly as the Talk poller does, room-scoped so
  an email gate under its synthetic thread token is untouched;
  `suppress_transcript_mirror` was never undone on approval, so an approved mail
  left an assistant row with no user row above it (the ISSUE-136 defect,
  re-reached) — `confirmations.approve` writes the withheld user row when the
  token is an existing room, existence-only for the same reason
  `record_inbound`'s `mirror_only` is; and `get_pending_confirmation_for_user`
  answers only the newest, so a bare "yes" during a burst landed on the wrong
  email — Path C now fires only when exactly one question is open and otherwise
  posts an addressable listing.

  **Answering a gate is surface-agnostic (ISSUE-241).** The prompt goes out
  through `notifications.send_confirmation_prompt`, which resolves the user's
  `alert` routing purpose rather than hardwiring Talk — so a user who has
  pointed alerts at web or ntfy is actually asked, while the ladder's Talk
  fallback keeps an unconfigured deployment behaving as before. It returns
  `(delivered, talk_message_id)`: the id still feeds `talk_response_id` (Path A
  matches a *reply* against it) and the flag is what the undeliverable-prompt
  WARNING keys on. The prompt names its own task id, because that id is the
  *address* of the question — `!confirm <id>` / `!confirm <id> no` /
  **The prompt is delivered after the poll transaction closes**
  (`_deliver_confirmation_prompts`, beside `_deliver_dmarc_alerts` and for the
  identical reason): routing by purpose means it can land on the *web* surface,
  whose delivery opens a second connection to this database, and the message's
  transaction is open from `create_task` onward — inline, the fix for "the web
  user is never asked" would have become a busy-timeout stall per gated email
  that still did not ask them. Since ISSUE-250 that transaction covers one
  message rather than the batch, which shortens the window but does not remove
  it. `talk_response_id` is written back
  in its own short transaction afterwards; losing it costs Path A alone.
  `run_cleanup_checks` buffers its expiry notices for the same reason.

  `!confirm <id> trust` work from any surface with a composer
  (`commands.cmd_confirm`, aliases `!yes` / `!no`), and the verbs themselves
  live in `istota.confirmations` so the Talk poller, the command and the web
  endpoints cannot drift. Web chat additionally gets the
  notification inbox (`GET /notifications`, the bell in the app nav; formerly
  `GET /chat/confirmations` and a banner above the transcript) — deliberately
  not a widening of the room history query, since the aux gap-fill renders
  `tasks.prompt`, i.e. the untrusted body the gate is holding back —
  `_AUX_ROOM_SCOPE` reaches an email task only through its mirrored
  `role='user'` row, and a gated turn has none. The card
  carries the sender, subject and routing method off `processed_emails`
  instead.

  **ISSUE-243 moved the last Talk-private pieces in with the verbs**: the word
  lists (`confirmations.parse_answer`) and the three-path lookup
  (`confirmations.resolve`), plus the ack text (`apply_answer`,
  `ambiguity_listing`). `handle_confirmation_reply` keeps only its Talk half —
  read the reply's parent id, post the ack, record the exchange — and the web
  POST intercepts a bare answer before `_chat_create_web_task`, which would
  otherwise cancel the very question the answer approves. The ownership check
  moved inside `resolve`, where it belonged: Path C already filters by user, so
  it only ever mattered for A and B. Talk gained a `"Confirmed."` on a plain
  approve, which it had never posted. Full reference in
  `.claude/rules/web-chat.md`.

  **The prompt is mirrored into the room's web transcript** (ISSUE-242).
  `_dispatch`'s `talk` leg used to write nothing to `messages`, so a prompt
  delivered to `talk:<token>` was invisible in the web view of that same room —
  and for the confirmation prompt that is silent mail loss. It now writes a
  `role='system'` row after a successful Talk post, gated on room existence and
  stamped with the Talk message id (which is what makes the reply walk-back,
  Path A, work on web). Best-effort: the Talk delivery has already happened.

  **The expiry notice routes by purpose too.** `run_cleanup_checks` used to post
  to the task's `conversation_token` verbatim, which for an email gate names no
  room, so the user was never told their mail had been dropped. It now goes
  through `purpose="alert"` with the token passed only when it really is a Talk
  channel (`_confirmation_notice_token`), and names the sender and subject
  (`_expired_confirmation_notice`) so the message can be found again in the
  mailbox. **A gated email is still marked processed at ingest** — not deferring
  that is deliberate: `processed_emails` is what stops the next poll from
  re-ingesting the same message as a fresh task, so withholding the row turns
  one unanswered question into one new task and one new prompt per poll cycle.
  (The ledger is keyed `(uidvalidity, email_id)` since ISSUE-250, so the UID is
  qualified now — but the poll cursor is an optimization, not a second
  authority, and a reset cursor re-walks straight back into these rows.)
  Making the expiry loud and specific is the recoverable version of the same
  concern.

  **Email-reply origin routing** — all of which is conditioned on the sender not
  being the routed user themselves; see "The user's own thread reply gets no
  origin copy" below. A thread-matched reply (recipient replies to a
  mail we sent) routes back to the *conversation the original send came from*,
  not unconditionally to Talk. At send time `routing.origin_descriptor(task,
  conn)` stamps `sent_emails.origin_target`. **When the origin is a registered
  live room the descriptor names the room** — `room:<canonical_token>` — rather
  than one of its views: a room is one conversation bound to several surfaces,
  and recording the leg the send happened to leave by throws away the fact that
  it was a room, so the reply reaches that leg alone and the other view stays
  blank. The room form re-expands by *live* bindings at delivery, so it also
  picks up a binding added after the send (an "Also open in Talk" promote is
  exactly that). The candidate token is `conversation_token`, else
  `talk_delivery_token`, each resolved to canonical through
  `routing._canonical_room_token` — which tries the token as canonical, then as
  this surface's ref, then as *any* surface's ref, the last being the promoted-room
  email continuation whose token belongs to Talk while its own surface owns no
  bindings. Without a `conn`, or for a destination that is not a live room (a
  Talk DM, a genuine email-only thread, an archived room), it falls back to the
  surface-qualified `web:<token>` / `talk:<token>` form as before. Rows stamped
  before the room form existed are upgraded at read time by
  `routing.upgrade_legacy_origin` — **which is not merely transitional**: any
  writer whose room is reachable from neither candidate token still stamps the
  surface form, so check `_room_descriptor`'s coverage before assuming it has
  become dead code. It replaced `room_fanout_descriptor`, which widened a
  descriptor to the bare `room` and so could only ever name the room the task was
  already sitting in. On the inbound
  reply, `poll_emails` reads that descriptor and applies the per-user
  `Config.email_reply_routing_for(user_id)` policy (`origin+thread` default |
  `origin` | `thread`) to build `output_target` (e.g. `room:<token>,email`), with
  `conversation_token` set to the origin room so the reply continues that
  conversation. A NULL `origin_target` (pre-migration row, or a non-deliverable
  origin) falls back to the exact legacy `talk,email` behavior + the
  `talk_delivery_token` ladder — which now refuses `web-`/`repl-`-prefixed tokens
  as Talk channels. A *foreign* reply routed into a web room is delivered via
  `WebTransport.deliver` (`process_one_task`'s web-push branch); it does not gate
  confirmations (only own-origin `source_type="web"` tasks do). Policy column lives
  in `user_profiles.email_reply_routing`; set via `istota user ensure
  --email-reply-routing`.

  **Mail the user sends themselves gets no room copy (ISSUE-254, widened by
  ISSUE-275).** The mirror above exists for mail the user did not write — an
  external contact replying to mail we sent on their behalf, or a stranger's
  first contact at `bot+<user>@` — where the room copy is the only way they learn
  it arrived. Mail from their own address is not that: the answer goes back the
  way the question came, so the room copy renders a conversation they are already
  having a second time, and then charges it to that room's LLM context. On a
  thread it is worse than redundant — each reply quotes the whole prior chain, so
  the N-th message wrote roughly N copies of the conversation into the
  transcript. The discriminator is **per-message, not per-user** — `email_reply_
  routing = "thread"` would suppress the emissary case too — and it is
  `email_support.sender_claims_to_be_user`, i.e. the envelope sender is one of the
  routed user's own addresses. **Not** `not is_emissary_reply`, which is false for
  a plus-address route as well: that is a third party writing to `bot+<user>@`,
  and it keeps its mirror. ISSUE-254 additionally required a matched thread
  (`sent_email_match`), and **ISSUE-275 dropped that conjunct**: it left the
  ordinary case untouched — the user mailing their own bot, which is first
  contact every time, and which therefore got the `room:<tok>,email` plan
  ISSUE-247 built for strangers. Three legs now key on the answer. On the thread
  routes, two — `output_target` drops the origin leg (in *both*
  branches — the legacy NULL-`origin_target` one hardcodes `talk,email` and is
  live for any send with no deliverable origin, not merely pre-migration rows),
  and `IncomingMessage.mirror_to_room` turns off `record_inbound`'s mirror, which
  would otherwise fire on rung 1 because the task inherits the origin room as its
  `conversation_token`. On the `plus_address` / `sender_match` routes, the third:
  the poller does not call `routed_notification_room` at all, leaving
  `output_target` unset — the pre-ISSUE-247 shape, delivered by mail alone. There
  the token is a thread hash rather than a room, so rung 1 misses and that leg is
  the only thing that could have named one; `mirror_to_room` is set from the same
  answer regardless, so the decision is stated rather than left resting on that
  coincidence. That inheritance stays — the reply still continues that
  conversation's *context*, it just does not write itself back into it. The answer
  side needs no third change: `_room_turn_belongs_here` wants either a delivery
  into the room or a question already in it, and neither holds. **It does change
  one thing beyond the transcript**: with no room leg the task is no longer a
  `_confirmable_surface`, so an answer matching `CONFIRMATION_PATTERN` completes
  and is mailed rather than parking. Kept deliberately, and only defensible here
  — the rule that an email task parks and asks in the room exists because the
  room leg is the only surface that can carry the question (the email leg would
  mail the principal's decision to an external correspondent), and parking with
  no such leg delivers the question nowhere and dies at
  `expire_stale_confirmations`, the failure `process_one_task`'s
  `is_confirmation_request` comment records fixing. On a self-reply the email leg
  goes to the *user*, so the question reaches the only person who can answer it,
  on the surface they are reading. The cost, stated: deferred ops a park would
  have held until the answer now apply on completion. The outbound email approval
  gate is unaffected — it runs on the delivery leg, not on this park. On a
  first-contact self-addressed mail the same follows: the email leg is the user's
  own address, so the question reaches them there.

  `tasks.withheld_from_room` stays **False** on the first-contact routes, and by
  the column's own rule rather than an exemption — it means "there is a room, and
  this exchange is deliberately not part of it", and with a thread hash for a
  token and no room in the plan, nothing was resolved to be absent from. The same
  rule already covered a genuine email-only thread. The approval path follows
  from that without a special case: `_room_holds_no_copy_of_this_exchange` reads
  False, `_restore_transcript_mirror` runs, and `transcript_room_for_task`
  resolves no room, so it publishes nothing.

  Residual: this rests on an unauthenticated `From:`, so a spoof also buys
  *suppression*, and ISSUE-275 widens that reach from a thread reply to any
  inbound mail. What it does not widen is who can use it quietly — a self-claim
  on either gated route still meets the confirmation gate under
  `confirm_sender_match`, and the canary still warns on a failing verdict. Same
  forgery the confirmation gate already faces, which is what the ISSUE-228 canary
  watches for and what ISSUE-249's authserv-id scoping made harder to hide; a new
  consequence of it, not a new class.

  **The decision is recorded on the task (ISSUE-255).** The task still carries
  the origin room as its `conversation_token` — kept deliberately, so the reply
  continues that conversation's context — so everything keyed on that *column*
  rather than on the transcript once saw the exchange anyway, and the suppression
  existed only for the length of one function call. `record_inbound` now writes
  `tasks.withheld_from_room` from the same answer that turns off the mirror, and
  six readers consult it. **`suppress_transcript_mirror` deliberately does not
  set it** — that one is a hold on a turn that *does* belong in the room.

  - `_conversation_history_from_tasks`, the fallback `get_conversation_history`
    serves whenever `_messages_caught_up` is False (any room with no completed
    talk/web task left in `tasks` — a mail-only room, or one whose last chat turn
    aged past `task_retention_days`). The `messages` path needs no equivalent: a
    withheld turn was never written there.
  - `get_previous_tasks`, the re-surfacing path `executor._build_db_context` runs
    on **every** task in the room with no caught-up gate above it — so this one
    reached LLM context even for a room reading cleanly from `messages`, which
    makes it the wider of the two history leaks rather than the narrower.
  - `index_conversation`'s `channel:<token>` namespace, which `_recall_memories`
    serves back to later tasks there. The per-user index is untouched: the
    exchange is the user's own and belongs in their own recall.
  - `get_completed_channel_tasks_since` and `get_active_channel_tokens`, the
    channel sleep cycle's collector and its discovery query. `CHANNEL.md` is
    durable and reaches every later prompt, and a room whose only recent traffic
    was withheld is not an active channel.
  - `confirmations._room_holds_no_copy_of_this_exchange`, now a column read
    (below).
  - `get_recent_conversation_skills`, the 30-minute skill-stickiness window. The
    weakest reader — skill names, not content — swept for consistency rather than
    for cost.

  Two copy paths carry the column forward, and both are load-bearing rather than
  tidy, because each inherits `conversation_token` from the task it copies:
  `commands._create_retry_task` (a bare `!retry` in the origin room can land on a
  withheld task, and this issue *raises* how often that happens, since the new
  failure alert is what tells the user to retry) and the deferred-subtask handler
  (pinned alongside the token it already pins, and not the JSON's to choose).

  **`transcript_token` is required, and is the whole difference between the
  column and `not mirror_to_room`.** The column says "there is a room, and this
  exchange is deliberately not part of it". The poller sets
  `mirror_to_room=False` for *every* self-addressed thread reply, including a
  genuine email-only thread whose `conversation_token` is a synthetic hash naming
  no room — flagging that one makes the readers above drop the thread's own prior
  turns from its own history, which is the only history such a thread has, since
  there is no `messages` store to fall back to. The consequence, stated: a
  room-less self-reply thread keeps its pre-existing silence on both failure
  paths. That is unchanged behaviour rather than a new gap — the issue's scope
  was the exposure ISSUE-254 *widened*, i.e. a plan that became email-only by
  default rather than by configuration.

  **The two failure paths an email-only plan has no channel for.** Dropping the
  origin leg leaves no Talk leg either, so `process_one_task`'s `plan_talk and
  talk_token` branch — beside the standing rule never to email errors — told the
  user nothing when their mailed request failed permanently, and an SMTP failure
  left the composed answer only in `tasks.result`. Both predate ISSUE-254 (any
  `email_reply_routing = "thread"` user had them); what changed is that an
  email-only plan became the *default* outcome for a self-reply. Both now raise
  through `purpose="alert"`, and the delivery-failure notice carries the answer
  body itself, since the point is that the answer survives.

  **The gate is "was the user themselves waiting for this answer", in two
  spellings** — `task.withheld_from_room or email_from_the_user`. Deliberately
  not "the plan is email-only", which is the tidier-looking gate that must not be
  taken: an external correspondent's reply under `email_reply_routing = "thread"`
  has the identical plan and the identical absent channel, and a stranger is
  waiting for that answer, not the user. (A cron mailing a report is excluded
  earlier still, by the `source_type in ("briefing", "scheduled")` arm above
  both.) Two spellings because the poller can record the answer on the task in
  only one of the two cases: `withheld_from_room` covers a self-addressed
  *thread* reply, and reads False for self-addressed *first contact* — correctly,
  by the column's own rule, since no room is resolved there. ISSUE-275 made first
  contact the common case, which put the user mailing their own bot straight back
  into the silence this branch exists to end, so `scheduler._email_task_from_the_
  user` recovers the fact from the `processed_emails` row the poller already
  writes, judged by the same `sender_claims_to_be_user` the ingest decision uses.
  A reconstruction rather than a second column, and the same one
  `confirmations._restore_transcript_mirror` makes; it never raises and answers
  False when the ledger row has been pruned, so a lost lookup costs a notice
  rather than a delivery. Both directions are pinned by
  `tests/test_email_self_reply_residue.py::TestAPermanentFailureReachesTheUser`.
  Both are buffered and sent after every DB transaction closes, for the reason
  every other notification on this path is: routed by purpose, an alert can land
  on `web`, whose delivery opens a second connection to the same database. The
  delivery-failure body goes through `email_transcript_body` first: an email
  task's `result` may *be* the `{"subject","body","format"}` envelope the send
  path parses, and a notice promising the answer must not hand over a JSON blob
  (the same unwrap the room transcript does, ISSUE-247).

  **One accepted trade in the failure alerts.** `purpose="alert"` resolves
  through the user's routing table, so if their `alert` route or legacy
  `alerts_channel` names the origin room, the delivery-failure notice puts the
  answer body into the room ISSUE-254 removed the exchange from. Accepted rather
  than gated: it lands as a `role='system'` row, which
  `_conversation_history_from_messages` excludes (it inner-joins user+assistant
  pairs), so the quadratic context bill ISSUE-254 was actually about is not
  reintroduced — only transcript visibility, on a failure, where the alternative
  is the answer existing nowhere the user can reach. Suppressing the body when
  the route happens to be that room would lose the answer in precisely the
  configuration where it is most likely to be lost.

  **`mirror_to_room` and `suppress_transcript_mirror` are not the same flag.**
  The second is a *hold* — the turn belongs in the room and
  `confirmations.approve` publishes it once answered. The first is permanent, with
  no restore path. They co-occur only under `confirm_sender_match` (which stops
  the own-address claim from counting as trust, so a self-addressed reply can
  reach the gate at all), and there the restore must not hand back the copy the
  suppression removed. `confirmations._room_holds_no_copy_of_this_exchange` is
  what stops it, and since ISSUE-255 it is a plain read of
  `withheld_from_room` — it used to reconstruct the answer from two observable
  halves (the plan naming no room, *and* the sender being the user), which needed
  a `Config` in scope and could only ever infer what the poller had already
  concluded. Both halves were load-bearing in that form, because a self-addressed
  *first contact* keeps its `room:<tok>,email` plan and its mirror; the column
  says so directly instead.

**Who wrote a row.** `messages` records the author on two nullable columns —
`author_user_id` (an istota user) and `author_label` (an external sender,
**pre-sanitized** through `db.external_email_sender`, so an addr-spec or the
fixed `UNATTRIBUTED_SENDER` and never a raw header). Exactly one is set, or
neither; readers test the label first, so a writer that wrongly set both breaks
toward naming the stranger rather than toward crediting the account the mail was
routed to. Both NULL means the room owner, which is what every pre-migration row
falls back to. Resolved once at write time — `transport.ingest.resolve_author`
where a `Config` is in scope, `db.author_for_email_task` for the two callers
without one (the confirmation-approval mirror and the `messages_author_v1`
backfill), and those callers should pass `Config.users[uid].email_addresses`
when they can, because the DB-only fallback
(`db.own_addresses_without_config`) cannot see addresses configured in TOML
alone. Every `role='user'` writer sets it: `record_inbound`, `!steer`, `!retry`,
and the confirmation exchange. This replaced a per-read recovery from
`processed_emails` (ISSUE-226) that answered only for email; the columns also
cover a co-member's ordinary turn in a shared room, which had no sender to
recover and rendered as the reader's own words.

`ingest_message` is the only shared inbound code; it maps an `IncomingMessage`
straight onto `db.create_task` (the duplicate-Talk-message guard returns the
existing id rather than inserting twice). **Both** surfaces route their creates
through it — Talk inside its poll transaction, email inside its poll transaction
— and it is the entry point a future driver-ingested surface (web chat) would
use across its own boundary. `record_inbound` stamps the surface-native message
id into the canonical user row's `external_ids` (Talk ids at ingest) — feeding
both the echo ledger and the Talk→web read-sync cursor cap.

**Per-room model/effort default.** Because `record_inbound` is the single
inbound choke point, it is also where a room's standing model default is
applied — uniformly across every surface. The default lives on the shared
`rooms` registry (`rooms.model` / `rooms.effort`, canonical values), so a Talk
message and a web message in the same room resolve the same default. After
resolving the canonical room token, when the incoming `model` is None (no inline
`!model` override — those are parsed upstream in the Talk poller / web POST),
`record_inbound` fills `model`/`effort` from the registry room; the inline
override wins as a unit (effort follows model). Set via the `!room` command
(surface-agnostic, through `commands.dispatch`) or the web room-settings PATCH
(`db.set_room_model_effort` / `db.set_room_effort`).

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
`mark_conversation_read` push (fire-and-forget, only on actual cursor advance,
with one forced-refresh retry on 401 since ISSUE-333);
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

### Briefing email bodies

Briefing bodies are chat markdown, so email has always flattened them with
`skills/briefing.strip_markdown` — which also destroys the article links the
news sections carry. `transport/email/outbound._briefing_email_bodies(config,
task, body, fmt)` is the single decision point: it maps a task to
`(plain_body, html_body, content_type)` and all three send sites (the legacy
unstructured-briefing branch, the reply-to-thread branch, the fresh-send branch)
pass its output straight through.

- Non-briefing task → `(body, None, fmt)`, i.e. today's behaviour untouched.
- Briefing + `briefing_email_html` on (default) + `format == "plain"` →
  `(strip_markdown(body), render_briefing_html(body) or None, "plain")`, sent
  `multipart/alternative` so a mail client shows clickable links and a
  plain-only client still gets readable text.
- Briefing + on + `format == "html"` (the rare hand-authored case) → the HTML
  passes through as the rich part and `_strip_html` derives the plain fallback.
- Briefing + off → exactly the pre-feature single-part plain send.

`html_body` of `None` means single-part, and `skills/email._set_body` treats an
**empty** `html_body` as none supplied — which is what makes the renderer's
failure signal (`render_briefing_html` returns `""` on any error) degrade to
plain text rather than shipping an empty HTML part. See AGENTS.md "Briefings"
for the renderer's grammar + safety rules.
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
  `talk_channel_for_task`) or is dropped with a WARNING (unregistered
  surface, or a configured surface whose user-level channel resolves to `None`).
  **Never raises** — plan resolution must not abort task finalization. An empty
  post-drop plan for an interactive source type falls back to reply-to-origin so
  a misconfigured `output_target` can't silently eat a reply.
- **`talk_channel_for_task(config, task) -> str | None`** — which Talk room a
  task's result goes to. Four rungs, in order: **(0)** `tasks.talk_delivery_token`
  when set, absolutely; **(1)** the task's room's `talk` binding; **(2)**
  `conversation_token` itself, when the task has one and is not email-sourced;
  **(3)** `notifications.resolve_conversation_token` (alerts → briefing →
  auto-DM) for an email task whose token is a synthetic 16-char hex thread hash
  naming no Talk room. No token and no room gives `None`, deliberately *not* the
  alerts ladder — a task with nothing to deliver to is not an email thread hash
  needing redirection. A synthetic token that resolves to nothing is returned
  as-is, preserving the pre-existing silent no-op rather than trading it for a
  different failure. `scheduler._talk_target_for_delivery` is a shim over this.

  Rung 1 replaced `tasks.talk_delivery_token` as the *general* answer: the
  column was a denormalized copy of the room's Talk binding, and it went stale
  whenever a room was promoted to Talk after the task was created. **Rung 0
  survives on purpose and must be deleted last.** While anything still writes
  the column it carries information nothing else has — the legacy thread-match
  branch in `transport/email/inbound.py`, reached when
  `sent_emails.origin_target` is NULL, copies a Talk room onto the task that the
  registry may never have heard of. Demoting rung 0 to "a hint for finding a
  room" reroutes those tasks to the alerts ladder with no error: ISSUE-057's fix
  undone. Room resolution inside rung 1 is **surface-scoped**
  (`_canonical_room_token(..., cross_surface=False)`), unlike descriptor
  stamping — a `surface_ref` is unique only within its surface, and an unscoped
  match on the delivery path posts the answer into a different conversation.
  (`cross_surface` has no default: both answers are defensible and the
  difference is invisible at the call site, so each caller states which it
  wants. A wrong *descriptor* is re-resolved by live bindings at delivery; a
  wrong *channel* is not.)
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

**One documented exemption: `talk.transient_client(config)`.** The `nextcloud`
skill's `talk` group (agent-facing room/message control — see
`.claude/rules/skills.md`) runs inside the skill CLI, a short-lived subprocess
with **no persistent asyncio runtime**: it makes one or two requests and exits.
Standing the persistent runtime up there costs more than it buys, so `talk.py`
exposes an explicit `transient_client(config)` async context manager that
constructs a client and closes it on exit. Its docstring names the skill CLI as
its only sanctioned caller. This is why the grep above finds a second
construction — it is a deliberate, named exemption rather than a drift, and no
daemon path may use it. The `web_app` bearer-token clients are a separate,
already-documented case (user-scoped OAuth, web process only).

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
*notification/log/alert* path — it appends a `role='system'` `messages` row to the
target room (`default_web_room_token` resolves a bare `web` route to the user's
`general` room). `resolve_target` returns that default token.
**Matrix** (see `Drafts/Matrix messaging surface spec.md`): a `MatrixTransport`
over matrix-nio, with Matrix's bridges (WhatsApp / Signal / Telegram) riding the
same seam.

## Input Channels

- **Talk**: long-poll, message cache, ack/progress/result via referenceId. `!commands` intercepted in poller.
- **Email**: IMAP poll, attachments to `inbox/`, threaded replies via deferred `email output` JSON. Outbound tracked in `sent_emails` for emissary thread matching. The email skill is a **two-way client** — read (`list`/`read`/`search`/`thread`/`attachments`/`from-senders`/`newsletters`) + richer send (`--cc`/`--bcc`/`--attach`/`reply`/`reply-all`) + gated `mark`/`delete`. Reads are scoped `--scope {mine,shared,all}` via the shared `email_ownership` resolver (plus-address → sender-match → thread-match), so a user only ever sees their own mail plus the unowned shared pool, never another user's. Per-user **quiet senders** (`user_profiles.quiet_email_senders`, fnmatch) file matching mail silently in `poll_emails` — marked processed, no task, read back on demand via `from-senders`. See `.claude/rules/skills.md`.
- **TASKS.md**: 30s poll, `[ ] [~] [x] [!]` markers, SHA-256 identity.
- **REPL** (`istota repl`): interactive terminal loop (`src/istota/repl/`). Each line becomes a `source_type="repl"` task with `output_target="stream"`, run inline via `scheduler.run_task_inline` (no daemon needed); `task_events` stream to the terminal via `TerminalSubscriber`. REPL tasks are inline-only — `db.claim_task` and the daemon's pending-user discovery exclude `source_type="repl"` so a running daemon never double-executes them.
- **Web chat**: always-on in-app chat surface in the web UI (full-page console at `/chat`, "Chat" nav tab before Feeds). Rooms are per-user channel tokens in `web_chat_rooms` (each gets its own `CHANNEL.md` + sleep-cycle handling); a sent message becomes a `source_type="web"` task (interactive: loads context + `CHANNEL.md` + `guidelines/web.md`) with `output_target="web"`, which routes as a **stream** surface — no Talk/email push, the result and progress live in `task_events`, tailed over SSE. Endpoints under `/api/chat/*` in `web_app.py` (rooms CRUD, message send/history/delete, task confirm/cancel, attachment upload, the session-scoped `GET /chat/files?path=` handover), plus a session-lived `GET /chat/stream` that tails the canonical `messages` store for every room the user is a member of. `!commands` and the `!model <alias>` prefix work identically across surfaces via `commands.dispatch(... surface=...)`. Web chat is also a *delivery* surface (ISSUE-121): alerts, the verbose execution log, and notifications routed to `web` are appended to a room as `role='system'` rows. Knobs under `[web.chat]`; frontend engine in `web/src/lib/stores/chat.ts`, widgets in `web/src/lib/components/chat/`. Not a Talk replacement — an in-app companion. Full reference (rooms + per-room model defaults, composer + attachment intake, drafts, send lifecycle + durability + `client_msg_id`, per-turn message actions, the live room-event stream, app-shell caching) in `.claude/rules/web-chat.md`.

## Unified Talk / web room sync

Talk and web chat share one surface-independent **room** model (spec in `Specs/Done/unified-talk-web-room-sync.md`). A `rooms` registry (PK = canonical `conversation_token`, `origin` talk|web) + `room_bindings` (per-surface ref) + a canonical `messages` store (role user|assistant|system, `task_id`, `origin_surface`, `external_ids`) + `room_read_state` supersede the de-facto `tasks`-as-history store; a markered one-time migration folds `web_chat_rooms`/`web_chat_messages`/distinct Talk tokens in and backfills. `get_conversation_history` reads `messages` with task-id re-pairing behind a self-healing dual-read (falls back to `tasks` until the store is caught up — a _completeness_ check: every completed turn mirrored, not just the newest, so a partial migration / mid-rollout window can't truncate history to the mirrored subset); Talk keeps its metadata-rich `_build_talk_api_context` for Talk-origin context. Inbound flows through one `transport.ingest.record_inbound` choke point (resolve canonical token → echo-check → store user turn → create task), used by `ingest_message` (Talk/email) and the web POST. The scheduler stores the assistant turn on completion via `_store_room_turn(conn, task, room_token, body)` — one general producer that mirrors **any** room-delivered bot post (subtask, scheduled, briefing, heartbeat, an email round-trip into its own web room, …) as an assistant spine row when the room exists, replacing the old per-source-type `_store_scheduled_room_turn` / `_store_web_room_turn` (canonical-room-transcript spec, ISSUE-176). Storage is *near*-universal and is decided by `scheduler._room_turn_belongs_here`, not by any delivery branch: the row is written when the plan delivers this answer into *that* room on some surface (a Talk leg landing there, or a surface whose `room_view` is `"canonical"` — that push **is** the row, ISSUE-164) **or** when the room already holds the question. Since ISSUE-247 the room is passed in rather than read off `task.conversation_token`, and the Talk rung asks where the Talk leg lands rather than merely whether the plan has one — see "The transcript room" below. Neither holding means a room that never received the question and is not being delivered into, where a row would be an answer-only bubble — ISSUE-136 from the other side. The two rungs are not interchangeable: an email task in a room with `output_target="email"` and no mirrored question must store nothing, while the same task with `output_target="web:<its own room>"` must store a bubble, so the delivery plan carries intent no task-only predicate can express. `origin_surface` = the real source type as provenance, not a visibility gate; only conversational turns additionally carry a `user` row, and a narrower set still gates the caught-up dual-read. **Those two sets are no longer the same** (ISSUE-136): `TRANSCRIPT_SURFACE_FILTER` is assistant-any / user-in-`('web','talk','email')`, while `_CONVERSATIONAL_SOURCE_TYPES` stays `('talk','web')` — email is mirrored as a user+assistant pair but its *complete* history is not guaranteed (a turn completed before the change, or under a `thread` reply-routing policy before the evidence rung existed, has no assistant row and never will), and counting those would peg the room to the legacy `tasks` path forever. Mirroring is not the gate criterion; guaranteed completeness is. A markered `nonconversational_transcript_cleanup_v1` migration drops the synthetic `user` rows the old `unified_rooms_v1` blanket backfill inserted for non-conversational tasks and normalizes backfilled briefing bodies from raw JSON to the delivered body; **its DELETE allowlist must stay in sync with `TRANSCRIPT_SURFACE_FILTER`** — it re-arms on any failure past the DELETE (both `except` branches return without writing the marker) and a restored pre-migration snapshot re-runs it, so a disagreement silently sweeps live turns.

**The transcript room (ISSUE-247).** Which room an exchange belongs to is resolved **once**, by `transport.routing.transcript_room` / `transcript_room_for_task`, and every writer of a room row keys on that answer. It is *not* `tasks.conversation_token`: on an email task that column is `compute_thread_id(...)`, a hash whose job is grouping `References`, and three writers used to read it as a room — the assistant-turn store, the Talk mirror, and `record_inbound`'s `mirror_only` gate. Each correctly found no room and fell back to a different workaround, so an email exchange reached the room as a `role='system'` note with no question above it while Talk and web were handed different bodies for the same message. The ladder is: the room a `role='user'` row for this task already lives in (strongest — it is where the question actually went, and it is what keeps the two halves together when routing changes mid-exchange); else `conversation_token` when it *is* a registered room (every talk/web task, and an email threaded back into its own room); else, **for an email task only**, a room named by `output_target` (the `room:<token>` form, or an explicit `talk:`/`web:`/bare `talk` leg — the bare one reads `tasks.talk_delivery_token` first, because that is `talk_channel_for_task`'s absolute rung 0 and the two ladders naming different rooms is this bug one level down). What is deliberately **not** a rung is "the room this user's notifications would go to": that is `routed_notification_room`, and only the email poller calls it, on the routes where the message names no conversation at all. As a rung it would fire for an ungated `thread_match` reply under the `thread` reply-routing policy too, writing an external correspondent's verbatim body — which `_conversation_history_from_messages` re-pairs into that room's LLM context — into the user's alerts room, a room the thread had no relationship with. Existence, never creation, at every rung, and never raises — an email task that resolves to no registered room (a cron mailing an external address) stays task-only with no transcript, unchanged. The email poller stamps the resolved room into `output_target` as `room:<token>,email` on the non-thread routes, which is what makes the room a *delivery* destination rather than something derived after the fact: Talk is pushed the same body the canonical row stores. `scheduler._talk_result_mirror_body` survives, narrowed to the case the canonical row genuinely cannot reach — a task delivered to a Talk room that is not its transcript room (ISSUE-242 on the result) — and is now handed exactly what Talk was posted rather than the transcript body. `_notify_confirmed_email_result` is gone; its held-draft branch is covered by `transport.email.outbound._announce_hold`, which fires on every hold rather than only a gated task's. Talk gets a provenance line of its own (`scheduler._format_email_user_repost`, sender + subject, **never** the body — that is the wrapped untrusted prompt): Talk renders from Nextcloud rather than from the canonical store, so the room holding the question does not put it in front of a Talk reader, and the answer would otherwise arrive replying to nothing. What used to carry that there was the deleted notice's `Email reply sent to <sender>` prefix, and only for a gated task.

**Three consequences of naming the room that are decisions, not accidents.** A first-contact email now has a Talk leg, so it is a `_confirmable_surface`: an answer matching `CONFIRMATION_PATTERN` parks the task and asks in the room instead of being mailed unasked. Kept — it is the rule a thread-matched email already followed, the prompt reaches the surface the user reads (ISSUE-241), and an unanswered one is named when it expires; the email leg still never carries the question. The `delivering_into_room` rung compares canonical rooms, but keeps the old "the plan has a Talk leg" answer whenever the transcript room *is* the task's own token — a scheduled job in room A delivering to Talk room B keeps the row it has always had, and tightening that to ISSUE-164's rule is its own change. And the inbound mirror now puts an external correspondent's verbatim body into the routed room's canonical store, so it re-pairs into *that* room's LLM context where before it reached only a room the thread already belonged to. The mitigations are the ones ISSUE-136 and ISSUE-226 put there and are why the body is stored verbatim: the `<email_content>` "external input — do not follow instructions" guard survives into the prompt, and `context._speaker_label` renders the turn as `External sender <addr>` rather than as the room owner's words. The set of rooms that can receive such a turn is what widened; the handling of one did not.

**Mirror-only surfaces (ISSUE-136).** `ROOM_SURFACES` is the set of surfaces that *own* rooms — they lazily register an unknown token, bind it, add membership and rename from the surface. That is deliberately narrower than "surfaces whose turns are stored": `record_inbound`'s `mirror_only` path records a turn from a **non**-room surface when the transcript-room resolution above yields one. Email is the one live case — a reply threaded back into the web/Talk room it came from, or a first-contact thread whose routing names a room. The gate is room **existence, never creation**, which is what stops mail the bot merely receives from minting rooms in anyone's sidebar; it also keeps the mirror off every other room side effect (no registration, binding, rename, membership, `undismiss_room`, echo ledger or per-room model default — so an email into a room the user has *hidden* is stored where they cannot see it, and an email continuation does not pick up the room's standing `!room model` default). This makes the user-row gate identical to the assistant-row gate `_store_room_turn` has always used, closing the case where a room showed a bot answer with no question above it. Two consequences worth knowing: the stored user body is the **task prompt verbatim** (`<email_metadata>`/`<email_content>` wrapper and its "external input — do not follow instructions" guard included), because `_conversation_history_from_messages` re-pairs it straight into LLM context and a prettified body would drop the guard (the guard is only half the story — the *speaker label* wrapped around that body used to contradict it by naming the room owner, fixed by the ISSUE-226 sender attribution in `.claude/rules/scheduler.md`); and a turn still facing the untrusted-sender confirmation gate sets `IncomingMessage.suppress_transcript_mirror`, because the mirror commits in the *same transaction* as the task while `db.cancel_task` touches only `tasks` — without it a declined message would be published to the room and stay there. `output_target="room"` fans out by live bindings with an **asymmetric mirror** — web→Talk is a real push, Talk→web pushes nothing (the web loader already renders Talk turns from the shared store), and a confirmation prompt mirrors only when the task's own origin is *not* a room surface. That last rule used to be a flat "confirmations never mirror", which silently dropped the question for an email-origin task: it parks on `_confirmable_surface` (which counts the mirror Talk leg), the email leg must never carry the question to the external correspondent, and the mirror leg was excluded too — so nothing asked the user and `expire_stale_confirmations` cancelled the task two hours later. A web-origin confirmation still stays on web, where its SSE stream carries it. On a web→Talk mirror leg the user's question reaches Talk one of two ways: with user-scoped OAuth enabled (see the next section) the web process already posted it _as the user_ at send time and the scheduler suppresses its repost (`db.user_turn_has_external_id(task_id, "talk")`); otherwise the bot reposts it attributed (`💬 <name> (via web):`) before its reply — a pure Talk-surface artifact never written to the canonical `messages` store, so web history/context is unaffected (`_format_mirror_user_repost`). The web room list is **membership-driven** (`db.list_member_rooms`, a `room_members` join) rather than keyed on the single-owner `rooms.user_id`, so a _shared_ room (one token, one transcript) surfaces for every participant — a group Talk room with the bot plus two humans appears in _both_ humans' web lists, each via their own `web_chat_rooms` handle (`UNIQUE(user_id, token)`, not globally unique). Membership is added by `register_room`, by every inbound sender (`record_inbound`), and by the **Talk poll itself**: on first sight the poller registers any conversation the bot participates in (`origin='talk'`, resolving the canonical token via the binding first so a _promoted_ web room isn't duplicated) and seeds membership from the human participants — mapped to istota users via the same `actor_id in config.users` + `actorType == "users"` gate as message processing, bot excluded — so a room the bot merely _lurks_ in surfaces in web chat without anyone having to message it first. This no longer depends on a `tasks` row existing for the token (the task-keyed `unified_rooms_v1` migration + the old `record_inbound`-only path left a polled-but-never-addressed room — e.g. a quiet `#sysadmin` — invisible, since its history is rebuilt from Nextcloud but its tasks were never carried over). The participant fetch (cached `_get_participants`, 5-min TTL) runs only for a genuinely new room, not every poll; DMs need no fetch (the other party comes from the conversation name). Thereafter membership for active users is kept current in the poll's message loop (any observed message from an istota user re-adds them) and by `record_inbound`. Backfilled for existing deploys by the `room_members_v1` migration. A per-user delete/hide writes a **dismissal tombstone** (`room_dismissals`, keyed `(room_token, user_id)`) _and_ drops that user's membership; `list_member_rooms` excludes a tombstoned room even while membership is later re-added, so the hide is durable. The tombstone is cleared by the user's **own** next message in the room — `record_inbound` → `undismiss_room` for an addressed message, and the poll message loop for a non-`@mention` post in a multi-user room (which never reaches `record_inbound`) — "re-engagement un-hides"; a co-member's hide is left intact. The web delete UI reflects this: an imported (Talk-origin) room is a one-click **Hide** with no type-the-name confirm, while a web-origin room keeps the destructive type-to-confirm **Delete**. The global `rooms.archived` flag stays reserved for `archive_orphaned_talk_rooms` — "the bot left the Nextcloud room", which a fresh inbound un-archives. Fixes ISSUE-134, where a group room was visible to only one arbitrary participant. Room titles are backfilled from Talk's `displayName` every poll cycle, not just on the next inbound message, so a migrated room stops showing the generic "Talk room". The same poll cycle reconciles the other direction (`db.archive_orphaned_talk_rooms`): a Talk room the bot is no longer in (deleted in Nextcloud / bot removed) is archived so it stops surfacing in web — guarded against a transient empty conversation fetch, archive-not-delete so mirror history survives. An "Also open in Talk" promote (`POST /chat/rooms/{id}/promote`) creates a real Talk conversation via new OCS `TalkClient` methods, with two-way rename propagation, and a Talk turn streams live into an open web room. Full reference in this file.

## User-scoped Nextcloud OAuth (post-as-user + read sync)

Opt-in retention of the web login's OAuth pair so the web process can act as the user against Nextcloud Talk (spec in `Specs/Done/user-scoped-nextcloud-oauth-talk-sync.md`). Gate: `[web] token_storage = "encrypted"` **and** `ISTOTA_WEB_TOKEN_KEY` (≥32 chars) present — the key is delivered to the **web unit only** (Ansible `web-secrets.env`, Docker `/data/.web_token_key`), never the scheduler/webhooks units or any task env, and the module (`web_tokens.py`) uses a distinct scrypt salt + table (`web_user_tokens`: Fernet ciphertext pair + plaintext `expires_at`) so "who can decrypt" stays auditable by grep. `callback()` persists the pair at every login (self-heals a dead refresh token); `get_access_token` refreshes within 60s of expiry under a per-user lock (NC rotates refresh tokens — persistence is atomic, `invalid_grant` deletes the row, transient failures keep it); the settings page shows a status card with Disconnect (`DELETE /api/settings/nextcloud-token`); `/api/me` carries `nextcloud_token: {connected, expires_at} | null`. Three consumers, all web-process, all falling back to legacy behaviour when the gate is off or no live token exists: **(1) Post-as-user mirroring** — a web send into a Talk-bound room posts the prompt to Talk immediately as the user (`TalkClient(config, bearer_token=…)`, `referenceId = istota:webmirror:<msg_id>`), stamps the Talk id on the canonical user row, and the scheduler skips its attributed repost; echo prevention is the referenceId fast-path in the Talk poller (race-free — the marker travels in the message) plus the `record_inbound` external-ids ledger as backstop (which excludes same-origin rows so a re-polled duplicate still hits `create_task`'s dedup). Inbound Talk messages now stamp their Talk id into `messages.external_ids` at ingest. **(2) Web→Talk read push** — mark-read/read-all call `mark_conversation_read` when the web cursor actually advanced, fire-and-forget. `_mark_read_as_user` carries the same single forced-refresh-on-401 retry `_post_as_user` has: without it a stale-but-present access token let the message mirror keep working while every read push failed silently, which is the asymmetry ISSUE-333 was reported as. `mark_conversation_read` takes `raise_on_error` for that caller — it swallowed every exception and returned a bool nobody read, so the one place that could act on a status could not see one. **(3) Talk→web read pull** — the rooms poll, throttled per user by `[web.chat] talk_read_sync_interval` (default 60s, 0 disables), fetches the user's conversation list with their bearer token and advances the web cursor of fully-read Talk-bound rooms, capped at `db.room_max_talk_synced_message_id` so web-only system messages stay unread. **There is deliberately no matching web→Talk leg**, and the reason is worth knowing before adding one: the obvious guard — Talk counts the room unread while the web cursor already covers `room_max_talk_synced_message_id` — is unsound in both of its terms. `cap` sees only Talk messages that reached `messages`, and `transport/talk/inbound.py` deliberately drops several classes before `record_inbound` (an unmentioned message in a multi-user room, a sender outside `config.users`, a guest or bot actor, system messages), every one of which still counts toward Talk's `unreadMessages`; and `current` is not evidence the user read anything, since `db.initialize_room_read_state` *seeds* a newly surfaced room's cursor to `MAX(messages.id)` so a backlog does not read as unread. Either alone makes the guard true for a room the user has never read, and `mark_conversation_read` posts with no `lastReadMessage`, so the whole conversation goes read — destroying unread state rather than restoring a badge. Doing this safely needs read-state provenance the schema does not record (seeded versus earned) plus a `lastReadMessage` push, which is its own change; ISSUE-333 stopped at the 401 retry, which is what the reported permanent failure actually was. Star sync is out of scope (Talk has no per-message star API).

**The credential and the session have independent lifetimes, and closing that gap is what ISSUE-333 is about.** The session is a signed cookie minted once at the callback and validated afterwards only against `ISTOTA_WEB_SESSION_SECRET_KEY` — no round trip to Nextcloud, ever — with a `max_age` Starlette re-issues on every response, so it rolls forward indefinitely while the user keeps clicking. The stored pair is a separate row that two paths delete at any moment (a 400/401 refresh rejection, or a decrypt failure after `ISTOTA_WEB_TOKEN_KEY` is rotated), and the login callback is the **only** writer, which an active user never revisits. So the credential can be dead for weeks behind a perfectly healthy session, and all three consumers fail *closed and silently* — `get_access_token` returns `None` on every failure path and never raises. The reported symptom was two unrelated-looking features degrading at once with nothing in any surface a user reads.

Three things now stand between that and a silent failure. **The loss is durable**: both deletion sites call `web_tokens.note_credential_lost`, which raises the existing `connected_service` notification under `object_id = "nextcloud"` — a row in the inbox plus one push, deduped by `service:nextcloud`. It is deliberately *not* raised for the other three `None` returns: a transient 5xx or network error keeps the row and is not a loss, and "no row stored" is the state of every user who has not logged in since the operator enabled the feature. Raising on either would push a warning nobody can act on, once per rooms poll. `store_tokens` closes the row, from inside the function rather than at its call sites, because "a pair is now stored" is exactly the condition and there are two writers. **The remedy is a button**: `GET /istota/reconnect` re-runs the authorize flow for an already signed-in user and lands back on `/settings`, replacing the card's old instruction to log out and back in. It mints and stores nothing itself — the callback already does that, under the identity the *provider* returns rather than the one in the session — so the only thing it adds is the landing page, carried across the hop as an allowlisted key in the session (`_POST_LOGIN_TARGETS`), never a URL. **The degraded consumers say so**: both bail at WARNING naming the missing token, where the mirror used to bail at DEBUG. A room with no Talk binding stays silent, since that is web-only rather than degraded.

One hardening from the entry was **not** taken, and the reason is structural rather than a matter of effort: a periodic keepalive that refreshes a quiet user's pair before the refresh token ages out would have to run from the scheduler, and this module's whole boundary is that the scheduler and webhook units never load its decrypt path. A keepalive there would put the web-only key in a second unit to solve a problem the rooms poll already covers for any user with the UI open.

## Transport abstraction

A uniform seam over messaging surfaces (`src/istota/transport/`). Inbound, a `Transport.poll()` normalizes a surface's messages into `IncomingMessage`; `ingest_message` maps those onto `db.create_task`. Outbound, `deliver` / `edit` push a task's result to a resolved channel; `resolve_target` picks the channel. `TransportRegistry` (`make_registry(config)`, no I/O on construction) holds the enabled surfaces and `for_task(task)` resolves the primary one by `source_type` (`email`→email, `repl`→repl, everything else→talk). Six transports ship — `TalkTransport`, `EmailTransport`, `NtfyTransport`, `IstotaFileTransport` (all `surface_class="push"`), `ReplTransport` (`surface_class="stream"` — `deliver` is a no-op; outbound is the `task_events` log a terminal tails), and `WebTransport` (`surface_class="stream"`, `user_routable=True`) — and **Matrix is the designed-for next consumer** (a new surface = one `Transport` subclass + a `make_registry` line). `WebTransport` is the web chat _delivery_ surface (ISSUE-121): an interactive `source_type="web"` task still streams its own result over `task_events` (routing short-circuits `web` to a stream destination), but the transport's `deliver()` is a real write — alerts, the verbose execution log, and any notification routed to `web` append an unsolicited system message to the user's room as a `role='system'` row in the canonical `messages` store, rendered merged into the room transcript and pushed live by the room stream. Because it's `user_routable`, web auto-appears in every routing UI that reads `registry.routable_names()`. `conversation_token` stays the opaque per-surface channel id and `source_type` the routing key — neither renamed; no DB/config change. Talk delivery/edit (the only `TalkClient` construction outside the CLI) and `notifications._send_talk` flow through `TalkTransport`; email delivery flows through `EmailTransport`; `scheduler.post_result_to_talk` / `edit_talk_message` / `post_result_to_email` are thin shims. The progress-ack subscriber is gated on `capabilities.supports_progress_ack`. Both surfaces are subpackages with both directions co-located (`__init__.py` seam + `inbound.py`; email adds `outbound.py`) and both self-create their tasks inside `poll` via the shared `ingest_message` (the `create_task` must share the inbound `db.get_db` transaction with the poll-cursor advance, or a create failure would lose messages): Talk's inbound body is `transport/talk/inbound.py` (`poll_talk_conversations`), email's is `transport/email/inbound.py` (`poll_emails`); both transports' `poll` return `[]`. Email's shared non-transport helpers live in `istota.email_support`; the low-level clients stay outside the seam (`istota.talk.TalkClient`, `istota.skills.email`). Outbound fan-out (a task delivering to several surfaces) is `transport.routing.resolve_delivery_plan`'s job: it parses `output_target` (`talk`/`email`/`ntfy`/`istota_file`/`stream`/`both`/`all`/`surface:channel`/comma lists), resolves channels, and drops unregistered/unconfigured destinations with a warning. Separately, the per-user **purpose-keyed routing table** (`UserConfig.routing`, purposes `reply`/`alert`/`log`/`briefing`/`notification`) routes _notifications_ via `notifications.send_notification(..., purpose=…)`; the `log` purpose additionally drives the verbose per-task execution log to any user-routable surface (`notifications.effective_log_destinations`, opt-in: `routing["log"]` > legacy `log_channel` > off; Talk streams live, email/ntfy get one final summary). Full reference in this file.
