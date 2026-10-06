# Web interface

SvelteKit frontend with FastAPI backend. Sign in with Nextcloud, an email address and password, or a one-time email link, according to the deployment's enabled methods.

The web UI is per-user: each authenticated user sees only the features they have configured (feeds, money, location, etc.). Nextcloud login requires a configured user. Email login requires a live user profile and an enabled email identity, read from the database on each login.

## Browser history

Selections within a page appear in its URL and survive a reload or a shared link. Back and Forward restore the previous selection. Moving to another place adds a history entry; changing the year or date range replaces the current entry. Defaults and fallbacks also replace the current entry, so loading a list does not add a Back step. Free-text searches, display preferences and open overlays are not part of this history.

| Page | Selection | URL parameter | History behavior |
|---|---|---|---|
| Chat | Room | `room` | Add an entry |
| Chat | All, Unread or Starred view | `view` | Add an entry |
| Chat | Search result or task link | `room`, `task` | Add an entry |
| Chat | Cited message | `room`, `msg` | Add an entry |
| Briefings | Archive item | `id` | Add an entry |
| Briefings | Briefing name filter | `name` | Add an entry |
| Feeds | Feed, category, Starred or Unread view | `feed`, `category`, `view` | Add an entry |
| Money transactions | Account | `account` | Add an entry |
| Money transactions | Year (`all` for All years) | `year` | Replace the current entry |
| Admin logs | Log source | `source` | Add an entry |
| Health stats | Date range | `range` | Replace the current entry |

## Prerequisites

- A Nextcloud instance for Nextcloud login, or operator-created email identities for email login
- An nginx reverse proxy (or equivalent) fronting the istota web service
- Node.js 20+ for building the SvelteKit frontend

No extra Nextcloud apps are required — istota uses NC's built-in OAuth 2.0 provider.

## Email login

Set `[web] auth = ["email"]` or `["nextcloud", "email"]`, provide a persistent session signing secret, and set `[site] hostname` to the public hostname. `ISTOTA_WEB_AUTH=email` or `nextcloud,email` overrides the config. Nextcloud remains the default regardless of the storage backend. Use HTTPS outside localhost.

Bootstrap the first administrator from the deployment shell, against the deployment's config and database:

```bash
istota init
istota user ensure alice --display-name Alice --email alice@example.com
istota auth add alice --email alice@example.com --print-link
```

Add `alice` to `/etc/istota/admins` before inviting more users. An empty allowlist grants every user task-admin privileges for historical compatibility, while the web admin pane requires an explicit entry. Doctor fails this combination when multiple email identities exist. The printed enrolment link opens a password form and signs the user in on submission. Set a password for the first administrator so mail failure cannot prevent login. `istota auth set-password alice` prompts privately; `--password-stdin` reads one line for automation. There is no password argument on the command line.

The login page has password and email-code forms whenever email is enabled. Asking for a code opens a pending sign-in tied to that browser: the browser keeps a secret in its session, and the emailed 6-digit code works only together with it. The code is valid for 10 minutes by default and dies after five wrong guesses. Asking again from the same browser retires the earlier code only once a new one is sent, so a request over the mail budget leaves the last code working. Wrong codes are also capped at 20 per address per day across every browser, because anyone can ask for a code for any address; `istota auth sign-in-code` clears that cap. The mail carries no link, so opening it in another app or on another device changes nothing, and the iOS app's sign-in completes in the app. The code sits in the subject and on its own line, which lets Apple's one-time-code autofill offer it from Mail. Enrolment and reset links set a password and sign in on submission, with default lifetimes of seven days and one hour. Anonymous reset links and sign-in codes share a send budget of three mails per address per hour and return the same page for unknown, disabled and throttled addresses. Passwordless identities can sign in by code; they are not incomplete accounts.

