# CLI reference

Istota provides three console scripts: `istota` (main CLI), `istota-scheduler` (daemon), and `istota-skill` (skill proxy client).

## istota

### Task execution

```bash
istota task "prompt" -u USER -x              # Execute a task
istota task "prompt" -u USER -x --dry-run    # Show prompt without running
istota task "prompt" -u USER -t ROOM -x      # With conversation context
```

### Task management

```bash
istota list [-s STATUS] [-u USER]            # List tasks
istota show <task-id>                        # Task details
istota run [--once] [--briefings]            # Process pending tasks
```

### User management

```bash
istota user list                             # List configured users
istota user lookup EMAIL                     # Find user by email
istota user init USER                        # Initialize user workspace
istota user status USER                      # User status and resources
istota user show --name USER_ID              # Dump the stored profile row as JSON
istota user remove --name USER_ID            # Delete a user_profiles row (no other tables touched)
istota user ensure --name USER_ID [--display-name NAME] [--tz TZ] [--email ADDR ...] [--max-foreground-workers N] [--max-background-workers N] [--log-channel TOKEN] [--alerts-channel TOKEN] [--site-enabled|--no-site] [--default-destination DESCRIPTOR] [--route PURPOSE=DESCRIPTOR ...] [--disabled-skill NAME ...] [--disabled-module NAME ...] [--trusted-sender PATTERN ...] [--email-reply-routing origin+thread|origin|thread]
```

`istota user ensure` has no `-u`/`--user` flag — the user id comes from `--name` (required). `--tz` and `--timezone` are aliases. `--email` takes a bare address and is repeatable (each pass replaces the stored list). Worker caps are `--max-foreground-workers` / `--max-background-workers`.

`--default-destination` sets the fallback delivery surface (`talk` | `email` | `ntfy` | `web` | `surface:channel` | comma list). `--route` is repeatable and sets a purpose-keyed override; `PURPOSE` is one of `reply`, `alert`, `log`, `briefing`, `notification`. See [per-user delivery routing](../configuration/per-user.md#delivery-routing).

### Resources

```bash
istota resource ensure -u USER -t TYPE -p PATH [--name NAME] [--permissions read|readwrite] [--extras k=v | --extras-json '{…}'] [--extras-clear]
istota resource add    -u USER -t TYPE -p PATH         # one-shot add (fails if duplicate)
istota resource list   -u USER                          # List resources
```

Resource types: `calendar`, `folder`, `todo_file`, `email_folder`, `reminders_file`, `shared_file`, `notes_folder`. The retired types (`feeds`, `money`, `monarch`, `moneyman`, `karakeep`, `overland`, `ledger`, `invoicing`) are auto-cleaned at scheduler startup — feeds/money/location are now modules, karakeep/monarch/overland/tumblr are connected services in the encrypted `secrets` table.

### Briefings

```bash
istota briefing ensure -u USER --name NAME --cron CRON [--conversation-token TOKEN] [--output talk|email|ntfy|both] [--component k=v] [--components-json '{…}'] [--disabled]
istota briefing list   -u USER
istota briefing delete -u USER --name NAME
```

`ensure` / `list` / `delete` are positional actions. There are no `-n`/`-c` short flags — use `--name` and `--cron` (`-c` is the global `--config`).

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

Every `kv` subcommand takes a required `NAMESPACE` positional and a required `-u`/`--user`:

```bash
istota kv get NAMESPACE KEY -u USER          # Get value
istota kv set NAMESPACE KEY VALUE -u USER    # Set value (JSON)
istota kv list NAMESPACE -u USER             # List entries in a namespace
istota kv delete NAMESPACE KEY -u USER       # Delete key
istota kv namespaces -u USER                 # List namespaces
```

The skill proxy client also exposes set operations for membership-tracking patterns (seen IDs, processed hashes). These operate on a JSON-array value and avoid round-tripping large blobs:

```bash
istota-skill kv set-contains <ns> <key> <member>              # Check membership
istota-skill kv set-size     <ns> <key>                       # Count members
istota-skill kv set-members  <ns> <key> [--limit N] [--offset N]  # Paginated slice
istota-skill kv set-add      <ns> <key> <member> [<member>...]    # Add members (deferred)
istota-skill kv set-remove   <ns> <key> <member> [<member>...]    # Remove members (deferred)
```

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
istota money import-csv -u USER ...           # import transactions from CSV
istota money sync-monarch -u USER             # sync from Monarch Money
istota money invoice -u USER <generate|list|paid|create|void> ...
istota money work -u USER <list|add|update|remove> ...
```

Config-management subcommands manage the per-user money DB config:

```bash
istota money config  <show|import|export|diff> ...
istota money client  <add|update|remove|list> ...
istota money company <add|update|remove|list> ...
istota money service <add|update|remove|list> ...
istota money tax     <set|rates|pattern> ...
istota money monarch <profile|account-map> ...
```

### Interactive REPL

```bash
istota repl [-u USER] [-t TOKEN] [--workspace cwd|standard|PATH] [--model ALIAS] [--effort LEVEL]
```

A streamed, full-stack terminal assistant. Each line becomes a `source_type="repl"` task with `output_target="stream"`, run inline (no daemon needed); `task_events` stream back to the terminal. `--workspace` selects the working directory: `cwd` (default), `standard` (the per-user temp dir the daemon sandboxes), or an explicit path.

### Database

```bash
istota init                                  # Initialize database
```

## istota-scheduler

```bash
istota-scheduler                             # Start daemon
istota-scheduler -d                          # Debug mode
istota-scheduler -v                          # Verbose logging
istota-scheduler --max-tasks N               # Limit tasks per run
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
