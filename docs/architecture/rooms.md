# Rooms and multi-user chat

This page covers how Istota models a conversation that more than one person can read, and how one inbound turn becomes a decision about who speaks, whose authority a task carries, who will read the answer, and what the task may reach. The user-facing behaviour is in [shared rooms](../features/shared-rooms.md) (including its private replies and My notes), [switching the bot off](../features/room-veto.md) and [groups](../features/groups.md). This page is the mechanism behind them.

Every rule here is a no-op in a room with one human in it. A private conversation goes through the same pipeline and comes out the way it always did.

## The room model

A **room** is one conversation, identified by a canonical token (`rooms.token`, conventionally `rm_…`), with one transcript in the `messages` table. A room is exposed on one or more **surfaces** through `room_bindings`, one row per surface, which maps the canonical token to that surface's own reference: a Nextcloud Talk conversation token, a web room id, a WhatsApp group JID, an email thread's root Message-ID.

Each surface answers two questions about rooms, recorded in one static table, `rooms/surfaces.py`:

| Surface | Room role | Transcript lives in |
|---|---|---|
| web | member | our `messages` table (canonical) |
| talk | member | Nextcloud (external) |
| sms, whatsapp | member | nowhere we can write as a whole (none) |
| email | guest | nowhere (none) |

- **Room role**: a `member` surface creates and owns rooms; a turn on it registers the room, binds it and adds the sender as a member. A `guest` surface (email) joins the transcript of a room that already exists and never creates one. A single-correspondent email therefore stays outside the room model.
- **Transcript view**: whether a person can open a durable place on that surface and read the whole conversation, that we can also write into. Web and Talk can. A phone thread cannot (it shows only its own half), and neither can an email thread.

Two consequences follow. An answer is fanned out to every `external` view (Talk) and is already delivered on the `canonical` one (the row is the delivery). A phone surface owns a room without being a view of it, so nothing written into a phone room ever causes a text; a text goes out only when the task came from the phone or something names `sms` or `whatsapp` explicitly. The phone-room mechanism is described in [SMS](../features/sms.md) and [WhatsApp](../features/whatsapp.md).

A room can be created on any of the five surfaces (`rooms.origin`), and a web room can later be opened in Talk, which adds a Talk binding to the same room.

## Members and participants

Two tables answer two different questions, and they are kept apart on purpose.

- **`room_members`** is sidebar membership: the Istota users who see the room in their web chat list. Web visibility, the delete and leave rules, and the "is this user in the room" checks read it.
- **`room_participants`** is everyone who has written in the room or appears on a surface's roster, Istota user or not. Each row has a `kind`: `principal` (an Istota user who is a member), `guest` (a human who is not, including an Istota user who was never added), or `agent` (a bot, the bot itself included). It is a history, not a set: leaving stamps `left_at`, and coming back inserts a new row, so "present" means `left_at IS NULL`.

The kind is decided in core, by `transport.participants.classify`, because it depends on membership, which no surface knows. Surfaces only report who wrote and who is on the roster: Talk through its participant sync, a WhatsApp group through the sidecar's `group_roster` frame, an email thread as the union of everyone who has been on it, web through member adds and removals.

Every stored turn records its author: `messages.author_user_id` for an Istota user, `messages.author_label` for an outside sender (already sanitized), and `messages.author_participant_id` naming the participant row.

**A room is shared** when more than one distinct human is present (`db.room_is_shared`). One person reading on both Talk and web counts once, bots do not count, and somebody who has left stops counting. `transport.participants.is_multi_human` is the one predicate the speech gate and the classifier share.

## One inbound turn

Every surface normalizes its message into an `IncomingMessage` and hands it to `transport/ingest.record_inbound`. The order is fixed, and past the veto it is **record first, then decide**.

1. The veto is checked. A room that is switched off records nothing and answers nothing: no row, no participant, no gate decision, no task (`dropped`).
2. The turn is stored as a `role='user'` row in the room's transcript, with no task yet. A duplicate is recognised and returns `replayed`.
3. The speech gate decides whether the bot answers.
4. Only if it does is a task created, linked to the stored row.

`record_inbound` returns an `InboundResult` whose `outcome` is `created`, `recorded`, `dropped` or `replayed`. A web send that the gate declines comes back to the browser as `status: "recorded"` with no task id.

