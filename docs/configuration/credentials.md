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

CalDAV credentials are derived from the Nextcloud app password automatically, so a Nextcloud-backed deployment configures nothing separately. The shape with no Nextcloud has no app password to derive from, so an install pointed at a CalDAV server of its own writes a `[caldav]` section, whose `password` takes `ISTOTA_CALDAV_PASSWORD`. Any field set there wins over the Nextcloud derivation, so it is equally how a Nextcloud deployment points at an external calendar server. That override is what keeps the credential out of `config.toml`.

The SMS and WhatsApp rows are the exception to the credential-proxy sentence introducing this table: those credentials are consumed by the transports, which run in the daemon and web processes, and never enter a task's environment at all — so there is nothing for the credential proxy to strip. The non-secret account identifiers beside them — Twilio's `account_sid`, `api_key_sid` and `messaging_service_sid`, and Telnyx's `messaging_profile_id` — take `ISTOTA_SMS_*` overrides of their own so a whole provider block can come from the environment. WhatsApp's `waba_id` and `phone_number_id` do not: they are plain config in `config.toml`. Every field is listed in the [configuration reference](reference.md). The `baileys` WhatsApp adapter has no credential in config: the paired WhatsApp Web session on disk *is* the credential — see [WhatsApp](../features/whatsapp.md).

