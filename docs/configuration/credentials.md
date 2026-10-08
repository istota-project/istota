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
| CalDAV (with Nextcloud) | derived from `[nextcloud]` | `ISTOTA_NEXTCLOUD_APP_PASSWORD` | `calendar`, `location` skills |
| CalDAV (without Nextcloud) | `[caldav]` | `ISTOTA_CALDAV_PASSWORD` | `calendar`, `location` skills |
| Nextcloud | `[nextcloud]` | `ISTOTA_NEXTCLOUD_APP_PASSWORD` | `nextcloud` skill |
| GitLab token | `[developer]` | `ISTOTA_DEVELOPER_GITLAB_TOKEN` | `developer` skill |
| GitHub token | `[developer]` | `ISTOTA_DEVELOPER_GITHUB_TOKEN` | `developer` skill |
| Google OAuth client secret | `[google_workspace]` | `ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET` | Google OAuth flow |
| Web OAuth2 client secret | `[web]` | `ISTOTA_WEB_OAUTH2_CLIENT_SECRET` | Nextcloud login flow |
| Web session signing key | `[web]` | `ISTOTA_WEB_SESSION_SECRET_KEY` | Session cookies |
| Twilio (SMS) | `[sms.twilio]` | `ISTOTA_SMS_TWILIO_AUTH_TOKEN`, `ISTOTA_SMS_TWILIO_API_KEY_SECRET` | SMS transport |
| Telnyx (SMS) | `[sms.telnyx]` | `ISTOTA_SMS_TELNYX_API_KEY` | SMS transport |
| WhatsApp Cloud API | `[whatsapp.cloud]` | `ISTOTA_WHATSAPP_ACCESS_TOKEN`, `ISTOTA_WHATSAPP_APP_SECRET`, `ISTOTA_WHATSAPP_VERIFY_TOKEN` | WhatsApp transport, `whatsapp_cloud` adapter |
| Native brain API key | `[brain.native]` | `ISTOTA_BRAIN_NATIVE_API_KEY` | The native brain's model provider, and the `code_review` skill, which calls a model of its own even where the native brain is otherwise unused |
| `ISTOTA_SECRET_KEY` | env only | `ISTOTA_SECRET_KEY` | Fernet encryption for tier-2 secrets |
| `ISTOTA_WEB_TOKEN_KEY` | env only | `ISTOTA_WEB_TOKEN_KEY` | Separate Fernet key for stored per-user Talk tokens (`web_user_tokens`) |

CalDAV credentials are derived from the Nextcloud app password automatically, so a Nextcloud-backed deployment configures nothing separately. The shape with no Nextcloud has no app password to derive from, so an install pointed at a CalDAV server of its own writes a `[caldav]` section, whose `password` takes `ISTOTA_CALDAV_PASSWORD`. Any field set there wins over the Nextcloud derivation, so it is equally how a Nextcloud deployment points at an external calendar server. That override is what keeps the credential out of `config.toml`, which on the standalone install is a generated file whose own header says secrets belong in the sibling `istota.env`.

The SMS and WhatsApp rows are the exception to the credential-proxy sentence introducing this table: those credentials are consumed by the transports, which run in the daemon and web processes, and never enter a task's environment at all — so there is nothing for the credential proxy to strip. The non-secret account identifiers beside them — Twilio's `account_sid`, `api_key_sid` and `messaging_service_sid`, and Telnyx's `messaging_profile_id` — take `ISTOTA_SMS_*` overrides of their own so a whole provider block can come from `secrets.env`. WhatsApp's `waba_id` and `phone_number_id` do not: they are plain config, written by the Ansible role or by the Docker `.env`. Every field is listed in the [configuration reference](reference.md). The `baileys` WhatsApp adapter has no credential in config: the paired WhatsApp Web session on disk *is* the credential — see [WhatsApp](../features/whatsapp.md).