Because recording does not depend on answering, the transcript holds turns nobody addressed to the bot. When someone does address it, those turns are in its context. An unanswered turn simply has no assistant row after it.

On Talk this means every human turn is recorded, guests and non-Istota users included; only the bot's own posts are skipped. An unaddressed turn in a group conversation only records, and its side effects (a `!model` prefix, a command, a relay or confirmation answer) apply only when the turn is addressed to the bot or the room has one human in it.

## The speech gate

`rooms/speech_gate.should_speak` is a ladder; the first rung that matches decides.

| Rung | Applies to | Result |
|---|---|---|
| `agent_author` | a turn written by a bot, including our own echo | record only |
| `host_lost` | a room whose host has left | record only |
| `guest_command` | a guest's `!command` (other than the veto) | record only |
| `guest_reply_off` | a guest's turn when the room's `guest_reply` is `off` | record only |
| `loop_cap` | a guest's turn after too many bot replies with no member speaking | record only |
| `not_multi_human` | a room with one human | speak |
| `addressed` | a turn the surface detected as addressed to the bot | speak |
| `mode_off` | `[speech_gate] mode = "off"` | speak |
| `mode_mention` | `mode = "mention"` (the default) | record only |
| `classifier` | `mode = "classifier"` | the model decides; any failure records only |

The agent rung comes first, before an explicit mention, because two bots mentioning each other is the loop it exists to stop. The `addressed` rung comes before any classifier, so a failing classifier can never make the bot unreachable. That is also why the gate can fail closed everywhere else: the cost of a wrong "no" is one retyped name.

What counts as addressed is surface-specific: a Talk @mention, `@name` (not followed by a word character or hyphen) or the bot's name as the first word on web, a mention or a reply to one of the bot's messages or the name as the first word in a WhatsApp group, and on an email thread the intake table in `threads.intake_facts` / `threads.thread_addressed`: the host's own mail asks with the bot's address (or plus address) in To or the bot named in the new text above the quoted history (`@name` anywhere, or the name as the first word of any line); anyone else's mail asks when it names the bot or when none of the host's addresses is on To or Cc, which also marks the task `host_absent`.

The classifier never runs under a database write lock. `ingest.classify_ahead` asks it on its own read connection before `record_inbound` opens its transaction, and passes the answer in on `IncomingMessage.classified`. WhatsApp groups do the same through `groups.classify_group_event`. The window the classifier reads is fenced as untrusted content, and its one-line reason is stored and never put into a prompt.

