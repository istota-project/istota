## Sending email

To send an email directly, use the CLI command:

```bash
python -m istota.skills.email send --to "recipient@example.com" --subject "Subject line" --body "Email body text"
```

Options:
- `--html` — send as HTML instead of plain text
- `--body-file /path/to/file` — read body from a file (useful for long HTML content)

The command prints JSON on success: `{"status": "ok", "to": "...", "subject": "..."}`

Use this command whenever you need to send an email — whether the user asks from Talk, a scheduled job, or any other channel. After sending, tell the user the email was sent (do NOT output raw JSON to the user).

For HTML emails with complex formatting, write the body to a temp file first and use `--body-file`.

## Email reply format (email-source tasks and scheduled email jobs)

When replying to an incoming email or producing output for a scheduled job with email delivery, use the email output tool instead of writing inline JSON:

```bash
python -m istota.skills.email output --subject "Subject line" --body "The email content"
```

Options:
- `--subject` — email subject (optional for replies; the original subject with "Re:" prefix is used if omitted)
- `--body` — the email body text (required, or use `--body-file`)
- `--body-file /path/to/file` — read body from a file (useful for long content)
- `--html` — format body as HTML instead of plain text

This writes a structured file that the scheduler picks up for delivery. It avoids transcription corruption (e.g., smart-quote substitution) that can break inline JSON.

For long email bodies, write the body to a temp file first and use `--body-file`:

```bash
# Write body to temp file, then use --body-file
cat > /tmp/email_body.txt << 'BODY'
The full email content goes here.
Multiple paragraphs, quotes, etc.
BODY
python -m istota.skills.email output --subject "Subject" --body-file /tmp/email_body.txt
```

**When to use HTML:** Use `--html` when the content benefits from rich formatting (tables, styled sections, links). For simple text responses, use plain text (the default).
