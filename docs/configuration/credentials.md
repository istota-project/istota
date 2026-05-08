# Credentials

One question decides where a credential lives: **whose account is it?**

- Bot authenticates as itself → **global credential**, injected at deploy time via TOML config or env var override.
- Bot accesses a user's account on their behalf → **per-user credential**, stored in the encrypted `secrets` table (or `google_oauth_tokens` for OAuth flows).

## Global credentials (bot identity)

These belong to the Istota instance, not to any user. They live in `config.toml` (or env var overrides) and are loaded once at startup. The [credential proxy](../deployment/security.md#credential-proxy) strips them from Claude's environment and injects them only into authorized skill subprocesses.

| Credential | Config section | Env var override | Consumed by |
|---|---|---|---|
| SMTP (email sending) | `[email]` | `ISTOTA_EMAIL_SMTP_PASSWORD` | `email` skill |
| IMAP (email receiving) | `[email]` | `ISTOTA_EMAIL_IMAP_PASSWORD` | `email` skill |
| CalDAV | derived from `[nextcloud]` | `ISTOTA_NEXTCLOUD_APP_PASSWORD` | `calendar`, `location` skills |
| Nextcloud | `[nextcloud]` | `ISTOTA_NEXTCLOUD_APP_PASSWORD` | `nextcloud` skill |
| GitLab token | `[developer]` | `ISTOTA_DEVELOPER_GITLAB_TOKEN` | `developer` skill |
| GitHub token | `[developer]` | `ISTOTA_DEVELOPER_GITHUB_TOKEN` | `developer` skill |
| Google OAuth client secret | `[google_workspace]` | `ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET` | Google OAuth flow |
| Web OAuth2 client secret | `[web]` | `ISTOTA_WEB_OAUTH2_CLIENT_SECRET` | Nextcloud login flow |
| Web session signing key | `[web]` | `ISTOTA_WEB_SESSION_SECRET_KEY` | Session cookies |
| `ISTOTA_SECRET_KEY` | env only | `ISTOTA_SECRET_KEY` | Fernet encryption for tier-2 secrets |

CalDAV credentials are derived from the Nextcloud app password automatically — no separate config needed.

`ISTOTA_SECRET_KEY` is the master encryption key for the `secrets` table and `google_oauth_tokens`. It must be at least 32 characters; the key is scrypt-derived into a Fernet key at runtime. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`.

### Provisioning global credentials

**Bare metal (Ansible)**: Set values in your Ansible vars or `/etc/istota/settings.toml`. Sensitive values go into `secrets.env` (via `istota_use_environment_file: true`), which systemd injects as env vars — keeping them out of the config file on disk.

**Docker**: Set values in `docker/.env`. The entrypoint auto-generates `ISTOTA_SECRET_KEY` on first start (persisted to `/data/.secret_key`).

**Manual**: Edit `config.toml` directly, or set `ISTOTA_*` env vars in the service unit's `EnvironmentFile=`.

## Per-user credentials (user's accounts)

These belong to individual users. Stored in the `secrets` table with Fernet encryption at rest (keyed from `ISTOTA_SECRET_KEY`). Users configure them via the web UI at `/istota/settings` or via CLI:

```bash
istota secret ensure --user alice --service SERVICE --key KEY --value VALUE
```

### Connected services

Available to all users regardless of which modules are enabled.

| Service | Keys | Consumed by |
|---|---|---|
| Karakeep | `base_url`, `api_key` | `bookmarks` skill |
| Google Workspace | (OAuth flow — tokens in `google_oauth_tokens` table) | `google_workspace` skill |
| ntfy | `topic`, `server_url`\*, `username`\*, `password`\*, `token`\* | push notifications |

\* = optional

### Module services

Gated by module enablement. Appear on per-module settings pages.

| Module | Service | Keys | Consumed by |
|---|---|---|---|
| money | Monarch Money | `email`, `password`, `session_token`\* | `money` skill (transaction sync) |
| feeds | Tumblr | `tumblr_api_key`\* | `feeds` skill (Tumblr feed ingestion) |
| location | Overland | `ingest_token` | `location` skill (GPS ingestion webhook) |

\* = optional

### Google OAuth tokens

A special case: stored in their own `google_oauth_tokens` table (not in `secrets`) because the OAuth flow writes `access_token` and `refresh_token` as a pair with expiry metadata. Fernet-encrypted at rest using the same `ISTOTA_SECRET_KEY`. A migration function auto-upgrades any pre-existing plaintext rows on read.

Users connect their Google account through the web dashboard at `/istota/` (the dashboard shows a Google Workspace card). See [Google Workspace](../features/google-workspace.md) for the full setup.

## How credentials flow at runtime

```
config.toml / env vars
        │
        ▼
  build_credential_env()     ← collects all credentials the task needs
        │
        ▼
  _split_credential_env()    ← splits into (credential_env, clean_env)
        │                │
        │                └──▶ Claude subprocess (clean_env — no secrets)
        ▼
  SkillProxy(credential_env) ← holds credentials in memory
        │
        ▼
  credential-fetch <VAR>     ← skill CLI requests a specific var
        │                       proxy checks _CREDENTIAL_SKILL_MAP
        ▼                       returns value only if skill is authorized
  skill subprocess env
```

The `_CREDENTIAL_SKILL_MAP` in `executor.py` is the authorization matrix: each env var maps to the set of skills allowed to request it. Claude's subprocess never sees credential values directly.

Authorization is **decoupled from skill selection**. A skill is authorized for credential access whenever its mapped credentials are present in the user's task environment — not when the skill is selected into the prompt. This prevents keyword-miss lockouts: if a user has Karakeep configured, the bookmarks skill can always request `KARAKEEP_API_KEY` at runtime, even if "bookmark" wasn't in the prompt.

For more on the proxy architecture, credential stripping, and rejection logging, see [security: credential proxy](../deployment/security.md#credential-proxy).

## Credential proxy variables

These env vars are stripped from the Claude subprocess and injected server-side by the proxy:

- `CALDAV_PASSWORD`
- `NC_PASS`
- `SMTP_PASSWORD`
- `IMAP_PASSWORD`
- `KARAKEEP_API_KEY`
- `GOOGLE_WORKSPACE_CLI_TOKEN`
- `GITLAB_TOKEN`
- `GITHUB_TOKEN`
- `MONARCH_SESSION_TOKEN`
- `ISTOTA_SECRET_KEY`

See [environment variables](../reference/environment-variables.md) for the complete env var reference.

## Adding credentials for new integrations

When adding a new service integration, follow this decision tree:

1. **Who authenticates?** If the bot logs in as itself (a service account, a bot token), it's global. If it accesses a user's personal account, it's per-user.
2. **Global** → add to the relevant `config.toml` section, wire the env var in `build_credential_env()`, add to `_PROXY_CREDENTIAL_VARS` and `_CREDENTIAL_SKILL_MAP` in `executor.py`, and document the env var override.
3. **Per-user** → add the service and keys to `secret_schema.py` (connected service or module service), wire the skill CLI to resolve via `secrets_store.get_secret()`. The `ISTOTA_SECRET_KEY` routing handles decryption.
4. **OAuth** → if the service uses OAuth, consider a dedicated table (like `google_oauth_tokens`) or store the refresh token as a regular secret. OAuth flows need a web UI endpoint for the redirect dance.

For the full skill development workflow including env var mapping, see [adding skills](../development/adding-skills.md).

## Edge cases

**ntfy** — could go either way. The bot could have one global ntfy topic and broadcast to all users. Instead, it's per-user: each user picks their own topic and optionally their own server. This scales better for multi-user and lets users opt out or use self-hosted ntfy.

**CalDAV** — currently global (one service account with shared calendar access via Nextcloud). If Istota ever supports users bringing their own CalDAV servers, this would need a per-user path.

**Browser** — `BROWSER_API_URL` and `BROWSER_VNC_URL` are deployment-level config, not credentials. They point to the headless browser container.
