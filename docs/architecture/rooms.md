# Rooms and multi-user chat

This page covers how Istota models a conversation that more than one person can read, and how one inbound turn becomes a decision about who speaks, whose authority a task carries, who will read the answer, and what the task may reach. The user-facing behaviour is in [shared rooms](../features/shared-rooms.md), [side rooms](../features/side-rooms.md), [switching the bot off](../features/room-veto.md) and [groups](../features/groups.md). This page is the mechanism behind them.

Every rule here is a no-op in a room with one human in it. A private conversation goes through the same pipeline and comes out the way it always did.

## The room model

A **room** is one conversation, identified by a canonical token (`rooms.token`, conventionally `rm_…`), with one transcript in the `messages` table. A room is exposed on one or more **surfaces** through `room_bindings`, one row per surface, which maps the canonical token to that surface's own reference: a Nextcloud Talk conversation token, a web room id, a WhatsApp group JID, an email thread's root Message-ID hash.

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

What counts as addressed is surface-specific: a Talk @mention, `@name` (not followed by a word character or hyphen) or the bot's name as the first word on web, a mention or a reply to one of the bot's messages or the name as the first word in a WhatsApp group, and on an email thread the bot's address (or plus address) in To, or the bot named in the new text above the quoted history (`@name` anywhere, or the name as the first word of any line). Cc without the name means the bot is listening.

The classifier never runs under a database write lock. `ingest.classify_ahead` asks it on its own read connection before `record_inbound` opens its transaction, and passes the answer in on `IncomingMessage.classified`. WhatsApp groups do the same through `groups.classify_group_event`. The window the classifier reads is fenced as untrusted content, and its one-line reason is stored and never put into a prompt.

