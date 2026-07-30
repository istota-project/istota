# Web interface

SvelteKit frontend with FastAPI backend, authenticated against Nextcloud's built-in OAuth2 provider.

The web UI is per-user: each authenticated user sees only the features they have configured (feeds, money, location, etc.). Any user with a Nextcloud account and an entry in istota's `config.users` (or a row in the `user_profiles` table) can log in.

## Prerequisites

- A Nextcloud instance (the same one istota connects to for Talk and files)
- An nginx reverse proxy (or equivalent) fronting the istota web service
- Node.js 20+ for building the SvelteKit frontend

No extra Nextcloud apps are required — istota uses NC's built-in OAuth 2.0 provider.

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

The Ansible role generates an nginx config automatically. The relevant block:

```nginx
location /istota/ {
    proxy_pass http://127.0.0.1:8766/istota/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

TLS is required — session cookies are set with `secure=true` and the registered redirect URI must use HTTPS. Use Let's Encrypt or your preferred certificate provider.

### 5. Run

```bash
uvicorn istota.web_app:app --host 127.0.0.1 --port 8766
```

The Ansible role installs this as the `istota-web` systemd service:

```bash
systemctl enable --now istota-web
systemctl status istota-web
journalctl -u istota-web -f
```

## How authentication works

1. User visits `https://{hostname}/istota/` and is redirected to `/istota/login`
2. Istota redirects to Nextcloud's OAuth 2.0 authorization endpoint (`{oauth2_provider}/index.php/apps/oauth2/authorize`)
3. User authenticates with their Nextcloud credentials (or is already logged in)
4. Nextcloud redirects back to `/istota/callback` with an authorization code
5. Istota exchanges the code for an access token; NC inlines `user_id` in the token response, so identity is known without a second round-trip
6. The access token is dropped immediately — only the username + display_name are kept in the session
7. If the username exists in `config.users` (or auto-seeds a `user_profiles` row), a signed session cookie is set (7-day expiry)
8. Subsequent requests use the session cookie — no re-authentication until expiry or logout

If the token response doesn't include `user_id` (older NC versions or custom auth backends), istota falls back to fetching identity from the OCS userinfo endpoint with the bearer token before discarding it.

Users not in the config are rejected with a 403 even if they have a valid Nextcloud account.

A callback that fails does not 500. A state mismatch or a declined authorization renders a login-shaped error card with a 400, and an unreachable provider a 502; the card never echoes provider- or exception-derived text back to the browser. Logging out is confirmation-gated in the UI — the logout and menu icons sit side by side and are small on a phone, so a mistap used to end the session and send you back through the login screen.

A light/dark theme toggle in the shell header switches the whole UI between themes; the choice persists per browser.

## Installing to a home screen

The UI ships a favicon, an Apple touch icon and a web app manifest, so adding it to a phone's home screen gives the Istota mark rather than a screenshot of the page, and the browser chrome takes the colour of the theme you picked in the app rather than the system's. There is no service worker: hashed assets are cached for a year and everything else revalidates, so a deployment can never leave an installed app pinned to a shell whose chunks the server has since deleted. When a new build lands, a toast offers a Reload rather than reloading under you — it also re-checks when you return to the app, since a suspended one stops polling.

## Pages

**Chat**: an always-on, full-page in-app chat console — the first nav tab, before Feeds. Discord/Slack-style rooms in a sidebar, live SSE streaming of tool use and intermediate text, `!commands` and the `!model` prefix, confirmation cards, attachments (drag, paste, the `+` button, or a voice message), and clickable attachment chips. See [Web chat](web-chat.md) for the full surface.

**Dashboard**: shows available features for the authenticated user. When [Google Workspace](google-workspace.md) is enabled, the dashboard also shows a connect/disconnect card for linking a Google account.

**Feeds**: RSS feed reader with masonry card grid, image/text filter, sort-by dropdown (published/added), grid/list view, navigable image lightbox, and a click-to-expand reader overlay that shows a card's full un-clipped content with `←`/`→` navigation between posts and an "Open original" link. The sidebar scopes the view to all, unread, an individual feed, or a whole category (click a category name to filter to it). Per-entry starring (`f` keyboard shortcut) and scope-aware bulk mark-as-read (`Shift-A` / toolbar button) honor the active feed or category scope. Viewport-based read tracking marks entries as read after 1.5s visible. Repeat images are suppressed as a reblogged photo travels through the blogs you follow: a duplicate inside one post is dropped, and across posts an image a newer entry already showed is hidden on the older ones (the post still appears, with a note counting the hidden repeats). Suppression is bounded to a recent look-back window and to the view you are in, so an image resurfacing much later still shows and browsing one blog never hides a tile because of another. Sprocket-icon settings page for managing subscriptions, categories, OPML import/export, and the repeat-image look-back window (switchable off). Served by the in-tree `istota.feeds` module against per-user SQLite. Requires the `feeds` module to be enabled (on by default).

**Briefings**: reader landing page for generated briefings with an archive sidebar (per-result kebab → delete) and a name filter in the header, plus a settings page (cog) for editing a briefing's content blocks and their sources, its schedule, and its delivery target. Source paths use a searching file picker with an advisory existence check. Admins additionally get a "Shared blocks" card for the module-owned blocks every user can read. Requires the `briefings` module to be enabled (on by default).

**Money**: accounting dashboard with ledger queries, transaction management, and reports. The Business section is **Work | Invoices | Clients**: Work is a full CRUD surface over the file-based work-entry store (entries addressed by stable id, with per-entry etags so a concurrent agent edit conflicts rather than being silently reverted), and Clients plus the money settings page are the CRUD surface over the invoicing config — clients, entities and services — so nothing about invoicing needs the CLI. Backed by the in-process `money` module (no external service). Requires the `money` module to be enabled (on by default).

**Admin**: read-only system health (task counts by source, worker pool, per-module DB stats, models pane showing the active brain and its resolved role tiers). A banner surfaces a degraded primary brain — when the availability breaker is open, automatic work is being skipped or routed to the fallback. Gated by the `/etc/istota/admins` allowlist, which fails closed when empty.

**Health**: body stats grid with sparklines, bloodwork matrix (dates × markers with flag-colored cells, CSV import/export), panel detail with inline edit and source preview, per-marker trend charts with out-of-range zones and LLM explainer, medical history timeline with encounters and diagnoses, immunization tracking with coverage status strip, vaccine drill-down pages with clinical explainers. Garmin Connect (daily-summary sync) is on the general Settings → Connected services page, shared with Location. Requires the `health` module to be enabled (on by default).

**Location**: today view (current position, day summary, trips), history (date picker, activity filter, heatmap), places (discover clusters, create/edit/delete, visit stats). Requires GPS tracking to be enabled.

## API routes

| Route | Purpose |
|---|---|
| `/istota/login` | OAuth2 redirect |
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
| `/istota/api/chat/rooms` | Room CRUD (list/create); `DELETE /chat/rooms/{id}` hard-deletes |
| `/istota/api/chat/rooms/{id}/messages` | Message history + send |
| `/istota/api/chat/tasks/{id}/stream` | SSE stream of a task's events (tool use, text deltas) |
| `/istota/api/chat/tasks/{id}/events` | Snapshot of a task's events |
| `/istota/api/chat/tasks/{id}/confirm` · `/cancel` | Confirm / cancel a chat task |
| `/istota/api/chat/attachments` | Attachment upload (multipart, one file per request) |

The SvelteKit build is served as static files for all other `/istota/*` paths.

## Deployment

The Ansible role handles the Node.js build when `istota_web_enabled` is set. The web app runs as a separate systemd service alongside the scheduler.
