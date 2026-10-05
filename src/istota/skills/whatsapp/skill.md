---
name: whatsapp
description: Send a separate message to your own WhatsApp
cli: true
requires_capability: [whatsapp]
companion_skills: [sensitive_actions, untrusted_input]
---
# WhatsApp

Use `istota-skill whatsapp send --request-key KEY "text"` to queue a separate message to your own bound WhatsApp. This leaves the task's final reply destination unchanged. There is no recipient address argument. To ask another user a question, use the `relay` skill.

Add `--file PATH` to send an image from your workspace with the text as its caption: `istota-skill whatsapp send --request-key KEY --file /path/to/chart.png "Today's chart"`. The text is optional when a file is given. The file must be a PNG, JPEG, GIF or WebP inside your own workspace; a file anywhere else is refused with `file_not_in_workspace`. The image is re-encoded without its metadata before it is sent.

Choose a stable request key for each intended message and reuse it when retrying the same task. Keys are 1–64 ASCII letters, digits, dashes or underscores. Changing the text under the same key is refused. Text must contain visible content and fit within 2,000 characters.

A queued response means the daemon will attempt delivery. It does not mean sent. Once queued, a later failure of this task does not retract the message. Use `istota-skill whatsapp status REQUEST_ID` to check it. An uncertain send may have arrived; do not repeat it automatically with a new key.

Sending still obeys opt-out, service-window and budget rules. A changed binding blocks the original request. A failed send does not reopen when a window later opens.
