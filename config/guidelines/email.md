# Email Response Guidelines

Use the email output tool to produce your response (see email skill). The `--body` content is the actual email text.

## Plain text format (default)

Email clients do not render markdown in plain text emails.

DO NOT USE in the body:
- Markdown headers (# or ##) - use ALL CAPS instead
- Bold or italic markdown - use plain text
- Markdown tables - use plain text lists or aligned columns
- Code blocks with backticks
- Markdown bullet points - use numbered lists or "- " with space

INSTEAD USE:
- ALL CAPS HEADERS for sections
- Plain numbered lists (1. 2. 3.) for clarity
- Simple separators: === or --- or * * *
- Clear paragraph breaks for structure

## HTML format (`--html`)

When using HTML format, write clean semantic HTML. Keep styling inline and minimal. Do not include `<html>`, `<head>`, or `<body>` wrapper tags — just the content markup.

## Email etiquette

- Open with a brief greeting if replying to someone external
- Match the formality of the incoming email
- Sign off with a simple "{BOT_NAME}"
- Keep subject lines concise when sending new emails
