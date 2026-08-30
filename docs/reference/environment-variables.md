# Environment variables

Environment variables set for Claude Code subprocess invocations and skill CLI commands.

## Core variables

Set for every task:

| Variable | Source |
|---|---|
| `ISTOTA_TASK_ID` | Task ID |
| `ISTOTA_USER_ID` | Task's user ID |
| `ISTOTA_DB_PATH` | Framework database path. Handed to skill CLIs via the proxy for every user; never present in the model's own environment |
| `ISTOTA_CONVERSATION_TOKEN` | Talk room token (if set) |
| `ISTOTA_DEFERRED_DIR` | Temp directory for deferred JSON writes |
| `ISTOTA_SKILL_PROXY_SOCK` | Skill proxy socket path (if proxy enabled) |
| `ISTOTA_SANDBOXED` | `1` when the task is really running under bwrap (sandbox and skill proxy both enabled *and* bwrap present), otherwise unset. `istota-skill` reads it to fail closed instead of running a skill module in-process: inside the sandbox the databases are masked out, so a direct run would report a missing table rather than the misconfiguration it is |
| `ISTOTA_BOT_DIR_NAME` | `config.bot_dir_name` — the per-user bot directory (`Users/<user>/<bot_dir_name>/`) skills write into |
| `ISTOTA_CONFIG_PATH` | Config file path (propagated to subprocess children so module-skill jobs find the config) |
| `ISTOTA_EXPERIMENTAL_FEATURES` | CSV of enabled experimental features (`config.experimental.features`). Injected by every subprocess builder so `@requires_feature`-gated CLI subcommands and gated skills see the same gate as the LLM path |
| `SHELLOPTS` | Fixed to `pipefail` (`shell_exec.pipefail_env`), applied last by `build_clean_env`. Bash imports it at startup, so every bash below a task has the option on — including the ones istota never sees, which is the point: a `claude_code` or `tmux_claude` task runs its commands through the Claude Code CLI's own Bash tool (`bash -c 'source <snapshot> && eval <cmd>'`), a process istota launches but does not instrument, and that shell started with `pipefail` off and reported a pipeline's *last* stage (ISSUE-321). `SHELLOPTS` rather than `BASH_ENV` because it names shell *options* and cannot name a file to source, so it opens no exec inlet — `pipefail:$(touch /tmp/x)` is rejected as an invalid option name rather than evaluated. Being inherited rather than a flag, it also reaches a pipeline inside a nested `bash script.sh`, which `-o pipefail` does not; it reaches nothing that is not bash. `BASH_ENV`, `SHELLOPTS` and `BASHOPTS` are stripped from the inherited environment by **both** env builders first, so no value from outside survives to be trusted |

Two variables belong to the daemon's own environment (`build_stripped_env`) rather than to a task, so they are not in the table above:

