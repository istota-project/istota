---
name: google_workspace
triggers: [google drive, google docs, google sheets, google calendar, google chat, google workspace, gmail, spreadsheet, gws]
description: Google Workspace operations via gws CLI (Drive, Gmail, Docs, Sheets, Calendar, Chat)
cli: true
env: [{"var":"GOOGLE_WORKSPACE_CLI_TOKEN","from":"setup_env","sensitive":true},{"var":"GOOGLE_WORKSPACE_CLI_CONFIG_DIR","from":"setup_env"}]
egress: external
---
# Google Workspace skill

Interact with Google Workspace services using the `gws` CLI, accessed through the skill wrapper.

## Authentication

Credentials are injected automatically via the skill proxy. Do not hardcode tokens or attempt to read credential files.

Two different failures look alike, and telling them apart is the difference between useful advice and wrong advice:

- **No token at all** — the user has never connected their Google account, or disconnected it. Send them a connect link (below) rather than naming a settings page.
- **Insufficient scope** — they are connected, but did not grant this service, or granted it read-only and the command writes. Google says so explicitly: `insufficientPermissions`, `ACCESS_TOKEN_SCOPE_INSUFFICIENT`, or a 403 mentioning scope. Tell them which service and level the command needs, and that Settings → Google Workspace shows what they granted and lets them change it. A change requires reconnecting — Google has to ask them again.

Read-only is the conservative default on most instances, so **assume nothing about write access**. The write verbs below (`gmail +send` / `+reply`, `sheets +append`, `drive +upload`, `calendar +insert`, `docs documents create`, `chat spaces.messages create`) all need the read-and-write level for their service. An instance can also decline to offer a service at all, in which case no amount of reconnecting will help and the operator has to enable the API.

### Connecting from chat

When the user asks to connect Google, or a command fails because they have no token, run:

```bash
istota-skill google_workspace connect-link
```

It sends the user a one-use link in their private chat. You never see the link. Opening it shows which Google services will be connected, and "Continue to Google" takes them to Google's consent screen. The link works once and expires after 15 minutes unless the operator set another time. The reply looks like `{"status": "ok", "services": ["Drive (read-only)", "Gmail (read and write)"], "connected": false, "expires_at": "...", "delivered_to": "whatsapp"}`. Tell the user the link is on its way, name the services, and say where it went. `connected: true` means a Google account is already connected; a new link reconnects it, which is how a changed service selection takes effect.

Refusals come back as `{"status": "error", "reason": ...}`:

- `no_scopes`: no services are selected. Send the user `settings_url` from the reply and tell them to pick services there first.
- `not_configured`: Google Workspace is not set up on this server. Only the operator can fix that.
- `not_owner_turn`, `scheduled_turn`, `incognito`: a link is only sent in answer to the user's own message, outside scheduled runs and incognito rooms. Tell them to ask themselves, in a normal chat.
- `no_private_room`, `no_external_origin`, `too_many_links`: say what it means in plain words. Never ask for a password or a code in chat instead.

## Commands

All commands go through the skill wrapper:

```bash
istota-skill google_workspace drive files list --query "name contains 'report'"
istota-skill google_workspace drive files get --fileId FILE_ID
istota-skill google_workspace drive +upload /path/to/file.pdf --parents FOLDER_ID

# Sheets
istota-skill google_workspace sheets spreadsheets create --properties '{"title": "Budget"}'
istota-skill google_workspace sheets +read --spreadsheetId SHEET_ID --range "Sheet1!A1:D10"
istota-skill google_workspace sheets +append --spreadsheetId SHEET_ID --range "Sheet1" --values '[["a","b","c"]]'

# Docs
istota-skill google_workspace docs documents get --documentId DOC_ID
istota-skill google_workspace docs documents create --body '{"title": "Notes"}'

# Calendar
istota-skill google_workspace calendar +agenda
istota-skill google_workspace calendar +insert --calendarId primary --summary "Meeting" --start "2025-01-15T10:00:00" --end "2025-01-15T11:00:00"
istota-skill google_workspace calendar events list --calendarId primary --timeMin "2025-01-15T00:00:00Z" --timeMax "2025-01-16T00:00:00Z"

# Gmail
istota-skill google_workspace gmail +send --to user@example.com --subject "Hello" --body "Message body"
istota-skill google_workspace gmail users.messages list --userId me --query "is:unread"

# Chat
istota-skill google_workspace chat spaces list
istota-skill google_workspace chat spaces.messages create --parent "spaces/SPACE_ID" --body '{"text": "Hello"}'
```

### Host paths

Any argument naming a file **on this machine** — `drive +upload /path/to/file.pdf` is the one the commands above use — must be inside the user's own workspace, their channel directory, the task's deferred directory, or the copy of an attachment you were sent. Anything else is refused before the command runs, with `"reason": "host_path_refused"`. Copy the file into the workspace first if it is somewhere else.

Google's own identifiers are untouched: `--fileId`, `--parents FOLDER_ID`, spreadsheet ids and a `--query` containing a slash are not host paths and are passed through as written.

Relative input paths resolve against the user's workspace, so `+upload notes.pdf` means the file of that name in it. Files are copied into private read-only storage before `gws` opens them. Directory arguments and local output files are unsupported; use stdout for output. Run through `istota-skill`, which cleans up the copies after the command ends.

`+upload` sends the file to Google, and the check it goes through is the read rule rather than the narrower rule `email --attach` uses — so an attachment you were sent or a channel file can be uploaded to Drive where it could not be mailed. Telling an upload from a read means knowing every gws verb, which is a list that would go stale against a program shipped elsewhere.

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

Use `istota-skill google_workspace <service> --help` for full parameter details.

## Working with output

All commands return JSON. Parse with Python:

```bash
istota-skill google_workspace drive files list --query "name contains 'budget'" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for f in data.get('files', []):
    print(f'{f[\"id\"]}  {f[\"name\"]}')
"
```

## Pagination

For large result sets, use `--page-all` to auto-paginate (streams NDJSON):

```bash
istota-skill google_workspace drive files list --page-all --query "mimeType='application/pdf'" | head -100
```

Limit output to avoid overwhelming context. Use `| head -c 50000` as a safety measure for unbounded queries.

## Dry run

Preview the API request without executing:

```bash
istota-skill google_workspace drive files list --query "test" --dry-run
```

## Error handling

Check exit code. On failure, stderr contains error details:

```bash
if ! result=$(istota-skill google_workspace drive files list --query "test" 2>/tmp/gws_err); then
    cat /tmp/gws_err
fi
```

## Schema introspection

Discover available parameters for any API method:

```bash
istota-skill google_workspace schema drive.files.list
```