`ISTOTA_SECRET_KEY` is the master encryption key for the `secrets` table and `google_oauth_tokens`. It must be at least 32 characters; the key is scrypt-derived into a Fernet key at runtime. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`.

`ISTOTA_WEB_TOKEN_KEY` is a *separate* key, env-only, used by `webui/tokens.py` for the per-user Nextcloud tokens the web UI stores when `[web] token_storage = "encrypted"`. It derives with a different salt, so the two key custodies stay independent — the web tier holding user tokens does not imply access to everything in the secrets table.

### Provisioning global credentials

**The stack**: `istota setup` writes each credential it asks for to a file under `/srv/app/istota/secrets/` on the VM (0400, owned by uid 10001), which compose mounts and the container exports as the matching `ISTOTA_*` variable, keeping it out of `config.toml`. Every file exists, empty when unused, because compose will not start without one. Change a credential from the web config editor (Admin → Configuration), or write it on the VM with `istota-stack put-files` (JSON on stdin) and restart the stack with `systemctl restart istota-stack`. `istota setup` also writes the master key to `/data/.secret_key` (`/srv/app/istota/data/.secret_key` on the VM), and the entrypoint generates one there on first start if it is missing; neither ever replaces an existing key.

**A Mac development checkout**: edit `config.toml` directly, or set the `ISTOTA_*` env vars in an `istota.env` file, which `istota serve` reads before it loads the config. It reads the file named with `--env-file`, else the one beside the config named with `-c`, else `~/.config/istota/istota.env`. A checkout's `config/config.toml` takes precedence over `~/.config/istota/config.toml`, so check which config is loading before you edit one. See [the edit-run loop on a Mac](../development/setup.md#the-edit-run-loop-on-a-mac).

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

The History action lists prior versions without their values. Restore and purge need a fresh confirmation (see below). Deleted credentials remain restorable until their saved versions expire. History keeps up to ten versions per field for 180 days by default; the Activity list records credential actions by name and actor, never by secret value.

On a multi-user deployment, the store requires effective sandboxing unless the operator sets `[security] allow_unsandboxed_multi_user_vaults = true` and restarts. The opt-in accepts same-uid cross-task access; it does not isolate users. `istota doctor --only security.credential_isolation` reports the policy.

## Import and export

Use **Import from KeePass** in Settings → Credentials. Choose a `.kdbx` file, enter its passphrase, and supply the source key file if it needs one. Preview groups entries as new, changed, unchanged, conflicting or skipped. New entries are selected by default; changed entries require an explicit choice so a stale file cannot silently replace newer values. Import replaces only selected credentials. Other credentials stay as they are, and replaced values enter history. The file, key file and passphrase are discarded after each request and cleared from the form on completion or cancellation.

If a top-level `istota/` group exists, import reads only that group. Otherwise the preview lists the whole file. Group and entry names become lowercase underscore-separated names; password, username, URL and custom fields become members of the same credential. OTP formats are parsed privately. Entries under `generated/` retain their generated source and can include OTP and recovery codes. A name already owned by a deployment or another source is a conflict. The preview reports parser limits and skipped entries.

### Credential bindings

KeePass URL fields bind a credential to its site. Custom fields `istota_hosts` and `istota_headers` add hosts and authentication headers. The `istota:reveal` tag allows public value reads when reveal enforcement is enabled. On first import, an entry with a host inside `istota/` gets access unless it has `istota:nogrant`. Unscoped imports and generated entries receive no automatic grant. Existing grants that were narrowed or revoked stay that way.

**Export to KeePass** creates a fresh KDBX4 file with Argon2id. Every web export needs a confirmation, on both email and Nextcloud sessions: a one-time code sent to your native sign-in email address, or, where the install has no mail set up, your account password. An account with no sign-in address cannot export until an operator adds one, and one with no password on an install without mail needs a password link from the operator (`istota auth reset <user> --print-link`, which works only where `[web] auth` includes `email`) or mail set up. Each export is announced by email and in the bell. Istota generates a random password and shows it once; save it before closing the dialog. You may also generate a browser-side key file, which you must keep with the password to open the export. Merge the downloaded file into your own password database if wanted. Istota never modifies that database.

Exports contain Settings → Credentials entries, including generated passwords, OTP seeds and recovery codes. Connected-service secrets and wallet cards are excluded. The host CLI uses the same importer and exporter:

```bash
istota secret export --user alice --out credentials.kdbx
istota secret import --user alice credentials.kdbx
istota secret import --user alice credentials.kdbx --include-changed --keyfile credentials.keyx
```

These commands require a terminal. Export refuses an existing output file and prints its generated password once. Import prompts privately for the passphrase, prints the preview, and imports new entries; `--include-changed` also replaces changed entries.

### Scheduled backups

Register an X25519 age public recipient (`age1…`) under Scheduled backup. Changing or clearing it requires a confirmation and creates a notice. Keep the corresponding private key outside Istota. The server writes dated `.tar.age` files to your `exports/credential-backups/` folder, daily by default. The encrypted archive contains a fresh `.kdbx` and `PASSWORD.txt`; plaintext stays in memory. Backups contain the same credential set as exports. Retention defaults to 30 backups. Failures are reported after three attempts, and `security.credential_backup` reports failed or overdue runs.

### Credentials Istota generates

`istota-credential new acme --url https://acme.example` creates a password in the table and returns names such as `generated_acme`, never its value. A task fills it through `browse interact --fill-credential`. The operator equivalent is `istota secret vault-new`. Neither command writes a KeePass file. Use export or scheduled backup for an independent copy.

OTP enrollment is set once with `istota-credential otp-set NAME` on stdin; a task can fill current codes with `browse interact --fill-otp`. Recovery codes can be saved through `browse interact --save-recovery SELECTOR=NAME` without returning them to the task. Settings reveals recovery codes only after a confirmation. `istota secret retire --user alice --name generated_acme --yes` removes a generated credential and its grant; exported files remain untouched.

A restored historical recovery set has no spent-code snapshot, so it becomes a plain text block and cannot be filled automatically. Restoring the same value leaves its existing spent-code state intact. KeePass exports preserve the recovery format and mark spent codes, and importing that export restores both. Older files without format metadata are treated as blocks unless they contain spent-code markers.

### Upgrading an existing vault

