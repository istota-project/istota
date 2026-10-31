# Environment variables

Environment variables set for Claude Code subprocess invocations and skill CLI commands, the secret overrides the config loader reads, and, at the end, the variables and host files the [stack](#the-stack) uses.

## Core variables

Set for every task:

| Variable | Source |
|---|---|
| `ISTOTA_TASK_ID` | Task ID |
| `ISTOTA_TASK_ATTEMPT` | Which attempt of that task is running, 1-based (`attempt_count + 1`), matching the session log's file name. `tasks transcript` reads it to exclude the transcript the calling run is still writing — a fact about the process, so it is fixed in the environment rather than re-read from a row the stuck-task reaper mutates. Handed to skill CLIs via the proxy; never present in the model's own environment, since it is what the exclusion trusts |
| `ISTOTA_USER_ID` | Task's user ID |
| `ISTOTA_DB_PATH` | Framework database path. Handed to skill CLIs via the proxy for every user; never present in the model's own environment |
| `ISTOTA_CONVERSATION_TOKEN` | Talk room token (if set) |
| `ISTOTA_TASK_GROUPS` | Comma-separated ids of the [groups](../features/groups.md) this task may carry, resolved once per task. `kv --group` and `memory --group` refuse any group not named here; absent means none. Handed to skill CLIs via the proxy; never present in the model's own environment |
| `ISTOTA_DEFERRED_DIR` | Temp directory for deferred JSON writes |
| `ISTOTA_SKILL_PROXY_SOCK` | Skill proxy socket path (if proxy enabled) |
| `ISTOTA_SKILL_CLIENT_WAIT` | `security.skill_client_wait_seconds`, set alongside the socket when the proxy is enabled. The sandboxed `istota-skill` client cannot read a config, so this is how an operator's value reaches it; it falls back to 600 when unset. The proxy derives its own ceiling from the config field and never from this export, so a task rewriting its copy changes only its own patience |
| `ISTOTA_SANDBOXED` | `1` when the task is really running under bwrap (sandbox and skill proxy both enabled *and* bwrap present), otherwise unset. `istota-skill` reads it to fail closed instead of running a skill module in-process: inside the sandbox the databases are masked out, so a direct run would report a missing table rather than the misconfiguration it is |
| `ISTOTA_BOT_DIR_NAME` | `config.bot_dir_name` — the per-user bot directory (`Users/<user>/<bot_dir_name>/`) skills write into |
| `ISTOTA_CONFIG_PATH` | Config file path (propagated to subprocess children so module-skill jobs find the config) |
| `ISTOTA_EXPERIMENTAL_FEATURES` | CSV of enabled experimental features (`config.experimental.features`). Injected by every subprocess builder so `@requires_feature`-gated CLI subcommands and gated skills see the same gate as the LLM path |
| `SHELLOPTS` | Fixed to `pipefail` (`sandbox.shell_exec.pipefail_env`), applied last by `build_clean_env`. Bash imports it at startup, so every bash below a task has the option on — including the ones istota never sees, which is the point: a `claude_code` task used to run its commands through the Claude Code CLI's own Bash tool (`bash -c 'source <snapshot> && eval <cmd>'`), a process istota launches but does not instrument, and that shell started with `pipefail` off and reported a pipeline's *last* stage (ISSUE-321). `SHELLOPTS` rather than `BASH_ENV` because it names shell *options* and cannot name a file to source, so it opens no exec inlet — `pipefail:$(touch /tmp/x)` is rejected as an invalid option name rather than evaluated. Being inherited rather than a flag, it also reaches a pipeline inside a nested `bash script.sh`, which `-o pipefail` does not; it reaches nothing that is not bash. `BASH_ENV`, `SHELLOPTS` and `BASHOPTS` are refused by `build_clean_env`'s passthrough first, so no value from outside survives to be trusted |

`sandbox.confined_command` adds `PRECOMMIT_SCANS_REQUIRED=1` on cron `command` jobs and heartbeat shell commands, on top of the task env, so the pre-commit scans refuse rather than warn where they cannot run. A model task is recognised as unattended by `ISTOTA_SANDBOXED` / `DEVELOPER_REPOS_DIR` instead. See [secret scanning](../development/secret-scanning.md).

## Nextcloud

| Variable | Source |
|---|---|
| `NC_URL` | `config.nextcloud.url` |
| `NC_USER` | `config.nextcloud.username` |
| `NC_PASS` | `config.nextcloud.app_password` |
| `ISTOTA_NEXTCLOUD_APP_PASSWORD` | `config.nextcloud.app_password`, handed to the relay skill only, which reads it back through `load_config` |
| `ISTOTA_WORKSPACE_PATH` | `config.workspace_path` (scoped to user dir for non-admin) |
| `NEXTCLOUD_MOUNT_PATH` | `config.workspace_path` (compatibility alias) |

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
| `ISTOTA_DEVBOX_MAX_OUTPUT_BYTES` | `config.devbox.max_output_bytes` |

There is deliberately **no** `ISTOTA_DEVBOX_EXEC_TIMEOUT`: the transport imposes no timeout, the task's own budget governs, and a caller wanting a kill passes `--timeout`. The exec protocol carries no `env` field either — a task's environment is never forwarded into the container, whose caches are set on the container itself.

## Credential proxy

When `skill_proxy_enabled = true`, every env var declared with `sensitive: true` in any skill manifest is stripped from the subprocess environment and injected server-side by the proxy. The set is computed at task time by `derive_credential_set(skill_index)`. Today's set:

- `CALDAV_PASSWORD`
- `NC_PASS`, and `ISTOTA_NEXTCLOUD_APP_PASSWORD` (the same value, for the relay skill)
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
| `ISTOTA_CALDAV_PASSWORD` | `caldav.password` (the no-Nextcloud shape's calendar credential) |
| `ISTOTA_EMAIL_IMAP_PASSWORD` | `email.imap_password` |
| `ISTOTA_EMAIL_SMTP_PASSWORD` | `email.smtp_password` |
| `ISTOTA_DEVELOPER_GITLAB_TOKEN` | `developer.gitlab_token` |
| `ISTOTA_DEVELOPER_GITHUB_TOKEN` | `developer.github_token` |
| `ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET` | `google_workspace.client_secret` |
| `ISTOTA_SERVARR_SONARR_API_KEY` | `servarr.apps.sonarr.api_key` |
| `ISTOTA_SERVARR_RADARR_API_KEY` | `servarr.apps.radarr.api_key` |
| `ISTOTA_SERVARR_LIDARR_API_KEY` | `servarr.apps.lidarr.api_key` |
| `ISTOTA_SERVARR_PROWLARR_API_KEY` | `servarr.apps.prowlarr.api_key` |
| `ISTOTA_WEB_OAUTH2_CLIENT_SECRET` | `web.oauth2_client_secret` |
| `ISTOTA_WEB_SESSION_SECRET_KEY` | `web.session_secret_key` |
| `ISTOTA_PROVIDER_<ID>_API_KEY` | `providers.<id>.api_key`, for every provider, built in or your own |
| `ISTOTA_PROVIDER_<ID>_EXTRA_HEADERS` | `providers.<id>.extra_headers`, as a JSON object of header name to string value. Replaces the table whole; a value that is not such an object is ignored with a warning |
| `ISTOTA_BRAIN_NATIVE_API_KEY` | Legacy, read for one release as native's provider's key; the provider's own variable wins |
| `ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS` | Legacy, read for one release as native's provider's headers, the same JSON object |
| `ISTOTA_SMS_TWILIO_ACCOUNT_SID` | `sms.twilio.account_sid` |
| `ISTOTA_SMS_TWILIO_AUTH_TOKEN` | `sms.twilio.auth_token` |
| `ISTOTA_SMS_TWILIO_API_KEY_SID` | `sms.twilio.api_key_sid` |
| `ISTOTA_SMS_TWILIO_API_KEY_SECRET` | `sms.twilio.api_key_secret` |
| `ISTOTA_SMS_TWILIO_MESSAGING_SERVICE_SID` | `sms.twilio.messaging_service_sid` |
| `ISTOTA_SMS_TELNYX_API_KEY` | `sms.telnyx.api_key` |
| `ISTOTA_SMS_TELNYX_PUBLIC_KEY` | `sms.telnyx.public_key` |
| `ISTOTA_SMS_TELNYX_MESSAGING_PROFILE_ID` | `sms.telnyx.messaging_profile_id` |
| `ISTOTA_WHATSAPP_ACCESS_TOKEN` | `whatsapp.cloud.access_token` |
| `ISTOTA_WHATSAPP_APP_SECRET` | `whatsapp.cloud.app_secret` |
| `ISTOTA_WHATSAPP_VERIFY_TOKEN` | `whatsapp.cloud.verify_token` |

The three WhatsApp names kept their un-nested spelling when the block moved under `cloud`, so an existing environment file keeps working across that upgrade. Each variable is listed in full rather than as a shared prefix and a list of suffixes: this is the page you reach by searching for a variable already sitting in your environment, and a suffix matches nothing.

See [credentials](../configuration/credentials.md) for what each override covers and the full env var → config mapping.

## The stack

These belong to the stack on its VM rather than to a task. `istota-stack` passes the first group to every `docker compose` call it makes, and they outrank the stack's `.env`. Run `docker compose` through `istota-stack compose` so it gets them.

| Variable | Value on the VM |
|---|---|
| `ISTOTA_CONFIG_DIR` | `/srv/app/istota/config`, bound at `/data/config` |
| `ISTOTA_DATA_DIR` | `/srv/app/istota/data`, bound at `/data`. Unset (Docker Desktop, the test tiers), `/data` is the named volume `data` |
| `ISTOTA_SECRETS_DIR` | `/srv/app/istota/secrets`, one file per credential |
| `ISTOTA_MOUNT_DIR` | The workspace mount's directory, bound at `/mnt/shared`: `/srv/mount/host-folder` when `host.env` says `STORAGE=host-folder`, else `/srv/mount/nextcloud/content` |
| `ISTOTA_HOST_ENV` | `/srv/app/istota/host.env`, which `daemon` and `nginx` read |
| `ISTOTA_WORKSPACE_FSTYPE` | The filesystem the workspace must be on, from `STORAGE`: `fuse.rclone` for `nextcloud`, `virtiofs` for `host-folder`, empty for `local`. The entrypoint refuses to start when `workspace_mount_path` is set and `/mnt/shared` is not that type |
| `ISTOTA_WORKSPACE_MARKER_REQUIRED` | `1` for `host-folder`: the entrypoint, doctor and the database backup require `.istota-workspace` at the folder's root, since a folder that went away still looks like a mount |
| `ISTOTA_LOG_DRIVER` | `journald`, so container output reaches the VM's journal. The compose default is `json-file` |
| `ISTOTA_BUILD_REVISION`, `ISTOTA_BUILD_TAG` | The commit and tag being built, set when a build names a tag. The image keeps both, so the running version reads `<version>+<short commit>` for anything but a release tag |
| `ISTOTA_REQUESTS_DIR` | `/srv/app/istota/requests`, the [web config editor's](../configuration/web-editor.md) trigger directory, bound into `web` alone. Set only where `provision.sh` made it |
| `CERTS_DIR`, `ACME_WEBROOT` | nginx's certificates (`letsencrypt/` with `TLS_CERT_SOURCE=acme`, else `certs/`) and the ACME webroot |

Two more are set for one command at a time:

| Variable | Effect |
|---|---|
| `ISTOTA_CONFIG_READ_ONLY` | Whether `/data/config` is read-only in the container, `true` by default. `istota-stack` sets `false` for the one-shot containers of `setup`, `apply`, the imports, `restore` and `localize-workspace`, and for nothing that keeps running |
| `ISTOTA_SETUP_PUBLISH` | Where `istota-stack setup --web` publishes its page: `0.0.0.0:443` by default (`[::]:443` on a machine with only IPv6), `127.0.0.1:<LOCAL_FORWARD_PORT>` with `--local`, the port `host.env` says Lima forwards (8080 when that is empty). Set it to a free address and port when that one is taken |

Inside the `daemon` and `web` containers, compose sets `ISTOTA_CONFIG_PATH=/data/config/config.toml` and `ISTOTA_ADMINS_FILE=/data/config/admins`, and each credential file becomes its uppercase name (`istota_nextcloud_app_password` becomes `ISTOTA_NEXTCLOUD_APP_PASSWORD`, one of the [secret overrides](#secret-overrides) above). Where the VM has the editor's trigger directory, `web` also gets `ISTOTA_CONFIG_APPLIER=host`, which says the host applies config changes and turns the web editor on. `ISTOTA_WEB_INSECURE_COOKIES=1` drops the `Secure` flag from the session cookie; setup writes `1` for `local` ingress, which is plain http on loopback where Safari and the Mac app drop a Secure cookie, and `0` for every other ingress.

On the Mac, `ISTOTA_LIMA_INSTANCE` names the Lima instance `istota-stack` and `istota` act on (default `istota`), and `ISTOTA_REPO_URL` the repository `create` reads the newest release from and the VM clones (default `https://github.com/istota-project/istota.git`). `RELEASE_SIGNING_KEY` in the Mac's environment is the key `create` uses when `--signing-key` is not given.

### host.env and .env

The VM keeps two settings files beside the checkout. `istota setup` writes the keys it owns in each and keeps every other line; `istota-stack host-env` reads and sets `host.env` keys by hand.

`/srv/app/istota/host.env` is read by `provision.sh`, the host units, and the `daemon` and `nginx` containers:

| Key | Meaning |
|---|---|
| `STORAGE` | Where the workspace is: `local` (`/data/workspace`, no mount), `nextcloud` (the rclone mount of the bot's Nextcloud files) or `host-folder` (a folder the Mac shares into the local VM). `nextcloud` means the mount, not Nextcloud itself: Talk and the `nextcloud` skill work with `local` too |
| `INGRESS` | How the stack is reached: `local`, `proxied` or `direct` |
| `DOMAIN` | The public hostname. In `direct` and `proxied` ingress the daemon refuses to start when `[site] hostname` differs |
| `TLS_CERT_SOURCE`, `UPSTREAM_PROXY`, `LISTEN_ADDR`, `LISTEN_PORT` | The ingress mode's details |
| `ACME_EMAIL`, `ACME_SERVER` | Optional, for certbot in `direct` ingress |
| `RCLONE_REMOTE` | The remote in `/srv/app/istota/rclone.conf` the mount uses (`nextcloud` when setup wrote the file) |
| `RELEASE_SIGNING_KEY` | The release key, one line of an SSH public key, written to `allowed_signers`. The installer writes the project's key (`host/release-signing-key.pub`) unless it was given another |
| `CLAUDE_CODE_VERSION` | The Claude Code version a build pins, when it pins one |
| `APT_MIRROR` | A Debian mirror (`http://` or `https://`, a path ending in `debian`, no trailing slash) used instead of deb.debian.org for Debian's main archive, by the host's apt (set by `provision.sh`) and by the image builds. The security archive is unchanged. Removing it puts deb.debian.org back on the next `provision.sh` |
| `APT_SNAPSHOT` | A snapshot.debian.org id (`YYYYMMDDTHHMMSSZ`) the host's apt reads Debian's archives at, through apt's `APT::Snapshot`. Set by release builds so the source of every package they ship stays available; unset on a server. Removing it puts the live archives back on the next `provision.sh` |
| `ISTOTA_LOCAL_LAYERS` | The layers `istota-stack local-layers` builds on a release's loaded images, separated by spaces or commas: `claude-code`, `chrome`. Absent means both, empty means neither. Leaving `claude-code` out removes its layer, and the stack goes back to the release's own image. A server builds both into its images and never reads it |
| `ISTOTA_DESKTOP_APP` | `1` on a VM the Mac's `istota-stack create --appliance` made, whose images are a release's loaded from its archive. A setup that needs Claude Code or the browser then builds the local layers rather than every image, and `check-image` names `local-layers`. Under `local` ingress setup also writes `[web] auth = ["local_code", "email"]`, the app's login codes. Empty on a server |
| `ZRAM_ENABLED` | `0` leaves swap alone |
| `SITE_DIR` | A directory of static files nginx serves read-only at `/` in place of the redirect to the web UI ([a static site at the root](../deployment/running-the-stack.md#a-static-site-at-the-root)). `istota-stack` passes it to compose as `ISTOTA_SITE_DIR` only when it overlaps no directory the stack writes |
| `HOST_LOOPBACK_BLOCK`, `HOST_GATEWAY_ADDR`, `HOST_LOOPBACK_ALLOW` | The local VM's block on container traffic to the Mac's loopback (`HOST_GATEWAY_ADDR`), except DNS and the TCP ports in `HOST_LOOPBACK_ALLOW`, which `allow-host-port` and `deny-host-port` edit |

`/srv/app/istota/.env` is compose's: `COMPOSE_PROFILES`, `DOMAIN`, `ISTOTA_SECRETS_DIR`, `NGINX_PUBLISH`, `NGINX_PUBLISH_TLS` and `ISTOTA_WEB_INSECURE_COOKIES` from setup, and `ISTOTA_TAG`, `ISTOTA_PREVIOUS_TAG`, `ISTOTA_TAG_UNSIGNED` and `ISTOTA_PREVIOUS_TAG_UNSIGNED` from `update` and `rollback`. `ISTOTA_IMAGE`, when set, is the image the `daemon` and `web` services run in place of `istota:<ISTOTA_TAG>`: the tag's Claude Code layer, `istota:<ISTOTA_TAG>-cc`, which `istota-stack local-layers` writes, and which `update` and `rollback` point at the new tag's layer or clear. istota's own settings are never here; they are in `config.toml`.
