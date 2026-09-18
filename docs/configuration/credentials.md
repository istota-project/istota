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

`ISTOTA_WEB_TOKEN_KEY` is a *separate* key, env-only, used by `web_tokens.py` for the per-user Nextcloud tokens the web UI stores when `[web] token_storage = "encrypted"`. It derives with a different salt, so the two key custodies stay independent — the web tier holding user tokens does not imply access to everything in the secrets table.

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
| Credential vault | `passphrase` | The KDBX vault sync — see [below](#credential-vault) |

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

## Credential vault

A user who keeps their credentials in a password manager otherwise maintains two copies of every key, and the copy Istota reads is the one they cannot see, search or back up. The credential vault removes the second edit: a KeePass (KDBX) file the user maintains on their own devices, which Istota reads on a schedule and copies into the `secrets` table. Off for every user until an operator turns it on.

It is **provisioning input, not a storage backend**. The table stays the live store, `resolve_secret`'s order is unchanged, and a vault that is missing, half-synced or locked leaves every credential working. Istota never writes the file.

### Turning it on

Two keys in the user's `[users.<id>]` block of `config.toml`, and nowhere else — they are deliberately not in `user_profiles` and cannot be set from the web UI, because one selects which file the daemon decrypts and the other selects which credentials that file may overwrite:

```toml
[users.alice]
vault_path = "istota/config/vault.kdbx"
vault_services = ["karakeep", "ntfy"]
```

A relative `vault_path` resolves under that user's own workspace directory, which is where a phone or a laptop can reach it. An absolute one is a host path and must resolve outside every tree a task sandbox can write; that form keeps the file away from a task entirely, at the cost of the user no longer being able to edit it from a phone. Empty — the default — means the feature is off for that user.

`vault_services` is the list of services the file owns. Empty reads the file and applies nothing, which is a usable dry run. A service whose credentials the daemon mints for itself can never be vault-owned (Monarch, Overland, Garmin, Google Workspace), and a name like that is dropped with a warning when the config loads. Today's eligible set is `karakeep`, `ntfy`, `native_brain`, `feeds` and `carto`.

Install the `vault` extra on the host. `pykeepass` and its five dependencies are optional because two of them carry compiled extensions, and a deployment with no vault should not pay for them.

### The passphrase

The passphrase is a per-user secret like any other, stored in the `secrets` table under the `vault` service. Provision it once, from a host shell:

```bash
istota secret ensure -u alice --service vault --key passphrase --generate
```

**It must be generated rather than chosen, and that rule is the whole security argument for this feature.** The vault file sits in a tree bound read-write into that user's own task sandbox, so a prompt-injected task can read the ciphertext of every credential the vault holds and carry it out. Argon2id makes that useless against 256 random bits. It does not make it useless against a memorable phrase. Everything else here is a boundary against a mistake; this is the only one standing in front of an adversary.

`--generate` mints the value, stores it and prints it once. Copy it into the password manager you keep everything else in, and use it as the master password when you create the KDBX file. A *supplied* `--value` shorter than 32 characters is refused rather than warned about, because a warning is read after provisioning and by then the file is already encrypted under it.

`--generate` refuses to replace a passphrase that is already there unless you pass `--force`. Minting a second one destroys the only copy the server has of the value the file is encrypted under, and the command otherwise advertises itself as idempotent — so an Ansible play re-running the documented provisioning line is the ordinary case rather than a careless one.

There is no web form for the passphrase, on purpose: a form field is an invitation to type a memorable one.

### The file

Group path `istota/<service>`, one subgroup per service. The **entry title** is the secret key and the **password field** is the value. Everything else is ignored — username, URL, notes, attachments, custom string fields, and anything in the recycle bin.

```
istota/
  karakeep/
    base_url     (password field: https://karakeep.example.com)
    api_key      (password field: ak_…)
  ntfy/
    topic
```

Entry titles rather than custom attributes on one per-service entry, because custom attributes are second-class in most mobile clients and several cannot create them at all — and editing from a phone is the point.

The `<service>` group name is matched case-insensitively, so `Karakeep` owns `karakeep`; a phone keyboard that autocapitalizes it costs nothing. Entry titles are matched exactly, because a key that does not match the schema is reported as a typo rather than silently discarded. Values are stripped of surrounding whitespace and nothing else is normalized. An entry with an empty password is skipped rather than treated as a deletion, and a title that appears twice in one group skips that key with a warning.

### What a sync does

The daemon reads the file at start-up and every `scheduler.vault_sync_interval` seconds (300 by default; 0 turns both off). Each cycle hashes the file bytes, and stops there when nothing has changed — no unlock, no database write, no log line. `istota secret vault-sync [-u alice]` runs one by hand and ignores the cached hash.

For a service in `vault_services`, the vault is the authority:

- A key the file's group holds is written over whatever the table had.
- **A key the table has, the schema declares, and the group does not hold is deleted.** Deleting a credential through the vault means deleting the entry.
- A service the file does not mention at all is left alone entirely. That rule is what makes a vault that parses but has lost its contents harmless: a resync that replaced it with an emptier copy removes nothing.

**Adopting a service deletes the keys the vault does not mention, on the first sync.** The group is present the moment you add the service to `vault_services`, so a user with an ntfy topic, token and username in the settings UI, and a vault group holding only `topic`, loses the other two within five minutes. So put every key you already hold into the file *before* adding its service to the list. `istota secret vault-sync` prints the count and names each deleted key, which is where that gets noticed.

`istota secret remove` is still the direct route for removing one credential, and `istota secret ensure` refuses to write a vault-owned key unless you pass `--force` — a CLI write there does not touch the file, so the digest never moves and the value would stand indefinitely against the file the user believes is authoritative.

### What the settings UI does

A vault-owned service's fields render disabled, with a sentence saying the vault owns them. `PUT` and `DELETE` on those keys answer 409. The "Connected services" heading carries a read-only status line: the resolved path, the owned services, when Istota last applied the file, and the error class when it is failing.

**"Last applied" is not a health check, and a healthy vault shows an old stamp.** The record is written only by a cycle that did work, and a cycle over an unchanged file does none — so a vault nobody has edited for three weeks reports a three-week-old timestamp and is working perfectly.

### When it fails

Each of these leaves the credentials in the table alone and raises a notification on the user's connected-services panel, once per transition rather than once per cycle:

| What happened | What to do |
|---|---|
| The stored passphrase does not open the file | Re-provision it, then run `istota secret vault-sync` |
| The file is not a readable KeePass database | Also what a sync caught mid-write looks like — check the mount before suspecting the file |
| Nothing at the configured path | Check `vault_path`, and that the file has synced to the server |
| The `vault` extra is not installed | An operator remedy, not a user one |
| No passphrase provisioned | `istota secret ensure … --generate` |
| `ISTOTA_SECRET_KEY` cannot read the stored passphrase | A deployment problem; see `security.secret_key` in `istota doctor` |
| `vault_path` is one the daemon may not open | An operator corrects the line in `config.toml` |

`istota secret vault-status -u alice` prints the whole answer for one user: the resolved path, whether a passphrase is provisioned, the groups the file holds, which of them are owned, and which owned services the file does not mention. `istota doctor`'s `security.credential_vault` answers the same questions across every configured user, and reports counts rather than key names.

**Rotate the vault's master password in the quiet order**: provision the new passphrase with `istota secret ensure` first, then change it in KeePassXC. The other order raises a notification in between, because the rewritten file no longer opens with the stored value.

### Two things that are not evidence

Both are honest and both mislead if read the other way:

- **A sync bumps `last_accessed_at` on every credential it owns**, because writing over a value means comparing it first. `istota secret vault-status` bumps it on the `vault/passphrase` row for the same reason. So that column stops being evidence that a vault-owned credential is read by anything.
- **A sync runs whenever the file's bytes change, not whenever a credential changes.** KDBX draws a fresh master seed on every save, so saving a database nobody edited produces different bytes and a full apply. That is harmless — every write is idempotent — and it is the one thing that eventually re-asserts the file's version of a value somebody changed in the table directly.

### What it does not fix

The secrets table still holds a copy of everything. What the vault removes is the second place a user has to *edit*.

The security accounting is that the vault adds no confidentiality and one new exposure. Every credential in it is also a row in the table, and the passphrase that opens it is another row in that same table, so one secret — `ISTOTA_SECRET_KEY` — opens both. What changes is *where* credential ciphertext sits: none of it used to be reachable from inside a sandbox, and now a copy of every owned credential is, in a file a task can read, copy out, delete or overwrite. Deleting or corrupting it is a denial of service that leaves every credential working and raises a notification. Replacing it with an older copy the user keeps in the same tree is a real rollback vector, bounded by `vault_services`.

The generated passphrase is the entire mitigation. The absolute `vault_path` form removes the exposure completely by putting the file outside every tree a sandbox can reach, at the cost of the phone; it is not the default because editing from a phone is the feature.

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
  credential-fetch <VAR>      ← skill CLI requests a specific var
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
| `derive_lookup_allowlist(authorized, skill_index)` | vars the proxy will respond to over `credential-fetch`, minus `_PROXY_LOOKUP_BLOCKED` |

There is no longer a hand-maintained `_PROXY_CREDENTIAL_VARS` constant or `_CREDENTIAL_SKILL_MAP` in code. Adding a credential is a manifest edit; everything else falls out of `derive_*`.

Authorization is **decoupled from skill selection**. A skill is authorized for credential access whenever its sensitive credentials actually resolve for this user — not when the skill is selected into the prompt. This prevents keyword-miss lockouts: if a user has Karakeep configured, the bookmarks skill can always request `KARAKEEP_API_KEY` at runtime, even if "bookmark" wasn't in the prompt. Doc-only skills like `developer` (no CLI module) are eligible too — they consume credentials via `credential-fetch` from helper scripts the skill's `setup_env` hook bind-mounts into the sandbox.

Auto-authorization passes `fallbacks_disabled=True` to the resolver: an instance-wide `EnvironmentFile` fallback for an operator-set value cannot fan out and auto-authorize every user, defeating the per-user privacy posture.

For more on the proxy architecture, PID-scoped socket paths, and rejection logging, see [security: credential proxy](../deployment/security.md#credential-proxy).

## Credential proxy variables

The proxy strips these env vars from the Claude subprocess and injects them server-side. The list is manifest-derived (every `EnvSpec` with `sensitive: true`); today's set:

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
- `ISTOTA_BRAIN_NATIVE_API_KEY` — declared by `code_review`, which calls a model itself. It is therefore in this set on a `claude_code` deployment where the native brain is otherwise unused
- `ISTOTA_SECRET_KEY` — routed to module-skill subprocesses that need to decrypt per-user secrets, but blocked at the lookup endpoint via `_PROXY_LOOKUP_BLOCKED` so `credential-fetch ISTOTA_SECRET_KEY` from inside Claude is rejected

See [environment variables](../reference/environment-variables.md) for the complete env var reference.

## Adding credentials for new integrations

When adding a new service integration, follow this decision tree:

1. **Who authenticates?** If the bot logs in as itself (a service account, a bot token), it's global. If it accesses a user's personal account, it's per-user.
2. **Global** → add the field to the relevant config dataclass + `[section]` in `config.toml`, declare the env var in the consuming skill's `skill.md` `env:` block with `from: "config"`, `sensitive: true`, and an optional `fallback_var` for `EnvironmentFile` overrides. The proxy strip-set, auth map, and lookup allowlist update automatically via `derive_*`.
3. **Per-user** → add the service and keys to `secret_schema.py` (connected service or module service), then declare the env var in the consuming skill's `skill.md` `env:` block with `from: "secret"` (and `sensitive: true` if it's a credential rather than a host/URL). For complex setup (e.g., `developer`'s git credential helper), use `from: "setup_env"` and write a `setup_env(ctx) -> dict[str, str]` hook in the skill's `__init__.py`.
4. **OAuth** → if the service uses OAuth, consider a dedicated table (like `google_oauth_tokens`) or store the refresh token as a regular secret. OAuth flows need a web UI endpoint for the redirect dance.

For the full skill development workflow including env var mapping, see [adding skills](../development/adding-skills.md).

## Edge cases

**ntfy** — could go either way. The bot could have one global ntfy topic and broadcast to all users. Instead, it's per-user: each user picks their own topic and optionally their own server. This scales better for multi-user and lets users opt out or use self-hosted ntfy.

**CalDAV** — currently global (one service account with shared calendar access via Nextcloud). If Istota ever supports users bringing their own CalDAV servers, this would need a per-user path.

**Browser** — `BROWSER_API_URL` and `BROWSER_VNC_URL` are deployment-level config, not credentials. They point to the headless browser container.