The daemon attempts one final non-deleting import for each configured legacy file, then removes the stored passphrase and sync state and sends one notice. Missing or unreadable files are retried for up to seven days. A wrong passphrase, corrupt file, refused path or missing library ends the automatic attempt; import the file manually when the problem is fixed. Your `.kdbx` bytes remain untouched. Legacy `vault_path` is used only by this migration; `vault_sync_interval` is ignored. Configuration loading warns about both retired settings.

## Back up the secret key separately

`ISTOTA_SECRET_KEY` opens the encrypted database. Losing it loses every stored credential unless you have an independent export. Keep a copy outside the storage and backups that hold the database.

The stack keeps it at `/data/.secret_key`, which is `/srv/app/istota/data/.secret_key` on the VM. The local VM also copies it into the Mac's login Keychain (service `istota.local`), which is what `istota-stack restore` reads. Database snapshots under `Backups/db/snapshots` do not include the key. A backup of all of `/data` may contain both the database and its key, so it does not provide that separation.

An age credential backup is an independent recovery path: decrypt it with the private age key you keep elsewhere, then open its KeePass file with the enclosed password. It needs no server secret key. `security.key_separation` checks file locations, and reports when scheduled credential backups are off.

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


## Credential broker

The credential broker lets a task use a stored credential without ever holding its value. The task writes a placeholder such as `{{cred:portal_token}}` where the secret would go, and the network proxy puts the real value in on the way out, only on a connection to that credential's own site. The value never enters the sandbox, so a task that has been steered by a prompt injection has nothing to copy out.

The broker and reveal enforcement are both on by default on the stack. On a shape without a sandbox and a network proxy, such as `istota serve` on a Mac, they are off by default, because a placeholder needs the proxy to be substituted and a value is only contained by the sandbox.

### What it changes

With both switches on:

- A task cannot read the value of a credential. `istota-credential get`, `istota-credential run` and `istota-credential env` are refused with `credential_brokered`. The one exception is an entry the user marked readable (below).
- A task authenticates an HTTP request by putting a placeholder in an authentication header. The proxy substitutes it only for a host the credential is bound to, and only if a grant covers the task.
- `git`, `gh` and `glab` use placeholders for the deployment's forge token without any change to how a task calls them. The token is not in the task's environment.
- `browse interact --fill-credential` and `--fill-otp` work as before, but need a grant that covered the task when it started.
- Skills that run host-side (email, calendar, the WordPress skill and the rest) still receive the credentials their manifests declare. They run outside the sandbox and read through a private channel the model cannot reach.

### Setting up a credential

A credential needs a site and a grant before a task can use it.

1. **A site.** In Settings → Credentials, the Site field and "Also used on" list the hosts the credential may be sent to. In a KeePass file, the entry's URL field and an `istota_hosts` custom field do the same. Hosts are matched exactly, including the port. HTTPS is required unless the grant allows HTTP.
2. **Headers.** By default a placeholder is accepted in `Authorization`, `Private-Token`, `X-API-Key` and `X-Auth-Token`. The Headers field (or an `istota_headers` custom field) adds others.
3. **A grant.** "Edit access" on the credential sets who may use it: all rooms or chosen rooms, and whether scheduled tasks may. A grant for a scoped KeePass import is made on first import unless the entry is tagged `istota:nogrant`. A credential a task creates with `istota-credential new` is granted to that task's room.
4. **Readable, if it must be.** A credential used outside an HTTP header, for a non-HTTP protocol or to sign something, cannot be substituted. Tick "Tasks may read the secret" (the `istota:reveal` tag in KeePass) and tasks may read its value as before. Prefer a host-side skill where one exists.

### Using it in a task

```sh
istota-credential list                     # names, bound hosts and grant state; never values
istota-credential placeholder portal_token # prints {{cred:portal_token}}; hosts go to stderr
curl -H 'Authorization: Bearer {{cred:portal_token}}' https://portal.example/api
```

