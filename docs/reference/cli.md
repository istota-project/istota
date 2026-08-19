# CLI reference

Istota provides three console scripts: `istota` (main CLI), `istota-scheduler` (daemon), and `istota-skill` (skill proxy client).

## istota

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
```

### User management

```bash
istota user list                             # List configured users
istota user lookup --email ADDR               # Find user by email
istota user init USER                        # Initialize user workspace
istota user status USER                      # User status and resources
istota user show --name USER_ID              # Dump the stored profile row as JSON
istota user remove --name USER_ID            # Delete a user_profiles row (no other tables touched)
istota user ensure --name USER_ID [--display-name NAME] [--tz TZ] [--email ADDR ...] [--max-foreground-workers N] [--max-background-workers N] [--log-channel TOKEN] [--alerts-channel TOKEN] [--default-destination DESCRIPTOR] [--route PURPOSE=DESCRIPTOR ...] [--disabled-skill NAME ...] [--disabled-module NAME ...] [--trusted-sender PATTERN ...] [--quiet-sender PATTERN ...] [--email-reply-routing origin+thread|origin|thread] [--outbound-approval off|untrusted|all|""] [--external-turn-display full|collapsed|hidden] [--default-briefings | --no-default-briefings] [--briefing-email-html | --no-briefing-email-html] [--timezone-follow-location | --no-timezone-follow-location]
```

`istota user ensure` has no `-u`/`--user` flag — the user id comes from `--name` (required). `--tz` and `--timezone` are aliases. `--email` takes a bare address and is repeatable (each pass replaces the stored list). Worker caps are `--max-foreground-workers` / `--max-background-workers`.

`--default-briefings` / `--no-default-briefings` controls whether the shared `[[default_briefings]]` set is seeded into this user (on by default). Seeding is one-time per briefing name, so a later opt-in never clobbers briefings the user has edited.

`--default-destination` sets the fallback delivery surface (`talk` | `email` | `ntfy` | `web` | `surface:channel` | comma list). `--route` is repeatable and sets a purpose-keyed override; `PURPOSE` is one of `reply`, `alert`, `log`, `briefing`, `notification`. See [per-user delivery routing](../configuration/per-user.md#delivery-routing).

`--outbound-approval` sets this user's outbound email approval policy (`off` | `untrusted` | `all`); pass `""` to clear it and follow the operator's `[email] outbound_approval_floor`, which is the default. The floor is a minimum — a user value weaker than it has no effect. This is the supported way to set the policy for an existing user: the `[users.X] outbound_approval` TOML key seeds only a user with no profile row yet. See [the outbound approval gate](../features/email.md#the-outbound-approval-gate).

`--external-turn-display` controls how much of a turn that arrived from outside the room (an external contact's email) is shown inline in web chat: `full`, `collapsed` (default — sender and subject, expandable), or `hidden`. The turn itself always renders at every setting.

`--quiet-sender` is the counterpart to `--trusted-sender`: mail matching the pattern is filed without creating a task. `--briefing-email-html` selects HTML rather than plain-text briefing email. `--timezone-follow-location` opts into having the stored timezone updated when the location module sees you settle in a new one (off by default; see [location](../features/location.md)).

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

`istota briefings` is the unified tree. Schedule and delivery are framework-owned (`briefing_configs`); content (blocks and their sources) lives in the per-user briefings module DB.

```bash
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

`istota briefing` (singular) still works as a deprecated shim for `istota briefings schedule` and prints a deprecation notice. Its `--component` / `--components-json` flags are gone — the boolean-component content model is retired, and content is authored as blocks (CLI above, config-authored `[[users.X.briefings.blocks]]`, or the web block editor).

### Secrets (encrypted store)

```bash
istota secret ensure -u USER --service SERVICE --key KEY --value VALUE   # value via flag, env, or stdin
istota secret list   -u USER                                             # service/key/last_accessed; values never printed
istota secret remove -u USER --service SERVICE --key KEY
```

Only `-u`/`--user` has a short form. `--service`, `--key`, and `--value` are long-only (`-v` is the global verbose flag).

### Ensure-CLI state contract

