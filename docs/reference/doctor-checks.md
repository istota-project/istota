# Doctor checks

`istota doctor` runs every check below against the loaded config and reports each as `ok`, `warn`, `fail` or `skip`, with a remedy on everything that is not `ok`. It exits 1 on any `fail`. The same checks run at daemon start-up (silent unless something is wrong), every hour from the scheduler (`[scheduler] doctor_check_interval`), from the `self-check` [heartbeat](../features/heartbeat.md), and in Admin > Health, which shows the runs the daemon records. An hourly run that moves a check into `fail` alerts each admin's alerts channel.

Select checks by name prefix with `--only` (`istota doctor --only security.` or `--only nextcloud.rclone_rc`). `--scope image` runs only the checks a bare `docker run` of the image can answer (marked *image* below); `--scope deployment` runs the rest. Two checks are opt-in: `sandbox.masks` runs with `--deep`, and `runtime.model_execution` runs only when named, since it calls a model.

A `skip` is not a pass. The forge checks skip when no forge token is configured, and most connection checks skip when their service is off, so a run with no `fail` says nothing about a feature whose checks skipped. Run inside a task, checks that need the daemon's environment skip rather than fail.

## Runtime

| Check | What it answers |
|---|---|
| `runtime.platform` | The operating system and architecture. *image* |
| `runtime.bwrap` | bubblewrap is installed and can run. *image* |
| `runtime.model_cli` | The `claude` CLI the `claude_code` brain runs is installed. *image* |
| `runtime.gitleaks` | `gitleaks` with its `git` subcommand is installed wherever developer tasks commit. *image* |
| `runtime.gws` | The `gws` binary is installed when Google Workspace is enabled. *image* |
| `runtime.pdf_tools` | `pdftotext` and `pdftoppm` are installed for the health module. *image* |
| `runtime.native_brain` | The native brain names a provider, and that provider has a key |
| `runtime.framework_db` | The framework database opens, passes `quick_check` and has a schema |
| `runtime.task_failure_rate` | Whether most of the last hour's tasks failed |
| `runtime.writable_dirs` | The directories the daemon writes to exist and are writable |
| `runtime.mount_liveness` | A configured workspace mount is mounted, and a host folder carries its `.istota-workspace` marker |
| `runtime.workspace_xattr` | Extended-attribute calls on a workspace file fail cleanly rather than with `EIO` |
| `runtime.workspace_mode` | The configured storage mode agrees with the mount the workspace is on |
| `runtime.egress_hold` | Whether `istota-stack hold` keeps the stack's containers off the network; warns while it does |
| `runtime.workspace_disk` | The local workspace's disk has room left: warns under 10% or 2 GiB free, fails under 2% |
| `runtime.data_disk` | The disk holding the databases (on a VM, also Docker's images and volumes) has room left. The remedy names `istota-stack reclaim` |
| `runtime.session_log_dir` | Where native-brain transcripts are written, and that no task can read them |
| `runtime.task_control_dir` | The daemon-owned directory holding each task's prompt and attachments sits where no task can write above it |
| `runtime.bare_metal_import` | Whether this install was imported from a bare-metal one; warns on every run for a rehearsal copy |
| `runtime.subscription_usage` | The Claude Code plan's usage against its limits, with reset times |
| `runtime.model_execution` | A real model call answers through the tool harness. Costs a call; runs only when named |

## Models

| Check | What it answers |
|---|---|
| `models.cli_auth` | How the `claude` CLI authenticates, and whether that credential is present and used as configured |
| `models.catalogue` | Every configured model resolves on the brain that runs it, each provider's model list is fresh, and the model-listing key file is private |

## Storage and backups

| Check | What it answers |
|---|---|
| `backups.off_volume` | The database snapshots are not on the same disk as the databases they protect |
| `nextcloud.connection` | The bot signs in to Nextcloud as the configured account, and Talk is installed when it is used. An outage warns; a wrong password or account fails |
| `nextcloud.rclone_rc` | In full Nextcloud integration, the mount's control socket answers with its password and refuses a caller without one |

## Security

| Check | What it answers |
|---|---|
| `security.skill_proxy` | The `istota-skill` entry point the proxy runs resolves on the daemon's PATH, and, as `security.skill_proxy.forge_posture`, where the forge token sits while the proxy is off |
| `security.sandbox_effective` | The sandbox the config asks for is in force |
| `security.task_cgroups` | Each task gets a cgroup of its own with its memory, process and CPU limits written |
| `security.linked_resource_binds` | This bubblewrap can bind from a file descriptor, which linked folders need |
| `security.sandbox_credentials` | What `skill_proxy_enabled = false` exposes while the sandbox is on |
| `security.proxy_peer_check` | The skill proxy can tell its own task's processes from others |
| `security.credential_broker` | The credential broker is ready, without generating a CA or reading a secret |
| `security.skill_model_credential` | A skill CLI that calls a model of its own (the code review) can authenticate |
| `security.secret_key` | The encrypted secrets store has a master key of sufficient length |
| `security.admins` | Fails when two or more users are configured and nobody is an admin |
| `security.key_separation` | Key files are not stored beside the databases or backups they protect |
| `security.credential_library` | The KeePass import and export libraries are installed. *image* |
| `security.credential_isolation` | Credential vaults for several users are kept apart by the sandbox |
| `security.credential_backup` | Scheduled credential backups to registered recipients are not failing or overdue |
| `security.wallet` | The wallet's payment cards are ready, without showing card details |
| `security.egress_open_jobs` | How many scheduled jobs still run on full access |
| `security.room_scope_confinement` | A guest's turn in a shared room is kept out of the host's files on disk, which needs the sandbox |
| `security.signup_tags` | No signup address stays open after its credential was removed |
| `security.inline_credentials` | No installation credential is written inline in `config.toml` rather than in its secret file |
| `security.devbox_netfilter` | A devbox cannot reach what the VM's egress rules drop |
| `sandbox.masks` | A real sandbox namespace hides the databases. Runs with `--deep` |

## Browser

| Check | What it answers |
|---|---|
| `browser.api_token` | The browser API accepts the token this process holds |
| `browser.chrome_version` | Which Chrome the browser runs, and whether it has trailed a stable release for more than seven days. The remedy is `istota-stack update` |

## Developer

These skip when the developer skill has no forge token configured.

| Check | What it answers |
|---|---|
| `developer.forge_binaries` | The `gh` and `glab` binaries the wrapper runs exist and are executable. *image* |
| `developer.forge_config_drift` | The configured forge binary path is the one that resolves |
| `developer.forge_wrapper_shadowing` | Nothing resolves `gh` or `glab` by name to an unexpected binary. *image* |
| `developer.forge_policy` | Every `forge_cli_permit` entry matches a rule |
| `developer.gitlab_reviewer` | `[developer] gitlab_reviewer` is a username `glab` can resolve |
| `developer.forge_transport` | No forge token travels over plain HTTP or sits in a URL |
| `developer.repos_layout` | Clones are in the per-user layout the daemon reads |
| `developer.container` | The development container's backend, transport, identity and the rest of what fails silently, as separate results |

## Messaging

| Check | What it answers |
|---|---|
| `talk.signaling_reachable` | The Talk high-performance backend answers |
| `talk.signaling_chat_relay` | The signaling server relays chat messages, not only refresh hints |
| `talk.signaling_auth` | Talk is in external signaling mode and has a token key |
| `talk.signaling_watchers` | Every live room has a connected watcher and none has fallen behind |
| `sms.common` | SMS is configured locally, without contacting a provider |
| `sms.twilio`, `sms.telnyx` | The provider's settings are complete |
| `whatsapp.common` | WhatsApp is configured locally, without contacting Meta |
| `whatsapp.billing` | Whether the Cloud API's spending stop is open, and what this month has spent |
| `whatsapp.media_staging` | The inbound image staging directory is private and empty |
| `whatsapp.gif_decoder` | A GIF sent over Baileys can be shown to the model |
| `whatsapp.baileys_bridge` | The Baileys sidecar is connected and its session is alive |
| `whatsapp.baileys_session` | Every paired-session credential on the host is private |
| `whatsapp.pairing_relay` | The pairing relay file is private, and whether a pairing is in progress |
| `email.confirmation_answers` | A user whose alerts reach only email can answer a held task by mail |
| `email.sender_match` | Whether mail from a user's own address is trusted on its `From:` header alone |
| `email.sender_flags` | Whether suspected-injection flags can be recorded against a sender on this deployment |
| `mail.connect_verified` | The TLS context every IMAP and SMTP connection uses verifies the server. *image* |
| `mail.account_isolation` | One user's task is kept away from another user's mail account passwords |
| `push.credentials` | The APNs signing key's metadata, without opening the key or sending. Skipped under relay delivery |
| `push.signing_key` | With `[push] content = "private"`: the private push signing key is a regular file, mode 0600, owned by the service user, parses as Ed25519, and matches its `.pub` file. Reports the key ID. Skipped when push is disabled or content is legacy |
| `push.relay` | With `[push] delivery = "relay"`: the relay URL is a normalized `https://` base URL, and the relay answers `GET <relay_url>/healthz` with 200. An unreachable relay or a 5xx is a warning, a redirect or a 4xx a failure. Fails when the push credentials directory is not a directory, has a mode looser than 0700, or holds a `.cap` file readable by others; warns when it cannot be written or created. Warns when devices are waiting to enroll again (their route expired or was revoked, or its credential file is missing), when route revocations have been queued for more than a day, and when APNs settings are set, since relay delivery ignores them. Skipped when push is disabled or delivery is direct |

## Web

| Check | What it answers |
|---|---|
| `web.auth` | Login prerequisites: methods, hostname, mail, identities, admins, proxy IP checks and the session secret, as seven results |
| `web.static` | The web UI's build exists. *image* |
| `web.tls` | In `direct` ingress, nginx serves a certificate a client accepts |
| `web.build_current` | The served web bundle is current for this checkout. Skips on the stack |
| `web.basemap` | The map pages have a background that will render. Opens no connection |
| `web.avatar_import` | How the last import of Nextcloud profile pictures went |
| `web.config_applier` | The host is taking the web config editor's requests |

## Connections and sharing

| Check | What it answers |
|---|---|
| `servarr.connection` | Each configured arr app answers with its key, one result per app |
| `sharing.ics_feeds` | How the calendar feeds users shared with Istota are doing, as counts |
| `sharing.ocm` | A remote server can reach the Open Cloud Mesh receiver at `[site] hostname` |

## Users and configuration

| Check | What it answers |
|---|---|
| `config.skill_overlays` | Every per-skill user overlay loads, or is named with the reason it does not |
| `config.operator_persona` | Which persona is in force, and whether a shipped update is waiting to be merged |
| `users.email_address_uniqueness` | No email address is held by more than one user |

Checks report names and counts, never credential values. The detail for each check, and the reasoning behind its severities, is in the source, `src/istota/doctor.py`.
