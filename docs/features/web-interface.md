# Web interface

SvelteKit frontend with FastAPI backend, authenticated via Nextcloud OIDC.

The web UI is per-user: each authenticated user sees only the features they have configured (feeds, money, location, etc.). Nextcloud's OpenID Connect plugin handles authentication, so any user with a Nextcloud account and an entry in istota's `config.users` can log in.

## Prerequisites

- A Nextcloud instance (the same one istota connects to for Talk and files)
- The **OpenID Connect (OIDC)** Nextcloud app installed and enabled
- An nginx reverse proxy (or equivalent) fronting the istota web service
- Node.js 20+ for building the SvelteKit frontend

## Nextcloud OIDC setup

### 1. Install the OpenID Connect app

In Nextcloud, go to **Apps** (top-right menu) and search for "OpenID Connect user backend" (package name `user_oidc`). Install and enable it. This app turns your Nextcloud instance into an OpenID Connect provider, exposing a `.well-known/openid-configuration` discovery endpoint.

If your Nextcloud already has third-party OIDC configured (e.g., for SSO), you don't need a second app — the built-in OAuth 2.0 client registration is enough. Istota only needs the standard OIDC discovery + token endpoints.

### 2. Register an OAuth 2.0 client

Go to **Settings > Administration > Security > OAuth 2.0 clients** and add a new client:

| Field | Value |
|---|---|
| Name | `istota-web` (or any label you prefer) |
| Redirect URI | `https://{your-hostname}/istota/callback` |

Nextcloud generates a **Client ID** and **Client Secret**. Copy both — you'll need them for istota's config.

The redirect URI must exactly match the callback route. If you're running behind a reverse proxy at a subpath or different hostname, adjust accordingly.

### 3. Configure istota

In your `config.toml` (or via Ansible vars):

```toml
[web]
enabled = true
port = 8766
oidc_issuer = "https://cloud.example.com"
oidc_client_id = "your-client-id-from-step-2"
oidc_client_secret = ""    # or set ISTOTA_OIDC_CLIENT_SECRET env var
session_secret_key = ""    # or set ISTOTA_WEB_SECRET_KEY env var
```

| Setting | Description |
|---|---|
| `oidc_issuer` | Your Nextcloud URL (no trailing slash). Istota appends `/index.php/.well-known/openid-configuration` to discover endpoints. |
| `oidc_client_id` | The client ID from the OAuth 2.0 registration. |
| `oidc_client_secret` | The client secret. Prefer the `ISTOTA_OIDC_CLIENT_SECRET` env var over storing this in the config file. |
| `session_secret_key` | Random string for encrypting session cookies. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`. Use the `ISTOTA_WEB_SECRET_KEY` env var in production. |

When using the Ansible role, set these in your vars:

```yaml
istota_web_enabled: true
istota_web_oidc_issuer: "https://cloud.example.com"
istota_web_oidc_client_id: "your-client-id"
istota_web_oidc_client_secret: "{{ vault_istota_oidc_secret }}"
istota_web_secret_key: "{{ vault_istota_web_secret }}"
```

Secrets stored in `secrets.env` (via `istota_use_environment_file: true`) are injected as env vars by systemd, keeping them out of the config file.

### 4. Build the frontend

```bash
uv sync --extra web
cd web && npm install && npm run build
```

The Ansible role handles this automatically when `istota_web_enabled` is set and `istota_nodejs_enabled` is true.

### 5. Reverse proxy

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

TLS is required — session cookies are set with `secure=true` and the OIDC flow depends on HTTPS redirect URIs. Use Let's Encrypt or your preferred certificate provider.

### 6. Run

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
2. Istota redirects to Nextcloud's OIDC authorization endpoint
3. User authenticates with their Nextcloud credentials (or is already logged in)
4. Nextcloud redirects back to `/istota/callback` with an authorization code
5. Istota exchanges the code for an ID token, extracts `preferred_username`
6. If the username exists in `config.users`, a session cookie is set (7-day expiry)
7. Subsequent requests use the session cookie — no re-authentication until expiry or logout

The `preferred_username` claim from the OIDC token must match a user ID in istota's config. Users not in the config are rejected even if they have a valid Nextcloud account.

## Pages

**Dashboard**: shows available features for the authenticated user. When [Google Workspace](google-workspace.md) is enabled, the dashboard also shows a connect/disconnect card for linking a Google account.

**Feeds**: Miniflux RSS feed reader with masonry card grid, image/text filter, sort by published/added, grid/list view, image lightbox. Viewport-based read tracking marks entries as read in Miniflux after 1.5s visible. Requires a `miniflux` resource.

**Money**: accounting dashboard with ledger queries, transaction management, invoicing, and work log tracking. Backed by the in-process `money` module (no external service). Requires a `money` resource.

**Location**: today view (current position, day summary, trips), history (date picker, activity filter, heatmap), places (discover clusters, create/edit/delete, visit stats). Requires GPS tracking to be enabled.

## API routes

| Route | Purpose |
|---|---|
| `/istota/login` | OIDC redirect |
| `/istota/callback` | Token exchange |
| `/istota/logout` | Session clear |
| `/istota/api/me` | User info + features |
| `/istota/google/connect` | Google OAuth initiation |
| `/istota/google/callback` | Google OAuth callback |
| `/istota/api/google/status` | Google connection status |
| `/istota/api/google/disconnect` | Remove Google tokens |
| `/istota/api/feeds` | Miniflux proxy |
| `/istota/money/api/*` | Money module (ledger, transactions, invoicing) |
| `/istota/api/location/*` | Places CRUD, pings, trips |

The SvelteKit build is served as static files for all other `/istota/*` paths.

## Deployment

The Ansible role handles the Node.js build when `istota_web_enabled` is set. The web app runs as a separate systemd service alongside the scheduler.
