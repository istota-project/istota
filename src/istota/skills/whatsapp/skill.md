---
name: whatsapp
description: Send a separate message to your own WhatsApp during a task and check its delivery
cli: true
requires_capability: [whatsapp]
companion_skills: [sensitive_actions, untrusted_input]
---
# WhatsApp

Use `istota-skill whatsapp send --request-key KEY "text"` to queue a separate message to your own bound WhatsApp. This leaves the task's final reply destination unchanged. There is no recipient address argument.

Choose a stable request key for each intended message and reuse it when retrying the same task. Keys are 1–64 ASCII letters, digits, dashes or underscores. Changing the text under the same key is refused. Text must contain visible content and fit within 2,000 characters.

A queued response means the daemon will attempt delivery. It does not mean sent. Once queued, a later failure of this task does not retract the message. Use `istota-skill whatsapp status REQUEST_ID` to check it. An uncertain send may have arrived; do not repeat it automatically with a new key.

Sending still obeys opt-out, service-window and budget rules. A changed binding blocks the original request. Cross-user questions are not available yet.
