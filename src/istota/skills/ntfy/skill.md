---
name: ntfy
triggers: [ntfy, push notification, notify me, notify my phone, mobile alert, alert my phone, beep me, ping me, to my phone]
description: Send a push notification to the user's configured ntfy device(s). One-way (bot to phone), no reply channel.
cli: true
companion_skills: [sensitive_actions]
env: [{"var":"NTFY_SERVER_URL","from":"secret","service":"ntfy","key":"server_url"},{"var":"NTFY_TOPIC","from":"secret","service":"ntfy","key":"topic"},{"var":"NTFY_TOKEN","from":"secret","service":"ntfy","key":"token"},{"var":"NTFY_USERNAME","from":"secret","service":"ntfy","key":"username"},{"var":"NTFY_PASSWORD","from":"secret","service":"ntfy","key":"password"}]
---

# ntfy push notifications

Send a push notification to the user's mobile device(s) via their configured ntfy topic.

## Commands

```bash
# Minimal — body only
istota-skill ntfy send "build finished"

# With title, priority (1-5, default 3), and tags
istota-skill ntfy send "disk 91% full" --title "zorg" --priority 4 --tags "warning,floppy_disk"

# Click action (opens URL when the notification is tapped)
istota-skill ntfy send "PR ready for review" --click "https://github.com/foo/bar/pull/42"
```

Returns JSON on stdout: `{"status":"ok"}` on success, `{"status":"error","error":"..."}` on failure (and exit code 1).

## When to use

- The user explicitly asks for a push / ntfy / mobile alert.
- A long-running task wants to tap the user on the shoulder when it finishes.
- An out-of-band alert (Talk would be too noisy or the user isn't checking it).

## When NOT to use

- The user might want to reply — ntfy is one-way. Use Talk instead.
- The reply target is a Talk room or email — those have their own delivery.
- The notification body would leak secrets to the user's phone screen.

## Failure mode

If ntfy isn't configured (no topic), the command returns an error envelope and exit 1. Tell the user to set the topic at `/istota/settings` (Connected services → ntfy) and proceed via Talk for this task.
