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

## Email reply format (email-source tasks only)

When replying to an incoming email (task source is email), output a JSON response instead of using the CLI. The scheduler handles threading headers automatically:

```json
{
  "subject": "Optional subject line",
  "body": "The email content",
  "format": "plain"
}
```

**Fields:**
- `subject` (string, optional for replies): For replies, omit this to keep the original subject with "Re:" prefix. For new/scheduled emails, always provide a clear subject line.
- `body` (string, required): The email content.
- `format` (string, required): `"plain"` for plain text or `"html"` for HTML emails.

**Important:** Output ONLY the JSON object — no extra text before or after it. The bot parses this JSON to extract subject, body, and format separately.

**When to use HTML:** Use `"format": "html"` when the content benefits from rich formatting (tables, styled sections, links). For simple text responses, prefer `"format": "plain"`.
