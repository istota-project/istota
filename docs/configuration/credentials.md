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

A user who keeps their credentials in a password manager otherwise maintains two copies of every key, and the copy Istota reads is the one they cannot see, search or back up. The credential vault removes the second edit: a KeePass (KDBX) file the user maintains on their own devices, which Istota reads on a schedule and copies into the `secrets` table. Off for every user until somebody puts a file in the folder and generates a passphrase.

It is **provisioning input, not a storage backend**. The table stays the live store, `resolve_secret`'s order is unchanged, and a vault that is missing, half-synced or locked leaves every credential working. Istota can create entries only in `generated/`; it cannot change or delete existing entries.

When a task needs a new site credential, it can run `istota-credential new acme --url https://acme.example`. Istota generates the password inside the daemon, writes it to the user's KDBX, and returns the three credential names and the username, never the password. The username defaults to a signup address in the bot mailbox when one is configured, or to the user's first configured email address otherwise; `--username` overrides it. The task can fill a form with `browse interact --fill-credential "#password=generated_acme"` and `"#email=generated_acme_username"`. For a site that rejects symbols, pass `--no-symbols`; `--length N` sets the password length. The entry is under `istota/generated/` when the file has a top-level `istota` group, or under root `generated/` otherwise. The latter never creates a top-level `istota` group, since that would narrow the next read and remove the other credentials from Istota's live namespace.

The default write budget is three requests per task attempt, including refusals. Set `[security] vault_writes_per_task = 0` to disable model-requested writes. Istota sends a notice for each credential it creates. A password manager that already has the database open may overwrite a new entry on its next save; check for the entry in the password manager after a task creates it.

What it holds is **shared credentials**: a flat namespace of name-to-value pairs the user chooses, stored under the `vault_entries` service and readable by that user's own tasks by name. It does not provision the typed services above — those are edited in the settings page and nothing here overwrites them.

### Turning it on

Two halves, both deliberate, and neither happens by accident.

**Put the file in the folder.** Every user gets a `vault` folder inside their own bot directory, made for them whether or not they use it — `istota/vault/` on a default deployment, since that directory is named from `bot_name` lowercased. Copy a `.kdbx` there. Settings, Connected services lists what it found: one file is read on sight, and with several there the card asks which. What is stored is that **filename**, not a path, so there is nothing to spell wrongly and nowhere else for it to point. Deleting or renaming the chosen file brings the question back rather than leaving Istota reading nothing.

**Generate the passphrase**, from the same card or from a host shell. A file with no passphrase behind it is not a vault and nothing reads it, which is why the card says "Not set up" until both halves are there. Turning a vault off is removing either one.

An operator can name the file instead, per user in `config.toml`, when they want to decide it or want the absolute form:

```toml
[users.alice]
vault_path = "istota/vault/credentials.kdbx"
```

A relative `vault_path` resolves under that user's own workspace directory, which is where a phone or a laptop can reach it. An absolute one is a host path and must resolve outside every tree a task sandbox can write; that form keeps the file away from a task entirely, at the cost of the user no longer being able to edit it from a phone. Empty — the default — means the folder decides.

**A path is operator-only, deliberately** — and the absolute form is the reason. It is checked against the list of trees a sandbox binds read-write, which is the right question for a path an operator wrote and is not a line a user may put themselves on the far side of: an absolute path chosen by a user would read any file the daemon can read, as the daemon, and decrypt the result into that user's own credential rows. The card offers no path at all, which is what makes the question unaskable there rather than refused.

**A `vault_path` outranks the folder**, and that is about precedence rather than permission. The card says so instead of offering a choice the line would override. `istota user ensure --clear-vault-config --user alice` forgets a filename a user chose, which returns them to the folder's own rules: the only file there if there is one, a question if there are several.

Install the `vault` extra on the host. `pykeepass` and its six dependencies are optional because two of them carry compiled extensions, and a deployment with no vault should not pay for them. Both shipped deployment shapes install it already: the Ansible role runs `uv sync --extra all`, and the Docker image bundles it.

