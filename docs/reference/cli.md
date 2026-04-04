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
```

### Resources

```bash
istota resource add -u USER -t TYPE -p PATH  # Add resource
istota resource list -u USER                  # List resources
```

Resource types: `calendar`, `folder`, `todo_file`, `email_folder`, `reminders_file`, `shared_file`, `ledger`, `invoicing`, `karakeep`, `monarch`, `miniflux`, `moneyman`.

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