All four `* ensure` subcommands (`user`, `resource`, `briefing`, `secret`) share a uniform contract: each computes `created` / `updated` / `noop` honestly by comparing the requested fields against the existing row, writes only when state would change, and prints a final `STATE: created|updated|noop` line. Ansible roles use `changed_when: "'STATE: noop' not in stdout"` for accurate change reporting.

Subsystem helpers that own the contract: `db.upsert_user_resource`, `secrets_store.upsert_secret`, `user_profiles.update_profile_with_status`, and `db.upsert_briefing_config` (via the existing briefing helper). Each returns `(thing, state)` (or just the state string) so the CLI is a thin printer.

### Email

```bash
istota email list                            # List recent emails
istota email poll                            # Poll for new emails
istota email test                            # Test email configuration
```

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

### Web chat maintenance

```bash
istota chat backfill-history [-t TOKEN]      # Recover dormant rooms' transcripts from the Talk message cache
```

Without `-t`, it walks every Talk-origin room. Use it after binding existing Talk rooms into web chat, so their history is visible on the web surface rather than starting from the next message.

### Nextcloud

```bash
istota nextcloud capabilities                # Curated summary of what the server supports
istota nextcloud capabilities --raw          # Full /cloud/capabilities payload
istota nextcloud capabilities --check talk,sharing.public   # Exits non-zero if any is missing
```

The `--check` form is the deployment fit-check — usable in a shell or a heartbeat `shell-command`. See [Nextcloud](../features/nextcloud.md) for the feature names and for the full `istota-skill nextcloud` surface.

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
istota money work -u USER <list|add|update|remove> ...
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

### Interactive REPL

```bash
istota repl [-u USER] [-t TOKEN] [--workspace cwd|standard|PATH] [--model ALIAS] [--effort LEVEL]
```

A streamed, full-stack terminal assistant. Each line becomes a `source_type="repl"` task with `output_target="stream"`, run inline (no daemon needed); `task_events` stream back to the terminal. `--workspace` selects the working directory: `cwd` (default), `standard` (the per-user temp dir the daemon sandboxes), or an explicit path.

### Local single-user install

```bash
istota setup [--yes] [--workspace DIR] [--brain claude_code|native] \
    [--native-base-url URL] [--native-model ID] [--native-api-key KEY] \
    [--user ID] [--display-name NAME] [--timezone TZ] [--port N] \
    [--email] [--location] [--no-money] [--force]   # Interactive first-run installer
istota serve [--host HOST] [--port N] [--env-file PATH]   # Scheduler loop + web server in one process
istota update [--force] [--channel stable|main]           # Self-update the standalone install
```

`istota setup` writes a local workspace (default `~/.istota`) and configures a single user; `--yes` runs non-interactively from flags and defaults. `istota serve` is the combined local launcher — it runs the scheduler loop and web server in one process (default bind `127.0.0.1`, port from `[web]`).

`istota update` reads the install record `install.sh` wrote, fetches and resets that checkout, reinstalls the tool, and runs fresh-code migrations. `--channel stable` (the default) tracks the latest release tag, `--channel main` the branch tip; the choice is remembered. It refuses to run on a server-shape deployment, which is updated through Ansible instead. See [Local install](../getting-started/local-install.md) for the full walkthrough.

### Database

```bash
istota init                                  # Initialize database
```

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

## `<namespace>-run` (production host wrapper)

Ansible deploys a host wrapper named `<namespace>-run` (e.g. `istota-run`) to `/usr/local/bin/`. It self-sudoes into the service user, loads the same secret bundle (`/etc/<namespace>/secrets.env`) and admins file (`ISTOTA_ADMINS_FILE`) the systemd units use, `cd`s to the install tree so the relative config search path resolves, then passes its arguments straight through to the `istota` CLI. The caller needs sudo rights (passwordless or interactive).

```bash
istota-run repl -u alice            # interactive REPL as the service user
istota-run list                     # any istota subcommand works
istota-run task "..." -u alice -x
```

For `repl` it defaults `--workspace` to `standard` (the per-user temp dir), because the install tree is a protected path the sandbox refuses to bind read-write; pass `--workspace` explicitly to override.