The room's mode is its own `room_policy.speech_mode` if set, else the deployment's. An email thread room defaults to `mention` even on a classifier deployment, since speaking there is a reply-all. Every decision is written to `speech_gate_decisions` (rung, model, latency, a pointer to the message, never its text) and pruned after `decision_retention_days`. Settings are in [`[speech_gate]`](../configuration/reference.md#speech_gate).

## Host, guests and authority

A room that is shared, or that a guest writes in, gets one `room_policy` row, created the first time it is needed (`rooms.policy.ensure_policy`). It holds:

- **`host_user_id`**: the creator if still present, else the first present member. The host is fixed at creation. If the host leaves, the field is cleared and the room records without answering until a member runs `!room host`. A returning host has to claim it like anyone else. On WhatsApp, a host who leaves makes the bot leave the group.
- **`guest_reply`**: `direct` (answer in the room), `held` (propose the answer to the host privately) or `off` (record only). It defaults by surface: `direct` on Talk and web, `held` on WhatsApp and email, `held` for anything else.
- **`max_bot_turns_without_human`** (default 3): the loop cap for guest turns.
- The veto state and the one-time announcement (below).

**A turn runs with its sender's reach.** A member's turn runs as that member. A guest's turn that the gate lets through runs as the room's host, in emissary mode: `tasks.guest_participant_id` is set, the guest's text is fenced in the prompt as `GUEST MESSAGE` (the transcript keeps it raw), every scope is withheld, the native brain's WebSearch and WebFetch are removed, deferred database operations are discarded, the task gets its own temp directory, nothing is extracted into memory, and any confirmation goes to the host privately. With `guest_reply = held` the answer becomes a proposal the host approves (`rooms.private_replies.propose_guest_reply`).

A guest's commands are ignored apart from the veto. A guest cannot stop, retry or steer a task or answer a confirmation.

## Whose assistant answers

A shared room is not bound to its host's bot. Every per-user input to a task is read for `task.user_id`, and `task.user_id` is the turn's author on a member's turn and the host only on a guest's turn.

| Input | Source | Varies per turn? |
|---|---|---|
| Per-skill overlays | that user's `{bot_dir}/config/skills/`; not loaded when `memory` is withheld (guest and unasked turns) | yes |
| Reach (skills, files, credentials) | that user's, minus the withheld scopes above | yes |
| `USER.md`, recall, knowledge facts, playbooks | not loaded in any shared room | no |
| My notes | the principal's `{bot_dir}/config/rooms/<token>.md` | yes |
| Persona | `load_persona(config)`: the operator's `{root}/PERSONA.md`, else the last good copy, else `config/persona.md` | no |
| Emissaries, guidelines, custom system prompt | `config/`, deployment-wide | no |
| Model, effort, brain | the room's own settings (host-only to change), else the deployment's | no |
| Transcript and `CHANNEL.md` | the room's, bounded by the latest audience epoch | no |

The persona is the one thing in the system half that used to follow the principal, and it no longer does. With a persona per user, a room with two members changed character from one message to the next under the same bot name. There is now one persona per installation, the operator's, and only reach follows the principal. The operator's file sits at the file root, which is bound into no sandbox, so no user's task can write it; the old per-user copies were writable from their owner's sandbox, which is why loading one user's persona into another's turn was never allowed. See [Persona](../configuration/persona.md).

## Audience

Every room turn records who read the room when it was written, in `tasks.audience` (`rooms.policy.audience_class`):

- `private`: one human.
- `principals`: every present human is a member.
- `mixed`: a guest is present.

The audience does not change what a member's own turn may reach. It decides what the prompt says about who is reading, keeps group material out of a mixed room, and keeps personal memory out of any shared room (below).

## What a task may reach

`rooms.scopes.withheld_for_task` is the one derivation of what a task may not reach, read from the task's own row. A **scope** is a skill whose manifest says `shared_room: private` (the default for every skill), plus two synthetic scopes: `files` (the user's workspace and per-resource mounts) and `memory` (`USER.md`, dated and recalled memories, playbooks, knowledge facts, per-skill overlays). A skill marked `shared_room: safe` is never withheld; today that is `room`, `untrusted_input` and `sensitive_actions`.

| Task | Withheld |
|---|---|
| A member's own turn, guest present or not | nothing |
| The user's own cron job or briefing, in a room they are a current member of | nothing |
| A guest's turn | every scope |
| A task nobody asked in a shared room (a subtask, a CLI or heartbeat task, someone else's scheduled job) | every scope |
| Any task in an email thread room, a correspondent's turn included (`rooms.scopes.is_email_thread_room`) | nothing, with ambient memory loaded |

The withheld set is applied at every seam that grants reach: the skill index and selection, the skill proxy's allowed CLIs, setup-env hooks, identity environment, the vault, and the sandbox mount plan. A restricted task gets no workspace bind without `files`, its own temp and deferred-op directory, no Talk bind (attachments are copied in read-only), and Talk writes that check the live roster. The details are in [security](../deployment/security.md) and `.claude/rules/sandbox.md`.

Without bubblewrap (the shipped Docker stack, macOS, the standalone install), withholding removes the scope from the prompt, the skill list and the environment, but the task's own file tools can still reach the host's workspace. `istota doctor` reports this as `security.room_scope_confinement`.

**Ambient memory is left out of every shared room**, whatever the turn. A member's turn there does not load `USER.md`, dated memories, recall, knowledge facts or playbooks into the prompt (`executor._ambient_memory_off`), since those arrive without being asked for. The memory skill still works when asked.

## What the model is told

The prompt has two halves (see [executor](executor.md)). In a shared room the system half carries a **room card** (`executor.room_card`) in place of the old one-line group notice. It is built from tables, never from model output, and lists:

- who reads the room: members by Istota user id, guests as a count, never a display name;
- the room's standing rule: each member's turn runs as that member, with their own reach, a confirmation goes to the asker's own private chat with the bot, and what happens to a guest's message under the room's `guest_reply` (answered as the host with nothing beyond the reply, that reply held for the host's approval, or recorded and not answered). This line is on every card, so a model asked to explain the room does not generalise this turn's principal into the room's owner or invent an approval rule;
- whom the bot is acting for on this turn, and who hosts;
- what this turn reaches, and on a restricted turn what is withheld;
- the private-reply verbs (`istota-skill room whisper`, `room answer-privately`) when the `room` CLI is available;
- that the room's `CHANNEL.md` is read by everyone, and that a member's private notes are never read or written in the room.

