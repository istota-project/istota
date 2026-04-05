# Web interface

SvelteKit frontend with FastAPI backend, authenticated via Nextcloud OIDC.

## Setup

Register an OAuth 2.0 client in Nextcloud: Settings > Administration > Security > OAuth 2.0 clients. Set the redirect URI to `https://{hostname}/istota/callback`.

```toml
[web]
enabled = true
port = 8766
oidc_issuer = "https://cloud.example.com"
oidc_client_id = "istota-web"
oidc_client_secret = ""    # or ISTOTA_OIDC_CLIENT_SECRET env var
session_secret_key = ""    # or ISTOTA_WEB_SECRET_KEY env var
```

Install web extras and build the frontend:

```bash
uv sync --extra web
cd web && npm install && npm run build
```

Run the web app:

```bash
uvicorn istota.web_app:app --port 8766
```

## Pages

**Dashboard**: shows available features for the authenticated user. When [Google Workspace](google-workspace.md) is enabled, the dashboard also shows a connect/disconnect card for linking a Google account.

**Feeds**: Miniflux RSS feed reader with masonry card grid, image/text filter, sort by published/added, grid/list view, image lightbox. Viewport-based read tracking marks entries as read in Miniflux after 1.5s visible. Requires a `miniflux` resource.

**Ledgers**: lists beancount ledgers with links to Fava instances. Fava is reverse-proxied via nginx at `/istota/fava/{user}/{ledger}/` with `auth_request` gating.

**Location**: today view (current position, day summary, trips), history (date picker, activity filter, heatmap), places (discover clusters, create/edit/delete, visit stats). Requires GPS tracking to be enabled.

## Authentication

`preferred_username` from the OIDC token must exist in `config.users`. Sessions are cookie-based (7-day expiry), rotated on login. CSRF protection via Origin header validation on state-changing endpoints.

## API routes

| Route | Purpose |
|---|---|
| `/istota/login` | OIDC redirect |
| `/istota/callback` | Token exchange |
| `/istota/logout` | Session clear |
| `/istota/api/me` | User info + features |
| `/istota/google/connect` | Google OAuth initiation |
| `/istota/callback/google` | Google OAuth callback |
| `/istota/api/google/status` | Google connection status |
| `/istota/api/google/disconnect` | Remove Google tokens |
| `/istota/api/feeds` | Miniflux proxy |
| `/istota/api/moneyman/ledgers` | Moneyman API proxy |
| `/istota/api/location/*` | Places CRUD, pings, trips |

The SvelteKit build is served as static files for all other `/istota/*` paths.

## Deployment

The Ansible role handles the Node.js build when `istota_web_enabled` is set. The web app runs as a separate systemd service alongside the scheduler.
