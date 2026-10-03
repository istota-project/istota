# Shared rooms

A room can hold more than one person: a Nextcloud Talk group conversation, a web chat room you have added someone to, a WhatsApp group on the Baileys adapter, or an email thread with two or more people besides the bot. Istota treats all four the same way. It records every turn, decides whether a turn is for it, works out whose authority the turn carries and who will read the answer, and limits what the task can read to what that audience may see.

A room with one person in it behaves exactly as it always did. Nothing on this page applies to a private conversation.

Related pages: [side rooms](side-rooms.md) for the private channel each member has beside a shared room, [switching the bot off](room-veto.md) for the veto, [WhatsApp groups](whatsapp.md#groups), [email thread rooms](email.md#email-thread-rooms), and [groups](groups.md) for a group's shared memory. How it works underneath is in [rooms and multi-user chat](../architecture/rooms.md).

## Who is in a room

Everyone who writes in a room, or appears on its roster, is a **participant**. There are three kinds:

- **Members** are Istota users who belong to the room and see it in their web sidebar. A member speaking is a *principal*: the bot works for them, with their data and their permissions.
- **Guests** are everyone else: a Talk guest, a Nextcloud user who is not an Istota user, a phone number in a WhatsApp group, a correspondent on an email thread. An Istota user who has not been added to the room is a guest there too.
- **Agents** are bots, including Istota itself. Their turns are recorded and never answered, which stops two bots answering each other in a loop.

Every turn is stored in the room's transcript with its author, guests' turns included. Before this, an unmentioned message in a Talk group was never stored, and a non-user's message was dropped.

A room is **shared** when more than one human is present. One person reading the room on both Talk and web counts once. Bots do not count, and somebody who has left stops counting.

## Whose assistant answers

A shared room has no single owner's bot. Each turn is answered by the assistant of the person the turn runs as, which is the person who wrote it, except on a guest's turn:

| Who wrote the turn | Runs as | Persona | Reach | Personal memory in the prompt |
|---|---|---|---|---|
| A member | that member | theirs (`PERSONA.md`) | everything they can reach in their private room | none |
| A guest | the host | the host's | nothing of the host's | none |
| Nobody in the room (a subtask, someone else's scheduled job) | the task's user | that user's | nothing | none |
| A member's own scheduled job or briefing posting here | that member | theirs | everything they can reach | none |

So if Alice and Bob share a room and each has their own `PERSONA.md`, Alice's questions are answered in Alice's persona with Alice's calendar, and Bob's in Bob's with his. Neither sees the other's data unless it was written into the room.

Some things are the same whatever the turn:

- **The deployment's own rules**: the emissary principles, the response guidelines and any custom system prompt are set by the operator for everyone.
- **The room's settings**: its model, effort and brain apply to every turn in the room, and only the host can change them.
- **The room's shared context**: the transcript (from the latest join onwards, see [newcomers and history](#newcomers-and-history)) and the room's `CHANNEL.md`, which everyone in the room can read and write.

What is per person, beyond persona and reach: a member's per-skill instructions (`{bot_dir}/config/skills/<skill>.md`) load on their own turns and not on a guest's, and a member's [side room](side-rooms.md) notes are read on their own turns, and on a guest's turn when they are the host.

## When the bot speaks

In a shared room the bot does not answer every message. The **speech gate** decides, using `[speech_gate] mode`:

- `mention` (the default): the bot answers a turn that addresses it and records the rest. On Talk that is an @mention; on web it is `@name` anywhere or the bot's name as the first word; on WhatsApp it is a mention, a reply to one of the bot's messages, or the name as the first word; on email it is the bot's address in To. Cc means the bot is listening and does not reply.
- `classifier`: a small, cheap model reads the last few turns and decides whether the latest one is meant for the bot. If the model fails or times out, the bot stays quiet. A turn that addresses the bot is always answered, whatever the model says. An email thread room stays on `mention` even then, because speaking there is a reply-all.
- `off`: the bot answers every turn.

The unanswered turns still reach the bot as context, so when somebody does address it, it knows what was said. Every decision is logged in the `speech_gate_decisions` table for tuning; the log holds no message text. See [`[speech_gate]`](../configuration/reference.md#speech_gate).

## Hosts and guests

Every shared room has a **host**: on web, the person who created it; on Talk, the first Istota member the bot saw there; on WhatsApp, the person who added the bot's number; on email, the person whose thread it is.

A guest's message never runs on the guest's authority. When the bot answers a guest, it acts **for the host**, as the host's emissary. The guest's words are passed to the model as data, not as instructions, and the task:

- reads nothing private of the host's, whatever the host has shared in that room,
- takes no action beyond its reply (no calendar write, no email, no web fetch on the native brain),
- sends anything else, including any question that needs approval, to the host's [side room](side-rooms.md).

How a guest is answered is the room's `guest_reply` setting, which the host changes with `!room guests <off|held|direct>`:

| Setting | What happens to a guest's message |
|---|---|
| `direct` | The bot answers in the room. Default on Talk and web. |
| `held` | The answer goes to the host's side room as a proposal, with the guest's words and the exact reply. The host approves it, and only then is it posted. Default on WhatsApp and email. |
| `off` | The guest's message is recorded and not answered. |

A guest's `!commands` are ignored, apart from switching the bot off. A guest cannot stop, retry or steer anyone's task, or answer a confirmation.

If the host leaves, the room goes quiet: turns are recorded and nobody is answered until a member runs `!room host` to take over. Nobody becomes host automatically. On WhatsApp the bot leaves the group instead.

After three of its own replies in a row with no member speaking, the bot stops answering guests until a member writes again, so a guest's autoresponder cannot keep it talking. The cap applies to guests only and never holds back a member's turn. Another bot is never answered at all.

## What a task can reach

A turn runs with its sender's reach. When you ask the bot something in a shared room, it can use everything it can use in your private room: your calendar, email, files, health data and the rest. Asking in a room you know others read is the decision that the answer can be read there, so there is nothing to switch on first. If the answer should stay private, ask in your private room, or ask the bot to answer you privately: it asks your question again in your side room (`istota-skill room answer-privately`) and tells the room it has answered you there.

This holds with a guest in the room too. Having the guest there, and asking in front of them, is your choice. The room card tells the bot that a guest is reading.

One thing is left out. In a shared room the bot does not load your personal memory into the prompt: `USER.md`, dated memories, recalled memories, remembered facts and playbooks. Those reach a private prompt without you asking for them, so a question about lunch could otherwise come back with something from your health notes. The memory files are still there, so "what did I note about X" works when you ask for it.

Two kinds of task run with less:

- **A guest's turn** runs as the room's host and reaches nothing of the host's: no workspace, no private skill, no memory. Only skills that read nothing personal (the room's own tools, untrusted-input handling) and the room's `CHANNEL.md` are available.
- **A task nobody asked in the room** is restricted the same way, since its answer lands in the room with no member asking there: a subtask whose conversation is a shared room, a scheduled job or briefing aimed at a shared room you are not a member of, or an outside correspondent's reply to an email the bot sent from that room.

Your own scheduled jobs (`CRON.md`) and briefings that post into a shared room you are a member of run as your turn there does: setting the job to post in that room is the same decision as asking there. They leave your personal memory out of the prompt in the same way.

Other participants' messages in the conversation history are shown to the model as content from someone else, not as instructions, so a co-member cannot use the transcript to steer your turn.

On a deployment with no bubblewrap sandbox (the shipped Docker stack, macOS, the standalone install), what a guest's turn loses is removed from the prompt, the skill list and the environment, but the task's own tools can still read and write the host's files on disk, including `CRON.md`, whose jobs run as the host. `istota doctor` warns about this. See [security](../deployment/security.md).

## What the bot is told

In a shared room the system prompt carries a short room card: who reads the room (members by user id, guests by count), whom the bot is acting for and who hosts, whose persona is in use, what this turn can reach (and on a guest's turn, what is withheld), and that `CHANNEL.md` is read by everyone. It never contains anybody's display name, since that is text the person chose.

The persona is always that of the person the task acts for: the host's on the host's turns and on guests' turns, each other member's own on theirs.

A shared room's `CHANNEL.md` is written by several people, so it is shown to the model marked as notes from the room rather than as instructions. That stays true after the room becomes private again.

## Newcomers and history

When someone joins a room that others already read, the bot stops drawing on the conversation from before they joined. Its context, its search of past turns and the classifier's window all start at the join, so the bot does not repeat to a newcomer what was said before they arrived. A member's own side room still sees the whole history.

On the web, adding a member asks you to confirm that they will see the existing transcript, so a web add does not narrow the bot's context. On Talk and WhatsApp a join always narrows it, since people are added there outside Istota. On email, anyone newly copied on a thread narrows it.

This bounds the transcript, not the room's `CHANNEL.md`, which everyone in the room can read and edit. If a note should not reach a newcomer, edit the note.

## Where personal deliveries go

A shared room is never a destination for your personal content. Briefings, alerts, the activity log, and any route you set are refused if they name a shared room, and the settings pages refuse to save one. A default room that becomes shared stops being your default, and Istota picks or creates a private room instead. A conversational reply still lands in the room where you asked.

Personal memory is not extracted from shared rooms: what is said in front of more than one person goes into the room's `CHANNEL.md` and into nobody's `USER.md`.

## Who can change what

- **Membership** of a web room: only the creator adds or removes members. Any other member can leave; the creator deletes the room instead. A room bound to Talk takes its membership from Talk.
- **Room settings** that affect everyone (name, model, effort, brain, opening it in Talk, the guest setting, the group link): the host only. Other members see them read-only.
- **Colour and hiding** stay per member.
- **`CHANNEL.md`**: any member.
- **Deleting a message**: only its author, in a shared room.
- **Tasks**: you can stop, steer, retry and confirm only your own. A confirmation asked by a task in a shared room is sent to the asker's side room, and a plain "yes" typed in the shared room no longer answers it.

## Commands

| Command | Who | What it does |
|---|---|---|
| `!room host` | any member | Take over a room that has lost its host |
| `!room guests [off\|held\|direct]` | host to change | Show or set how guests are answered |
| `!room group [<id>\|none]` | host to change | Show or set the room's [group](groups.md#linking-a-room-to-a-group) link |
| `!<bot name> off` / `on` | anyone | [Switch the bot off](room-veto.md) in this room, or ask for it back |

The full list is in the [command reference](../reference/commands.md).