Quote the placeholder in single quotes so the shell passes it through unchanged. A client can build a Basic header itself: the proxy decodes it and substitutes a placeholder in the password half. When the proxy refuses a request it answers with a `X-Istota-Refused` header naming the reason, for example `credential_not_granted`, `credential_not_bound`, `credential_header_not_allowed` or `credential_https_required`. The fix for each is in Settings, not in the task.

### How it works

1. **At task start** the daemon syncs the deployment forge token's binding, then freezes the task's grant snapshot: every grant whose room scope covers the task's room, and for a scheduled task, only grants that allow scheduled use. A guest's turn, and a task nobody asked in a shared room, get no credentials. A task with no room gets none either. Revoking or changing a grant refuses its next use, even in a running task.
2. **A certificate authority** lives in daemon state under the database directory (`credential-broker/`, mode 0700), outside every sandbox bind. Each task gets a trust bundle, the system roots plus this CA's public certificate, in its read-only control directory. `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO` and `NODE_EXTRA_CA_CERTS` point at it inside the sandbox only.
3. **A connection to a bound host** in the snapshot is intercepted: the proxy ends the task's TLS with a certificate for that host (valid for 24 hours), checks that SNI and `Host` match the CONNECT target, and opens its own TLS connection upstream, verified against the daemon's trust store. Both legs use HTTP/1.1. A connection to any other host is tunnelled unchanged, so a placeholder sent there arrives as inert text.
4. **On each request** the proxy re-reads the grant and the binding, then replaces a placeholder only in an allowed authentication header. A placeholder in the URL, or in the first `scan_max_bytes` of the body, refuses the request. Responses up to the same size are scrubbed of the substituted value before the task sees them.
5. **Reveal enforcement** is in the skill proxy, which serves `istota-credential`. It refuses a public read of any entry not marked readable, and refuses manifest `env` reads, which have no readable marker. Host-side skills read through their private channel and are unaffected.

Audit records name the task, the credential and the outcome. They never hold a value.

### Limits

- Clients that need HTTP/2 or pin certificates cannot use an intercepted host. Use a host-side skill or a readable entry.
- Compressed request bodies, compressed responses to authenticated requests, trailers and protocol upgrades are refused on intercepted hosts.
- Body bytes past `scan_max_bytes` (1 MiB by default) stream unscanned: a placeholder there is not substituted, and a value echoed there is not scrubbed.
- The scrub matches the exact substituted bytes. A service that encodes or transforms the credential in its response is not caught.
- The devbox has its own credential proxy and is outside the broker. Forge tokens reach a build in the devbox as they did before.

### Upgrading an install that ran without it

Grants were never consulted while the broker was off, so an existing install has none. The first task each user runs after the upgrade grants every credential that has a site, once: all rooms, scheduled use on. An entry the user declined (tagged `istota:nogrant`, or created by a task) stays ungranted. This is the same action as the "Allow all existing" button in Settings, which disappears once it has run. Narrow the grants afterwards if you want them narrower.

A credential with no site cannot be used through the broker at all. Give it a site, or mark it readable. A forge token configured after that first task needs its grant set in Settings.

Scripts that read values with `get`, `run` or `env` stop working. Move them to placeholders or a host-side skill, or mark the entries they need readable.

### Turning it down

```toml
[security.credential_broker]
enforce_reveal = false   # values readable again; each read of a brokered entry logs credential_reveal action=would_refuse
enabled = false          # no interception and no grants; enforce_reveal follows unless set
```

With `enforce_reveal = false` the broker still substitutes placeholders, and every public read of a brokered entry is allowed and logged at WARNING with `credential_reveal`, `action=would_refuse`, the task, the request type and the credential name. That log is how to find the scripts that still read values before turning enforcement back on. `enforce_reveal = true` with `enabled = false` has no effect and warns at startup. Restart the daemon after changing either setting.

`istota doctor --only security.credential_broker` reports the CA, the task trust bundles, the network proxy, the peer check, whether sandboxing is in effect, and per user how many credentials have no site or no grant.
