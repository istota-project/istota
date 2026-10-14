# Google Workspace

Access Google Drive, Gmail, Calendar, Sheets, Docs, and Chat through the [Google Workspace CLI](https://github.com/googleworkspace/cli) (`gws`). The bot uses `gws` commands via Bash with structured JSON output. [Contacts](contacts.md) uses the same Google connection through its own skill and the People API.

The operator configures one OAuth client ID and secret for the instance. Each user then connects their own Google account and chooses which services Istota may access. Configuring the client does not grant access to any account or to an entire Workspace organization. Personal Google accounts can connect too, if the OAuth app's audience allows them.

Istota ships without Google client credentials. Create a client in a Google Cloud project you control: it owns the consent screen, enabled APIs and registered callback URL. An API key cannot replace the OAuth client ID and secret for this connection. Do not distribute a shared client secret with an image or source checkout; Google's [OAuth credential policy](https://developers.google.com/identity/protocols/oauth2/policies#handle-client-credentials-securely) requires keeping it private.

## Setup

### 1. Create Google Cloud OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create a project (or select an existing one)
3. Enable the APIs you need. Only an enabled API can be offered to users, so this list bounds the `scopes` ceiling below:
    - Google Drive API
    - Gmail API
    - Google Calendar API
    - Google Sheets API
    - People API (for Contacts)
    - Google Docs API
    - Google Chat API (only if you intend to offer the skill's `chat` verbs)
4. Configure **Google Auth platform → Branding** with an app name, support email and contact email. Under **Audience**, choose Internal for an app restricted to your Google Workspace organization, or External for personal accounts or users outside it. Internal is available only to projects associated with an organization. For an External app in Testing, add every account that will connect under **Test users**. See Google's [consent setup guide](https://developers.google.com/workspace/guides/configure-oauth-consent).
5. Under **Data Access**, add the scopes you intend to allow in Istota. Use the [scope table below](#scopes-reference); enabling an API and declaring its OAuth scopes are separate steps.
6. Open **Google Auth platform → Clients → Create client** and choose **Web application**. This is the server-side OAuth client, not a Desktop app client or a service account.
7. Add an **Authorized redirect URI**:
   ```
   https://your-hostname/istota/google/callback
   ```
8. Copy the **Client ID** and **Client Secret** into the instance settings described below. Keep the secret private.

:::note
Istota constructs this URL from `[site] hostname`, with `/istota/google/callback` as the path. Register the exact URL, including scheme, port if present, path and absence of a trailing slash. Production hosts use HTTPS; literal localhost/loopback development hosts use HTTP. The Nextcloud sign-in callback (`/istota/callback`) is a different OAuth flow. See Google's [redirect URI rules](https://developers.google.com/identity/protocols/oauth2/web-server#redirect-uri-validation).
:::

For an External app left in Testing, refresh tokens for Workspace scopes expire after seven days, so users must reconnect. For ongoing use, move the app to In production and complete any verification Google requires for your audience and scopes. Publishing and verification are separate steps; personal-use and internal apps can qualify for verification exceptions. A Workspace administrator may also need to allow the app. See Google's [token expiry rules](https://developers.google.com/identity/protocols/oauth2#expiration) and [verification requirements](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification).

### 2. Configure Istota

On the stack, open **Admin → Configuration** (`/istota/admin/config`):

1. Set `google_workspace.enabled` to `true` and `google_workspace.client_id` to the ID from Google.
2. Set the deployment credential `istota_google_workspace_client_secret` to the matching secret. Use the credential field, rather than putting the secret in the raw TOML editor.
3. Set `google_workspace.scopes` if you want a different service ceiling from the defaults below.
4. Review and submit the change, complete the editor's confirmation, and wait for the stack to restart.

The [web config editor](../configuration/web-editor.md) applies the settings and secret together. It is read-only without the host applier, or when the relevant resource is managed by an external plan. **Admin → Connections** currently has Nextcloud and WhatsApp cards; Google client setup is on Configuration. **Settings → Connections** is where each user authorizes their own Google account.

For file-based configuration, set these values in `config.toml`:

```toml
[google_workspace]
enabled = true
client_id = "123456789-abc.apps.googleusercontent.com"
```

On the stack, the secret goes in `/srv/app/istota/secrets/istota_google_workspace_client_secret` on the VM. The container exposes it as `ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET`. Follow the [deployment credential procedure](../configuration/credentials.md#provisioning-global-credentials) to replace it and restart the services. Outside the stack, set that environment variable for both the web process and daemon; it overrides `[google_workspace] client_secret`. The client ID stays in TOML. Restart both processes after changes. Keep the existing `ISTOTA_SECRET_KEY`: it encrypts users' stored OAuth tokens and is unrelated to the Google client secret.

The default scopes offer read-only access to Drive, Gmail, Calendar, Sheets, Docs and Contacts (including Other contacts).

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

For Contacts on an existing install, enable the People API in the Google Cloud project. If `scopes` is explicit, add `https://www.googleapis.com/auth/contacts.readonly` and `https://www.googleapis.com/auth/contacts.other.readonly`; use `https://www.googleapis.com/auth/contacts` instead of `contacts.readonly` to offer read and write. Users must select Contacts and reconnect to grant the new access. Other contacts remains read-only at both levels.

### 3. Install the gws CLI

The istota image ships `gws` on `PATH`, so a server needs no step here. For an install outside the image, download the matching release binary from the [googleworkspace/cli releases page](https://github.com/googleworkspace/cli/releases) and place it on your `PATH`.

### 4. Connect a user's Google account

Each user connects their own Google account from **Settings → Connections → Google Workspace**:

1. User logs in to the Istota web interface and opens `/istota/settings/connections`
2. The **Google Workspace** card lists one row per service with an access level: no access, read-only, or read and write. Services outside the instance ceiling are fixed at "No access" and say so
3. User picks their levels and saves, then clicks **Connect**
4. Google's consent screen asks for exactly the scopes that selection resolves to. The user may still deselect individual boxes there
5. After granting access they land back in the app, and each service's row reports what Google actually granted beside the level being asked for

OAuth tokens are stored per-user in the database and auto-refreshed on each task execution. Users can disconnect at any time from the same card.

### What the card shows

The card reads the granted scopes back out of the stored token, so it reports what the user consented to rather than what was asked for. Each service's row carries the whole account of that service — the level to ask for next time, what Google currently holds, and whether that grant is partial — rather than splitting it across a picker and a separate grant list; the connection state itself is a status pill beside the card title, as on every other service card. Three states are worth knowing:

- **Partial** on a granted service — the consent screen's boxes were not all ticked.
- **"Your grant is narrower than what this instance now asks for"** — the selection or the ceiling changed after the grant was made. Some commands will fail until the user reconnects. This is the state behind most "the bot can't see my calendar" reports.
- **"Also granted, not recognised as a service"** — a scope in the token that Istota's service map does not know, usually a hand-edited `scopes` list. It is displayed verbatim rather than hidden.

Changing the selection requires reconnecting: Google has to ask again. The connect flow already sets `access_type=offline` and `prompt=consent`, so a reconnect issues a fresh refresh token.

:::note
The consent screen appears on every connect, not only the first. Istota requests offline access and forces consent on reconnect so it can obtain a refresh token again. Offline access alone can omit a refresh token when the user has already authorized the client. Without one, the new access token would expire without a way to renew it.
:::

### Troubleshooting setup

| Symptom | What to check |
|---|---|
| Google Workspace card is missing | Enable `google_workspace.enabled` in the instance configuration and apply the restart. |
| “Google Workspace not configured” | Set the client ID and enable the integration, then restart the web process. |
| `redirect_uri_mismatch` | Compare Google's registered URI with the URL built from `site.hostname`, including `/istota/google/callback`. |
| `invalid_client` or a failed code exchange | Check that the client ID and secret belong to the same Web application client and that both services loaded the new secret. |
| Access blocked or user not allowed | Check the app's audience, test-user list, verification status and Workspace administrator's app policy. |
| Reconnect needed after seven days | Check whether the External OAuth app is still in Testing. |
| An API is disabled or permissions are missing | Enable the service's API in the same Cloud project, check the instance scope ceiling, then have the user select access and reconnect. |

Replacing the OAuth client ID requires users to reconnect: existing refresh tokens belong to the old client. Rotating a secret for the same client is different; update the instance credential and restart both services before retiring the old secret.

## Usage

Once connected, the bot can use `gws` commands for any task that matches the skill triggers (e.g., "upload this to google drive", "create a spreadsheet", "check my google calendar").

The bot invokes `istota-skill google_workspace <args>` (the skill wrapper); the OAuth token is injected proxy-side rather than exposed to the model.

### Example interactions

- "Upload the Q1 report to my Google Drive"
- "Create a spreadsheet with these expenses"
- "What's on my Google Calendar this week?"
- "Send an email via Gmail to user@example.com"
- "Read the data from my Budget spreadsheet"

## Settings

`[google_workspace]` in `config.toml` holds `enabled`, `client_id` and `scopes` (the ceiling each user's own selection is bounded by). The client secret is `ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET`; on a server it is the secret file `istota_google_workspace_client_secret`.

## Security

- OAuth tokens are stored in the database, scoped per-user
- The access token is routed through the credential proxy (`GOOGLE_WORKSPACE_CLI_TOKEN` is stripped from the subprocess env and injected server-side)
- Network isolation allowlists specific Google API hosts (googleapis.com subdomains) when the user's Google credentials are present (authorized via the credential set, decoupled from prompt-time skill selection)
- Users can only access their own Google account data
- Disconnect removes all stored tokens immediately
- The per-user scope selection is clamped to the instance ceiling server-side, at connect time — a client asking for more than the operator offers gets the operator's level, not its own
- The token is what Google enforces. The scope selection narrows what a user hands over; it is not a second authorization layer, and the network allowlist is not keyed on it

## Scopes reference

One row per service, matching `src/istota/credentials/google_scopes.py` — the single table the picker, the granted-scope display and this document all read.

| Service | Read-only | Read and write |
|---|---|---|
| Drive | `.../auth/drive.readonly` | `.../auth/drive` |
| Gmail | `.../auth/gmail.readonly` | `.../auth/gmail.modify` |
| Calendar | `.../auth/calendar.readonly` | `.../auth/calendar` |
| Sheets | `.../auth/spreadsheets.readonly` | `.../auth/spreadsheets` |
| Docs | `.../auth/documents.readonly` | `.../auth/documents` |
| Contacts | `.../auth/contacts.readonly` + `.../auth/contacts.other.readonly` | `.../auth/contacts` + `.../auth/contacts.other.readonly` |
| Chat | `.../auth/chat.spaces.readonly` + `.../auth/chat.messages.readonly` | `.../auth/chat.spaces` + `.../auth/chat.messages` |

Prefix every scope with `https://www.googleapis.com`. Gmail's write level is `gmail.modify` rather than the bare `gmail` scope: it covers reading, sending and labelling without granting permanent deletion.

Chat needs both a spaces scope and a messages scope at each level, and appears in no default configuration — the skill documents `chat spaces` verbs that need it added to `scopes` (and the Chat API enabled in the Cloud project) before they work.

A scope outside this table still works if you put it in `scopes`. It gets no picker row, so no user can decline it — which is exactly why it is **requested unconditionally**, on top of whatever they did choose, and named on the card as something the instance always asks for. That is what keeps narrow scopes (`drive.file`, `gmail.send`, `calendar.events`) working, and it is why an entirely unmapped `scopes` list still connects. A grant carrying an unmapped scope is displayed verbatim under "not recognised as a service".
