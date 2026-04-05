---
name: google_workspace
triggers: [google drive, google docs, google sheets, google calendar, google chat, google workspace, spreadsheet, gws]
description: Google Workspace operations via gws CLI (Drive, Docs, Sheets, Calendar, Chat)
---
# Google Workspace skill

Interact with Google Workspace services using the `gws` CLI. All output is structured JSON.

## Authentication

Credentials are injected automatically via `$GOOGLE_WORKSPACE_CLI_TOKEN`. Do not hardcode tokens or attempt to read credential files.

If the token is missing, the user has not connected their Google account. Tell them to connect via the web dashboard.

## Available services

Drive, Gmail, Calendar, Sheets, Docs, Chat, and any other Google Workspace API.

## Commands

### Direct API commands

```bash
# Drive
gws drive files list --query "name contains 'report'"
gws drive files get --fileId FILE_ID
gws drive +upload /path/to/file.pdf --parents FOLDER_ID

# Sheets
gws sheets spreadsheets create --properties '{"title": "Budget"}'
gws sheets +read --spreadsheetId SHEET_ID --range "Sheet1!A1:D10"
gws sheets +append --spreadsheetId SHEET_ID --range "Sheet1" --values '[["a","b","c"]]'

# Docs
gws docs documents get --documentId DOC_ID
gws docs documents create --body '{"title": "Notes"}'

# Calendar
gws calendar +agenda
gws calendar +insert --calendarId primary --summary "Meeting" --start "2025-01-15T10:00:00" --end "2025-01-15T11:00:00"
gws calendar events list --calendarId primary --timeMin "2025-01-15T00:00:00Z" --timeMax "2025-01-16T00:00:00Z"

# Gmail
gws gmail +send --to user@example.com --subject "Hello" --body "Message body"
gws gmail users.messages list --userId me --query "is:unread"

# Chat
gws chat spaces list
gws chat spaces.messages create --parent "spaces/SPACE_ID" --body '{"text": "Hello"}'
```

### Helper commands (prefixed with +)

| Service | Command | Description |
|---|---|---|
| Gmail | `+send` | Send an email |
| Gmail | `+reply` | Reply to a message |
| Gmail | `+triage` | Triage inbox |
| Sheets | `+read` | Read a range |
| Sheets | `+append` | Append rows |
| Calendar | `+agenda` | Show upcoming events |
| Calendar | `+insert` | Create an event |
| Drive | `+upload` | Upload a file |

Use `gws <service> --help` or `gws <service> <resource> <method> --help` for full parameter details.

## Working with output

All commands return JSON. Parse with Python:

```bash
gws drive files list --query "name contains 'budget'" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for f in data.get('files', []):
    print(f'{f[\"id\"]}  {f[\"name\"]}')
"
```

## Pagination

For large result sets, use `--page-all` to auto-paginate (streams NDJSON):

```bash
gws drive files list --page-all --query "mimeType='application/pdf'" | head -100
```

Limit output to avoid overwhelming context. Use `| head -c 50000` as a safety measure for unbounded queries.

## Dry run

Preview the API request without executing:

```bash
gws drive files list --query "test" --dry-run
```

## Error handling

Check exit code. On failure, stderr contains error details:

```bash
if ! result=$(gws drive files list --query "test" 2>/tmp/gws_err); then
    cat /tmp/gws_err
fi
```

## Schema introspection

Discover available parameters for any API method:

```bash
gws schema drive.files.list
```
