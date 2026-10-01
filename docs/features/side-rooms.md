# Side rooms

A [shared room](shared-rooms.md) is read by everyone in it. Each member also has a **side room** beside it: a private room with only that member in it, linked to the shared room. Anything meant for one person and not the room goes there.

A side room is created the first time something needs it, and it is an ordinary private room in every other respect. It has its own transcript and its own `CHANNEL.md`, and a task there runs with everything of yours, as in any private conversation.

## What lands in your side room

- **Private answers.** A task in the shared room can put a note for you alone in your side room (`istota-skill room whisper`). When answering you needs something the room withholds, such as your calendar or your files, the bot asks your question again in your side room, answers it there with everything of yours, and tells the room only that it answered you privately (`istota-skill room answer-privately`). The question asked again is your own message as you wrote it, never text the model composed after reading other people's turns.
- **Confirmations.** Any question a task in a shared room needs you to approve is asked in your side room, never in the room. A plain "yes" typed in the shared room no longer answers it.
- **Guest proposals.** When a guest's message is answered in `held` mode, the proposed reply arrives in the host's side room with the guest's words and the exact text. Approving it posts that text into the room; nothing is posted until you do. On an email thread the proposal shows the exact mail and its recipients, and approving it is the approval to send it.
- **Backstage notes.** Your side room's `CHANNEL.md` is your private notes about the shared room ("don't bring up the move"). A task in the shared room reads them when it acts for you: on your own turns, and on a guest's turn when you are the host. The room never sees them. A shared room's own `CHANNEL.md` is read by everyone.

## Context

A task in your side room sees the shared room's recent transcript (the last 40 messages, up to 12,000 characters), marked as the room's conversation rather than instructions, for as long as you are still in that room. It sees the whole history, including turns from before someone joined, since only you read the answer.

## Posting back into the room

Nothing written in a side room reaches the shared room on its own. A task's answer there stays there. To post into the room, a task asks with `istota-skill room post`, which is held for your approval with the exact text shown; approving releases that text and nothing else. If the text is your own words from your message and posting was the first thing the task did, it goes straight through.

A side room task that tries to post into a Talk room other people read with the Nextcloud skill is refused and pointed at `room post`.

## Where you see it

| Surface | Your side room appears as |
|---|---|
| Web | A private room shown under the shared one in the sidebar. A message arriving there while the shared room is open also shows inline in the shared room as a bubble only you can see, with a link to the side room. The bubble is gone after a reload; the side room keeps the message. |
| Talk | Your private conversation with the bot, each message headed "re: <room>". Confirmations asked there take a reply as usual. |
| WhatsApp | Your own chat with the bot's number, headed "re: <room>". A confirmation carries `!confirm <id> yes\|no`, since a bare yes there only answers what was asked in that chat. |
| Email | A private mail to your own address, never a reply on the thread. |

A side room cannot be opened in Talk as a conversation of its own and cannot take a second member. If you leave the shared room, your side room stays as your private transcript, and posting from it is refused.
