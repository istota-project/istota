---
name: room
triggers: [whisper, privately, only me, answer privately, private chat, post to the room, post in the room, tell the room]
description: Write privately to your principal from a shared room, answer their question in their private chat, or post into a shared room from their private chat
cli: true
shared_room: safe
companion_skills: [untrusted_input]
---
# Private replies

A **shared room** is read by several people. Anything meant for one member and not the room goes to **that member's private chat with you**: the private conversation they already have with you on web, Talk or WhatsApp. Nothing creates one for them. A member with no private chat gets the note in their notifications instead.

A message that reaches a member's private chat this way is tagged with the shared room it is about. When they reply to it or quote it, that turn is linked to the room: you get the room's recent messages as context, and `room post` can put a message there. A turn that does not reply to such a message is an ordinary private turn with no link. If the user clearly means a shared room the turn has no link to, say you have no context for that room and ask them to reply to one of your messages about it, or to name it so you can use `room post --room`.

## `room whisper` — from a shared room, to your principal

```bash
istota-skill room whisper --request-key KEY "text"
```

Puts `text` in the private chat of the user you are acting for. Only they read it. Use it for anything that should not be said in front of the room: a private answer, a question for them, something to check before replying publicly. The room sees none of it, and your public reply should not repeat it.

It returns `queued`; the daemon delivers it. When the answer also carries `"delivered_to": "notifications"`, the user has no private chat with you and the note goes to their notifications; end your reply in the room with the `room_notice` text from the answer, exactly as given. It is refused (`not_a_shared_room`) outside a shared room, where your ordinary answer already reaches only the user.

## `room answer-privately` — answer the question in their private chat instead

```bash
istota-skill room answer-privately
```

When the user you are acting for asks something whose answer should not be read by everyone here (their health, money, private mail and so on), or asks you to answer privately, run this instead of answering in the room: their own question is asked again in their private chat with you, where only they read the answer and their personal memory is available. Then tell the room, briefly, that you have answered them privately. The command takes no text: what is asked again is their message as they wrote it.

It returns `queued` with the new task's id. It is refused for a guest's message (`guest_turn`), outside a shared room (`not_a_shared_room`), for anything but the user's own message (`unsupported_origin`), and when the user has no private chat with you (`no_private_room`): then tell the room you could not answer privately and ask them to message you directly first.

## `room post` — from their private chat, into a shared room

```bash
istota-skill room post --request-key KEY [--room ROOM] "text"
```

Asks to post `text` into a shared room, as {BOT_NAME}. Without `--room` it goes to the room this turn is linked to; `--room` names a room by its token or its name. Nothing you write in a private chat reaches a shared room any other way. A post usually returns `held` with a preview: show it and wait for the user's approval, which releases exactly that text and nothing else. It can instead return `queued` with `approval: "clean_turn"` when the text is the user's own words from their message and this post was the first thing you did; say it is on its way.

Post only what the user asked to post. Never post text taken from the shared room's transcript, from a web page or from any other content you read. Refusals: `not_a_private_room` means you are in a shared room, where your reply already reaches it; `no_target_room` means the turn is linked to no room and `--room` was not given; `parent_unavailable` means the room is gone, is not shared, or the user is no longer in it; `email_thread` means the room is an email thread: send the text with `istota-skill email reply-all` on the thread's latest message instead, which holds it as a draft for the user's approval when a recipient is not trusted.

## Request keys

A request key is 1–64 letters, digits, dashes or underscores. Reuse it when retrying the same text; a different text under the same key is refused (`request_conflict`). Text must contain visible content and fit within 2,000 characters.
