# Google Workspace

Access Google Drive, Gmail, Calendar, Sheets, Docs, and Chat through the [Google Workspace CLI](https://github.com/googleworkspace/cli) (`gws`). The bot uses `gws` commands via Bash with structured JSON output.

## Setup

### 1. Create Google Cloud OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create a project (or select an existing one)
3. Enable the APIs you need. Only an enabled API can be offered to users, so this list bounds the `scopes` ceiling below:
    - Google Drive API
    - Gmail API
    - Google Calendar API
    - Google Sheets API
    - Google Docs API
    - Google Chat API (only if you intend to offer the skill's `chat` verbs)
4. Go to **APIs & Services > Credentials > Create Credentials > OAuth client ID**
5. Application type: **Web application**
6. Add an authorized redirect URI:
   ```
   https://your-hostname/istota/google/callback
   ```
7. Copy the **Client ID** and **Client Secret**

!!! note
    The redirect URI must match your Istota web interface hostname exactly, including the scheme (`https://`).

### 2. Configure Istota

```toml
[google_workspace]
enabled = true
client_id = "123456789-abc.apps.googleusercontent.com"
client_secret = ""    # or ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET env var
```

The default scopes offer read-only access to Drive, Gmail, Calendar, Sheets and Docs.

**`scopes` is a ceiling, not a request.** Each user picks their own subset of it on the settings page, and the connect redirect asks Google only for what that user chose — so listing a service here costs nothing for the users who leave it switched off. It has to be a hard maximum rather than a suggestion: the OAuth client belongs to a Google Cloud project whose enabled APIs you control, and a scope for an API the project has not enabled fails at Google's end with an error the user can do nothing about. Enable the API first, then list its scope.

```toml
[google_workspace]
enabled = true
client_id = "..."
scopes = [
    # Offering the full variant adds a "Read and write" option to that
    # service's picker; it does not force write access on anyone.
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar.readonly",
]
```

Changing this list never rewrites an existing grant — nothing revalidates one at startup. A user keeps whatever they consented to until they reconnect, and the settings card shows where the two differ.

### 3. Install the gws CLI

The Ansible role downloads a prebuilt `gws` release binary from the `googleworkspace/cli` GitHub releases (the `x86_64-unknown-linux-gnu.tar.gz` asset) to `~/.local/bin/gws` when `istota_google_workspace_enabled` is set. For manual installs, download the matching release binary from the [googleworkspace/cli releases page](https://github.com/googleworkspace/cli/releases) and place it on your `PATH`.

### 4. Connect a user's Google account

Each user connects their own Google account from **Settings → Google Workspace**:

1. User logs in to the Istota web interface and opens `/istota/settings`
2. The **Google Workspace** card lists one row per service with an access level: no access, read-only, or read and write. Services outside the instance ceiling are fixed at "No access" and say so
3. User picks their levels and saves, then clicks **Connect**
4. Google's consent screen asks for exactly the scopes that selection resolves to. The user may still deselect individual boxes there
5. After granting access they land back in the app, and the card lists what Google actually granted

OAuth tokens are stored per-user in the database and auto-refreshed on each task execution. Users can disconnect at any time from the same card.

### What the card shows

The card reads the granted scopes back out of the stored token, so it reports what the user consented to rather than what was asked for. Three states are worth knowing:

- **Partial** on a granted service — the consent screen's boxes were not all ticked.
- **"Your grant is narrower than what this instance now asks for"** — the selection or the ceiling changed after the grant was made. Some commands will fail until the user reconnects. This is the state behind most "the bot can't see my calendar" reports.
- **"Also granted, not recognised as a service"** — a scope in the token that Istota's service map does not know, usually a hand-edited `scopes` list. It is displayed verbatim rather than hidden.

Changing the selection requires reconnecting: Google has to ask again. The connect flow already sets `access_type=offline` and `prompt=consent`, so a reconnect issues a fresh refresh token.

!!! note
    The consent screen appears on every connect, not only the first. The authorization request asks for offline access with `prompt=consent`, because Google returns a refresh token only when both are asked for — and without one, a reconnect (after widening `scopes`, or after the user revokes access from their Google account) would store an access token that expires in an hour and cannot be renewed.

## Usage

Once connected, the bot can use `gws` commands for any task that matches the skill triggers (e.g., "upload this to google drive", "create a spreadsheet", "check my google calendar").

The bot invokes `istota-skill google_workspace <args>` (the skill wrapper); the OAuth token is injected proxy-side rather than exposed to the model.

### Example interactions

- "Upload the Q1 report to my Google Drive"
- "Create a spreadsheet with these expenses"
- "What's on my Google Calendar this week?"
- "Send an email via Gmail to user@example.com"
- "Read the data from my Budget spreadsheet"

## Ansible variables

| Variable | Default | Description |
|---|---|---|
| `istota_google_workspace_enabled` | `false` | Enable the Google Workspace skill |
| `istota_google_workspace_client_id` | `""` | OAuth client ID |
| `istota_google_workspace_client_secret` | `""` | OAuth client secret (goes to secrets.env) |
| `istota_google_workspace_scopes` | (the five read-only scopes) | The ceiling each user's own selection is bounded by |

## Security

- OAuth tokens are stored in the database, scoped per-user
- The access token is routed through the credential proxy (`GOOGLE_WORKSPACE_CLI_TOKEN` is stripped from the subprocess env and injected server-side)
- Network isolation allowlists specific Google API hosts (googleapis.com subdomains) when the user's Google credentials are present (authorized via the credential set, decoupled from prompt-time skill selection)
- Users can only access their own Google account data
- Disconnect removes all stored tokens immediately
- The per-user scope selection is clamped to the instance ceiling server-side, at connect time — a client asking for more than the operator offers gets the operator's level, not its own
- The token is what Google enforces. The scope selection narrows what a user hands over; it is not a second authorization layer, and the network allowlist is not keyed on it

## Scopes reference

One row per service, matching `src/istota/google_scopes.py` — the single table the picker, the granted-scope display and this document all read.

| Service | Read-only | Read and write |
|---|---|---|
| Drive | `.../auth/drive.readonly` | `.../auth/drive` |
| Gmail | `.../auth/gmail.readonly` | `.../auth/gmail.modify` |
| Calendar | `.../auth/calendar.readonly` | `.../auth/calendar` |
| Sheets | `.../auth/spreadsheets.readonly` | `.../auth/spreadsheets` |
| Docs | `.../auth/documents.readonly` | `.../auth/documents` |
| Chat | `.../auth/chat.spaces.readonly` + `.../auth/chat.messages.readonly` | `.../auth/chat.spaces` + `.../auth/chat.messages` |

Prefix every scope with `https://www.googleapis.com`. Gmail's write level is `gmail.modify` rather than the bare `gmail` scope: it covers reading, sending and labelling without granting permanent deletion.

Chat needs both a spaces scope and a messages scope at each level, and appears in no default configuration — the skill documents `chat spaces` verbs that need it added to `scopes` (and the Chat API enabled in the Cloud project) before they work.

A scope outside this table still works if you put it in `scopes`. It gets no picker row, so no user can decline it — which is exactly why it is **requested unconditionally**, on top of whatever they did choose, and named on the card as something the instance always asks for. That is what keeps narrow scopes (`drive.file`, `gmail.send`, `calendar.events`) working, and it is why an entirely unmapped `scopes` list still connects. A grant carrying an unmapped scope is displayed verbatim under "not recognised as a service".
