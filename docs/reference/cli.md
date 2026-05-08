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
istota user ensure -u USER --name NAME [--timezone TZ] [--log-channel TOKEN] [--alerts-channel TOKEN] [--email k=v ...] [--max-fg N] [--max-bg N] [--site-enabled]
```

### Resources

```bash
istota resource ensure -u USER -t TYPE -p PATH [--name NAME] [--permissions read|readwrite] [--extras k=v | --extras-json '{…}'] [--extras-clear]
istota resource add    -u USER -t TYPE -p PATH         # one-shot add (fails if duplicate)
istota resource list   -u USER                          # List resources
```

Resource types: `calendar`, `folder`, `todo_file`, `email_folder`, `reminders_file`, `shared_file`, `notes_folder`. The retired types (`feeds`, `money`, `monarch`, `moneyman`, `karakeep`, `overland`, `ledger`, `invoicing`) are auto-cleaned at scheduler startup — feeds/money/location are now modules, karakeep/monarch/overland/tumblr are connected services in the encrypted `secrets` table.

### Briefings

```bash
istota briefing ensure -u USER -n NAME -c CRON [--conversation-token TOKEN] [--output talk|email|both] [--component k=v] [--components-json '{…}'] [--disabled]
istota briefing list   -u USER
```

### Secrets (encrypted store)

```bash
istota secret ensure -u USER -s SERVICE -k KEY -v VALUE   # value via flag, env, or stdin
istota secret list   -u USER                               # service/key/last_accessed; values never printed
istota secret remove -u USER -s SERVICE -k KEY
```

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

```bash
istota kv get KEY                            # Get value
istota kv set KEY VALUE                      # Set value
istota kv list [--namespace NS]              # List keys
istota kv delete KEY                         # Delete key
istota kv namespaces                         # List namespaces
```

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