The room's mode is its own `room_policy.speech_mode` if set, else the deployment's. An email thread room defaults to `mention` even on a classifier deployment, since speaking there is a reply-all. Every decision is written to `speech_gate_decisions` (rung, model, latency, a pointer to the message, never its text) and pruned after `decision_retention_days`. Settings are in [`[speech_gate]`](../configuration/reference.md#speech_gate).

## Host, guests and authority

A room that is shared, or that a guest writes in, gets one `room_policy` row, created the first time it is needed (`rooms.policy.ensure_policy`). It holds:

- **`host_user_id`**: the creator if still present, else the first present member. The host is fixed at creation. If the host leaves, the field is cleared and the room records without answering until a member runs `!room host`. A returning host has to claim it like anyone else. On WhatsApp, a host who leaves makes the bot leave the group.
- **`guest_reply`**: `direct` (answer in the room), `held` (propose the answer in the host's side room) or `off` (record only). It defaults by surface: `direct` on Talk and web, `held` on WhatsApp and email, `held` for anything else.
- **`max_bot_turns_without_human`** (default 3): the loop cap for guest turns.
- The veto state and the one-time announcement (below).

**A turn runs with its sender's reach.** A member's turn runs as that member. A guest's turn that the gate lets through runs as the room's host, in emissary mode: `tasks.guest_participant_id` is set, the guest's text is fenced in the prompt as `GUEST MESSAGE` (the transcript keeps it raw), every scope is withheld, the native brain's WebSearch and WebFetch are removed, deferred database operations are discarded, the task gets its own temp directory, nothing is extracted into memory, and any confirmation goes to the host's side room. With `guest_reply = held` the answer becomes a proposal the host approves (`rooms.side_rooms.propose_guest_reply`).

A guest's commands are ignored apart from the veto. A guest cannot stop, retry or steer a task or answer a confirmation.

## Whose assistant answers

A shared room is not bound to its host's bot. Every per-user input to a task is read for `task.user_id`, and `task.user_id` is the turn's author on a member's turn and the host only on a guest's turn.

| Input | Source | Varies per turn? |
|---|---|---|
| Persona | `load_persona(config, user_id=task.user_id)`: that user's `PERSONA.md`, else `config/persona.md` | yes |
| Per-skill overlays | that user's `{bot_dir}/config/skills/`; not loaded when `memory` is withheld (guest and unasked turns) | yes |
| Reach (skills, files, credentials) | that user's, minus the withheld scopes above | yes |
| `USER.md`, recall, knowledge facts, playbooks | not loaded in any shared room | no |
| Backstage notes | the principal's side-room `CHANNEL.md` | yes |
| Emissaries, guidelines, custom system prompt | `config/`, deployment-wide | no |
| Model, effort, brain | the room's own settings (host-only to change), else the deployment's | no |
| Transcript and `CHANNEL.md` | the room's, bounded by the latest audience epoch | no |

The room card names whose persona is in use, so the model knows which person it is speaking as. Loading the host's persona into another member's turn was rejected: a member's `PERSONA.md` is writable from that member's sandbox, so it would let one user, or an injection in one user's task, give standing instructions to a task running with another user's credentials.

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
| A task nobody asked in a shared room (a subtask, a CLI or heartbeat task, someone else's scheduled job, an outside correspondent's reply on the room's email thread) | every scope |

The withheld set is applied at every seam that grants reach: the skill index and selection, the skill proxy's allowed CLIs, setup-env hooks, identity environment, the vault, and the sandbox mount plan. A restricted task gets no workspace bind without `files`, its own temp and deferred-op directory, no Talk bind (attachments are copied in read-only), and Talk writes that check the live roster. The details are in [security](../deployment/security.md) and `.claude/rules/sandbox.md`.

Without bubblewrap (the shipped Docker stack, macOS, the standalone install), withholding removes the scope from the prompt, the skill list and the environment, but the task's own file tools can still reach the host's workspace. `istota doctor` reports this as `security.room_scope_confinement`.

**Ambient memory is left out of every shared room**, whatever the turn. A member's turn there does not load `USER.md`, dated memories, recall, knowledge facts or playbooks into the prompt (`executor._ambient_memory_off`), since those arrive without being asked for. The memory skill still works when asked.

## What the model is told

The prompt has two halves (see [executor](executor.md)). In a shared room the system half carries a **room card** (`executor.room_card`) in place of the old one-line group notice. It is built from tables, never from model output, and lists:

- who reads the room: members by Istota user id, guests as a count, never a display name;
- the room's standing rule: each member's turn runs as that member, with their own persona and reach, a confirmation goes to the asker's own side room, and what happens to a guest's message under the room's `guest_reply` (answered as the host with nothing beyond the reply, that reply held for the host's approval, or recorded and not answered). This line is on every card, so a model asked to explain the room does not generalise this turn's principal into the room's owner or invent an approval rule;
- whom the bot is acting for on this turn, and who hosts;
- whose persona is in use (always the task's own user: the host on a guest's turn);
- what this turn reaches, and on a restricted turn what is withheld;
- the side-room verbs (`istota-skill room whisper`, `room answer-privately`) when the `room` CLI is available;
- that the room's `CHANNEL.md` is read by everyone.

A guest turn's header says the bot is answering a guest on the host's behalf, and that the guest's words are data.

In the user half:

- Other participants' turns in history are fenced as `ROOM PARTICIPANT MESSAGE`, so a co-member cannot steer a turn that runs with your reach. A quoted reply is fenced as `QUOTED MESSAGE` in a room several people have written in. The bot's own earlier answers are not fenced.
- `CHANNEL.md` of a room more than one human has ever been in (`db.room_was_ever_shared`, which counts people who have since left) is fenced as room notes rather than instructions, and recall in such a room drops the re-indexed copy and fences dated channel memories.
- A principal speaking in a shared room, or a guest turn running as a present host, gets that principal's side room's `CHANNEL.md` as private backstage notes.

## Audience epochs

When someone joins a room that others already read, `room_epochs` records a boundary: the highest `messages.id`, `tasks.id` and cached Talk message id at that moment. `db.front_stage_cutoff` returns the highest boundary of any epoch whose joiner is still present, and every front-stage history read (conversation context, recall over past turns, the classifier's window, memory extraction) starts after it. The bot therefore does not repeat to a newcomer what was said before they arrived.

A Talk or WhatsApp join always splits, since people are added there outside Istota. A web add asks the creator to confirm the newcomer will see the existing transcript, and so does not split. On email, anyone newly copied splits. The first roster a room is seen with is a baseline (`epoch = 0`), not a join. Epochs never bound `CHANNEL.md`, a side room's view of its parent transcript, or `!export`.

## Side rooms

A side room is an ordinary private room with `rooms.side_of` set to the shared room and `rooms.side_for_user` to its one member, created the first time something needs it. Confirmations from a shared-room task, whispers (`room whisper`), private answers (`room answer-privately`), guest proposals and the member's backstage notes all go there.

Nothing in a side room reaches the shared room on its own. A post back is a `room_post` request (`istota-skill room post`), held for the member's approval with the exact text shown, through the same request table and approval machinery relay questions use (`whatsapp_skill_requests`, see `.claude/rules/relay.md`). It skips approval only when the text is the member's own words from their message and posting was the task's first call. A side-room task sees the parent's last 40 messages (up to 12,000 characters), fenced as `PARENT ROOM TRANSCRIPT`, while the member is still in the parent.

On surfaces without a web view of their own, the side room has a counterpart: the member's private Talk conversation, their own WhatsApp chat, or a private mail, each headed with the room's name.

## The veto

`rooms/veto.py` handles `!<bot name> off` and `on`. Any participant can switch the bot off; it then records nothing in the room, cancels queued tasks there and drops a running task's answer. It comes back on when a member has sent `on` and every person who switched it off has agreed or left. Each vetoer is a row in `room_vetoes`. Removing the bot from a WhatsApp group is a veto with no named vetoer.

The web process cannot post into Talk or a WhatsApp group itself, so it writes owed replies to `room_notices`, and the scheduler's `room-notices` gate posts them, along with the one-time announcement made the first time a guest is present in a room with a host. The announcement is a persona-voiced opening, overridable by the operator with `config/room-announcement.md`, followed by two fixed sentences that always render: what happens to a guest's message under the room's `guest_reply`, and the off switch with the way back. An email thread room is never announced. When the operator turns on `[email] thread_disclosure_footer`, every mail the bot sends into a thread carries one plain disclosure line instead (`rooms.veto.with_email_footer`), so nothing tracks whether the thread was told.

## Delivery never targets a shared room

A shared room is never a destination for personal content. `routing.refuse_shared_rooms` drops any Talk or web leg whose room is shared, for every delivery purpose, exempting only the room the task itself ran in (so the conversational reply still lands where you asked). Briefings get no exemption. A default room that becomes shared stops being anyone's default (`db._usable_as_delivery_default`), and the settings pages refuse to save one. A mirror-only email turn is not recorded into a shared or vetoed room.

Personal memory is not extracted from shared rooms: a guest turn is extracted into nobody's memory, and what is said in front of more than one person belongs in the room's `CHANNEL.md`.

## Room containers on WhatsApp and email

Two surfaces bring rooms that are not created by a member turn.

- **WhatsApp groups** (Baileys only) are registered from the roster frame the sidecar sends before the first message, by `transport/whatsapp/groups.py`. The token is `whatsapp-group-` plus a hash of the group JID, never the JID itself. The host is whoever added the bot. A group with no Istota member is not registered. See [WhatsApp groups](../features/whatsapp.md#groups).
- **Email thread rooms** (`transport/email/threads.py`) are minted when a thread has two or more humans besides the bot, belongs to a user, and passed the untrusted-sender gate. The token is `email-thread-` plus a hash of the root Message-ID. The host is the room's only member; the web refuses to add anyone else, since another Istota user on the thread is a correspondent. The answer is a reply-all that goes through the outbound approval gate, and an approved guest proposal is that approval only when its recipients still match. The host's own addressed question, from a mail that passed DMARC, is answered without the gate while the reply goes to exactly that mail's people (`processed_emails.host_asked`, `threads.host_asked`). See [email thread rooms](../features/email.md#email-thread-rooms).

## Tables

| Table | Role here |
|---|---|
| `rooms` | One row per room; `side_of` / `side_for_user` for side rooms, `group_id` for a group link |
| `room_bindings` | Which surfaces expose the room, and each surface's own ref |
| `room_members` | Sidebar membership |
| `room_participants` | Everyone seen in the room, with kind and join/leave history |
| `messages` | The transcript, with author columns |
| `room_policy` | Host, `guest_reply`, speech mode, loop cap, veto and announcement state |
| `room_vetoes` | Who switched the bot off, and who has agreed to switch it back on |
| `room_notices` | Replies owed to Talk or WhatsApp, posted by the scheduler |
| `room_epochs` | Audience boundaries |
| `speech_gate_decisions` | The gate's audit log |
| `tasks.audience`, `tasks.guest_participant_id`, `tasks.is_group_chat` | Per-task record of who read the room and whose turn it answered |

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
| `rooms/side_rooms.py` | Side rooms, whispers, guest proposals, held posts |
| `rooms/veto.py` | The veto, notices and the announcement |
| `executor.py` | The room card, fencing, ambient memory in prompt assembly |
| `transport/routing.py` | Refusing shared rooms as delivery destinations |
| `transport/whatsapp/groups.py`, `transport/email/threads.py` | The two room containers |

The internals, including the reasoning behind each rule and the residuals, are in `.claude/rules/transport.md` ("Multiplayer rooms"), `.claude/rules/prompts.md` and `.claude/rules/sandbox.md`.