A guest turn's header says the bot is answering a guest on the host's behalf, and that the guest's words are data.

In the user half:

- Other participants' turns in history are fenced as `ROOM PARTICIPANT MESSAGE`, so a co-member cannot steer a turn that runs with your reach. A quoted reply is fenced as `QUOTED MESSAGE` in a room several people have written in. The bot's own earlier answers are not fenced.
- `CHANNEL.md` of a room more than one human has ever been in (`db.room_was_ever_shared`, which counts people who have since left) is fenced as room notes rather than instructions, and recall in such a room drops the re-indexed copy and fences dated channel memories.
- A principal speaking in a shared room, or a guest turn running as a present host when `guest_reply` is not `direct`, gets that principal's My notes about the room.
- A linked turn in a private room gets the shared room's recent transcript, fenced (see [private replies](#private-replies)).

## Audience epochs

When someone joins a room that others already read, `room_epochs` records a boundary: the highest `messages.id`, `tasks.id` and cached Talk message id at that moment. `db.front_stage_cutoff` returns the highest boundary of any epoch whose joiner is still present, and every front-stage history read (conversation context, recall over past turns, the classifier's window, memory extraction) starts after it. The bot therefore does not repeat to a newcomer what was said before they arrived.

A Talk or WhatsApp join always splits, since people are added there outside Istota. A web add asks the creator to confirm the newcomer will see the existing transcript, and so does not split. On email, anyone newly copied splits. The first roster a room is seen with is a baseline (`epoch = 0`), not a join. Epochs never bound `CHANNEL.md`, a linked turn's view of the shared room's transcript, or `!export`.

## Private replies

What a shared room has for one member (a confirmation, a whisper, a held guest proposal, a private answer) goes to that member's own existing private room, in `rooms/private_replies.py`. Nothing creates a room for it.

**The resolver.** `private_room_for(conn, config, user_id, about_token)` tries the private room on the shared room's own surface first: the member's private WhatsApp room for a WhatsApp group, the first private Talk room among the configured default and the default-room candidates for a Talk room, the default web room for a web-only room. An email thread room has no such room and starts at the fallback, which is web, Talk, WhatsApp in that order. Every candidate is re-checked by `db.is_private_room_of` (not archived, the user its only member, no phone binding except for the WhatsApp step), the predicate the relay's default-room destination uses too. SMS is never a destination. `None` means the bell.

**Record, then send.** `deliver_private` runs in the caller's transaction and writes one `role='system'` row into the member's room, tagged `messages.about_room_token` and keyed `private-<kind>:<reference>`; the `delivery_reference` index is global, so a retry returns the first row. `send_private` runs after commit with its own connections and never raises: a Talk post after the live audience check, with the Talk id stamped on the row so a Talk reply resolves to it; a WhatsApp send keyed `private-reply:<messages.id>`; for an email-thread parent, a heads-up mail to the member's own address saying where to answer. Talk and WhatsApp bodies start `re: <room>`; web renders a chip from `about_room_token` instead.

**No private room.** Nothing is written into any room. A confirmation or proposal is left to its `confirmation` bell row, which is then delivered rather than withheld, and a whisper becomes a `task_alert` bell row. The shared room gets one fixed line naming nobody. The scheduler marks every privately routed park, room or bell, with `tasks.private_park`, which is what keeps the park from holding the shared room's dispatch gate and from being cancelled by the asker's next message there.

**Linking.** `tasks.about_room_token` is set by `transport.ingest.record_inbound` when the new turn replies to or quotes a tagged row in the same room, on every surface that supplies a parent: web's reply-to, Talk's parent message through the stamped external id, and a WhatsApp quote resolved back through the `private-reply:` ledger key, or through `task-result:` for a linked turn's answer, which the scheduler tags with the same link. `room answer-privately` sets it directly on the question it asks again. Nothing carries a link to the next turn. A linked task, while its user is still a current member of the room, gets one system line naming the room by token only and the room's last 40 messages (up to 12,000 characters) fenced as `PARENT ROOM TRANSCRIPT` in the user half; `pin_plan` drops any delivery leg into the linked room, so its answer stays private.

**The verbs.** `room whisper` queues at once, since it reaches only the principal, and resolves the destination at claim time. `room answer-privately` records the principal's own message again as their turn in their private room, under that room's own model defaults, and refuses `no_private_room` when there is none. `room post` runs from a private room into the linked room or a `--room` named through `rooms.lookup.resolve_room`, held for approval of the exact text through the request table and approval machinery relay questions use (`whatsapp_skill_requests`, see `.claude/rules/relay.md`); it skips approval only when the text is the member's own words and posting was the task's first call, and a switched-off room refuses it.

**Confirmations.** A bare answer in a private room, after nothing parked in that room itself, resolves the user's one task whose private question is in that room; with more than one it asks for `!confirm <id> yes|no`. A bare yes in a shared room resolves nothing.

**My notes.** A member's private notes about a shared room are `{bot_dir}/config/rooms/<canonical token>.md` in their workspace, inside the memory refusals with no new entry (`storage.room_notes_path`, `read_room_notes`, which falls back to the room's live aliases, `write_room_notes`). `private_replies.my_notes_room` loads them into the user half on the member's own speaking turn in the room (Talk, web, WhatsApp, or email when the stored turn is theirs), and on a guest turn for the host unless `guest_reply` is `direct`. They are edited from the web pane, `memory --room` from a private room, and read with `!room notes`, which is refused in every shared room. Deleting a room deletes every member's notes for it.

