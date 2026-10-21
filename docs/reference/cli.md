# CLI reference

Istota provides three console scripts: `istota` (main CLI), `istota-scheduler` (daemon), and `istota-skill` (skill proxy client).

## istota

### Settings plans and snapshots

```bash
istota -c CONFIG snapshot [-o FILE] [--format toml|json|ansible] [--user USER] [--no-meta]
istota -c CONFIG snapshot --patch 'SOURCE=DEST' -o settings.toml
istota -c CONFIG apply -f PLAN --dry-run [--format json]
istota -c CONFIG apply -f PLAN [--user USER] [--prune] [--secrets-file PRIVATE_FILE]
istota -c CONFIG apply -f PLAN --prune --prune-secrets
istota apply -f PLAN --check --json
```

`--user` and `--patch` are repeatable on both commands. Snapshot defaults to TOML on stdout; file output is atomic and private. It exports settings without credential values or runtime history and refuses stored briefing definitions with embedded credentials. Diagnostics go to stderr. `--no-meta` removes the ownership-preserving snapshot metadata. `--format ansible` writes the JSON under an `istota_plan` key, a `group_vars` file for the [management role](../deployment/ansible.md).

Apply exits 0 unchanged, 2 changed or would change, and 1 refused or failed. `--check` uses its own 0 valid / 1 invalid contract and `--json` envelope. `--continue-on-error` permits later independent users after a runtime failure. `--backup N` retains configuration backups. See [declarative settings](../configuration/declarative-settings.md) for defaults, pruning, credentials, path patches and partial failure behavior.

### Task execution

```bash
istota task "prompt" -u USER -x              # Execute a task
istota task "prompt" -u USER -x --dry-run    # Show prompt without running
istota task "prompt" -u USER -t ROOM -x      # With conversation context
istota task "prompt" -u USER -x --source-type talk   # Fake a source type
istota task "prompt" -u USER -x --no-context         # Skip conversation context
```

### Task management

```bash
istota list [-s STATUS] [-u USER] [-n N]     # List tasks (default limit 20)
istota show <task-id>                        # Task details
istota run [--once] [--briefings] [--dry-run]  # Process pending tasks
istota job run <user> <name> [--briefing]    # Queue a scheduled job (or briefing) now
```

`istota job run` queues the job as it is defined now and leaves its schedule alone; the daemon runs it. It exits 1 while another run of that job is queued or running, within five minutes of the last run, and for a `_module.*` job.

### Usage and cost

```bash
istota usage                                 # Last 30 days, one total
istota usage --days 7                        # Window size in days
istota usage --since 2026-08-01 --until 2026-08-14   # Explicit window (--until inclusive)
istota usage --by day|user|model|source|brain|origin  # Group results
istota usage -u USER                         # Filter by user
istota usage --brain native                  # Filter by brain kind
istota usage --source talk                   # Filter by task source type
istota usage --model MODEL                   # Filter by model
istota usage --origin sleep_cycle            # Filter by where the call came from
istota usage --json                          # Machine-readable
```

Operator-facing: it runs from the operator's shell, so `--user` is a convenience filter rather than a boundary. Filters combine with any grouping.

A cost column shows the group's real charged cost and nothing else. A group whose rows are all plan-equivalents or catalog estimates renders as a bare `—`, and a mixed group shows the real figure alone — the other bases are never summed into it and never named beside it. `--json` still carries the whole `cost_by_basis` map. See [token usage and cost](../features/usage.md) for what each basis means and for `--origin`'s values.

### User management

```bash
istota user list                             # List configured users
istota user lookup --email ADDR               # Find user by email
istota user init USER                        # Initialize user workspace
istota user status USER                      # User status and resources
istota user show --name USER_ID              # Dump the stored profile row as JSON
istota user remove --name USER_ID            # Delete a user_profiles row, with the user's avatars, WhatsApp binding, device tokens, quick note keys, location ingest tokens and managed-field record
istota user ensure --name USER_ID [--display-name NAME] [--tz TZ] [--email ADDR ...] [--sms-number E164 | --clear-sms-number] [--whatsapp-number E164 | --clear-whatsapp | --reset-whatsapp-identity] [--whatsapp-bsuid BSUID] [--max-foreground-workers N] [--max-background-workers N] [--log-channel TOKEN] [--alerts-channel TOKEN] [--default-destination DESCRIPTOR] [--route PURPOSE=DESCRIPTOR ...] [--disabled-skill NAME ...] [--disabled-module NAME ...] [--trusted-sender PATTERN ...] [--quiet-sender PATTERN ...] [--outbound-approval off|untrusted|all|""] [--external-turn-display full|collapsed|hidden] [--briefing-email-html | --no-briefing-email-html] [--timezone-follow-location | --no-timezone-follow-location]
```

`istota user ensure` has no `-u`/`--user` flag — the user id comes from `--name` (required). `--tz` and `--timezone` are aliases. `--email` takes a bare address and is repeatable (each pass replaces the stored list). Worker caps are `--max-foreground-workers` / `--max-background-workers`.

`--sms-number` binds one exact E.164 number to the user; `--clear-sms-number` removes it. The number gains authority to create tasks and answer the user's pending SMS confirmations, so assignment and number recycling are operator-controlled. See [SMS](../features/sms.md).

`--whatsapp-number` binds a bootstrap E.164 number for the WhatsApp surface, used for the first binding and as an outbound fallback; `--whatsapp-bsuid` enrolls the business-scoped user id explicitly, which is the only route for a WhatsApp user with a username and no reachable number. The two may be given together. `--reset-whatsapp-identity` clears the learned identity and keeps the number, `--clear-whatsapp` removes the binding. Changing the number discards the 24-hour service window along with everything else learned about the previous holder. See [WhatsApp](../features/whatsapp.md).


The former `--default-briefings` flags are retired and return a migration diagnostic. Manage All users delivery mutes in Briefings; use `istota briefings migrate` for existing defaults.