`ISTOTA_SECRET_KEY` is the master encryption key for the `secrets` table and `google_oauth_tokens`. It must be at least 32 characters; the key is scrypt-derived into a Fernet key at runtime. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`.

`ISTOTA_WEB_TOKEN_KEY` is a *separate* key, env-only, used by `webui/tokens.py` for the per-user Nextcloud tokens the web UI stores when `[web] token_storage = "encrypted"`. It derives with a different salt, so the two key custodies stay independent — the web tier holding user tokens does not imply access to everything in the secrets table.

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
| Garmin Connect | (interactive email/password → MFA; the stored blob is machine-managed) | `health` and `location` modules |
| ntfy | `topic`, `server_url`\*, `username`\*, `password`\*, `token`\* | push notifications |
| Native brain provider | `api_key` | The native brain — a per-user key overlaying the instance one |

\* = optional

Garmin is neither a writable-fields service nor a plain OAuth redirect, so the settings page renders a bespoke card for it; its auth routes live under `/istota/api/garmin/*` and are shared by both modules. The native-brain key is CLI-only — provision it with `istota secret ensure -s native_brain` — because a web knob that set only the key would be a per-user *billing* override dressed up as "bring your own brain".

### Module services

Gated by module enablement. Appear on per-module settings pages.

| Module | Service | Keys | Consumed by |
|---|---|---|---|
| money | Monarch Money | `session_id`, `csrftoken` | `money` skill (transaction sync via cookie auth) |
| feeds | Tumblr | `tumblr_api_key`\* | `feeds` skill (Tumblr feed ingestion) |
| location | Overland | `ingest_token` | `location` skill (GPS ingestion webhook) |
| location | CARTO basemap | `api_key`\* | The map surfaces, via `GET /api/map/basemap` |

\* = optional

CARTO is its own service rather than a second field on Overland: a different credential with a different lifecycle, and adding it there would flip that card to "Partial" for every user who has an ingest token and no basemap key. **It is the one credential in the schema that is meant to reach the browser** — MapLibre puts it in the tile URL, so it ships to every client that loads a map, and CARTO issues these free for exactly that. It lives in the store for the encryption at rest, the per-user scoping and the input UI, not because it is confidential; nothing reads it back as a value, so the store stays write-only to the browser where that means something. Setting one **selects CARTO as that user's basemap**, overriding the operator's [`[web.map] provider`](reference.md#webmap); leaving it empty uses the deployment default.

### Google OAuth tokens

A special case: stored in their own `google_oauth_tokens` table (not in `secrets`) because the OAuth flow writes `access_token` and `refresh_token` as a pair with expiry metadata. Fernet-encrypted at rest using the same `ISTOTA_SECRET_KEY`. A migration function auto-upgrades any pre-existing plaintext rows on read.

Users connect their Google account through the web dashboard at `/istota/` (the dashboard shows a Google Workspace card). See [Google Workspace](../features/google-workspace.md) for the full setup.

## Shared credentials

Settings → Credentials lists named secrets that your tasks can use. The encrypted `secrets` table is the only live store. A credential imported from KeePass has source `local`, just like one added in Settings; `generated` means Istota created it, and `config` means it comes from deployment configuration. The page labels these Istota, Generated and Deployment.

Add or edit a local credential in Settings. Names start with a lowercase letter and contain lowercase letters, digits and underscores. The `generated_` and `forge.` prefixes are reserved. A site binds the credential to a host; "Also used on", allowed authentication headers and "Tasks may read the secret" set its other binding rules. Values and OTP seeds are write-only on this form. Leave a value empty while editing to keep it. Delete removes the credential and its access together.

The History action lists prior versions without their values. Restore and purge need a fresh emailed confirmation code. Deleted credentials remain restorable until their saved versions expire. History keeps up to ten versions per field for 180 days by default; the Activity list records credential actions by name and actor, never by secret value.

On a multi-user deployment, the store requires effective sandboxing unless the operator sets `[security] allow_unsandboxed_multi_user_vaults = true` and restarts. The opt-in accepts same-uid cross-task access; it does not isolate users. `istota doctor --only security.credential_isolation` reports the policy.

## Import and export

Use **Import from KeePass** in Settings → Credentials. Choose a `.kdbx` file, enter its passphrase, and supply the source key file if it needs one. Preview groups entries as new, changed, unchanged, conflicting or skipped. New entries are selected by default; changed entries require an explicit choice so a stale file cannot silently replace newer values. Import replaces only selected credentials. Other credentials stay as they are, and replaced values enter history. The file, key file and passphrase are discarded after each request and cleared from the form on completion or cancellation.

If a top-level `istota/` group exists, import reads only that group. Otherwise the preview lists the whole file. Group and entry names become lowercase underscore-separated names; password, username, URL and custom fields become members of the same credential. OTP formats are parsed privately. Entries under `generated/` retain their generated source and can include OTP and recovery codes. A name already owned by a deployment or another source is a conflict. The preview reports parser limits and skipped entries.

### Credential bindings

KeePass URL fields bind a credential to its site. Custom fields `istota_hosts` and `istota_headers` add hosts and authentication headers. The `istota:reveal` tag allows public value reads when reveal enforcement is enabled. On first import, an entry with a host inside `istota/` gets access unless it has `istota:nogrant`. Unscoped imports and generated entries receive no automatic grant. Existing grants that were narrowed or revoked stay that way.

**Export to KeePass** creates a fresh KDBX4 file with Argon2id. Every web export requires a one-time code sent to your native sign-in email address, on both email and Nextcloud sessions. An account with no sign-in address cannot export until an operator adds one. Each export is announced by email and in the bell. Istota generates a random password and shows it once; save it before closing the dialog. You may also generate a browser-side key file, which you must keep with the password to open the export. Merge the downloaded file into your own password database if wanted. Istota never modifies that database.

Exports contain Settings → Credentials entries, including generated passwords, OTP seeds and recovery codes. Connected-service secrets and wallet cards are excluded. The host CLI uses the same importer and exporter:

```bash
istota secret export --user alice --out credentials.kdbx
istota secret import --user alice credentials.kdbx
istota secret import --user alice credentials.kdbx --include-changed --keyfile credentials.keyx
```

These commands require a terminal. Export refuses an existing output file and prints its generated password once. Import prompts privately for the passphrase, prints the preview, and imports new entries; `--include-changed` also replaces changed entries.

### Scheduled backups

Register an X25519 age public recipient (`age1…`) under Scheduled backup. Changing or clearing it requires an emailed code and creates a notice. Keep the corresponding private key outside Istota. The server writes dated `.tar.age` files to your `exports/credential-backups/` folder, daily by default. The encrypted archive contains a fresh `.kdbx` and `PASSWORD.txt`; plaintext stays in memory. Backups contain the same credential set as exports. Retention defaults to 30 backups. Failures are reported after three attempts, and `security.credential_backup` reports failed or overdue runs.

### Credentials Istota generates

`istota-credential new acme --url https://acme.example` creates a password in the table and returns names such as `generated_acme`, never its value. A task fills it through `browse interact --fill-credential`. The operator equivalent is `istota secret vault-new`. Neither command writes a KeePass file. Use export or scheduled backup for an independent copy.

OTP enrollment is set once with `istota-credential otp-set NAME` on stdin; a task can fill current codes with `browse interact --fill-otp`. Recovery codes can be saved through `browse interact --save-recovery SELECTOR=NAME` without returning them to the task. Settings reveals recovery codes only after an emailed confirmation. `istota secret retire --user alice --name generated_acme --yes` removes a generated credential and its grant; exported files remain untouched.

### Upgrading an existing vault

The daemon attempts one final non-deleting import for each configured legacy file, then removes the stored passphrase and sync state and sends one notice. Missing or unreadable files are retried for up to seven days. A wrong passphrase, corrupt file, refused path or missing library ends the automatic attempt; import the file manually when the problem is fixed. Your `.kdbx` bytes remain untouched. Legacy `vault_path` is used only by this migration; `vault_sync_interval` is ignored. Configuration loading warns about both retired settings.

## Back up the secret key separately

`ISTOTA_SECRET_KEY` opens the encrypted database. Losing it loses every stored credential unless you have an independent export. Keep a copy outside the storage and backups that hold the database.

Ansible stores it in `/etc/<namespace>/secrets.env`. Docker uses `/data/.secret_key` unless the environment supplies the key. A standalone installation uses `istota.env` beside its configuration. Database snapshots under `Backups/db/snapshots` do not include the key. A backup of all of `/data` may contain both the database and its key, so it does not provide that separation.

An age credential backup is an independent recovery path: decrypt it with the private age key you keep elsewhere, then open its KeePass file with the enclosed password. It needs no server secret key. `security.secret_key_separation` checks file locations, and reports when scheduled credential backups are off.

## How credentials flow at runtime

```
config.toml / env vars / encrypted secrets table
        │
        ▼
  build_skill_env(skill_index, ctx)   ← walks every skill manifest, resolves each EnvSpec
        │
        ▼
  _split_credential_env(env, derive_credential_set(skill_index))
  _split_credential_env(env, derive_proxy_only_set(skill_index))   ← second pass: DB paths
        │                │
        │                └──▶ Claude subprocess (clean_env — no secrets, no DB paths)
        ▼
  SkillProxy(credential_env, derive_skill_credential_map(...), derive_lookup_allowlist(...))
        │
        ▼
  istota-credential env <VAR> ← skill CLI requests a specific var
        │                        proxy checks the per-skill credential map
        ▼                        and the lookup allowlist (minus _PROXY_LOOKUP_BLOCKED)
  skill subprocess env
```

The credential set, per-skill scope, and lookup allowlist are all **derived from skill manifests** by five pure helpers in `executor.py`:

| Helper | Returns |
|---|---|
| `derive_credential_set(skill_index)` | every env var declared with `sensitive: true` across all skills |
| `derive_proxy_only_set(skill_index)` | `ISTOTA_DB_PATH` plus manifest `proxy_only: true` vars (`HEALTH_DB_PATH`, `LOCATION_DB_PATH`). Not secrets — paths that route to the proxy so the model never holds them |
| `derive_authorized_skills(selected, skill_index, ctx, hook_env=None)` | selected skills ∪ skills whose sensitive `EnvSpec`s actually resolve under this task's context. `hook_env` matters: without it a credential produced by a `setup_env` hook — the live `google_workspace` case — can never authorize its own skill |
| `derive_skill_credential_map(authorized, skill_index)` | per-skill credential map (proxy uses this to scope injection) |
| `derive_lookup_allowlist(authorized, skill_index)` | vars the proxy will respond to over `istota-credential env`, minus `_PROXY_LOOKUP_BLOCKED` |

There is no longer a hand-maintained `_PROXY_CREDENTIAL_VARS` constant or `_CREDENTIAL_SKILL_MAP` in code. Adding a credential is a manifest edit; everything else falls out of `derive_*`.

Authorization is **decoupled from skill selection**. A skill is authorized for credential access whenever its sensitive credentials actually resolve for this user — not when the skill is selected into the prompt. This prevents keyword-miss lockouts: if a user has Karakeep configured, the bookmarks skill can always request `KARAKEEP_API_KEY` at runtime, even if "bookmark" wasn't in the prompt. Doc-only skills like `developer` (no CLI module) are eligible too — they consume credentials via `istota-credential env` from helper scripts the skill's `setup_env` hook writes into the sandbox.

Auto-authorization passes `fallbacks_disabled=True` to the resolver: an instance-wide `EnvironmentFile` fallback for an operator-set value cannot fan out and auto-authorize every user, defeating the per-user privacy posture.

For more on the proxy architecture, PID-scoped socket paths, and rejection logging, see [security: credential proxy](../deployment/security.md#credential-proxy).

## Credential proxy variables

The proxy strips these env vars from the Claude subprocess and injects them server-side. The list is manifest-derived (every `EnvSpec` with `sensitive: true`); today's set:

- `CALDAV_PASSWORD`
- `NC_PASS`, and `ISTOTA_NEXTCLOUD_APP_PASSWORD` (the same value, for the relay skill's own config load)
- `SMTP_PASSWORD`
- `IMAP_PASSWORD`
- `KARAKEEP_API_KEY`
- `GOOGLE_WORKSPACE_CLI_TOKEN`
- `GITLAB_TOKEN`
- `GITHUB_TOKEN`
- `MONARCH_SESSION_ID`, `MONARCH_CSRFTOKEN`
- `NTFY_TOKEN`, `NTFY_PASSWORD`
- `TUMBLR_API_KEY`
- `ISTOTA_BRAIN_NATIVE_API_KEY` — declared by `code_review`, which calls a model itself. It is therefore in this set on a `claude_code` deployment where the native brain is otherwise unused
- `ISTOTA_SECRET_KEY` — routed to module-skill subprocesses that need to decrypt per-user secrets, but blocked at the lookup endpoint via `_PROXY_LOOKUP_BLOCKED` so `istota-credential env ISTOTA_SECRET_KEY` from inside Claude is rejected

See [environment variables](../reference/environment-variables.md) for the complete env var reference.

## Adding credentials for new integrations

When adding a new service integration, follow this decision tree:

1. **Who authenticates?** If the bot logs in as itself (a service account, a bot token), it's global. If it accesses a user's personal account, it's per-user.
2. **Global** → add the field to the relevant config dataclass + `[section]` in `config.toml`, declare the env var in the consuming skill's `skill.md` `env:` block with `from: "config"`, `sensitive: true`, and an optional `fallback_var` for `EnvironmentFile` overrides. The proxy strip-set, auth map, and lookup allowlist update automatically via `derive_*`.
3. **Per-user** → add the service and keys to `credentials/schema.py` (connected service or module service), then declare the env var in the consuming skill's `skill.md` `env:` block with `from: "secret"` (and `sensitive: true` if it's a credential rather than a host/URL). For complex setup (e.g., `developer`'s git credential helper), use `from: "setup_env"` and write a `setup_env(ctx) -> dict[str, str]` hook in the skill's `__init__.py`.
4. **OAuth** → if the service uses OAuth, consider a dedicated table (like `google_oauth_tokens`) or store the refresh token as a regular secret. OAuth flows need a web UI endpoint for the redirect dance.

For the full skill development workflow including env var mapping, see [adding skills](../development/adding-skills.md).

## Edge cases

**ntfy** — could go either way. The bot could have one global ntfy topic and broadcast to all users. Instead, it's per-user: each user picks their own topic and optionally their own server. This scales better for multi-user and lets users opt out or use self-hosted ntfy.

**CalDAV** — currently global (one service account with shared calendar access via Nextcloud). If Istota ever supports users bringing their own CalDAV servers, this would need a per-user path.

**Browser** — `BROWSER_API_URL` and `BROWSER_VNC_URL` are deployment-level config, not credentials. They point to the headless browser container.


## HTTP credential broker

The broker is off by default. Set `[security.credential_broker] enabled = true` with the network proxy enabled to use literal placeholders in authentication headers. `istota-credential list` shows names and bindings; `istota-credential placeholder NAME` prints the placeholder on stdout and its bound hosts on stderr without fetching a value.

```sh
curl -H 'Authorization: Bearer {{cred:portal_token}}' https://portal.example/api
```

Bind a credential with a site (a KeePassXC entry's URL field or `istota_hosts`, or the Site and "Also used on" fields of one added in Istota), and grant access in Settings (a scoped import can grant a new entry; see above). Grants limit rooms and scheduled use, for the proxy's substitution and for host-side skills alike. With the broker enabled, `browse interact --fill-credential` needs a bound entry whose grant covered the task when it started, so it is refused with `credential_not_granted` for an ungranted entry, in a room the grant does not cover, and in a scheduled task without scheduled use. The one exception is an entry the same task created with `istota-credential new`, which it may fill; later tasks need a grant for it like any other entry. Public value reads (`get`, `run`) are governed by reveal enforcement, not by grants. With the broker disabled, grants are not consulted. Every HTTP method is allowed on a bound host, including DELETE and WebDAV methods; saved method restrictions from older versions no longer apply. Each task keeps its original grant snapshot across retries; revoking or changing a grant refuses its next use. The broker decodes Basic authentication before substituting a placeholder password, so clients can build the Basic header themselves.

Only a host bound to a credential in the task snapshot is intercepted. Every other connection keeps its original TLS session and carries placeholders as literal text. On an intercepted connection, SNI and Host must match the CONNECT host. IP-literal destinations may omit SNI, as standard TLS clients do. A placeholder in a disallowed header or URL is refused. A placeholder in the first `scan_max_bytes` of a request body is refused before forwarding; later body bytes stream unchanged and are never substituted. The default cap is 1 MiB.

Response headers and bodies no larger than that cap are scrubbed for the exact substituted bytes. A response field name containing a substituted value is refused, since a placeholder cannot be a valid field name. Larger bodies stream without body scrubbing; audit records state both scan limits. This is not protection against an upstream service deliberately encoding or transforming a credential in its response. Compressed request bodies, compressed responses to authenticated requests, trailers and upgrades are unsupported. Both TLS legs use HTTP/1.1, so pinned-certificate clients and clients that require HTTP/2 need a host-side skill or a revealable credential.

`istota doctor --only security.credential_broker` reports the CA, task trust bundles, proxy and peer-check readiness, effective sandboxing, and counts of unbound or ungranted entries. The CA stays in daemon state; only public trust bundles enter tasks. The daemon verifies upstream TLS using its own trust store. Without effective sandboxing, credentials are not contained. With the broker enabled, developer git helpers and gh/glab use placeholders. Public value reads remain available until reveal enforcement is enabled.


### Reveal enforcement rollout

`[security.credential_broker] enforce_reveal = false` is the default. Each public value read of a brokered credential logs a WARNING with `credential_reveal`, `action=would_refuse`, the task, request type, credential name and claimed mode. Values are never logged. This includes callers claiming `mode=skill`: only the private inherited channel given to a host-side skill is trusted. Audit logging does not contain credentials; callers still receive values in this mode.

Enable the broker and migrate scripts to placeholders or host-side skills. Watch the `credential_reveal` records for a week of normal use. Resolve every `would_refuse` use, then observe a full week without one before setting `enforce_reveal = true`. This is an operator rollout step, with no automatic timer or activation. `enforce_reveal` has no effect while `enabled = false`, since without placeholders it would only break git, the forge CLIs and `run`; the daemon logs a warning at startup in that combination. Restart the daemon after changing the setting. Setting it back to false restores public reads and their audit records.

Under enforcement, `get`, `run` and `run --stdin` return `credential_brokered` for an entry without the `istota:reveal` tag. The daemon checks live metadata on each read, so removing the tag and importing the entry revokes the exception for running tasks too. A missing binding grants no exception. Revealable reads keep the existing fetch cap and read WARNING. `list`, `placeholder` and `new` remain available; a new entry is brokered by default.

`env` reads manifest variables, which have no reveal marker, so enforcement refuses all of them, including forge tokens. A hand-written socket client receives the same refusal. Use the forge placeholders or a host-side skill instead; skills still receive their declared environment credentials and resolve vault names through their private channel. A forge wrapper still on its legacy token path will fail until the broker is enabled. The devbox has a separate proxy and is outside this rollout.

Enforcement is independent of the interception switch. Turning it on before migrating consumers can break their authentication. It cannot contain values on an unsandboxed deployment.