Neither shape lets you hand-edit that `config.toml`, because both rewrite it — the role's template task on every converge, the entrypoint on every boot. Write the key where the generator reads it.

**Ansible**, per user in `istota_users`:

```yaml
istota_users:
  alice:
    vault_path: "istota/vault/credentials.kdbx"
```

A user who declares no path gets no `[users.<id>]` block at all, which is the unchanged default for everybody else and is what the folder route needs. `istota_scheduler_vault_sync_interval` sets the cadence.

**Docker**, in `docker/.env` — single-user, so the key is unprefixed:

```
USER_VAULT_PATH=istota/vault/credentials.kdbx
```

with `ISTOTA_SCHEDULER_VAULT_SYNC_INTERVAL` for the cadence.

### The passphrase

The passphrase is a per-user secret like any other, stored in the `secrets` table under the `vault` service. It is set once, either from the vault card in Settings, Connected services — **Generate a new passphrase** — or from a host shell:

```bash
istota secret ensure -u alice --service vault --key passphrase --generate
```

**It must be generated rather than chosen.** The vault file sits in a tree bound read-write into that user's own task sandbox, so a prompt-injected task can read the ciphertext of the whole file and carry it out. Argon2id makes that useless against 256 random bits. It does not make it useless against a memorable phrase.

Read that as bounding the *offline* case, and be exact about what it does not bound. It protects everything in the file that is not shared — the rest of the user's password database, if they pointed at a copy of it — and it protects a copy of the file carried out of the sandbox. It does not protect the shared credentials themselves from a task, because a task can ask for those by name; see [what a task can read](#what-a-task-can-read).

On Ansible you can provision it from inventory instead, alongside the other per-user secrets:

```yaml
istota_user_secrets:
  alice:
    - { service: vault, key: passphrase, value: "{{ vault_alice_kdbx_pass }}" }
```