| Operator command | Effect |
|---|---|
| `istota auth list` | Profiles and identities, password and disabled flags, last login, orphan status |
| `istota auth add alice --email alice@example.com` | Attach email to an existing profile; add `--create-user` to create one |
| `istota auth invite alice --send` | Send an enrolment link |
| `istota auth reset alice --print-link` | Print a password-reset link when mail is unavailable |
| `istota auth sign-in-code alice` | Print a fresh code for the newest pending email sign-in for alice's address, with when it was requested, for when mail is down. It works only in the browser that asked, and anyone can open a request for an address, so confirm the request time with alice before reading the code out |
| `istota auth disable alice` / `enable alice` | Change login availability and revoke existing sessions |
| `istota auth logout-all alice` | Revoke every session for an identity |
| `istota auth remove alice` | Remove login identity and invalidate its links; preserve profile and user data |

Both link commands accept `--send` or `--print-link`; omitted means send. `auth add` has `--send-invite` and `--print-link`. Successful commands end with `state=created`, `state=updated` or `state=unchanged`; list always reports unchanged, and issued links and credential operations report updated. Identity changes are separate from inbound routing addresses.

The admin Users pane can create a profile, attach or change a login email (in the per-user editor described under [User settings editor](#user-settings-editor)), send invitation and reset links, disable login and remove an identity. It has no action that sends a sign-in code, because a code only works in the browser that asked for it. It refuses disabling or removing the last enabled administrator identity, even with Nextcloud also enabled. The operator CLI remains the recovery authority and can override that protection. Disable blocks both email and Nextcloud login for that identity. Removing an identity blocks email login and revokes its sessions, but leaves Nextcloud login possible if that method is enabled. Nextcloud-only profiles have no per-user revoke or disable control until an email identity is attached. New users can sign in immediately, and the scheduler picks them up within one tick.

Settings has a Security card for email sessions. Password users supply their current password to change it; the change signs out every tab and device, including the caller. Passwordless users see a set-password link. Attaching an identity, changing its email or password, disabling it, and signing out everywhere revoke the user's existing sessions, including Nextcloud sessions. Epochs start with a random generation and then increase, so deleting and recreating an identity cannot revive old cookies. Removing an identity retains a session generation in `web_auth_retired_epochs`, so earlier Nextcloud and legacy cookies stay revoked; a fresh Nextcloud login uses that retained generation. Database read errors fail closed. Removing an authentication method rejects sessions minted by that method. Active streams repeat the checks and close after revocation.

Run `istota doctor --only web.auth` for method source, hostname, mail configuration, identity counts, admin allowlist, proxy IP throttle and session-secret checks. Mail configuration checks do not send a probe. The IP verification budget is inactive while `web.trusted_proxy_hops` is zero. A positive value selects the client address from the forwarded chain; only enable it when the backend cannot be reached around those proxies.

The shipped proxies suppress access logs for `/istota/auth/set-password`; both web launchers disable uvicorn access logs. An outer proxy must do the same or omit query strings, since the token grants access. Authentication pages are not cached and send no referrer. `["none"]` is reserved for the direct `istota serve` loopback launcher without a public reverse proxy; Docker, Ansible and direct uvicorn refuse it.

## User settings editor

Admin → Users opens a settings editor for one user: click the user's row, or pick "Edit settings" from its menu. A user you add from the same page opens in the editor once it is created. The editor sets the per-user fields that otherwise come from `istota user ensure` or the Ansible `istota_users` inventory, so a new person can be set up without a deploy. It does not edit the admin list (the admins file), resources, secrets, or the preferences a user owns on their own `/settings` page, such as routing and default rooms.

The sections are:

- **Identity**: display name and timezone. The admin badge is shown read-only.
- **Login**: the current login email, its sign-in state and the last login, and a field to attach or change it. "Also add to email addresses" also lists the address for inbound mail, and "Send invitation" mails an enrolment link. The Login section saves with its own button, never with the footer Save. Invitations, password resets, signing out, disabling and removing the login stay in the row menu.
- **Email**: inbound email addresses, trusted senders, quiet senders, and outbound approval. A value below the deployment's `outbound_approval_floor` is refused unless it is already stored.
- **Phone**: the SMS number and the WhatsApp number, in E.164 form. "Same as SMS" copies the SMS number into the WhatsApp field. Below it are the WhatsApp enrollment state (Unbound, Waiting for first message, Enrolled, Opted out), the masked identity and when it was last seen. "Reset identity" keeps the number and forgets the enrolled phone, so the next message from that number enrolls again; use it when someone moves the number to a new phone. Changing an enrolled number discards the enrollment, the open service window and any opt-out. Each phone field is hidden when its surface is off, unless a value is already stored.
- **Access and limits**: disabled modules and skills, the foreground and background worker limits (empty means the deployment default), and whether the default briefings are seeded. The last only matters when the user's briefings are first created.
- **Channels**: the log and alerts channel tokens, read-only.

The footer Save sends only the fields you changed, and either all of them are written or none are. A refused field is marked with the server's reason.

**Fields set by the deployment are locked.** With `istota_user_profile_mode: enforce` (the default), every field the inventory names for a user shows "Set by deployment", cannot be edited, and is refused with 409 by the API. The hint names the inventory key to change instead. The user's own `/settings` page shows the same fields as "Set by your administrator". Set the mode to `seed` to hand these fields to the web UI; see [Ansible: who owns a profile field](../deployment/ansible.md#who-owns-a-profile-field-istota_user_profile_mode).

**Addresses and numbers belong to one user.** An email address, SMS number or WhatsApp number that another user already holds is refused, and the refusal names that user. An address counts as held when it is in another user's email addresses or is their login email. A duplicate stored before this check is not refused on resubmit, and `istota doctor --only users.email_address_uniqueness` lists any that remain. A user editing their own addresses on `/settings` gets the same refusal without the holder's name.

**Changing the login email signs the user out.** It ends every session the user has, including Nextcloud sessions, and voids any invitation or reset link already sent. The editor says so before you save. If sending the invitation fails, the new login email is still saved.

**Saves take effect without a restart.** The scheduler, the web app and the SMS webhook re-read user profiles within a few seconds of a change, so a new address or number routes mail and texts on the next scheduler tick.

**The user is told.** When an admin changes another user's email addresses, SMS number, WhatsApp number or login email, that user gets a notification, "An administrator changed your contact details", naming the fields and the admin but not the values. It goes through the user's normal alert delivery and closes once seen. Editing your own row raises nothing, and neither do changes to other fields.

Every admin save logs one `admin_user_update` line with the admin, the user and the field names, never the values. A refused collision logs `admin_user_conflict` with its kind (email, SMS or WhatsApp) and no value or holder.

## Nextcloud OAuth2 setup

### 1. Register an OAuth 2.0 client

In Nextcloud, go to **Settings > Administration > Security > OAuth 2.0 clients** and add a new client:

| Field | Value |
|---|---|
| Name | `istota-web` (or any label you prefer) |
| Redirect URI | `https://{your-hostname}/istota/callback` |

Nextcloud generates a **Client ID** and **Client Secret**. Copy both.

The redirect URI must exactly match the callback route. If you're running behind a reverse proxy at a subpath or different hostname, adjust accordingly.

### 2. Configure istota

In your `config.toml` (or via Ansible vars):

```toml
[web]
enabled = true
port = 8766
auth = ["nextcloud"]
oauth2_provider = "https://cloud.example.com"
oauth2_client_id = "your-client-id-from-step-1"
oauth2_client_secret = ""    # or set ISTOTA_WEB_OAUTH2_CLIENT_SECRET env var
session_secret_key = ""      # or set ISTOTA_WEB_SESSION_SECRET_KEY env var
```

| Setting | Description |
|---|---|
| `oauth2_provider` | Your Nextcloud URL (no trailing slash) — what the browser hits to authorize. |
| `oauth2_client_id` | The client ID from the OAuth 2.0 registration. |
| `oauth2_client_secret` | The client secret. Prefer the `ISTOTA_WEB_OAUTH2_CLIENT_SECRET` env var. |
| `session_secret_key` | Random string for signing session cookies. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`. Use the `ISTOTA_WEB_SESSION_SECRET_KEY` env var in production. |

Optional overrides (defaults derive from `oauth2_provider`):

| Setting | Description |
|---|---|
| `oauth2_token_endpoint` | Server-to-server token URL. In Docker this often points at the internal NC service URL while `oauth2_provider` points at the host-mapped URL. |
| `oauth2_userinfo_endpoint` | Server-to-server userinfo URL. Same Docker pattern. |
| `oauth2_redirect_uri` | Explicit redirect URI override; otherwise derived from request host + scheme. |

When using the Ansible role, set these in your vars:

```yaml
istota_web_enabled: true
istota_web_oauth2_provider: "https://cloud.example.com"
istota_web_oauth2_client_id: "your-client-id"
istota_web_oauth2_client_secret: "{{ vault_istota_oauth2_secret }}"
istota_web_secret_key: "{{ vault_istota_web_secret }}"
```

Secrets stored in `secrets.env` (via `istota_use_environment_file: true`) are injected as env vars by systemd, keeping them out of the config file.

### 3. Build the frontend

```bash
uv sync --extra web
cd web && npm install && npm run build
```

The Ansible role handles this automatically when `istota_web_enabled` is set and `istota_nodejs_enabled` is true.

### 4. Reverse proxy

The web app listens on `127.0.0.1:{port}` and should not be exposed directly. Put it behind nginx (or your preferred reverse proxy).

The Ansible role generates an nginx config automatically. The general location is below. Email login also requires the exact token location so query strings never enter access logs:

```nginx
location = /istota/auth/set-password {
    access_log off;
    proxy_pass http://127.0.0.1:8766;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location /istota/ {
    proxy_pass http://127.0.0.1:8766/istota/;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

TLS is required — session cookies are set with `secure=true` and the registered redirect URI must use HTTPS. Use Let's Encrypt or your preferred certificate provider.

### 5. Run

```bash
uvicorn istota.webui.app:app --no-access-log --host 127.0.0.1 --port 8766
```

The Ansible role installs this as the `istota-web` systemd service:

```bash
systemctl enable --now istota-web
systemctl status istota-web
journalctl -u istota-web -f
```

## How Nextcloud authentication works

1. User visits `https://{hostname}/istota/` and is redirected to `/istota/login`
2. Istota redirects to Nextcloud's OAuth 2.0 authorization endpoint (`{oauth2_provider}/index.php/apps/oauth2/authorize`)
3. User authenticates with their Nextcloud credentials (or is already logged in)
4. Nextcloud redirects back to `/istota/callback` with an authorization code
5. Istota exchanges the code for an access token; NC inlines `user_id` in the token response, so identity is known without a second round-trip
6. Token retention follows `web.token_storage`: ephemeral discards it; encrypted retains it with the web-only token key. The cookie carries the user and authentication method/epoch.
7. If the username exists in `config.users` (or auto-seeds a `user_profiles` row), a signed session cookie is set (7-day expiry)
8. Subsequent requests validate the session method and any attached email identity, including its disabled state and credential epoch

If the token response doesn't include `user_id` (older NC versions or custom auth backends), istota falls back to fetching identity from the OCS userinfo endpoint with the bearer token before discarding it.

Users not in the config are rejected with a 403 even if they have a valid Nextcloud account.

A callback that fails does not 500. A state mismatch or a declined authorization renders a login-shaped error card with a 400, and an unreachable provider a 502; the card never echoes provider- or exception-derived text back to the browser. Logging out is confirmation-gated in the UI — the logout and menu icons sit side by side and are small on a phone, so a mistap used to end the session and send you back through the login screen.

A light/dark theme toggle in the shell header switches the whole UI between themes; the choice persists per browser.

## Installing to a home screen

The UI ships a favicon, an Apple touch icon and a web app manifest, so adding it to a phone's home screen gives the Istota mark rather than a screenshot of the page, and the browser chrome takes the colour of the theme you picked in the app rather than the system's. A service worker is registered **only inside the iOS shell**, where it is what lets a cold launch with no connection reach the cached rooms and transcript; SvelteKit's own registration is off (`kit.serviceWorker.register = false` in `svelte.config.js`) and `routes/+layout.svelte` registers it behind `isNativeShell()`. An ordinary browser gets none of it, deliberately: hashed assets are cached for a year and everything else revalidates, so a continuously deployed app can never leave a browser pinned to a shell whose chunks the server has since deleted. See [web chat](web-chat.md#with-no-connection) for what the app keeps and how to clear it. When a new build lands, a toast offers a Reload rather than reloading under you — it also re-checks when you return to the app, since a suspended one stops polling.

The manifest asks for a portrait orientation. The layout is a single column with a docked composer, and landscape leaves too little height once the keyboard is up. That request only binds an Android home-screen install: iOS ignores the manifest key (the native shell locks iPhone to portrait itself, and leaves iPad free to rotate), and an ordinary browser tab cannot be locked at all.

## The app bar

A **notification bell** sits in the app bar on every page, not only in the chat. The badge counts what is open — a task held for your approval, a reply held at the outbound gate, a scheduled job that switched itself off, a credential the remote rejected, a bloodwork panel left in draft, or a one-shot alert. Clicking it opens a panel with an "All" and a "Needs action" tab, carrying the same Confirm and Discard buttons the chat cards have. An answer given on any surface closes the item on all of them. See [notifications and the inbox](notifications.md).

## Pages

**Chat**: an always-on, full-page in-app chat console — the first nav tab, before Feeds. Discord/Slack-style rooms in a sidebar, live SSE streaming of tool use and intermediate text, `!commands` and the `!model` prefix, confirmation cards, attachments (drag, paste, the `+` button, or a voice message), clickable attachment chips, and per-message copy / star / delete. Email thread rooms render each message as a mail card, are read-only, and sit in a collapsed "Email threads" group under the room list. See [Web chat](web-chat.md) for the full surface.

**Dashboard**: shows available features for the authenticated user. When [Google Workspace](google-workspace.md) is enabled, the dashboard also shows a connect/disconnect card for linking a Google account.

**Feeds**: RSS feed reader with masonry card grid, image/text filter, sort-by dropdown (published/added), grid/list view, navigable image lightbox, and a click-to-expand reader overlay that shows a card's full un-clipped content with `←`/`→` navigation between posts and an "Open original" link. The sidebar scopes the view to all, unread, an individual feed, or a whole category (click a category name to filter to it). Per-entry starring (`f` keyboard shortcut) and scope-aware bulk mark-as-read (`Shift-A` / toolbar button) honor the active feed or category scope. Viewport-based read tracking marks entries as read after 1.5s visible. Repeat images are suppressed as a reblogged photo travels through the blogs you follow: a duplicate inside one post is dropped, and across posts an image a newer entry already showed is hidden on the older ones (the post still appears, with a note counting the hidden repeats). Suppression is bounded to a recent look-back window and to the view you are in, so an image resurfacing much later still shows and browsing one blog never hides a tile because of another. Video embedded in a post plays inline with normal controls; nothing autoplays, and the image/text filter hides inline video along with pictures. Sprocket-icon settings page for managing subscriptions, categories, OPML import/export, and the repeat-image look-back window (switchable off). Served by the in-tree `istota.feeds` module against per-user SQLite. See [Feeds](feeds.md). Requires the `feeds` module to be enabled (on by default).

**Briefings**: reader landing page for generated briefings with an archive sidebar (per-result kebab → delete) and a name filter in the header, plus a settings page (cog) for editing a briefing's content blocks and their sources, its schedule, and its delivery target. Source paths use a searching file picker with an advisory existence check. Admins additionally get a "Shared blocks" card for the module-owned blocks every user can read. Requires the `briefings` module to be enabled (on by default).

**Money**: accounting dashboard with ledger queries, transaction management, and reports. The Business section is **Work | Invoices | Clients**: Work is a full CRUD surface over the file-based work-entry store (entries addressed by stable id, with per-entry etags so a concurrent agent edit conflicts rather than being silently reverted), and Clients plus the money settings page are the CRUD surface over the invoicing config — clients, entities and services — so nothing about invoicing needs the CLI. Backed by the in-process `money` module (no external service); also covers quarterly tax estimates and portfolio tracking. See [Money](money.md). Requires the `money` module to be enabled (on by default).

**Admin**: read-only system health (task counts by source, worker pool, per-module DB stats, models pane showing the active brain and its resolved role tiers). A banner surfaces a degraded primary brain — when the availability breaker is open, automatic work is being skipped or routed to the fallback. Gated by the `/etc/istota/admins` allowlist, which fails closed when empty.

A **Claude Code subscription** card carries the plan's rate-limit windows — a tile per window, tinted by the configured warn and high thresholds, plus an Extra usage tile where pay-as-you-go credits are enabled. It is the budget the cost column below it cannot report, since pricing plan tokens at list rates would be inventing an invoice. A footer gives the reading's age and, where the last fetch failed, the error behind the stale number. The card is **absent** rather than showing an error when there is no reading to draw — on a deployment that cannot use the subscription it never polls at all. See [the subscription reading](usage.md#the-subscription-reading).

A **Token usage** card carries 24-hour and 30-day totals, the cache hit rate, and average initial and peak context, then breaks the 30-day window down by model, by brain and by origin. Per-user tokens and cost sit on the Users rows beside that user's task counts rather than being repeated here, so the two copies cannot disagree. Cost is shown as currency only where it is real charged spend; a plan-equivalent or a catalog estimate renders as a dash. Two honesty counters sit alongside: how many tasks in the window recorded no usage at all, and how many rows recorded no context measurement. The card renders on its own — a database that has not yet been upgraded with the usage table takes out that section alone rather than blanking the dashboard. See [token usage and cost](usage.md).

A **Browsers** page lists each live browser instance (user, slot, idle time) and links to that user's noVNC view. It reads `GET /instances` on the browser service every ten seconds and never starts a browser or keeps an idle one alive, so only browsers already running appear. The links need `[browser] vnc_url`. Without it, or with one that is not an absolute `http(s)` URL or that embeds a VNC password, the page lists instances with no links. The viewer uses the deployment's own VNC authentication and network access. See [browser profiles](../deployment/docker.md#browser-profiles).

"Last active" in the user list counts only interactive tasks. Scheduled jobs, briefings and module pollers do not move it, so someone whose only traffic is automated shows a dash. Their task total still counts everything.

**Health**: body stats grid with sparklines, labs matrix (dates × markers with flag-colored cells, a specimen filter, CSV import/export), panel detail with inline edit and source preview, per-marker trend charts with out-of-range zones and LLM explainer, medical history timeline with encounters and diagnoses, immunization tracking with coverage status strip, vaccine drill-down pages with clinical explainers. Garmin Connect (daily-summary sync) is on the general Settings → Connections page, shared with Location. Requires the `health` module to be enabled (on by default).

**Location**: today view (current position, day summary, trips), history (date picker, activity filter, heatmap), places (discover clusters, create/edit/delete, visit stats). Requires GPS tracking to be enabled.

**Settings**: a sidebar of five sections — Account (identity, profile picture, sign-in), Preferences (appearance, email senders, disabled skills and modules, offline data), Delivery (default destination, relay questions, alert and log routes), Credentials and Connections (Nextcloud, Google Workspace, Garmin, Karakeep, ntfy) — plus the per-module settings pages. Connections is where service credentials are entered — write-only, bullet-masked fields backed by the encrypted secrets store. Credentials lists every credential a task can use, with its bound hosts and the grant that decides which rooms and scheduled tasks may use it, edited from each row's menu. The [credential vault](../configuration/credentials.md#credential-vault) card sits on that section too, and is the one surface a user sets one up from: it lists the `.kdbx` files in their own vault folder, takes a filename rather than a path, generates the passphrase, and names the shared credentials istota holds from the file.

## API routes

| Route | Purpose |
|---|---|
| `/istota/login` | Enabled login methods, password and email-code forms |
| `/istota/login/email` | Password sign-in (POST) |
| `/istota/auth/sign-in-code/request` | Request an emailed sign-in code for this browser (POST) |
| `/istota/auth/sign-in-code` | Enter the code (GET/POST) |
| `/istota/auth/reset` | Request a password-reset link (GET/POST) |
| `/istota/auth/set-password` | Enrolment or reset form (GET/POST) |
| `/istota/api/account/password` | Change password and end every session (POST) |
| `/istota/api/admin/users` | List or create profiles and email identities |
| `/istota/api/admin/users/{user_id}` | One user's settings (GET, PATCH); remove an email identity (DELETE); action suffixes: invite, reset, disable, logout-all (POST) |
| `/istota/api/admin/users/{user_id}/identity` | Attach or change the login email (PUT) |
| `/istota/api/admin/users/{user_id}/whatsapp/reset` | Forget the enrolled WhatsApp identity, keeping the number (POST) |
| `/istota/callback` | Token exchange + identity resolution |
| `/istota/logout` | Session clear |
| `/istota/api/me` | User info + features |
| `/istota/google/connect` | Google OAuth initiation (separate, for the gws skill) |
| `/istota/google/callback` | Google OAuth callback |
| `/istota/api/google/status` | Google connection status |
| `/istota/api/google/disconnect` | Remove Google tokens |
| `/istota/api/feeds` | Native feeds module (per-user SQLite) |
| `/istota/api/money/*` | Money module (ledger, transactions, invoicing, work entries, invoicing config) |
| `/istota/api/briefings/*` | Briefings module (reader, archive, blocks/sources editor, shared blocks) |
| `/istota/api/location/*` | Places CRUD, pings, trips |
| `/istota/api/health/*` | Stats, panels, biomarkers, encounters, diagnoses, immunizations, Garmin sync, settings |
| `/istota/api/garmin/*` | Garmin connected-service auth (status, connect, MFA, disconnect) + GPS track import; shared by Health and Location |
| `/istota/api/chat/config` | Chat limits + streaming intervals |
| `/istota/api/settings/*` | Per-user preferences, connected services, per-module settings |
| `/istota/api/admin/*` | Admin dashboard aggregates (stats, logs, config view) — allowlist-gated |
| `/istota/api/chat/rooms` | Room CRUD (list/create); `PATCH /chat/rooms/{id}` renames and changes per-user settings, `listed` among them (an email thread only; any other room is 400); `DELETE` hard-deletes |
| `/istota/api/chat/rooms/{id}/promote` | Create a Talk conversation for a web-origin room and bind them, or replace a binding whose conversation was deleted |
| `/istota/api/chat/rooms/{id}/read` · `/chat/rooms/read-all` | Mark read cursors |
| `/istota/api/chat/rooms/{id}/messages` | Message history + send. A send into an email room is 409 `read_only`. A send may carry `about_room: <token>` to link the turn to an email thread with no note to reply to; a link that fails its checks is 400 and creates no task |
| `/istota/api/chat/messages` | Cross-room message query |
| `/istota/api/chat/messages/{id}/star` · `DELETE /chat/messages/{id}` | Star / delete a message |
| `/istota/api/chat/stream` · `/chat/events` | Room-level event stream and snapshot |
| `/istota/api/chat/commands` | The `!command` catalogue for the composer |
| `/istota/api/chat/files` | Files shared into chat |
| `/istota/api/chat/tasks/{id}/stream` | SSE stream of a task's events (tool use, text deltas) |
| `/istota/api/chat/tasks/{id}/events` | Snapshot of a task's events |
| `/istota/api/chat/tasks/{id}/confirm` · `/cancel` | Confirm / cancel a chat task. Confirm takes an optional `{"room": <token>}`; a relay question, room post or guest proposal needs the private room showing its preview |
| `/istota/chat/r/{room}` · `/istota/chat/r/{room}/t/{task}` | Deep link from a notification: redirects to that room (and task) for a member, else to the chat page |
| `/istota/api/chat/attachments` | Attachment upload (multipart, one file per request) |
| `/istota/api/notifications/count` | Bell badge: `{"open": N, "actionable": M}`. Plain SQL, no resolver pass — the layout polls it every 30s from every route |
| `/istota/api/notifications` | The panel's rendered rows plus `total_open`; `?filter=` and `?limit=` |
| `/istota/api/notifications/{id}/dismiss` | Close one row. Another user's row is a 404, never a 403 |
| `/istota/api/notifications/seen` | Mark rendered rows seen. The body carries `(id, updated_at)` pairs, so a row bumped between the fetch and the POST is stamped but not closed |
| `/istota/api/map/basemap` | The resolved map tile spec for this user — style URLs and attribution, never a bare key. Reads the same resolver as doctor's `web.basemap` check, so the check cannot pass while the map is blank. See [`[web.map]`](../configuration/reference.md#webmap) |

The SvelteKit build is served as static files for all other `/istota/*` paths.

## Deployment

The Ansible role handles the Node.js build when `istota_web_enabled` is set. The web app runs as a separate systemd service alongside the scheduler.
