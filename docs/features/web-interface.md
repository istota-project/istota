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

A light/dark theme toggle in the shell header switches the whole UI between themes; the choice persists per browser.

## Pages

**Chat**: an always-on, full-page in-app chat console — the first nav tab, before Feeds. Discord/Slack-style rooms in a sidebar, live SSE streaming of tool use and intermediate text, `!commands` and the `!model` prefix, confirmation cards, and drag-drop/paste attachments. See [Web chat](web-chat.md) for the full surface.

**Dashboard**: shows available features for the authenticated user. When [Google Workspace](google-workspace.md) is enabled, the dashboard also shows a connect/disconnect card for linking a Google account.

**Feeds**: RSS feed reader with masonry card grid, image/text filter, sort-by dropdown (published/added), grid/list view, navigable image lightbox, per-entry starring (`f` keyboard shortcut), and scope-aware bulk mark-as-read (`Shift-A` / toolbar button). Viewport-based read tracking marks entries as read after 1.5s visible. Sprocket-icon settings page for managing subscriptions, categories, and OPML import/export. Served by the in-tree `istota.feeds` module against per-user SQLite. Requires the `feeds` module to be enabled (on by default).

**Money**: accounting dashboard with ledger queries, transaction management, invoicing, and work log tracking. Backed by the in-process `money` module (no external service). Requires the `money` module to be enabled (on by default).

**Health**: body stats grid with sparklines, bloodwork matrix (dates × markers with flag-colored cells, CSV import/export), panel detail with inline edit and source preview, per-marker trend charts with out-of-range zones and LLM explainer, medical history timeline with encounters and diagnoses, immunization tracking with coverage status strip, vaccine drill-down pages with clinical explainers, Garmin Connect integration on the settings page. Requires the `health` module to be enabled (on by default).

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
| `/istota/money/api/*` | Money module (ledger, transactions, invoicing) |
| `/istota/api/location/*` | Places CRUD, pings, trips |
| `/istota/api/health/*` | Stats, panels, biomarkers, encounters, diagnoses, immunizations, Garmin, settings |
| `/istota/api/chat/config` | Chat limits + streaming intervals |
| `/istota/api/chat/rooms` | Room CRUD (list/create); `DELETE /chat/rooms/{id}` hard-deletes |
| `/istota/api/chat/rooms/{id}/messages` | Message history + send |
| `/istota/api/chat/tasks/{id}/stream` | SSE stream of a task's events (tool use, text deltas) |
| `/istota/api/chat/tasks/{id}/events` | Snapshot of a task's events |
| `/istota/api/chat/tasks/{id}/confirm` · `/cancel` | Confirm / cancel a chat task |
| `/istota/api/chat/attachments/upload` | Attachment upload (multipart) |

The SvelteKit build is served as static files for all other `/istota/*` paths.

## Deployment

The Ansible role handles the Node.js build when `istota_web_enabled` is set. The web app runs as a separate systemd service alongside the scheduler.
