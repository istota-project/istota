# Shared rooms

A room can hold more than one person: a Nextcloud Talk group conversation, a web chat room you have added someone to, a WhatsApp group on the Baileys adapter, or an email thread with two or more people besides the bot. Istota treats all four the same way. It records every turn, decides whether a turn is for it, works out whose authority the turn carries and who will read the answer, and limits what the task can read to what that audience may see.

A room with one person in it behaves exactly as it always did. Nothing on this page applies to a private conversation.

Related pages: [side rooms](side-rooms.md) for the private channel each member has beside a shared room, [switching the bot off](room-veto.md) for the veto, [WhatsApp groups](whatsapp.md#groups), [email thread rooms](email.md#email-thread-rooms), and [groups](groups.md) for a group's shared memory.

## Who is in a room

Everyone who writes in a room, or appears on its roster, is a **participant**. There are three kinds:

- **Members** are Istota users who belong to the room and see it in their web sidebar. A member speaking is a *principal*: the bot works for them, with their data and their permissions.
- **Guests** are everyone else: a Talk guest, a Nextcloud user who is not an Istota user, a phone number in a WhatsApp group, a correspondent on an email thread. An Istota user who has not been added to the room is a guest there too.
- **Agents** are bots, including Istota itself. Their turns are recorded and never answered, which stops two bots answering each other in a loop.

Every turn is stored in the room's transcript with its author, guests' turns included. Before this, an unmentioned message in a Talk group was never stored, and a non-user's message was dropped.

A room is **shared** when more than one human is present. One person reading the room on both Talk and web counts once. Bots do not count, and somebody who has left stops counting.

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

The bot also stops after three of its own replies in a row with no member speaking, so it cannot be kept talking by another bot or an autoresponder.

## What a task can reach

A shared room is read by everyone in it, so an answer there is a disclosure to all of them. Istota controls this by what the task can reach, not by asking the model to be careful.

Your private data comes in **scopes**: each skill that reads something of yours (calendar, email, health, location, and so on) is one, and two more cover your files and your memory:

- `files` is your Nextcloud workspace. Without it, the folder is not mounted into the task's sandbox at all.
- `memory` is your `USER.md`, dated memories, playbooks and remembered facts. It is separate from `files`: with `files` shared and `memory` not, the memory folders are hidden inside the workspace.

In a shared room every scope is withheld until **you** share it there:

```
!room share                # list what you share here and what is withheld
!room share calendar       # share one scope, for your own turns, in this room
!room unshare calendar     # take it back
!room share all            # share everything
!room share none           # share nothing
```

The same toggles are under the room's settings on the web. A share is yours alone: it applies only to your own turns, nobody can share on your behalf, and it covers only this room. Skills that read nothing personal (the room's own tools, untrusted-input handling) are always available, and so is the room's own `CHANNEL.md`.

**While a guest is present, shares are ignored.** Every scope is withheld from a member's turn too, because the answer would reach the guest. If you need something private while a guest is present, the bot can answer it in your side room instead (`istota-skill room answer-privately`); it tells the room it has answered you privately.

`[rooms] shared_room_data_policy = "off"` switches the share rule off for members, so a member's turn in a shared room reaches everything a private one does. A guest's turn stays withheld under either setting. See [`[rooms]`](../configuration/reference.md#rooms).

On a deployment with no bubblewrap sandbox (the shipped Docker stack, macOS, the standalone install), withheld scopes are removed from the prompt, the skill list and the environment, but the task's own tools can still read the files on disk. `istota doctor` warns about this. See [security](../deployment/security.md).

## What the bot is told

In a shared room the system prompt carries a short room card: who reads the room (members by user id, guests by count), whom the bot is acting for and who hosts, whose persona is in use, what is withheld from this turn and how to share it, and that `CHANNEL.md` is read by everyone. It never contains anybody's display name, since that is text the person chose.

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
| `!room share [<scope>\|all\|none]` | any member | Show or change what you share in this room |
| `!room unshare <scope>\|all` | any member | Withdraw a share |
| `!room host` | any member | Take over a room that has lost its host |
| `!room guests [off\|held\|direct]` | host to change | Show or set how guests are answered |
| `!room group [<id>\|none]` | host to change | Show or set the room's [group](groups.md#linking-a-room-to-a-group) link |
| `!<bot name> off` / `on` | anyone | [Switch the bot off](room-veto.md) in this room, or ask for it back |

The full list is in the [command reference](../reference/commands.md).
