# Environment variables

Environment variables set for Claude Code subprocess invocations and skill CLI commands.

## Core variables

Set for every task:

| Variable | Source |
|---|---|
| `ISTOTA_TASK_ID` | Task ID |
| `ISTOTA_USER_ID` | Task's user ID |
| `ISTOTA_DB_PATH` | Database path (admin only) |
| `ISTOTA_CONVERSATION_TOKEN` | Talk room token (if set) |
| `ISTOTA_DEFERRED_DIR` | Temp directory for deferred JSON writes |
| `ISTOTA_SKILL_PROXY_SOCK` | Skill proxy socket path (if proxy enabled) |

## Nextcloud

| Variable | Source |
|---|---|
| `NC_URL` | `config.nextcloud.url` |
| `NC_USER` | `config.nextcloud.username` |
| `NC_PASS` | `config.nextcloud.app_password` |
| `NEXTCLOUD_MOUNT_PATH` | `config.nextcloud_mount_path` (scoped to user dir for non-admin) |

## CalDAV

Derived from Nextcloud credentials:

| Variable | Source |
|---|---|
| `CALDAV_URL` | `config.nextcloud.url + /remote.php/dav` |
| `CALDAV_USERNAME` | `config.nextcloud.username` |
| `CALDAV_PASSWORD` | `config.nextcloud.app_password` |

## Email

| Variable | Source |
|---|---|
| `SMTP_HOST` | `config.email.smtp_host` |
| `SMTP_PORT` | `config.email.smtp_port` |
| `SMTP_USER` | `config.email.effective_smtp_user` |
| `SMTP_PASSWORD` | `config.email.effective_smtp_password` |
| `SMTP_FROM` | Plus-addressed: `bot+user_id@domain` |
| `IMAP_HOST` | `config.email.imap_host` |
| `IMAP_PORT` | `config.email.imap_port` |
| `IMAP_USER` | `config.email.imap_user` |
| `IMAP_PASSWORD` | `config.email.imap_password` |

## Browser

| Variable | Source |
|---|---|
| `BROWSER_API_URL` | `config.browser.api_url` |
| `BROWSER_VNC_URL` | `config.browser.vnc_url` |

## Service integrations

| Variable | Source |
|---|---|
| `KARAKEEP_BASE_URL` | From resource config `extra` |
| `KARAKEEP_API_KEY` | From resource config `extra` |
| `MONARCH_SESSION_TOKEN` | From resource config `extra` |
| `MINIFLUX_BASE_URL` | From resource config (type=miniflux) |
| `MINIFLUX_API_KEY` | From resource config (type=miniflux) |
| `MONEYMAN_API_URL` | From resource config (type=moneyman) |
| `MONEYMAN_API_KEY` | From resource config (type=moneyman) |

## Developer

| Variable | Source |
|---|---|
| `DEVELOPER_REPOS_DIR` | `config.developer.repos_dir` |
| `GITLAB_URL` | `config.developer.gitlab_url` |
| `GITLAB_DEFAULT_NAMESPACE` | `config.developer.gitlab_default_namespace` |
| `GITLAB_API_CMD` | Path to API wrapper script |
| `GITHUB_URL` | `config.developer.github_url` |
| `GITHUB_DEFAULT_OWNER` | `config.developer.github_default_owner` |
| `GITHUB_REVIEWER` | `config.developer.github_reviewer` |
| `GITHUB_API_CMD` | Path to API wrapper script |
| `GIT_CONFIG_*` | Git credential helpers for HTTPS auth |

## Website

| Variable | Source |
|---|---|
| `WEBSITE_PATH` | `config.site.base_path` |
| `WEBSITE_URL` | `config.site.hostname` |

## Credential proxy

When `skill_proxy_enabled = true`, these variables are stripped from the subprocess environment and injected server-side by the proxy:

- `CALDAV_PASSWORD`
- `NC_PASS`
- `SMTP_PASSWORD`
- `IMAP_PASSWORD`
- `KARAKEEP_API_KEY`
- `MINIFLUX_API_KEY`
- `MONEYMAN_API_KEY`
- `GITLAB_TOKEN`
- `GITHUB_TOKEN`
- `MONARCH_SESSION_TOKEN`

## Secret overrides

These env vars override TOML config values (for use with systemd `EnvironmentFile=`):

| Env var | Config field |
|---|---|
| `ISTOTA_NC_APP_PASSWORD` | `nextcloud.app_password` |
| `ISTOTA_IMAP_PASSWORD` | `email.imap_password` |
| `ISTOTA_SMTP_PASSWORD` | `email.smtp_password` |
| `ISTOTA_GITLAB_TOKEN` | `developer.gitlab_token` |
| `ISTOTA_GITHUB_TOKEN` | `developer.github_token` |
| `ISTOTA_NTFY_TOKEN` | `ntfy.token` |
| `ISTOTA_NTFY_PASSWORD` | `ntfy.password` |
| `ISTOTA_OIDC_CLIENT_SECRET` | `web.oidc_client_secret` |
| `ISTOTA_WEB_SECRET_KEY` | `web.session_secret_key` |