Generate the value yourself (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`) and keep it in Ansible Vault. Under 32 characters is refused. Re-running the play rewrites the same value, which is a no-op.

`--generate` mints the value, stores it and prints it once. Copy it into the password manager you keep everything else in, and use it as the master password when you create the KDBX file. A *supplied* `--value` shorter than 32 characters is refused rather than warned about, because a warning is read after provisioning and by then the file is already encrypted under it.

`--generate` refuses to replace a passphrase that is already there unless you pass `--force`. Minting a second one destroys the only copy the server has of the value the file is encrypted under, and the command otherwise advertises itself as idempotent — so an Ansible play re-running the documented provisioning line is the ordinary case rather than a careless one.

The web form takes a typed passphrase too, at the same 32-character floor, and puts Generate first for the reason above. A generated value is shown once, in the response that mints it, because nothing reads it back — there is no route that could, and you need it to open your own KDBX. Copy it before you navigate away.

### The file

Put what you want to share under a top-level group named `istota`. Everything under it is read; nothing outside it is. The group name is matched case-insensitively and with surrounding whitespace stripped, so `Istota`, `ISTOTA` and `istota ` all work.

**A file with no such group is read in full.** That is the right answer for a file you made for Istota and put in its folder, and the wrong one for a copy of your everyday password database — so the card, `istota secret vault-status` and a one-off notification all say when a read was unscoped and how many names it produced.

Each entry contributes one name per field it has filled:

| Field | Name |
|---|---|
| Password | the entry's own name |
| Username | that name with `_username` |
| URL | that name with `_url` |
| Any custom string field | that name with `_<field>` |

The name itself is the group path below `istota`, plus the entry title, lowercased and joined with underscores. So an entry titled `GitHub PAT` at the top level is `github_pat`, and an entry titled `Token` in a group `Home Assistant` is `home_assistant_token`. Notes are not read: they are free text and frequently hold something other than a credential.

```
istota/
  GitHub PAT            (password)          -> github_pat
  Home Assistant/
    Token               (password)          -> home_assistant_token
    URL                 (password + URL)    -> home_assistant_url
                                            -> home_assistant_url_url
```

That last pair is the rule being literal rather than clever: an entry titled `URL` whose KeePass URL field is also filled contributes both.

A name must start with a letter and be at most 64 characters after slugging. Values are stripped of surrounding whitespace and nothing else is normalized. What is skipped, each with a warning naming the entry and never the value: an empty field, a value over 8 KiB, a title that slugs to nothing or to a name already produced by another entry. The walk stops at 8 levels deep, 512 entries or 1024 names, warns, and applies what it read — half a namespace is a user with some credentials working, where a refusal is a user with none. Entries in the recycle bin are not read.

### What a sync does

The daemon reads the file at start-up and every `scheduler.vault_sync_interval` seconds (300 by default; 0 turns both off). Each cycle hashes the file bytes, and stops there when nothing has changed — no unlock, no database write, no log line. `istota secret vault-sync [-u alice]` runs one by hand and ignores the cached hash.

The file is the authority for the whole `vault_entries` namespace and for nothing else:

- Every name the file produces is written over whatever the table had.
- **Every `vault_entries` row the file does not produce is deleted.** Deleting a credential through the vault means deleting the entry, and emptying the `istota` group revokes the whole namespace at once.
- No other service is touched. A credential typed into the settings page is not something the vault can overwrite or delete, whatever it is called.

A row whose stored value will not decrypt is held back from deletion rather than removed, and counted. `istota secret vault-sync` prints the count and names each deleted key, which is where a surprise gets noticed.

### What a task can read

Every name in the namespace is fetchable by that user's own tasks, and this is the one place the vault widens what a task can reach. A prompt-injected task can enumerate the namespace and read all of it.

Three things bound that, and the first is the real one:

- **The file is the consent boundary.** A credential is reachable because the user put it under `istota/` — or, on an unscoped file, because they pointed at that file. That is the same decision as typing the credential into the settings page, made in a different editor.
- **A skill takes a name rather than a value.** `istota-skill browse interact --fill-credential` sends the value from the daemon to the browser container without it crossing into the sandbox at all. Where a skill covers the job, that is the path.
- **Values stay out of the transcript unless the model puts them there.** Nothing is in the task's environment and the prompt carries no names and no values. `istota-credential run VAR=name -- <command>` hands the value to one child process and prints none of it.

A task holding the socket can still read a value deliberately (`istota-credential get <name>`), and from there it is in the session transcript and in whatever the task then says. So this is a boundary against accident rather than against intent, and `[security] vault_fetch_limit_per_task` (10 by default, `0` unlimited) bounds how many fetches one task attempt may make. The kill switch is that there is no vault: no file in the folder, or no passphrase, which is where every user starts.

### What the settings UI does

The "Connected services" heading carries a status line for a user who has a vault: where the file is read from, how many shared credentials Istota holds and what they are called, how many were created under `generated/`, when it was last applied, and the error class when it is failing. It also says when the last read was unscoped.

Under it is the card that sets the vault up: the folder to put the file in, the files found there, and the passphrase. It renders for a user who has no vault at all, which is who it is for. What it does not offer is a path of any kind — see [turning it on](#turning-it-on) — and the file half is withheld for a vault a `vault_path` already names, which it says instead. The passphrase half renders either way: it is a credential the user owns rather than a setting an operator made.

The credential-name list is the feedback this feature exists to give. A name in it is a credential Istota holds; a name you expected and cannot see is a group you misspelled or an entry with a warning in the log. Names only — no value reaches that page — and only that user's own.

**"Last applied" is not a health check, and a healthy vault shows an old stamp.** The record is written only by a cycle that did work, and a cycle over an unchanged file does none — so a vault nobody has edited for three weeks reports a three-week-old timestamp and is working perfectly.

### When it fails

Each of these leaves the credentials in the table alone and raises a notification on the user's connected-services panel, once per transition rather than once per cycle:

| What happened | What to do |
|---|---|
| The stored passphrase does not open the file | Re-provision it, then run `istota secret vault-sync` |
| The file is not a readable KeePass database | Also what a sync caught mid-write looks like — check the mount before suspecting the file |
| There is no vault file to read | Check the `vault` folder in your own files, and that the file has synced to the server; or check `vault_path` where an operator set one |
| The `vault` extra is not installed | An operator remedy, not a user one |
| No passphrase provisioned | `istota secret ensure … --generate` |
| `ISTOTA_SECRET_KEY` cannot read the stored passphrase | A deployment problem; see `security.secret_key` in `istota doctor` |
| The file was refused unread — not a regular file, or over the 8 MiB cap | Check what is actually at the path; a symlink and a FIFO are both refused |
| `vault_path` is one the daemon may not open | An operator corrects the line in `config.toml` |

Two more states the card reports and nothing notifies about, because neither is a failure and both are one step from being answered: the folder holds no `.kdbx` at all, and it holds several with none chosen. The second is a dropdown away.

`istota secret vault-status -u alice` prints the whole answer for one user: the resolved path, whether a passphrase is provisioned, whether the read was scoped, the names the file produces and the reason beside each one it skipped.

`istota doctor` answers across every configured user instead, in two checks. `security.credential_vault` covers the cheap questions — the extra, the schedule, each path, each passphrase — and runs wherever doctor runs. `security.vault_contents` is the one that opens each file and reports counts rather than names; opening a vault costs about a second per user, so it is excluded from the hourly sweep and from the `self-check` heartbeat, and answers at boot, from `istota doctor`, from `!check`, and on the admin Health pane. Counts rather than names there, deliberately, because a `CheckResult` is read by every admin where the settings card is read only by its own user.

**Rotate the vault's master password in the quiet order**: provision the new passphrase with `istota secret ensure` first, then change it in KeePassXC. The other order raises a notification in between, because the rewritten file no longer opens with the stored value.

### Two things that are not evidence

Both are honest and both mislead if read the other way:

- **A sync bumps `last_accessed_at` on every credential it writes.** Three other things bump it on the `vault/passphrase` row alone, since each has to resolve the passphrase to open the file: `istota secret vault-status`, `istota doctor`'s `security.vault_contents`, and the same check reached through `!check` or the admin Health pane. So that column stops being evidence that a shared credential is read by anything, and on the passphrase row it records a diagnostic as readily as a sync.
- **A sync runs whenever the file's bytes change, not whenever a credential changes.** KDBX draws a fresh master seed on every save, so saving a database nobody edited produces different bytes and a full apply. That is harmless — every write is idempotent — and it is the one thing that eventually re-asserts the file's version of a value somebody changed in the table directly.

### What it does not fix

The secrets table still holds a copy of everything. What the vault removes is the second place a user has to *edit*.

The security accounting is that the vault adds no confidentiality and two new exposures. Every credential in it is also a row in the table, and the passphrase that opens it is another row in that same table, so one secret — `ISTOTA_SECRET_KEY` — opens both.

The first exposure is *where* credential ciphertext sits: none of it used to be reachable from inside a sandbox, and now a copy of the whole file is, where a task can read it, copy it out, delete it or overwrite it. Deleting or corrupting it is a denial of service that leaves every credential working and raises a notification. Replacing it with an older copy the user keeps in the same tree is a real rollback vector, bounded by what the file holds — which, under an unscoped read, is everything in it.

The second is that a task can ask for any shared credential by name, which is [above](#what-a-task-can-read) and is a widening rather than a side effect.

The generated passphrase mitigates the first and not the second. The absolute `vault_path` form removes the first completely by putting the file outside every tree a sandbox can reach, at the cost of the phone; it is not the default because editing from a phone is the feature, and it is the reason the card offers no path of any kind — the card's own users are inside the tree the absolute form exists to escape, so handing them a path field would be handing them an arbitrary read as the daemon.

The folder is inside that tree too, and the consequences are bounded rather than absent. A task can add a `.kdbx` there, which moves a one-file folder to "several, none chosen" until the user picks — a denial of service, and a visible one on the card. It cannot plant a vault that is *read*: the passphrase is a row in a table no sandbox binds, so a planted file fails to open and applies nothing.

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
- `ISTOTA_SECRET_KEY` — routed to module-skill subprocesses that need to decrypt per-user secrets, but blocked at the lookup endpoint via `_PROXY_LOOKUP_BLOCKED` so `istota-credential env ISTOTA_SECRET_KEY` from inside Claude is rejected

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