## The veto

`rooms/veto.py` handles `!<bot name> off` and `on`. Any participant can switch the bot off; it then records nothing in the room, cancels queued tasks there and drops a running task's answer. It comes back on when a member has sent `on` and every person who switched it off has agreed or left. Each vetoer is a row in `room_vetoes`. Removing the bot from a WhatsApp group is a veto with no named vetoer.

The web process cannot post into Talk or a WhatsApp group itself, so it writes owed replies to `room_notices`, and the scheduler's `room-notices` gate posts them, along with the one-time announcement made the first time a guest is present in a room with a host. The announcement is a persona-voiced opening, overridable by the operator with `config/room-announcement.md`, followed by two fixed sentences that always render: what happens to a guest's message under the room's `guest_reply`, and the off switch with the way back. An email thread room is never announced. When the operator turns on `[email] thread_disclosure_footer`, every mail the bot sends into a thread carries one plain disclosure line instead (`rooms.veto.with_email_footer`), so nothing tracks whether the thread was told.

## Delivery never targets a shared room

A shared room is never a destination for personal content. `routing.refuse_shared_rooms` drops any Talk or web leg whose room is shared, for every delivery purpose, exempting only the room the task itself ran in (so the conversational reply still lands where you asked). Briefings get no exemption. A default room that becomes shared stops being anyone's default (`db._usable_as_delivery_default`), and the settings pages refuse to save one. A mirror-only email turn is not recorded into a shared or vetoed room.

Personal memory is not extracted from shared rooms: a guest turn is extracted into nobody's memory, and what is said in front of more than one person belongs in the room's `CHANNEL.md`.

## Room containers on WhatsApp and email

Two surfaces bring rooms that are not created by a member turn.