- `PRECOMMIT_SCANS_REQUIRED=1` on cron `command` jobs and heartbeat shell commands, so the pre-commit scans refuse rather than warn where they cannot run. A model task is recognised as unattended by `ISTOTA_SANDBOXED` / `DEVELOPER_REPOS_DIR` instead, and those are built per task. See [secret scanning](../development/secret-scanning.md).
- No `SHELLOPTS`. Those two paths take `pipefail` from `shell_exec.shell_argv`'s `bash -o pipefail -c` instead — flag depth only, deliberately, because the commands there are operator-authored rather than model-authored.

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
| `FEEDS_USER` | task `user_id` | declared in the feeds `skill.md` env spec (`from: user_id`) and resolved by `build_skill_env` in every subprocess path (LLM `execute_task`, skill-task, command-task) |
| `TUMBLR_API_KEY` | `secrets` (`feeds.tumblr_api_key`) | per-user, sensitive |
| `NTFY_TOPIC` / `NTFY_SERVER_URL` / `NTFY_USERNAME` | `secrets` (`ntfy.*`) | per-user (non-credential) |
| `NTFY_TOKEN` / `NTFY_PASSWORD` | `secrets` (`ntfy.token` / `ntfy.password`) | per-user, sensitive |
| `MONEY_USER` | task `user_id` | the only money env var; config is resolved from the per-user money DB via `resolve_for_user`. `MONEY_CONFIG` / `MONEY_SECRETS_FILE` and the standalone `money` binary are gone — money is fully istota-native (reachable as `istota money …`). |

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
| `DEVELOPER_REPOS_DIR` | `{config.developer.repos_dir}/{user_id}`, derived by the developer skill's `setup_env` hook. Admin tasks only — it is the subtree the sandbox binds, and a non-admin has no bind behind it. |
| `GITLAB_URL` | `config.developer.gitlab_url` |
| `GITLAB_DEFAULT_NAMESPACE` | `config.developer.gitlab_default_namespace` |
| `GITLAB_REVIEWER` | `config.developer.gitlab_reviewer` |
| `GITHUB_URL` | `config.developer.github_url` |
| `GITHUB_DEFAULT_OWNER` | `config.developer.github_default_owner` |
| `GITHUB_REVIEWER` | `config.developer.github_reviewer` |
| `DEVELOPER_AUTHOR_CREDIT` | `config.developer.author_credit` |
| `GIT_CONFIG_*` | Git credential helpers for HTTPS auth |
| `GH_HOST`, `GITLAB_HOST` | Written by the forge wrapper into the real CLI's environment, derived from the two URLs |
| `ISTOTA_PATH_PREPEND` | Internal. The task's `{user_temp_dir}/.developer` directory, which holds the `gh` / `glab` wrappers, plus `.developer/exec-shims` where development work runs in the devbox (`[developer] enabled`, `[developer] repos_dir` and `[devbox] enabled` all set); the executor folds them onto `PATH` and strips the variable before the model sees it |

## Package caches

Set only where the filesystem sandbox is really in force, and only when a cache root resolves for the task's user — with `[developer] enabled` and a `repos_dir` that is `{repos_dir}/{user_id}/.package-caches`, derived rather than configured; otherwise `{sandbox_cache_dir}/{user_id}`. Without them a task's downloads land on bubblewrap's root tmpfs, which is RAM (ISSUE-305). What is left behind is bounded by `sandbox_cache_sweep_interval` and `sandbox_cache_max_gb`.

| Variable | Source |
|---|---|
| `UV_CACHE_DIR` | `{cache root}/uv` |
| `npm_config_cache` | `{cache root}/npm` — npm on Linux uses `~/.npm` and ignores XDG, so `XDG_CACHE_HOME` alone would leave it in RAM |
| `XDG_CACHE_HOME` | The cache root itself, so a third tool's cache lands there too. It counts against the budget while neither sweep verb can touch it |
| `HF_HOME` | Pinned back to `$HOME/.cache/huggingface`, *not* moved with XDG. It defaults to `$XDG_CACHE_HOME/huggingface`, so moving XDG would orphan the read-only pre-warmed model bind and every task would re-download it |

## Devbox

Set when `[devbox] enabled`. The exec socket directory is bound into a sandbox only when `developer` is in the task's authorized skills.

| Variable | Source |
|---|---|
| `ISTOTA_DEVBOX_CONTAINER` | `{devbox.container_prefix}{user_id}` |
| `ISTOTA_DEVBOX_DOCKER_CLI` | `config.devbox.docker_cli` |
| `ISTOTA_DEVBOX_MAX_OUTPUT_BYTES` | `config.devbox.max_output_bytes` |

There is deliberately **no** `ISTOTA_DEVBOX_EXEC_TIMEOUT`: the transport imposes no timeout, the task's own budget governs, and a caller wanting a kill passes `--timeout`. The exec protocol carries no `env` field either — a task's environment is never forwarded into the container, whose caches are set on the container itself.

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
- `ISTOTA_BRAIN_NATIVE_API_KEY` — declared by `code_review`, which calls a model itself
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
