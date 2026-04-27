---
name: email
triggers: [email, mail, send, inbox, reply, message]
description: Email sending and response formatting
cli: true
source_types: [email]
dependencies: [imap_tools]
---
## Which command to use: `send` vs `output`

- **`send`** — sends the email immediately via SMTP. Use this when **you** need to send an email (the user asked you to email someone, compose a message, etc.). This is the default — if in doubt, use `send`.
- **`output`** — does NOT send anything. It writes a deferred file that the scheduler picks up to deliver as a reply in the original email thread. **Only use `output` when this task arrived as an incoming email** (source_type is "email") and you are composing the reply body. The scheduler handles threading headers (In-Reply-To, References) automatically.

**Common mistake:** If a user in Talk says "email me a report," use `send` (you are originating a new email). Do NOT use `output` — that writes a file the scheduler will ignore because the task didn't come from email.

## Sender identity

When emailing external contacts (people outside the user's organization), default to sending **as {BOT_NAME}** — the user's assistant. Write in your own voice, identify yourself as the user's assistant, and sign with your name. The recipient should know they're communicating with an agent, not with the user directly.

Only send as the user (first person, signed with the user's name) when they explicitly ask: "email them as me", "send from my address", "write it as if it's from me."

This applies to `send` only. The `output` command (email replies) inherits the thread's existing sender identity.

## Sending email (`send`)

```bash
istota-skill email send --to "recipient@example.com" --subject "Subject line" --body "Email body text"
```

Options:
- `--html` — send as HTML instead of plain text
- `--body-file /path/to/file` — read body from a file (useful for long HTML content)

The command prints JSON on success: `{"status": "ok", "to": "...", "subject": "..."}`

After sending, tell the user the email was sent (do NOT output raw JSON to the user).

For HTML emails with complex formatting, write the body to a temp file first and use `--body-file`.

### Unknown-recipient gate

Sends to addresses not in the user's known-contacts list (no prior correspondence, not in user config, not mentioned in the user's request) are **queued for confirmation** instead of going out immediately. When this happens you'll see:

```json
{"status": "pending_confirmation", "to": "...", "reason": "unknown_recipient", "message": "..."}
```

When you see this status, give the user a **brief one-sentence acknowledgment** and **end your turn**. Example: `"Drafted — waiting for your approval."` Nothing more. Specifically, do **not**:

- Repeat the recipient address, subject, or body (the system has already shown them — repeating is noisy)
- Re-narrate the draft as your own confirmation request (the system already produced one)
- Retry the send with a different recipient or call send again (the gate is a security boundary, not a hint to try again)
- Apologize at length — a single short sentence is enough

If the user later replies "yes", your task will re-execute with the approved recipient on a per-task allowlist; call `send` once with the same args and it goes through.

## Replying to incoming emails (`output`)

When this task originated from an incoming email (source_type "email") and you are composing the reply, use `output`:

```bash
istota-skill email output --subject "Subject line" --body "The email content"
```

Options:
- `--subject` — email subject (optional for replies; the original subject with "Re:" prefix is used if omitted)
- `--body` — the email body text (required, or use `--body-file`)
- `--body-file /path/to/file` — read body from a file (useful for long content)
- `--html` — format body as HTML instead of plain text

This writes a structured file that the scheduler picks up for delivery. The scheduler adds proper threading headers so the reply appears in the same email thread.

For long email bodies, write the body to a temp file first and use `--body-file`:

```bash
# Write body to temp file, then use --body-file
cat > /tmp/email_body.txt << 'BODY'
The full email content goes here.
Multiple paragraphs, quotes, etc.
BODY
istota-skill email output --subject "Subject" --body-file /tmp/email_body.txt
```

**When to use HTML:** Use `--html` when the content benefits from rich formatting (tables, styled sections, links). For simple text responses, use plain text (the default).

## Email formatting

### HTML emails
When sending HTML emails, use semantic HTML structure:
- Heading hierarchy: `h2`, `h3`, `h4`
- Lists with `ul`/`li`, use `strong` for emphasis within items
- Inline elements: `code`, `s` (strikethrough) where appropriate
- Structural markup only — no inline CSS, no `style` attributes
- Always use `--html` flag when sending HTML content

### Sending behavior
After composing an email, execute the send command. Don't narrate what you would send — actually send it (after confirmation if required by sensitive actions rules).
