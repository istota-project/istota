# Switching the bot off

The other people in a [shared room](shared-rooms.md) did not choose to have an assistant reading their conversation. Anyone in the room, guest or member, can switch it off for that room, and while it is off it records nothing.

## Switching it off

Send the bot's name as a command, on its own line:

```
!istota off
```

The command word is the deployment's `bot_name`, lowercased, without spaces, so a bot named "Ada" is switched off with `!ada off`. Case does not matter and a trailing full stop or exclamation mark is fine; anything more is an ordinary message. On email, put it as the first line of your reply. On WhatsApp, removing the bot's number from the group also switches it off.

While the room is off:

- nothing anyone writes there is recorded, and nothing is answered;
- tasks waiting to run in the room are cancelled, and a task already running has its answer dropped;
- every other command in the room is ignored, and a held post into the room is refused;
- on the web the room shows that it is switched off and who switched it off, and sending is refused.

The bot confirms in the room on Talk, web and WhatsApp. On email it sends nothing, since any reply would be a mail to everyone on the thread.

In a room with one person in it, `!<name> off` is not a veto and does nothing special.

## Switching it back on

The room comes back on when both of these hold, in either order:

- a member has sent `!<name> on` since it was switched off, and
- everyone who switched it off has either sent `!<name> on` themselves or left the room.

A member who switched it off brings it back with their own `on`. If several people switched it off, each of them has to agree. On WhatsApp, a room switched off by removing the bot stays off after the bot is added again, until a member sends `!<name> on`.

On email, a member can switch the room back on only from the web view. A guest's agreement by mail counts only when the mail passes DMARC, so a forged reply cannot speak for them.

## The announcement

The first time a guest is present in a room with a host, the bot introduces itself once: who it is, whom it works for, and how anyone there can switch it off. On Talk, web and WhatsApp it is posted to the room by the scheduler. On an email thread there is no way to post except a mail, so the announcement is added to the bot's first reply-all on the thread and shown inside any guest proposal's preview; it counts as made only once such a mail is sent.

Existing rooms that already have a guest get their announcement once after upgrading.

## Limits

- The bot's own notices (the veto confirmation, the announcement) are written into the room's transcript, even while it is off.
- On email a guest on a domain without DMARC can lift their own veto only by leaving the thread, and nobody leaves an email thread, since its participants are everyone who has been on it. A host who needs the bot back then has to start a new thread.
- On WhatsApp a removal is a veto with no named person behind it, so any member's `on` after the bot is re-added lifts it, even if a guest removed it.
