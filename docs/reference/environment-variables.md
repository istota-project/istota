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
| `ISTOTA_CONFIG_PATH` | Config file path (propagated to subprocess children so module-skill jobs find the config) |
| `ISTOTA_EXPERIMENTAL_FEATURES` | CSV of enabled experimental features (`config.experimental.features`). Injected by every subprocess builder so `@requires_feature`-gated CLI subcommands and gated skills see the same gate as the LLM path |

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

Every service-integration env var is declared in the consuming skill's `skill.md` `env:` block and resolved by `build_skill_env()` against the per-task `EnvContext`. Per-user credentials come from the encrypted `secrets` table (`from: "secret"`); module-skill subprocesses receive `ISTOTA_SECRET_KEY` via the proxy so they can decrypt in-process.

| Variable | Source | Notes |
|---|---|---|
| `KARAKEEP_BASE_URL` | `secrets` (`karakeep.base_url`) | per-user |
| `KARAKEEP_API_KEY` | `secrets` (`karakeep.api_key`) | per-user, sensitive |
| `MONARCH_SESSION_ID` | `secrets` (`monarch.session_id`) | per-user, sensitive |
| `MONARCH_CSRFTOKEN` | `secrets` (`monarch.csrftoken`) | per-user, sensitive |
| `FEEDS_USER` | task `user_id` | set by `_execute_skill_task` for the native feeds skill |
| `TUMBLR_API_KEY` | `secrets` (`feeds.tumblr_api_key`) | per-user, sensitive |
| `NTFY_TOPIC` / `NTFY_SERVER_URL` / `NTFY_USERNAME` | `secrets` (`ntfy.*`) | per-user (non-credential) |
| `NTFY_TOKEN` / `NTFY_PASSWORD` | `secrets` (`ntfy.token` / `ntfy.password`) | per-user, sensitive |
| `MONEY_CONFIG` | legacy resource `extras.config_path` | per-user; current deployments resolve money in-process |
| `MONEY_USER` | task `user_id` | override via `user_key` on the resource |
| `MONEY_SECRETS_FILE` | escape hatch | optional, for direct `money` CLI use; the skill reads creds in-process |

## Module setup_env hooks

Some module env vars are resolved at runtime by Python hooks rather than static config lookups. These are declared `from: "setup_env"` in the skill manifest and dispatched by `dispatch_setup_env_hooks` in the scheduler, command-task, skill-task, and heartbeat paths.

| Variable | Source | Notes |
|---|---|---|
| `HEALTH_DB_PATH` | `istota.health.resolve_for_user(user_id, config).db_path` | per-user; no-op when health module is disabled |
| `LOCATION_DB_PATH` | `istota.location.resolve_for_user(user_id, config).db_path` | per-user; no-op when location module is disabled |

## Google Workspace

| Variable | Source |
|---|---|
| `GOOGLE_WORKSPACE_CLI_TOKEN` | OAuth access token from DB (injected via `setup_env()` hook, auto-refreshed) |

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

When `skill_proxy_enabled = true`, every env var declared with `sensitive: true` in any skill manifest is stripped from the subprocess environment and injected server-side by the proxy. The set is computed at task time by `derive_credential_set(skill_index)`. Today's set:

- `CALDAV_PASSWORD`
- `NC_PASS`
- `SMTP_PASSWORD`
- `IMAP_PASSWORD`
- `KARAKEEP_API_KEY`
- `GOOGLE_WORKSPACE_CLI_TOKEN`
- `GITLAB_TOKEN`
- `GITHUB_TOKEN`
- `MONARCH_SESSION_ID`, `MONARCH_CSRFTOKEN`
- `NTFY_TOKEN`, `NTFY_PASSWORD`
- `TUMBLR_API_KEY`
- `ISTOTA_SECRET_KEY` — routed to module-skill subprocesses, hard-blocked at the lookup endpoint via `_PROXY_LOOKUP_BLOCKED`

The proxy injects each credential only into the skill CLIs whose manifest declared it (`derive_skill_credential_map`). Authorization is based on credential presence in the task env — not skill selection — so any skill whose credentials the user has configured can request them at runtime. See [security: credential proxy](../deployment/security.md#credential-proxy) for the authorization model and rejection logging. See [credentials](../configuration/credentials.md) for the full two-tier credential inventory and provisioning guide.

## Secret overrides

These env vars override TOML config values (for use with systemd `EnvironmentFile=`):

| Env var | Config field |
|---|---|
| `ISTOTA_NEXTCLOUD_APP_PASSWORD` | `nextcloud.app_password` |
| `ISTOTA_EMAIL_IMAP_PASSWORD` | `email.imap_password` |
| `ISTOTA_EMAIL_SMTP_PASSWORD` | `email.smtp_password` |
| `ISTOTA_DEVELOPER_GITLAB_TOKEN` | `developer.gitlab_token` |
| `ISTOTA_DEVELOPER_GITHUB_TOKEN` | `developer.github_token` |
| `ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET` | `google_workspace.client_secret` |
| `ISTOTA_WEB_OAUTH2_CLIENT_SECRET` | `web.oauth2_client_secret` |
| `ISTOTA_WEB_SESSION_SECRET_KEY` | `web.session_secret_key` |
| `ISTOTA_BRAIN_NATIVE_API_KEY` | `brain.native.api_key` (native brain provider key; kept out of TOML) |

See [credentials](../configuration/credentials.md) for what each override covers and the full env var → config mapping.
