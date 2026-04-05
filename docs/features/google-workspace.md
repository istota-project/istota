# Google Workspace

Access Google Drive, Gmail, Calendar, Sheets, Docs, and Chat through the [Google Workspace CLI](https://github.com/googleworkspace/cli) (`gws`). The bot uses `gws` commands via Bash with structured JSON output.

## Setup

### 1. Create Google Cloud OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create a project (or select an existing one)
3. Enable the APIs you need:
    - Google Drive API
    - Gmail API
    - Google Calendar API
    - Google Sheets API
    - Google Docs API
4. Go to **APIs & Services > Credentials > Create Credentials > OAuth client ID**
5. Application type: **Web application**
6. Add an authorized redirect URI:
   ```
   https://your-hostname/istota/callback/google
   ```
7. Copy the **Client ID** and **Client Secret**

!!! note
    The redirect URI must match your Istota web interface hostname exactly, including the scheme (`https://`).

### 2. Configure Istota

```toml
[google_workspace]
enabled = true
client_id = "123456789-abc.apps.googleusercontent.com"
client_secret = ""    # or ISTOTA_GOOGLE_CLIENT_SECRET env var
```

The default scopes request access to Drive, Gmail, Calendar, Sheets, and Docs. To restrict access, override the `scopes` list:

```toml
[google_workspace]
enabled = true
client_id = "..."
scopes = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]
```

### 3. Install the gws CLI

The Ansible role installs `gws` via npm when `istota_google_workspace_enabled` is set. For manual installs:

```bash
npm install -g @googleworkspace/cli
```

### 4. Connect a user's Google account

Each user connects their own Google account through the web dashboard:

1. User logs in to the Istota web interface
2. The dashboard shows a **Google Workspace** card with "Connect your Google account"
3. User clicks the card, is redirected to Google's consent screen
4. After granting access, they're redirected back to the dashboard
5. The card now shows "Connected" with a disconnect option

OAuth tokens are stored per-user in the database and auto-refreshed on each task execution. Users can disconnect at any time from the dashboard.

## Usage

Once connected, the bot can use `gws` commands for any task that matches the skill triggers (e.g., "upload this to google drive", "create a spreadsheet", "check my google calendar").

The bot receives the user's OAuth token via `GOOGLE_WORKSPACE_CLI_TOKEN` and uses the `gws` CLI directly through Bash.

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

## Security

- OAuth tokens are stored in the database, scoped per-user
- The access token is routed through the credential proxy (`GOOGLE_WORKSPACE_CLI_TOKEN` is stripped from the subprocess env and injected server-side)
- Network isolation allowlists specific Google API hosts (googleapis.com subdomains) only when the skill is selected
- Users can only access their own Google account data
- Disconnect removes all stored tokens immediately

## Scopes reference

| Scope | Access |
|---|---|
| `https://www.googleapis.com/auth/drive` | Full Drive access |
| `https://www.googleapis.com/auth/gmail.modify` | Read, send, and modify Gmail |
| `https://www.googleapis.com/auth/calendar` | Full Calendar access |
| `https://www.googleapis.com/auth/spreadsheets` | Read and write Sheets |
| `https://www.googleapis.com/auth/documents` | Read and write Docs |

Use read-only variants (e.g., `drive.readonly`, `calendar.readonly`) to restrict access.