`--default-destination` sets the fallback delivery surface (`talk` | `email` | `ntfy` | `web` | `surface:channel` | comma list). `--route` is repeatable and sets a purpose-keyed override; `PURPOSE` is one of `reply`, `alert`, `log`, `briefing`, `notification`. See [per-user delivery routing](../configuration/per-user.md#delivery-routing).

`--outbound-approval` sets this user's outbound email approval policy (`off` | `untrusted` | `all`); pass `""` to clear it and follow the operator's `[email] outbound_approval_floor`, which is the default. The floor is a minimum — a user value weaker than it has no effect. This is the supported way to set the policy for an existing user: the `[users.X] outbound_approval` TOML key seeds only a user with no profile row yet. See [the outbound approval gate](../features/email.md#the-outbound-approval-gate).

`--external-turn-display` controls how much of a turn that arrived from outside the room (an external contact's email) is shown inline in web chat: `full`, `collapsed` (default — sender and subject, expandable), or `hidden`. The turn itself always renders at every setting.

`--quiet-sender` is the counterpart to `--trusted-sender`: mail matching the pattern is filed without creating a task. `--briefing-email-html` selects HTML rather than plain-text briefing email. `--timezone-follow-location` opts into having the stored timezone updated when the location module sees you settle in a new one (off by default; see [location](../features/location.md)).

### Web login identities

```bash
istota auth list                             # Profiles and email identities, flags, last login
istota auth add USER --email ADDR [--create-user] [--send-invite | --print-link | --password-stdin]
istota auth set-password USER [--password-stdin]   # Prompts privately; no password argument
istota auth invite USER [--send | --print-link]    # Enrolment link (default: send)
istota auth reset USER [--send | --print-link]     # Password-reset link
istota auth sign-in-code USER                # Fresh code for the user's pending email sign-in
istota auth disable USER | enable USER       # Change login availability, revoke sessions
istota auth logout-all USER                  # Revoke every session for the identity
istota auth remove USER                      # Remove the login identity; keep profile and data
istota web login-code [--user USER]          # One-use code for the Mac app's local login (local_code installs)
```

Email login for the web UI, enabled by `[web] auth`. A sign-in code works only in the browser that asked for it. See [email login](../features/web-interface.md#email-login).

### Resources

```bash
istota resource ensure -u USER -t folder -p PATH [--name NAME] [--permissions read|readwrite] [--extras k=v | --extras-json '{…}'] [--extras-clear]
istota resource add    -u USER -t folder -p PATH         # one-shot add (fails if duplicate)
istota resource list   -u USER                          # List resources
```

Only `folder` is declarable (an out-of-workspace sandbox mount) after the
Resources sunset. The retired types (`calendar`, `notes_folder`,
`email_folder`, `feeds`, `money`, `monarch`, `moneyman`, `karakeep`,
`overland`, `ledger`, `invoicing`) are auto-cleaned at scheduler startup —
feeds/money/location are modules, karakeep/monarch/overland/tumblr are
connected services in the encrypted `secrets` table, and calendars are
CalDAV-discovered. `todo_file`/`reminders_file` are **not** auto-cleaned:
they survive as deprecated explicit-path overrides read by the legacy
briefing fetcher (todo/reminders/notes otherwise take an explicit
briefing-source `path`, with no convention-default filename).

### Briefings

`istota briefings` is the unified tree. Group definitions live in the framework DB and use `group list --group-id ID --actor USER` or `group save --group-id ID --actor USER --file definition.json --revision N`. Revision 0 creates a disabled draft; later saves require the current revision. The actor must be a current owner, or an admin for All users. See [group migration commands](../deployment/migrating-group-briefings.md) for `migrate report|import|review|cutover|rollback` and their required flags.

For personal briefings, schedule and delivery are framework-owned (`briefing_configs`); content (blocks and their sources) lives in the per-user briefings module DB.

```bash
# Every briefing name this user actually has, with block counts and the
# latest archived generation. The set is the effective one -- config defaults
# and disabled rows applied, plus block-backed names with no schedule.
istota briefings list -u USER

# Schedule + delivery
istota briefings schedule ensure -u USER --name NAME --cron CRON [--title TITLE] [--conversation-token TOKEN] [--output talk|email|ntfy|both] [--disabled]
istota briefings schedule list   -u USER
istota briefings schedule delete -u USER --name NAME

# Content blocks
istota briefings blocks list    -u USER [--briefing NAME]
istota briefings blocks add     -u USER --briefing NAME --title TITLE [--directive TEXT] [--render-mode synthesis|structured] [--options '{…}']
istota briefings blocks set     -u USER --id BLOCK_ID [--title …] [--directive …] [--render-mode …] [--options '{…}']
istota briefings blocks reorder -u USER --briefing NAME --ids 3,1,2
istota briefings blocks remove  -u USER --id BLOCK_ID

# A block's sources
istota briefings sources list   -u USER --block BLOCK_ID
istota briefings sources add    -u USER --block BLOCK_ID --kind rss|email|browse|markets|calendar|todos|reminders|notes|shared_block --config '{…}'
istota briefings sources remove -u USER --id SOURCE_ID

# Module-owned shared blocks (global, admin)
istota briefings shared list
istota briefings shared ensure --name NAME --cron CRON [--title …] [--directive …] [--render-mode …] [--trusted] [--disabled] [--source-json '{"kind":"markets","config":{}}']
istota briefings shared run    --name NAME
istota briefings shared remove --name NAME

# Archive
istota briefings archive list -u USER [--briefing NAME] [--limit N]
istota briefings archive show -u USER --id ARCHIVE_ID
```

`--source-json` is repeatable. `ensure` / `list` / `delete` are positional actions. There are no `-n`/`-c` short flags — use `--name` and `--cron` (`-c` is the global `--config`).

`istota briefing` (singular) still works as a deprecated shim for `istota briefings schedule` and prints a deprecation notice. Its `--component` / `--components-json` flags are gone — the boolean-component content model is retired, and content is authored as blocks (CLI above, a settings plan's `[[users.X.briefings.blocks]]`, or the web block editor).

### Secrets (encrypted store)

```bash
istota secret ensure -u USER --service SERVICE --key KEY --value VALUE   # or --value-stdin
istota secret list   -u USER                                             # service/key/last_accessed; values never printed
istota secret remove -u USER --service SERVICE --key KEY

istota secret export --user USER --out PATH
istota secret import --user USER PATH [--include-changed] [--keyfile PATH]
istota secret vault-new    -u USER --slug SLUG [--username USER] [--url URL] [--length N] [--no-symbols]
```

Only `-u`/`--user` has a short form. `--service`, `--key`, and `--value` are long-only (`-v` is the global verbose flag).

`--value-stdin` reads the value from stdin instead (the whole input, less one trailing line ending), so it is not on the command line, where other processes on the host can read it. The [Ansible management role](../deployment/ansible.md) passes every user secret this way.


Export requires a terminal and prints a generated password once. Import prompts privately for the source passphrase, prints a preview, and imports new entries; `--include-changed` also replaces changed entries. Both use the same credential modules as Settings.

`vault-new` creates a generated credential in the encrypted table and prints its names, never its password. It requires a configured user and a unique slug; `--length` and `--no-symbols` set password requirements. It does not write a KeePass file.

### Ensure-CLI state contract

All four `* ensure` subcommands (`user`, `resource`, `briefing`, `secret`) share a uniform contract: each computes `created` / `updated` / `noop` honestly by comparing the requested fields against the existing row, writes only when state would change, and prints a final `STATE: created|updated|noop` line. A config manager can use it for change reporting (Ansible: `changed_when: "'STATE: noop' not in stdout"`).

Subsystem helpers that own the contract: `db.upsert_user_resource`, `credentials.store.upsert_secret`, `user_profiles.update_profile_with_status`, and `db.upsert_briefing_config` (via the existing briefing helper). Each returns `(thing, state)` (or just the state string) so the CLI is a thin printer.

`user ensure` writes one table those helpers do not cover: the WhatsApp binding is its own row, so the command compares it before and after and escalates a `noop` to `updated` when it moved. A WhatsApp-only change has to report `updated`, or a play keyed on `STATE:` suppresses the restarts and both processes carry on with a user set snapshotted before the enrollment.

### Email

```bash
istota email list                            # List recent emails
istota email poll                            # Poll for new emails
istota email test                            # Test email configuration
```

### WhatsApp

```bash
istota whatsapp pair                         # Baileys: link the number by scanning a QR code
istota whatsapp pair --reset                 # Baileys: move an unusable session aside first
istota whatsapp restore-session --list      # Baileys: list archived sessions, change nothing
istota whatsapp restore-session [--date TS] # Baileys: put an archived session back (daemon stopped)
istota whatsapp billing-status               # Cloud: read the billable circuit breaker
istota whatsapp billing-unblock              # Cloud: clear it, after checking Meta billing
```

`pair` belongs to the `baileys` adapter. It draws the QR code in the terminal, redrawing it each time WhatsApp rotates it, and returns when the session is ready. Recovering a session WhatsApp has unlinked takes `--reset`: the credential left on disk reads as a registered account, so the sidecar keeps trying to log in with it and no code is ever offered. The flag moves that directory to a timestamped sibling and pairs into a fresh one, deleting nothing. Both forms refuse to run while a bridge is listening on the socket, so stop the scheduler and any sidecar running as a unit of its own first. The two billing verbs belong to `whatsapp_cloud` and report nothing under Baileys, which has no per-message charge.

`billing-status` reads and prints; `billing-unblock` destroys the evidence row along with the block, which is why the two are separate. Both print the Meta message id in full — this is the private operator surface. See [WhatsApp](../features/whatsapp.md).

### Calendar

```bash
istota calendar discover                     # Discover CalDAV calendars
istota calendar test                         # Test calendar access
```

### TASKS.md

```bash
istota tasks-file poll [-u USER]             # Poll TASKS.md files
istota tasks-file status [-u USER]           # Show file task status
```

### Key-value store

Every `kv` subcommand takes a required `-u`/`--user`; the ones that address a single namespace also take it as a positional:

```bash
istota kv get NAMESPACE KEY -u USER          # Get value
istota kv set NAMESPACE KEY VALUE -u USER    # Set value (JSON)
istota kv set NAMESPACE KEY -u USER --value-file PATH   # Set from a file
istota kv list NAMESPACE -u USER             # List entries in a namespace
istota kv list NAMESPACE -u USER --keys-only # Keys and value sizes, no values
istota kv list NAMESPACE -u USER --max-value-chars 500   # Preview long values
istota kv delete NAMESPACE KEY -u USER       # Delete key
istota kv namespaces -u USER                 # List namespaces
istota kv shared-status -u USER              # Report whether this user may write shared KV
```

Add `--shared` to `get`/`set`/`list`/`delete`/`namespaces` to operate on the cross-user `shared_kv` store instead of the user's own. Reads are open to everyone; writes are admin-only and fail closed, which is what `shared-status` reports on. See [shared curated content](../features/briefings.md#shared-curated-content).

The skill proxy client also exposes set operations for membership-tracking patterns (seen IDs, processed hashes). These operate on a JSON-array value and avoid round-tripping large blobs:

```bash
istota-skill kv set-contains <ns> <key> <member> [<member>...]    # Check membership, batched
istota-skill kv set-size     <ns> <key>                       # Count members
istota-skill kv set-members  <ns> <key> [--limit N] [--offset N]  # Paginated slice
istota-skill kv set-add      <ns> <key> <member> [<member>...]    # Add members (deferred)
istota-skill kv set-remove   <ns> <key> <member> [<member>...]    # Remove members (deferred)
istota-skill kv set-trim     <ns> <key> --keep-newest N           # Cap the collection (deferred)
```

`set-contains` returns `{"contains": bool}` for one member and a per-member map for several, with a `batched` flag saying which; a run checking many items against a stored set costs one call rather than one each.

A value passed as a command argument is capped at 128 KiB by the kernel, not by the store — `execve` refuses a longer argument, so an oversized `kv set` fails before any code runs. Use `--value-file` for a large whole value, and the set operations above for a collection that grows.

The two `--value-file` flags differ in scope, because the two commands run as different principals. `istota kv set --value-file` reads any path you can read: it runs in your shell, as you. `istota-skill kv set --value-file` runs host-side on behalf of a task, so its path must resolve under that task's deferred directory, its user's own workspace, or the conversation's channel directory — the same subtrees the sandbox binds. Outside a task neither of those roots exists, so the skill form refuses every path; use the operator form there.

The skill form also takes `--group <id>` on every verb, set operations included, to reach a [group's](../features/groups.md) store. Values read that way come back fenced as untrusted content, and every refusal reads `not a member of group '<id>'`.

### Groups

The operator CLI and Groups web pages share the group management service. CLI writes use explicit operator authority and record `operator` as actor; refusals exit 1 with the reason on stderr.

```bash
istota group create ID --owner USER [--owner USER ...] [--kind LABEL] [--name "Display name"]
istota group update ID --revision N [--name "Display name"] [--kind LABEL]
istota group ensure-storage ID              # Retry directory seeding after restoring storage
istota group list [--all]                    # Include archived groups with --all
istota group show ID                         # Revision, metadata, members and membership history
istota group add-member ID USER --revision N [--role owner|member] [--acknowledge-history] [--replacement-owner USER]
istota group remove-member ID USER --revision N [--replacement-owner USER]
istota group archive ID --revision N --confirm
istota group kv-get ID NAMESPACE KEY         # Read the group's KV store, with written_by
istota group kv-list ID NAMESPACE            # List a namespace, e.g. _memory_audit
```

Creation requires at least one existing enabled owner. Use the current revision from `show` for mutations; adding a new member requires `--acknowledge-history`. Changing an existing member's role does not create a new membership period. The last owner cannot leave other members without a replacement appointed in the same operation. `--kind` is a display label; owners can manage the group through the web.

An id is lowercase letters, digits, `.`, `_` and `-`, 2 to 64 characters, starting with a letter or digit; `none` is reserved. There is no `delete`: membership is a history and an archived group's id is not reused. A failed storage seed does not undo creation; restore storage and run `ensure-storage`. See [groups](../features/groups.md).

### Web chat maintenance

```bash
istota chat backfill-history [-t TOKEN]      # Recover dormant rooms' transcripts from the Talk message cache
```

Without `-t`, it walks every Talk-origin room. Use it after binding existing Talk rooms into web chat, so their history is visible on the web surface rather than starting from the next message.

### Devboxes

```bash
istota devbox compose-file > compose.devbox.yml   # One devbox service per [devbox] users entry
```

Prints the stack's second compose file: a `devbox-<user>` service per user and the `daemon` service's mounts of each user's exec and credential socket volumes. It creates each user's repos directory first, and exits 1 naming the key when a socket directory or `repos_dir` is outside `/data`. See [Devboxes](../deployment/running-the-stack.md#devboxes).

### Workspace

```bash
istota workspace localize --from /mnt/shared --to /data/workspace --scheduler-stopped \
    [--resume] [--dry-run] [--check] [--report PATH]                                    # Copy the mounted workspace
istota workspace localize-config [--workspace PATH] [--check]                           # Point config.toml at /data/workspace
istota workspace moved-note --from /mnt/shared [--remove]                               # MOVED.md in each user's Nextcloud bot folder
istota workspace reindex --forget-root /mnt/shared                                      # Rebuild the memory index on the new paths
```

The four steps of `istota-stack localize-workspace`, which runs them in order with the stack stopped. `localize` exits 0 copied, 1 refused, 2 copied with entries skipped, 3 stopped on an I/O error; `--check` only answers whether it would start, and `--resume` continues an earlier copy, keeping a destination file of the same size and modification time. `localize-config` keeps the old file as `config.toml.pre-localize`. `moved-note` refuses while the config still names `--from` as the workspace, and `--remove` takes the notes off again when going back; either exits 2 when a user's own `MOVED.md` was left in place. See [Moving to a local workspace](../deployment/moving-to-a-local-workspace.md).

### Nextcloud

```bash
istota nextcloud capabilities                # Curated summary of what the server supports
istota nextcloud capabilities --raw          # Full /cloud/capabilities payload
istota nextcloud capabilities --check talk,sharing.public   # Exits non-zero if any is missing

istota nextcloud provision-rooms --user alice        # Ensure general/logs/alerts, seed the channel tokens
istota nextcloud provision-rooms --user alice --room general   # Only the named room(s); repeatable
istota nextcloud provision-rooms --user alice --no-seed        # Create the rooms, write nothing to the profile
istota nextcloud provision-rooms --user alice --reseed         # Re-point log_channel/alerts_channel at these rooms
istota nextcloud provision-rooms --user alice --json           # Machine-readable
istota nextcloud provision-rooms --user alice --adopt general=abc123  # Record an existing room and exit
```

The `--check` form is the deployment fit-check — usable in a shell or a heartbeat `shell-command`. See [Nextcloud](../features/nextcloud.md) for the feature names and for the full `istota-skill nextcloud` surface.

`provision-rooms` creates a user's default Talk rooms; the stack's entrypoint runs it for the first admin, and you run it for anyone else. It is idempotent and prints `STATE: created|updated|noop`. A channel room whose profile column is already set is left alone entirely, and a token is written only for a room the run actually made usable — so it can neither overwrite a pinned value nor re-enable an execution log you turned off. `--reseed` is the deliberate re-point, and overrides both rules.

Each room's token is remembered per user, so renaming a room does not make the next run create a second one under the old name (ISSUE-342). A run prefers the remembered token and only matches on the room's name when there is nothing remembered for it — the first provision, or a room whose conversation has since been deleted in Nextcloud.

A remembered room the user is no longer in is left alone, and the invite is retried only where this tool's own last invite for that room is recorded as having failed (ISSUE-408). A bot-only room does not settle that on its own: it is equally what a failed invite leaves behind and what a user leaves by walking out of their own `general`, so the record decides rather than the participant list. Either way the room is reported `user not a member`, so a room nobody can read is still visible in the output rather than passing as `existing`.

Records written before that outcome was stored carry no failure and are never retried. If such a room really was stranded by an invite that failed before the upgrade, drop its remembered token and the next run falls back to matching by name, which adopts the room and retries the invite:

```bash
istota kv delete _provisioned_rooms general --user alice
```

A retry that does fail still prints `invite FAILED`, and a warning on stderr names the rooms.

`--adopt NAME=TOKEN` writes that record by hand and exits without contacting Talk. It exists for an install that already carries a duplicate from before the record existed: the room you kept no longer answers to its old name, so nothing else can point the record at it. Repeatable. A wrong token costs nothing beyond a fall back to name matching on the next run.

### Bot icon

```bash
istota bot-icon set path/to/icon.png         # Store it as the deployment's bot icon
istota bot-icon clear                        # Remove it; the UI reverts to the initial chip
istota bot-icon show                         # mime, byte size, content hash, updated_at
```

The headless counterpart to the Bot icon card at the foot of `/admin`. The file is decoded, centre-cropped to a 192px square and re-encoded as WebP by the same code an upload takes, so a JPEG, PNG, WebP, GIF or HEIC all work and nothing of the original's metadata is stored.

Idempotent by the content hash of what is stored, printing `STATE: created|updated|noop`, so a config manager can call it on every run without reporting a change. Setting the same file twice does not rewrite the row.

`[web] max_avatar_kb` bounds the file, except that `0` — which switches both web upload endpoints off, the admin one included — falls back to the shipped default here: that setting is about an unauthenticated network body, and this reads a local file as the operator. So `0` is the way to leave the icon settable by a deploy and by nobody else.

A database that predates the `bot_avatar` table is a named refusal and exit 1, not a traceback — the play runs before the migrations on exactly one ordering, and `Error: … Run `istota init`` is what an operator can act on.

The icon is separate from the bot account's Nextcloud profile picture and cannot change it (the daemon holds an app password, and Nextcloud's avatar route is session-and-CSRF-guarded). Set that one in Nextcloud if you want the two to match.

### Experimental features

```bash
istota experimental list                     # List known feature flags with on/off status
```

### Money

Accounting operations are reachable as `istota money <op> …`. Operational commands are forwarded verbatim to the money engine (resolve the user with `-u USER`):

```bash
istota money list -u USER                     # list transactions in a ledger
istota money check -u USER                    # bean-check a ledger
istota money balances -u USER                 # account balances
istota money query -u USER "<bql>"            # run a BQL query
istota money report -u USER                   # financial report
istota money add-transaction -u USER ...      # append a transaction
istota money edit-transaction -u USER ...     # edit a transaction in place by id
istota money backfill-ids -u USER             # backfill stable transaction ids
istota money import-csv -u USER ...           # import transactions from CSV
istota money sync-monarch -u USER             # sync from Monarch Money (auto-matches payments to open invoices)
istota money debug-monarch -u USER            # health-check Monarch credentials
istota money run-scheduled -u USER            # periodic sync + invoice scheduler
istota money users                            # users visible to the money CLI
istota money invoice -u USER <generate|list|paid|unpaid|create|void> ...
istota money work -u USER <list|add|update|remove|migrate|backfill-ids> ...
istota money portfolio -u USER <import|snapshots|summary|history|diff|accounts|classify> ...
istota money lots -u USER                     # tax lots (experimental: money_tax)
istota money wash-sales -u USER               # wash sales (experimental: money_wash_sales)
```

`lots` and `wash-sales` are behind operator feature flags — see [experimental features](../EXPERIMENTAL.md).

Config-management subcommands manage the per-user money DB config:

```bash
istota money config  <show|import|export|diff> ...
istota money client  <add|update|remove|list> ...
istota money company <add|update|remove|list> ...
istota money service <add|update|remove|list> ...
istota money tax     <set|rates|schedule|pattern> ...
istota money monarch <profile|account-map|category-map|tag-filter> ...
istota money rules   <list|add|update|remove|test> ...
```

`tax set` takes `--state CA` (or `--state ""` for no state tax) alongside the
filing status and year.

`tax rates` carries the payroll scalars, which really are year-keyed and the
same for every filing status:

```bash
istota money tax rates set -u USER --year 2026 --ss-wage-base 184500
```

Brackets and standard deductions moved to `tax schedule`, keyed on the three
dimensions they actually have. The old `--ca-brackets-json` /
`--ca-standard-deduction` flags on `tax rates` are gone; they were
filing-status-agnostic, so an override entered while filing jointly silently
continued to apply after switching to single.

```bash
istota money tax schedule set -u USER --year 2026 --jurisdiction NY \
    --filing-status mfj --standard-deduction 16050 \
    --brackets-json '[[0, 0.04], [100000, 0.06]]'
istota money tax schedule remove -u USER --year 2026 --jurisdiction NY --filing-status mfj
istota money tax schedule list -u USER
```

`remove` reverts both fields to the bundled figures. An omitted flag on `set`
leaves that field alone; `--brackets-json null` reverts just the brackets.

`istota money tax set --state` is validated against the jurisdiction registry —
a typo'd code would otherwise store fine and resolve to nothing forever.

`rules` is the operator surface over the transaction rules every importer
resolves against — the table the three `monarch` map groups are dict views of.
`--ledger` and `--source` are required on `add` and `test`: both columns default
to `''` and the engine reads `''` as "any", so an omitted ledger would be a rule
silently applying everywhere. `remove` takes an id from `list`, and is the only
delete path — the agent-side `transaction-rules` verbs have none. `test`
resolves a transaction given on the command line and prints what the rules would
do with it without importing anything; it refuses outright where the one-time
migration from the legacy maps has not completed. Hard errors exit 2, matching
every other verb in this CLI. See [Money](../features/money.md#transaction-rules).

### Interactive REPL

```bash
istota repl [-u USER] [-t TOKEN] [--workspace cwd|standard|PATH] [--model ALIAS] [--effort LEVEL]
```

A streamed, full-stack terminal assistant. Each line becomes a `source_type="repl"` task with `output_target="stream"`, run inline (no daemon needed); `task_events` stream back to the terminal. `--workspace` selects the working directory: `cwd` (default), `standard` (the per-user temp dir the daemon sandboxes), or an explicit path. On the Claude Code brains a repository's own `CLAUDE.md` and `.claude/` settings in that directory are not loaded; ask for the file to be read if you want its conventions applied.

### Setup, serve and update

```bash
istota setup --vm-dir DIR [--hostname HOST] [--ingress local|proxied|direct] \
    [--storage local|host-folder] [--nextcloud-url URL [--workspace mount|local]] \
    [--yes] [--force] ...                       # The stack's wizard, run by `istota-stack setup`
istota setup --web [--force] --vm-dir DIR [--local]   # The same questions in a browser, run by `istota-stack setup --web`
istota setup --snapshot DIR | --restore DIR --vm-dir DIR   # A browser re-run's rollback copy, taken and put back by `istota-stack`
istota serve [--host HOST] [--port N] [--env-file PATH]   # Scheduler loop + web server in one process
```

On the stack, `istota-stack setup` runs `istota setup --vm-dir` inside the image; it writes `config.toml`, the admins file, the master key and, in the `--vm-dir`, the stack's `.env`, `host.env` and `secrets/`. `istota setup --help` lists every flag. In the image, `--workspace mount|local` says where the workspace lives when `--nextcloud-url` is given (default `mount`, the rclone mount); `--storage host-folder` is the local VM's shared folder and is kept on a re-run without the flag. A `--force` re-run starts from what the stack's files say: every question defaults to the current answer, a credential prompt says Enter keeps the current one, and `--yes` with no flags writes the same files again. It backs up `config.toml` to `config.toml.bak.<time>` first, keeping the newest ten, and refuses a config it could not write back (a `[brain] kind` other than `claude_code` or `native`, a compose profile setup does not know). `--web` serves the setup page instead of asking in the terminal, over HTTPS on a one-use code, or plain HTTP on port 8080 inside the container with `--local`. It refuses when a config already exists, unless `--force` is given, which makes it the re-run page, filled in from the current files; then it refuses when there is no config, when the config does not parse, or when the user is not one id both in the admins file and with a `[users.<id>]` block. `--snapshot DIR` copies `config.toml`, `managed.json` and the secret files aside, and `--restore DIR` puts them back and removes the copy; `istota-stack setup --web --force` runs both. Outside the image (which sets `ISTOTA_SETUP_SHAPE=container`) and without `--vm-dir`, it refuses and writes nothing: the standalone install it used to set up was removed, and a Mac takes the [local install](../getting-started/local-install.md).

`istota serve` runs the scheduler loop and web server in one process (default bind `127.0.0.1`, port from `[web]`). It is the development loop on a Mac checkout; see [development setup](../development/setup.md). There is no `istota update`: the stack is updated with `istota-stack update`.

### Database

```bash
istota init                                  # Initialize database
istota init --check-schema                   # Exit 3 when a newer release migrated it; writes nothing
istota init --relocate-rooms --scheduler-stopped   # Offline room-identity relocation; stop every writer first
```

`istota init` exits 3, writing nothing, when the database was migrated by a newer release; `istota-stack rollback` asks this before it switches. `--relocate-rooms` is the offline step described in [Running the stack](../deployment/running-the-stack.md#room-identities-offline-maintenance-required).

### Doctor

```bash
istota doctor                                # Every check against the loaded config
istota doctor --json                         # Machine-readable; statuses are lowercase
istota doctor --only PREFIX                  # Only checks whose name starts with PREFIX (repeatable)
istota doctor --scope image|deployment       # Only facts about the image, or about the install
istota doctor --deep                         # Include checks that start a sandbox namespace
```

Each result is `ok`, `warn`, `fail` or `skip`, with a remedy on everything that is not `ok`. It exits 1 on any failure and 0 otherwise.

### Config plans

```bash
istota apply -f PLAN [--dry-run] [--backup N]   # Make config.toml what the plan's [config] says
istota apply -f PLAN --check [--json]           # Validate the plan only; read no config
```

`istota apply` exits 0 when nothing changed, 2 when it changed `config.toml`, and 1 when it refused. A plan is TOML, or JSON when its name ends in `.json`; `-f -` reads it from stdin. `--dry-run` prints the diff and the exit code it would take. `--backup N` keeps the replaced file as `config.toml.bak.<UTC time>` and the newest N of those. On the stack it runs through `istota-stack apply`, which opens the config directory for it and restarts the stack on exit 2.

```bash
istota config-request take --out DIR         # Claim the oldest pending web config request
istota config-request mark --id N --status building
istota config-request finish --id N --status applied|unchanged|refused|failed --output-file PATH
```

The host half of the [web config editor](../configuration/web-editor.md). `istota-stack config-apply` runs these; you do not.

### Migrations into the stack

The first three run in a one-shot container with the stack down, started by `istota-stack import-bare-metal`, `istota-stack restore` and `istota-stack import-standalone`; you do not run them by hand.

```bash
istota migrate import-bare-metal ...                     # an old bare-metal install; see Migrating a bare-metal install
istota migrate restore-workspace [--date YYYY-MM-DD] [--force]   # the local VM's restore, master key on stdin
istota migrate import-standalone --source DIR --hostname HOST [--dry-run]   # a staged standalone install, istota.env on stdin
```

The other two you run yourself:

```bash
istota migrate workspace-paths [--map OLD=NEW] [--old-home DIR] [--dry-run]   # rewrite old host paths in users' bot directories
istota migrate role-vars --old-vars FILE -o FILE [--config FILE] [--host-env FILE] [--stack-env FILE] [--rclone-conf FILE]
```

`workspace-paths` runs on the stack through the `istota` wrapper. `role-vars` is only for an estate moving from the old bare-metal Ansible role to the optional management role: it runs from a checkout and writes the new role's variables (credentials included, 0600) from the old role's decrypted variables and the migrated VM's files.

`restore-workspace` puts back the master key, `config.toml`, the admins file, the `.env` and `host.env` copies and the databases from the workspace's `Backups/`, refusing before it writes anything when a check fails. `import-standalone` writes a stack config from the standalone one, moves every `istota.env` credential into the secrets files, restores the databases and rebases their stored paths. Both exit 0 when done, 1 when refused with nothing written, 2 when partial. See [Local install](../getting-started/local-install.md#backups-and-recovery).

## istota-scheduler

```bash
istota-scheduler                             # Start daemon
istota-scheduler -c PATH                     # Explicit config file
istota-scheduler -d                          # Run as daemon (continuous loop)
istota-scheduler -v                          # Verbose logging
istota-scheduler --max-tasks N               # Limit tasks per run
istota-scheduler --dry-run                   # Walk the loop without executing
```

## istota-skill

The skill proxy client. Connects to the Unix socket proxy when available, falls back to direct execution.

```bash
istota-skill calendar list --date 2025-01-26
istota-skill email send --to user@example.com --subject "Hello"
istota-skill markets quote AAPL
```

Used by Claude Code inside the sandbox to invoke skill CLIs with credentials injected server-side.

## istota-stack on the VM

`host/provision.sh` installs `istota-stack`, the VM's one command for the stack. Run it as root. It works on `/srv/app/istota` and runs the compose file from the deployed checkout, `/srv/app/istota/src`.

```bash
istota-stack update                          # the newest signed release (vX.Y.Z); nothing when already on it
istota-stack update TAG                      # verify a signed release tag, build it, switch to it
istota-stack update --unsigned TAG           # any tag, without the signature check, for this run
istota-stack update --unsigned latest        # the tip of main, recorded as main-<12 hex>
istota-stack update TAG --bundle FILE        # the same signed release, its tag from a bundle, no fetch from origin
istota-stack update TAG --images DIR         # the same signed release, its images loaded from a signed archive, not built
istota-stack trust-signer KEY                # print the key's fingerprint and make it the release key
istota-stack local-layers                    # build Claude Code and Chrome on a release's loaded images
istota-stack local-layers --status           # one JSON line per layer: absent, building, ready or failed
istota-stack latest-tag                      # the name `update --unsigned latest` would install; changes nothing
istota-stack rollback [--force]              # back to the previous tag, if its image can run the database
istota-stack setup [istota setup flags]      # the terminal wizard (first install, or --force to change it)
istota-stack setup --web [--local]           # the first setup in a browser: an address and a one-use code
istota-stack setup --web --force [--local]   # change a configured stack in a browser; the stack stops meanwhile
istota-stack setup-code                      # print the address and code of a setup --web in progress
istota-stack setup-cancel                    # end a setup --web in progress; a re-run puts the old setup back
istota-stack apply PLAN [--dry-run] [--backup N]   # `istota apply` a plan; restarts the stack on a change
istota-stack config-apply                    # apply what the web config editor requested
istota-stack check-image                     # exit 2 when the image lacks what config.toml needs
istota-stack put-files [--dry-run]           # write secrets/ and config/admins from JSON on stdin, as uid 10001
istota-stack up | down                       # start or stop the stack (istota-stack.service)
istota-stack exec CMD...                     # run a command in the daemon container as uid 10001
istota-stack compose ARGS...                 # docker compose with this stack's files and settings
istota-stack host-env KEY | KEY=VALUE...     # print one host.env value, or set some
istota-stack localize-workspace [--copy-only] [--resume] [--dry-run] [--no-note] [--accept-skipped]
                                             # move a mounted workspace onto local storage, offline
istota-stack import-bare-metal DIR [args]    # import a bare-metal export, stack down
istota-stack restore [--date YYYY-MM-DD] [--force]   # key on stdin; config and databases from the workspace's backups
istota-stack import-standalone DIR --hostname HOST [--dry-run]   # a standalone install the Mac staged, istota.env on stdin
istota-stack retire-bare-metal --namespace NS [--dry-run]       # remove what the old Ansible role installed
istota-stack stage [--daemon] PATH | unstage PATH   # hand a file from the Mac to a verb (used by the Mac's istota-stack)
istota-stack help
```

`update` refuses a branch, a bare commit, a lightweight or unsigned tag, and a signature the release key in `allowed_signers` does not accept; `--unsigned` drops only the signature checks. `--bundle` takes the one tag from a git bundle (the Mac app's release) instead of fetching from origin, never over a tag the checkout already has, and is refused with `--unsigned`. `--images DIR` loads `images-TAG.tar` from DIR in place of a build, after checking `images-TAG.json`: its signature in the `istota-images` namespace against `allowed_signers`, the tag and the archive's SHA-256 it names, and the ID of every image the load reports. A refused archive changes nothing, and images a refused load replaced are put back. It is also refused with `--unsigned`. Once the stack is up on those images it runs `local-layers`, which builds what no release may carry: Claude Code as `istota:TAG-cc`, which `.env`'s `ISTOTA_IMAGE` then makes daemon and web run, and Google Chrome as `istota-browser:TAG`, the browser Dockerfile's `chrome` stage on the archive's `istota-browser-base:TAG`. `host.env`'s `ISTOTA_LOCAL_LAYERS` picks which; a Claude Code layer that is no longer wanted is removed, and a layer that fails leaves the stack on the release's own image and is tried again on the next run. No update changes `allowed_signers`: `trust-signer` is the only way the release key changes, and it writes `RELEASE_SIGNING_KEY` in `host.env` with it. A failed build leaves the previous tag running. `rollback` asks the previous image whether it can run the database first, and `--force` goes back to a release from before the state directory, onto the old state volume. Both reprovision the host when the release changed what `provision.sh` installs.

Every verb that writes (`update`, `rollback`, `setup`, `apply`, `put-files`, `import-bare-metal`, `restore`, `import-standalone`, `retire-bare-metal`, `localize-workspace`, `up`, `down`, `host-env KEY=VALUE`, `trust-signer`, `local-layers`) takes one lock; `local-layers --status` takes none. A second one waits up to ten minutes, then names the verb holding it. `setup --web` never waits, and `config-apply` exits quietly when another verb holds the lock. `setup-cancel` takes no lock, since the setup it ends holds it.

`setup --force` and `setup --web --force` first copy `.env`, `host.env` and `rclone.conf` into `/srv/app/istota/backups/setup-<time>/`, keeping the newest ten. `setup --web --force` then copies the config and the secrets aside, pauses the browser watchdog timer and stops the stack before it prints the address. A finished run installs the new files and starts the stack and the timer again. A run that does not finish (Ctrl-C, `setup-cancel`, an idle hour) puts the previous config and secrets back and starts the stack on them. `--local` publishes the page on `127.0.0.1:<LOCAL_FORWARD_PORT>` from `host.env`, 8080 when that is empty. `restore` and `import-standalone` are for a stack that was never set up, and all three imports refuse while any container of the stack is running.

Exit codes: 0 done, 1 a failure, 2 `apply` changed the config (or `check-image` wants a rebuild), 3 the previous image refused the database (`rollback`), 64 a usage error, 65 a tag refused. `exec` and `compose` return the command's own status.

## istota-stack on the Mac

The [local install](../getting-started/local-install.md) puts a Mac-side `istota-stack` and `istota` into `~/.local/bin`. Run them as yourself, never with `sudo`. The VM is the Lima instance `istota`, or `ISTOTA_LIMA_INSTANCE`.

```bash
istota-stack create [--workspace DIR] [--port N] [--cpus N] [--memory GIB] [--disk GIB] \
    ([--tag TAG] [--signing-key KEY] | --repo DIR [--tag TAG]) [--adopt] [--terminal-setup | --no-setup]
istota-stack start | stop | status | shell
istota-stack open [--files]                 # the web UI; --files the workspace in Finder
istota-stack autostart on|off               # limactl autostart enable|disable
istota-stack allow-host-port N | deny-host-port N
istota-stack setup [--web] [--force] [setup's options]   # the VM's setup; --web opens the page once it answers
istota-stack update [TAG]                   # the VM's update, then the Mac scripts from its checkout
istota-stack destroy [--force] [--purge]    # refuses without the master key in the Keychain;
                                            # --purge also deletes the folder, the key and the saved settings
istota-stack restore [--date YYYY-MM-DD] [--force] [create's options]
istota-stack import-standalone [--from DIR] [--config FILE] [--dry-run] [create's options]
istota ...                                  # the istota CLI in the daemon container, as uid 10001
```

A release `create` with no `--tag` installs the newest `vX.Y.Z` tag in the repository (read with `git ls-remote`), and with no `--signing-key` verifies it against the project's release key. A re-run takes the tag and key the first run recorded. `create` sets the stack up in the browser: it runs the VM's `setup --web --local` and opens the page once the setup server answers. `--terminal-setup` runs the terminal wizard instead, and `--no-setup` stops before either.

`setup` writes the VM's `LOCAL_FORWARD_PORT` first when it differs from the port this Mac forwards, then runs the VM's `setup`. With `--web` it runs `setup --web --local` with the other arguments (`--force` to change a configured stack), prints the address, and on a terminal opens the page once it answers `locked`, giving up after 120 seconds. Ctrl-C ends it through the VM's `setup-cancel`. It returns the VM's exit status.

Every other verb is passed to the VM's `istota-stack` as root, with its arguments and exit status unchanged; `apply <plan>` and `import-bare-metal <dir>` take a path on the Mac. Exit codes: a forwarded verb's own, else 64 for a usage error, 69 when `limactl` is missing or older than 2.0, 78 for a refused folder, port, machine or Keychain item.

The Mac CLI also drives five [VM verbs](#istota-stack-on-the-vm) itself: `host-env` (`create` sets `STORAGE=host-folder`, `INGRESS=local`, the loopback block and `LOCAL_FORWARD_PORT`; `setup`, `restore` and `import-standalone` rewrite `LOCAL_FORWARD_PORT` when it differs), `stage [--daemon] PATH` and `unstage PATH` (a root-only, or uid 10001's, directory under `/srv/app/istota/staging/` for a file from the Mac), `restore` and `import-standalone`. The Mac's `restore` deletes the VM, makes a new one with `create --no-setup` and runs the VM's `restore`; its `import-standalone` makes the VM with `create --no-setup` when there is none, stages `~/.istota` into it and runs the VM's `import-standalone`, leaving `~/.istota` as it is.

## `istota` on the machine (host wrapper)

`host/provision.sh` installs an `istota` command on the machine. It runs the CLI inside the running daemon container as uid 10001, with every capability dropped (`istota-stack exec istota ...`), so a CLI call cannot leave a root-owned file under `/data` that locks the daemon out of its own database. Run it as root; arguments pass straight through.

```bash
istota repl -u alice            # interactive REPL
istota list                     # any istota subcommand works
istota task "..." -u alice -x
```
