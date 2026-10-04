# Relay questions

A task can ask another user of the same installation a question on your behalf. The question reaches them in their own room, on WhatsApp or by SMS. When they reply to it explicitly, their exact text comes back to the private conversation you asked from. Nothing else they say is shared.

No prior permission is needed between users of one installation. A recipient who does not want questions from someone blocks them (see [Blocking](#blocking)).

## Asking

Ask in ordinary words, naming the person: "ask bob whether he is free on Thursday". The task uses the `relay` skill:

```bash
istota-skill relay ask bob --request-key thursday "Are you free on Thursday?"
```

The recipient is named by exact user ID. There is no directory search, and no way to send to an arbitrary phone number. `--via room|whatsapp|sms` names a transport; leave it out and the question goes to the recipient's default room. A question is limited to 2,000 characters and is never shortened: if it would not arrive whole on the chosen transport, the ask is refused.

You can ask from a private web or Talk room, or from your own bound WhatsApp or SMS conversation. Email, the CLI and REPL, scheduled jobs, subtasks, commands and shared rooms cannot start a relay. Final-output overrides do not change where a preview or an answer goes.

## Approval

A question is held until you approve it. Istota shows the recipient, where the question will go, the exact wording, where the answer will come back and the expiry, through the normal task confirmation controls (`!confirm`, the buttons in web chat and Talk, the bell). Approving releases only that question.

One case skips approval. When the request you typed names the recipient, and asking them is the first and only thing the task did in this attempt, the question is sent straight away. The request has to name them by user ID or display name, as a whole word and in any case, in the message you sent. Earlier conversation, memory and attachment file names do not count. Names shorter than two characters never match. This applies to tasks started from web chat, Talk, WhatsApp and SMS; a question from anywhere else is always held. A second question in the same turn is held, as is one asked after the task has run any other tool, such as reading a page or a file. The ask itself has to be a plain `istota-skill relay ask` command on its own: one chained to another command, or whose text is filled in from a file or another command's output, is held. A task re-run after you approved something it proposed is also held, because its prompt carries what the earlier run wrote.

`!relay show` and the skill's status say which way a question was released: approved by you, or sent without approval.

## Where the question goes

A question has one destination, chosen when it is asked and fixed from then on:

1. **The recipient's own setting**, if they have one and it works. It overrides what you asked for. If their chosen transport stops working, the question goes to their default room, never to the transport you named.
2. **The transport you named with `--via`.** If it cannot reach them, the ask is refused and says why.
3. **Their default room.**

A room question appears in the recipient's web chat and, when the room is bound to Talk, in Talk as well. The room has to be one only the recipient belongs to. Istota never creates a room to deliver a question.

Recipients set their preference in Settings, Preferences, under "Questions from other users": the asker's choice, their default room, WhatsApp or SMS. A choice that is not set up for their account is shown greyed out.

If the recipient changes their default room or their setting between your approval and the send, the question is not redirected. It fails, and you can ask again.

### When an ask is refused

| Code | Meaning |
|---|---|
| `unknown_user` | No user with that ID. |
| `recipient_unavailable` | The recipient is not accepting questions from you. The reason is not given. |
| `recipient_not_on_whatsapp` / `recipient_not_on_sms` | You named WhatsApp or SMS and the recipient has no binding for it. |
| `whatsapp_unavailable` / `sms_unavailable` | That transport is not enabled on this installation. |
| `recipient_has_no_private_room` | The recipient has no default room that only they belong to. |

A refused ask leaves nothing behind.

## What the recipient sees

The question says who it is from and how to answer:

```text
Istota, on behalf of Alice (alice):

Are you free on Thursday?

To answer Alice, reply to this message. Only that answer will be shared.
```

It also appears in the recipient's [notification inbox](notifications.md) until it is answered, expires, fails or is cancelled. The pushed notice names who asked and how to answer, and never contains the question, because a push can reach a shared room or a third-party service. The bell shows the question itself. A room question pushes; a WhatsApp or SMS question does not, since the message on the phone is already the alert. For a question in a room, the notice's Open button takes you to that room.

## Answering

Only an explicit reply is an answer:

- **In a room**, reply to the question message in web chat or in Talk, or send `!relay reply RELAY_ID text` in that room.
- **On WhatsApp**, quote the question, or send `!relay reply RELAY_ID text`.
- **By SMS**, send `!relay reply RELAY_ID text`. SMS has no quoting.

The answer is shared exactly as written, with an attribution line. A reply is checked against the question before anything else, so replying `yes` to a question never approves some other task of yours that is waiting for confirmation. In a room, a reply that starts with `!` is still run as a command. `!relay reply` from a room answers only a question delivered to that room, and a question sent to WhatsApp or SMS has to be answered there.

The first answer wins. A second one, a closed or expired relay, an empty reply or one too long to return in one piece gets a short notice and is not forwarded. Photographs and captions are never answers.

An answer given by replying to the question, or by `!relay reply` on WhatsApp or SMS, also starts an ordinary task for the recipient's own assistant, with the question and the outcome as context, so it can respond to them as usual. The asker's conversation and memories are not included. `!relay reply` typed in a room records the answer and returns the notice without starting a task.

## The answer coming back

The answer returns unchanged to the private web, Talk, WhatsApp or SMS conversation you asked from. It is never summarized, shortened or split. If that conversation has become shared, or delivery is blocked or uncertain, Istota keeps the exact answer for 30 days and sends you a notice without its content; read it with `!relay show` in a private conversation. It is never moved to another conversation on its own.

When a question cannot be delivered, you get a "Relay update" notice that does not say why.

## Limits

- A question expires 24 hours after it is released, and its first send must start within ten minutes.
- One unanswered question per ordered pair of users.
- Ten open questions per asker, twenty per recipient.
- WhatsApp and SMS questions follow those surfaces' own rules: opt-out, the Cloud service window and budgets, and single-send ledgers. An uncertain send is never repeated.

## Blocking

Blocking is directional and admins cannot bypass it. In a verified private conversation:

- `!relay block USER_ID` stops questions from that user and closes their unanswered ones.
- `!relay unblock USER_ID` allows them again.
- `!relay blocked` lists who you have blocked.

The asker is told only that you are unavailable. STOP on WhatsApp or SMS stops all delivery on that surface; START does not lift a relay block.

## Following your relays

In a verified private conversation, `!relay list` shows your relays, `!relay show RELAY_ID` shows one with any retained answer, and `!relay cancel RELAY_ID` cancels one that has not been answered. A task can read the same through `istota-skill relay list` and `istota-skill relay status REQUEST_ID`. From a Talk-bound room both check the room's Talk participants live before answering, and refuse with `audience_unavailable` when the list cannot be fetched. `!relay list` makes the same check, so it fails the same way during a Nextcloud outage.