- **WhatsApp groups** (Baileys only) are registered from the roster frame the sidecar sends before the first message, by `transport/whatsapp/groups.py`. The token is `whatsapp-group-` plus a hash of the group JID, never the JID itself. The host is whoever added the bot. A group with no Istota member is not registered. See [WhatsApp groups](../features/whatsapp.md#groups).
- **Email thread rooms** (`transport/email/threads.py`) are minted at the send for a thread the bot starts (`threads.register_sent_thread`, called from every place a sent mail is recorded: the task result, the `email` skill's direct and deferred records, a released draft) whenever a recipient is not the user's own address, with the sent mail as the room's first row. A received thread mints when it has two or more humans besides the bot, belongs to a user, and passed the untrusted-sender gate, or with one human when it reached the bot at that user's plus address (stranger first contact, `resolve_thread(plus_address=True)`); a held mail mints at approval instead (`threads.admit_approved_mail`, called by `confirmations.approve`); a reply on a thread sent before minting at send existed mints there, before its prompt is built, with the `sent_emails` row as the first row. The token is `email-thread-` plus a hash of the root Message-ID. The host is the room's only member; the web refuses to add anyone else, since another Istota user on the thread is a correspondent. Every admitted turn runs as the host at full reach: `record_inbound` skips the guest branch for an email-bound room (no `guest_participant_id`, no guest fence; the sender is still a guest participant and the row's author), `withheld_for_task` returns nothing and `ambient_memory_off` answers False, and `guest_reply_mode` answers `direct`. The room card is the email one (`executor._email_room_card`). A `host_absent` turn that answers `NO_ACTION:` sends nothing and writes one `pass_on` private reply to the host, built from the stored turn (`private_replies.deliver_pass_on`). The answer is a reply-all that goes through the outbound approval gate; a held one is a draft. `room post` and guest proposals refuse an email-bound room (`private_replies._post_destination`, `email_thread`), so mail waiting for approval is always a draft. The host's own addressed question, from a mail that passed DMARC, is answered without the gate while the reply goes to exactly that mail's people (`processed_emails.host_asked`, `threads.host_asked`). The D9 loop cap is not applied on an email thread room (`ingest._ask_policy`).

Mail between the user and the bot alone is not a thread: it is the user's **private email room**, bound `surface='email'`, `surface_ref=email_conversation_token(user)` (`transport/email/private_room.py`), minted through `ingest.record_phone_turn` like an SMS room. Email stays a `guest` surface in `rooms.surfaces`, so the private room takes the container path. An email binding is a thread only when its ref is not the creator's private token (`threads.thread_binding`, `scopes.is_email_thread_room`), so no message threads into the private room and its turns are the user's own. `routing.phone_room` and `phone_transcript_surface` answer `email` for it, which makes it read-only in web. See [your private email room](../features/email.md#your-private-email-room).

## Tables

| Table | Role here |
|---|---|
| `rooms` | One row per room; `group_id` for a group link |
| `room_bindings` | Which surfaces expose the room, and each surface's own ref |
| `room_members` | Sidebar membership |
| `room_participants` | Everyone seen in the room, with kind and join/leave history |
| `messages` | The transcript, with author columns; `about_room_token` tags a private reply with the shared room it is about |
| `room_policy` | Host, `guest_reply`, speech mode, loop cap, veto and announcement state |
| `room_vetoes` | Who switched the bot off, and who has agreed to switch it back on |
| `room_notices` | Replies owed to Talk or WhatsApp, posted by the scheduler |
| `room_epochs` | Audience boundaries |
| `speech_gate_decisions` | The gate's audit log |
| `tasks.audience`, `tasks.guest_participant_id`, `tasks.is_group_chat` | Per-task record of who read the room and whose turn it answered |
| `tasks.about_room_token`, `tasks.private_park` | A turn linked to a shared room; a park whose question was asked privately |

Column-level detail is in [database](database.md) and `schema.sql`.

## Where the code lives

| Module | Owns |
|---|---|
| `rooms/surfaces.py` | The per-surface room facts |
| `transport/ingest.py` | `record_inbound`, `classify_ahead`, phone-room recording |
| `transport/participants.py` | Participant kind and the multi-human predicate |
| `rooms/speech_gate.py` | The speech gate ladder and the classifier call |
| `rooms/policy.py` | Host, `guest_reply`, audience class, room readers |
| `rooms/scopes.py` | What a task withholds, ambient memory, a task's groups |
| `rooms/private_replies.py` | The private-room resolver, private delivery and the bell fallback, linking, whispers, private answers, guest proposals, held posts, My notes loading |
| `rooms/lookup.py` | Resolving a room a person named |
| `rooms/veto.py` | The veto, notices and the announcement |
| `executor.py` | The room card, fencing, ambient memory in prompt assembly |
| `transport/routing.py` | Refusing shared rooms as delivery destinations |
| `transport/whatsapp/groups.py`, `transport/email/threads.py` | The two room containers |

The internals, including the reasoning behind each rule and the residuals, are in `.claude/rules/transport.md` ("Multiplayer rooms"), `.claude/rules/prompts.md` and `.claude/rules/sandbox.md`.
