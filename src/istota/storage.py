"""Bot-managed Nextcloud storage operations."""

import logging
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("istota.storage")

BOT_USER_BASE = "/Users"
CHANNEL_BASE = "/Channels"

WORKSPACE_README = """\
# Istota

This is a shared collaboration folder — both you and Istota have \
read/write access. Everything you interact with lives here.

## Files

Configuration files live in the `config/` subfolder:

- **config/USER.md** — Persistent memory
- **config/TASKS.md** — Task queue (`- [ ] do something`)
- **config/BRIEFINGS.md** — Briefing schedule configuration
- **config/HEARTBEAT.md** — Health monitoring configuration
- **config/INVOICING.md** — Invoicing configuration
- **config/ACCOUNTING.md** — Accounting / Monarch sync configuration
- **config/CRON.md** — Scheduled recurring jobs
- **config/FEEDS.md** — Feed subscriptions (RSS, Tumblr, Are.na)
- **config/PERSONA.md** — Bot personality (editable copy of global persona)

See `examples/` for detailed documentation and configuration reference.
"""

WORKSPACE_README_EXAMPLE = """\
# Istota

This is a shared collaboration folder — both you and Istota have \
read/write access. Everything you interact with lives here.

## Files

Configuration files live in the `config/` subfolder:

- **config/USER.md** — Persistent memory. Istota reads this at the start of every \
task and appends to it when you ask it to remember something.
- **config/TASKS.md** — Task queue. Write `- [ ] do something` and Istota picks \
it up automatically. Status updates are written back to the file.
- **config/BRIEFINGS.md** — (Optional) Briefing schedule configuration. \
Control your own briefing times, delivery channel, and components.
- **config/HEARTBEAT.md** — (Optional) Health monitoring configuration. \
Set up periodic checks that alert you when something needs attention.
- **config/INVOICING.md** — (Optional) Invoicing configuration. \
Define clients, services, and billing rules for invoice generation.
- **config/ACCOUNTING.md** — (Optional) Accounting configuration. \
Configure Monarch Money API integration for automated ledger syncing.
- **config/CRON.md** — (Optional) Scheduled recurring jobs. \
Configure tasks that run on a cron schedule with results delivered to Talk or email.
- **config/FEEDS.md** — (Optional) Feed subscriptions. \
Configure RSS, Tumblr, and Are.na feeds aggregated into a static web page.
- **config/PERSONA.md** — (Optional) Bot personality. \
Edit this to customize how Istota behaves and communicates with you.

## Other content

Istota saves drafts, summaries, research, and anything else you ask it to \
produce in this folder. You can also drop files here for Istota to read \
in future conversations.

Additionally, you can share any of your own Nextcloud folders with Istota \
for direct access to your files.
"""

BRIEFINGS_TEMPLATE = """\
# Briefing Schedule

See `examples/BRIEFINGS.md` for all available options.

```toml
# [[briefings]]
# name = "morning"
# cron = "0 7 * * 1-5"         # 7am weekdays (in your timezone)
# conversation_token = "{conversation_token}"
# output = "talk"               # "talk", "email", or "both"
#
# [briefings.components]
# markets = true
# news = true
# calendar = true
# todos = true
# reminders = true
# notes = true
```
"""

BRIEFINGS_EXAMPLE = """\
# Briefing Schedule

Control your briefing times, delivery channel, and components.
The scheduler reads this file automatically — changes take effect within ~60 seconds.

## Example

```toml
[[briefings]]
name = "morning"
cron = "0 7 * * 1-5"         # 7am weekdays (in your timezone)
conversation_token = "abc123"
output = "talk"               # "talk", "email", or "both"

[briefings.components]
markets = true
news = true
calendar = true
todos = true
reminders = true
notes = true

[[briefings]]
name = "evening"
cron = "0 18 * * 1-5"        # 6pm weekdays
conversation_token = "abc123"
output = "talk"

[briefings.components]
markets = true
news = true
calendar = true
```

## Component Reference

- **calendar** — Today's calendar events
- **todos** — Pending items from your configured TODO file resource
- **markets** — Market data from configured symbols
- **news** — Headlines from configured news sources
- **reminders** — Random reminder from your configured reminders file resource
- **notes** — Summary of recent notes

Components set to `true` expand using admin-configured defaults.
Use a dict to override, e.g.: `markets = { enabled = true, futures = ["ES=F"] }`

## Output Options

- `output = "talk"` — Send to Nextcloud Talk room (requires `conversation_token`)
- `output = "email"` — Send via email
- `output = "both"` — Send to both Talk and email

## Cron Format

Standard 5-field cron: `minute hour day-of-month month day-of-week`

- `0 7 * * 1-5` — 7am weekdays
- `0 18 * * *` — 6pm every day
- `30 8 * * 1` — 8:30am Mondays only
- `0 */6 * * *` — Every 6 hours

Evaluated in the user's configured timezone.
"""


def _build_briefings_seed(config: "Config", user_id: str) -> str:
    """Build seed BRIEFINGS.md content, filling conversation_token from admin config."""
    token = ""
    user_config = config.users.get(user_id)
    if user_config:
        for b in user_config.briefings:
            if b.conversation_token:
                token = b.conversation_token
                break
    return BRIEFINGS_TEMPLATE.format(conversation_token=token)


# Template for initial HEARTBEAT.md file
HEARTBEAT_TEMPLATE = """\
# Heartbeat Monitoring

See `examples/HEARTBEAT.md` for all check types and options.

```toml
# [settings]
# conversation_token = "{conversation_token}"  # Talk room for alerts
# quiet_hours = ["22:00-07:00"]                # Suppress alerts during these hours
# default_cooldown_minutes = 60                # Time between repeat alerts

# [[checks]]
# name = "backup-fresh"
# type = "file-watch"
# path = "/Users/{user_id}/backups/latest.log"
# max_age_hours = 25
```
"""

HEARTBEAT_EXAMPLE = """\
# Heartbeat Monitoring

Configure periodic health checks that alert you when something needs attention.
HEARTBEAT.md is for monitoring — checking conditions and alerting on failures.
For running tasks on a schedule (including AI-powered checks), use CRON.md instead.

The scheduler evaluates these checks automatically — changes take effect within ~60 seconds.

## Example

```toml
[settings]
conversation_token = "abc123"          # Talk room for alerts
quiet_hours = ["22:00-07:00"]          # Suppress alerts during these hours
default_cooldown_minutes = 60          # Time between repeat alerts

[[checks]]
name = "backup-fresh"
type = "file-watch"
path = "/Users/alice/backups/latest.log"
max_age_hours = 25
cooldown_minutes = 120                 # Override default cooldown
interval_minutes = 15                  # Run every 15 min (default: every cycle)

[[checks]]
name = "disk-space"
type = "shell-command"
command = "df -h / | tail -1 | awk '{print $5}' | tr -d '%'"
condition = "< 90"
message = "Disk usage at {value}%"

[[checks]]
name = "api-health"
type = "url-health"
url = "https://api.example.com/health"
expected_status = 200
timeout = 10

[[checks]]
name = "schedule-conflicts"
type = "calendar-conflicts"
lookahead_hours = 24

[[checks]]
name = "overdue-tasks"
type = "task-deadline"
source = "file"
warn_hours_before = 24

[[checks]]
name = "system-health"
type = "self-check"
interval_minutes = 30                  # Run every 30 min (expensive: spawns Claude)
cooldown_minutes = 60

[checks.config]
execution_test = true                  # Test actual Claude CLI invocation
```

## Check Types

- **file-watch** — Check file age or existence (`path`, `max_age_hours`)
- **shell-command** — Run command, evaluate condition (`command`, `condition`, `message`, `timeout`)
- **url-health** — HTTP health check (`url`, `expected_status`, `timeout`)
- **calendar-conflicts** — Find overlapping events (`lookahead_hours`)
- **task-deadline** — Check for overdue tasks (`source`, `warn_hours_before`)
- **self-check** — System health diagnostics: Claude binary, bwrap, DB, failure rate, execution test (`execution_test`)

## Per-Check Fields

- `cooldown_minutes` — Override `default_cooldown_minutes` for this check
- `interval_minutes` — Run this check every N minutes instead of every cycle (~60s). Useful for expensive checks like `self-check`. Omit to run every cycle.

## Conditions (shell-command)

- `< N` / `> N` — Numeric comparison
- `== value` — Exact string match
- `contains:text` — Substring match
- `not-contains:text` — Negative substring match

## Quiet Hours

Time ranges like `22:00-07:00` suppress alert delivery, but checks still run.
Cross-midnight ranges are supported. When quiet hours end, the next failure triggers an alert.

## Cooldown

After an alert, no repeat alerts are sent until the cooldown expires.
Set `cooldown_minutes` per-check to override `default_cooldown_minutes`.
"""


def _build_heartbeat_seed(config: "Config", user_id: str) -> str:
    """Build seed HEARTBEAT.md content, filling conversation_token and user_id."""
    token = ""
    user_config = config.users.get(user_id)
    if user_config:
        for b in user_config.briefings:
            if b.conversation_token:
                token = b.conversation_token
                break
    return HEARTBEAT_TEMPLATE.format(conversation_token=token, user_id=user_id)


# Template for initial INVOICING.md file
INVOICING_TEMPLATE = """\
# Invoicing Configuration

See `examples/INVOICING.md` for all options, service types, and CLI commands.

```toml
# accounting_path = "/Users/{user_id}/shared/Accounting"
# work_log = "/Users/{user_id}/shared/Notes/_INVOICES.md"
# invoice_output = "invoices/generated"
# next_invoice_number = 1

# [company]
# name = "Your Company"
# address = "123 Main St\\nCity, ST 12345"
# email = "billing@example.com"
# payment_instructions = "Wire transfer to: ..."

# [clients.example]
# name = "Example Corp"
# address = "456 Oak Ave"
# email = "billing@example.com"
# terms = 30

# [services.consulting]
# display_name = "Consulting Services"
# rate = 150
# type = "hours"
```
"""

INVOICING_EXAMPLE = """\
# Invoicing Configuration

Configure your company info, clients, services, and billing rules.
The accounting skill reads this file for invoice generation.

## Global Settings

```ini
accounting_path = "path/to/accounting"   # Base path for invoice output and logos
work_log = "_INVOICES.md"               # Work log filename (relative to accounting_path)
invoice_output = "invoices"             # Output directory for PDFs (relative to accounting_path)
next_invoice_number = 1                 # Auto-incremented after each invoice
currency = "USD"                        # Default currency (overridable per entity)
default_ar_account = "Assets:Accounts-Receivable"
default_bank_account = "Assets:Bank:Checking"
default_entity = "default"              # Entity key when multiple companies defined
notifications = ""                      # Default notification surface: "talk", "email", or "both"
days_until_overdue = 0                  # Days after invoice date to flag overdue (0 = disabled)
```

## Company / Entity

Single company (`[company]`) or multi-entity (`[companies.<key>]`):

```ini
[company]
name = "My Company LLC"
address = "123 Main St\\nCity, ST 12345"
email = "billing@example.com"
payment_instructions = "Wire to ..."    # Shown on invoice PDF
logo = "logo.png"                       # Path relative to accounting_path
ar_account = ""                         # Per-entity A/R account override
bank_account = ""                       # Per-entity bank account override
currency = ""                           # Per-entity currency override
```

## Clients

```ini
[clients.<key>]
name = "Client Name"
address = "456 Oak Ave\\nTown, ST 67890"
email = "client@example.com"
terms = 30                              # Payment terms in days (or string like "Net 30")
ar_account = ""                         # Client-specific A/R account
entity = ""                             # Default entity key for this client
```

### Client Invoicing Options

```ini
[clients.<key>.invoicing]
schedule = "on-demand"                  # "on-demand" or "monthly"
day = 1                                 # Day of month for scheduled generation
reminder_days = 3                       # Days before schedule_day to send reminder
notifications = ""                      # Per-client: "talk", "email", or "both"
days_until_overdue = 0                  # Per-client override (0 = use global)
bundles = []                            # Group services into single line items
separate = []                           # Force separate invoices for these services
```

## Services

```ini
[services.<key>]
display_name = "Consulting"
rate = 150.0                            # Rate per unit (or flat amount)
type = "hours"                          # "hours", "days", "flat", or "other"
income_account = ""                     # e.g. "Income:Consulting" (auto-generated if empty)
```

### Service Types

- **hours** — `qty x rate` (most common)
- **days** — `qty x rate`
- **flat** — Fixed rate per entry
- **other** — Uses `amount` from work log (for expenses, reimbursements)

## Work Log

Create a separate work log file (configured as `work_log` above) with entries \
inside a `toml` fenced block:

```ini
[[entries]]
date = "2026-01-15"
client = "client_key"
service = "service_key"
qty = 8.0            # For hours/days/flat
# amount = 500.00    # For type = "other" or expenses (use instead of qty)
# discount = 10      # Percentage discount
# description = ""   # Optional line item description
# entity = ""        # Override entity for this entry
# invoice = ""       # Auto-set when invoiced (e.g. "INV-000042")
# paid_date = ""     # Auto-set when payment recorded
```

## CLI Commands

```bash
# Generate invoices for a billing period
python -m istota.skills.accounting invoice generate --period 2026-02

# Preview without generating files
python -m istota.skills.accounting invoice generate --period 2026-02 --dry-run

# List outstanding receivables
python -m istota.skills.accounting invoice list

# Record payment
python -m istota.skills.accounting invoice paid INV-000001 --date 2026-02-15

# Create a manual invoice
python -m istota.skills.accounting invoice create example --service consulting --hours 40
```
"""


# Template for initial TASKS.md file
TASKS_FILE_TEMPLATE = """\
# Tasks
"""

TASKS_FILE_EXAMPLE = """\
# Tasks

Write a task as `- [ ] do something` and Istota picks it up automatically.
Status updates are written back to this file.

## Status Markers

- `[ ]` — Pending (Istota will pick this up)
- `[~]` — In progress (Istota is working on it)
- `[x]` — Completed
- `[!]` — Failed

## Examples

```markdown
- [ ] summarize my inbox
- [ ] check the weather forecast for this weekend
- [ ] draft a reply to the last email from Alice
```

Tasks are identified by content hash, so you can reorder freely.
Completed/failed tasks can be deleted or kept for reference.
"""

# Template for initial memory file
MEMORY_TEMPLATE = """# User Memory

This file contains remembered information about the user.
The bot can append to this file to remember things for future conversations.

## Notes

"""


def get_user_base_path(user_id: str) -> str:
    """Get the base path for a user's bot-managed directory."""
    return f"{BOT_USER_BASE}/{user_id}"


def get_user_memory_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's memory file (USER.md in bot dir config/)."""
    return f"{get_user_config_path(user_id, bot_dir)}/USER.md"


def get_user_memories_path(user_id: str) -> str:
    """Get the path to a user's dated memories directory."""
    return f"{get_user_base_path(user_id)}/memories"


def get_user_bot_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's bot directory (e.g. /Users/{uid}/istota/)."""
    return f"{get_user_base_path(user_id)}/{bot_dir}"


def get_user_config_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's bot config/ directory."""
    return f"{get_user_bot_path(user_id, bot_dir)}/config"


def get_user_tasks_file_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's TASKS.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/TASKS.md"


def get_user_shared_path(user_id: str) -> str:
    """Get the path to a user's shared folder (for auto-organized shared files)."""
    return f"{get_user_base_path(user_id)}/shared"


def get_user_scripts_path(user_id: str) -> str:
    """Get the path to a user's scripts directory."""
    return f"{get_user_base_path(user_id)}/scripts"


def get_user_briefings_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's BRIEFINGS.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/BRIEFINGS.md"


def get_user_heartbeat_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's HEARTBEAT.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/HEARTBEAT.md"


def get_user_invoicing_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's INVOICING.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/INVOICING.md"


def get_user_accounting_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's ACCOUNTING.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/ACCOUNTING.md"


def get_user_cron_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's CRON.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/CRON.md"


def get_user_feeds_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's FEEDS.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/FEEDS.md"


def get_user_persona_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's PERSONA.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/PERSONA.md"


CRON_TEMPLATE = """\
# Scheduled Jobs

See `examples/CRON.md` for all options and cron format reference.

```toml
# [[jobs]]
# name = "daily-report"
# cron = "0 9 * * *"             # 9am daily (in your timezone)
# prompt = "Generate my daily report"
# target = "talk"                 # "talk", "email", or omit
# room = "{conversation_token}"   # Talk room token (required for target = "talk")
```
"""

CRON_EXAMPLE = """\
# Scheduled Jobs

CRON.md is for running tasks and commands on a schedule.
For monitoring conditions and alerting on failures, use HEARTBEAT.md instead.

Configure recurring tasks that run on a schedule.
The scheduler reads this file automatically — changes take effect within ~60 seconds.

## Example

```toml
[[jobs]]
name = "morning-briefing"
cron = "0 9 * * *"               # 9am daily (in your timezone)
prompt = "Generate my morning briefing"
target = "talk"                   # Post result to Talk room
room = "abc123"                   # Conversation token

[[jobs]]
name = "weekly-review"
cron = "0 18 * * 0"              # 6pm Sundays
prompt = "Generate weekly review of completed tasks"
target = "email"                  # Send result via email

[[jobs]]
name = "check-deadlines"
cron = "0 8 * * 1-5"             # 8am weekdays
prompt = "Check for any upcoming deadlines this week"
target = "talk"
room = "abc123"
silent_unless_action = true       # Only post if something needs attention
```

## Fields

- **name** — Unique identifier for the job (e.g., `daily-report`, `weekly-cleanup`)
- **cron** — Standard 5-field cron expression (minute hour day month weekday)
- **prompt** — The full prompt text that will be executed as a task
- **target** — Where to deliver results: `"talk"` or `"email"` (omit for no delivery)
- **room** — Talk conversation token (required when target is `"talk"`)
- **enabled** — Set to `false` to pause the job (default: true)
- **silent_unless_action** — When true, only posts output if response starts with \
`ACTION:` (default: false)

## Runtime Control

Use `!cron` in Talk to manage jobs at runtime:

- `!cron` — List all jobs and their status
- `!cron enable <name>` — Re-enable a disabled job (resets failure count)
- `!cron disable <name>` — Disable a job

Jobs auto-disable after 5 consecutive failures. Use `!cron enable` to re-activate.

## Cron Format

Standard 5-field cron: `minute hour day-of-month month day-of-week`

- `0 9 * * *` — Every day at 9:00 AM
- `0 9 * * 1-5` — Weekdays at 9:00 AM
- `30 18 * * 0` — Sundays at 6:30 PM
- `0 */6 * * *` — Every 6 hours
- `0 8 1 * *` — First of every month at 8:00 AM

Evaluated in the user's configured timezone.
"""


def _build_cron_seed(config: "Config", user_id: str) -> str:
    """Build seed CRON.md content, filling conversation_token from admin config."""
    token = ""
    user_config = config.users.get(user_id)
    if user_config:
        for b in user_config.briefings:
            if b.conversation_token:
                token = b.conversation_token
                break
    return CRON_TEMPLATE.format(conversation_token=token)


FEEDS_TEMPLATE = """\
# Feed Subscriptions

See `examples/FEEDS.md` for all options and feed types.

```toml
# [tumblr]
# api_key = "your-tumblr-api-key"

# [[feeds]]
# name = "hn-best"
# type = "rss"
# url = "https://hnrss.org/best"
# interval_minutes = 30
```
"""

FEEDS_EXAMPLE = """\
# Feed Subscriptions

Configure RSS, Tumblr, and Are.na feeds. Items are aggregated into a
static web page at your site's `/feeds/` path.

## Feed Types

### RSS

```toml
[[feeds]]
name = "hn-best"
type = "rss"
url = "https://hnrss.org/best"
interval_minutes = 30        # Default: 30
```

### Tumblr

Requires a Tumblr API key (register at api.tumblr.com).

```toml
[tumblr]
api_key = "your-api-key"

[[feeds]]
name = "photoblog"
type = "tumblr"
url = "blogname"             # Just the blog name, not full URL
interval_minutes = 180       # Default: 180
```

### Are.na

```toml
[[feeds]]
name = "inspiration"
type = "arena"
url = "channel-slug"         # The channel slug from the URL
interval_minutes = 60        # Default: 60
```

## Defaults

- RSS: polls every 30 minutes
- Tumblr: polls every 180 minutes
- Are.na: polls every 60 minutes
"""

# Template for initial ACCOUNTING.md file
ACCOUNTING_TEMPLATE = """\
# Accounting Configuration

See `examples/ACCOUNTING.md` for all options, mappings, and CLI commands.

```toml
# [monarch]
# email = "your@email.com"
# password = "your_password"

# [monarch.sync]
# lookback_days = 30
# default_account = "Assets:Bank:Checking"

# [monarch.accounts]
# "Chase Checking" = "Assets:Bank:Chase:Checking"
```
"""

ACCOUNTING_EXAMPLE = """\
# Accounting Configuration

Configure Monarch Money API integration for automated ledger syncing.

## Settings

```ini
[monarch]
email = "your@email.com"
password = "your_password"
# OR use session_token instead:
# session_token = "..."

[monarch.sync]
lookback_days = 30
default_account = "Assets:Bank:Checking"

# Map Monarch account names to beancount accounts
[monarch.accounts]
"Chase Checking" = "Assets:Bank:Chase:Checking"
"Amex Gold" = "Liabilities:CreditCard:Amex"

# Map Monarch categories to beancount accounts (overrides defaults)
[monarch.categories]
"Custom Category" = "Expenses:Custom"

# Filter transactions by tags
[monarch.tags]
include = ["business"]        # Only sync transactions with these tags
exclude = ["personal"]        # Exclude transactions with these tags
```

## Account Mapping

The `[monarch.accounts]` section maps Monarch account names (as shown in the app)
to beancount account paths. Unmapped accounts use `default_account`.

## Category Mapping

Built-in mappings exist for common categories (Groceries > Expenses:Food:Groceries, etc.).
Use `[monarch.categories]` to override or add custom mappings.

## Tag Filtering

Use tags to control which transactions sync:
- `include = ["business"]` — Only transactions tagged "business"
- `exclude = ["personal"]` — Skip transactions tagged "personal"
- If both are set, include is applied first, then exclude

## CLI Commands

```bash
# Import from CSV export (manual)
python -m istota.skills.accounting import-monarch FILE --account ACCT

# Sync via API (automated)
python -m istota.skills.accounting sync-monarch

# Preview sync without writing
python -m istota.skills.accounting sync-monarch --dry-run
```
"""


def _rclone_mkdir(remote: str, path: str) -> bool:
    """Create a directory via rclone. Returns True on success."""
    result = subprocess.run(
        ["rclone", "mkdir", f"{remote}:{path}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _rclone_path_exists(remote: str, path: str) -> bool:
    """Check if a path exists via rclone lsjson."""
    result = subprocess.run(
        ["rclone", "lsjson", f"{remote}:{path}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _rclone_cat(remote: str, path: str) -> str | None:
    """Read a file via rclone cat. Returns None on failure."""
    result = subprocess.run(
        ["rclone", "cat", f"{remote}:{path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _rclone_rcat(remote: str, path: str, content: str) -> bool:
    """Write content to a file via rclone rcat. Returns True on success."""
    result = subprocess.run(
        ["rclone", "rcat", f"{remote}:{path}"],
        input=content,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def ensure_user_directories(remote: str, user_id: str, bot_dir: str) -> bool:
    """
    Create the bot-managed directory structure for a user.

    Returns True if all directories were created or already exist.
    """
    base = get_user_base_path(user_id)
    subdirs = ["inbox", "memories", bot_dir, "shared", "scripts"]

    success = True
    for subdir in subdirs:
        path = f"{base}/{subdir}"
        if not _rclone_mkdir(remote, path):
            # mkdir may fail if it already exists, so check existence
            if not _rclone_path_exists(remote, path):
                success = False

    # Create bot_dir/exports/
    exports_path = f"{base}/{bot_dir}/exports"
    if not _rclone_mkdir(remote, exports_path):
        if not _rclone_path_exists(remote, exports_path):
            success = False

    return success


def user_directories_exist(remote: str, user_id: str, bot_dir: str) -> dict[str, bool]:
    """
    Check which user directories exist.

    Returns dict mapping directory name to existence status.
    """
    base = get_user_base_path(user_id)
    subdirs = ["inbox", "memories", bot_dir, "shared", "scripts"]

    result = {}
    for subdir in subdirs:
        path = f"{base}/{subdir}"
        result[subdir] = _rclone_path_exists(remote, path)

    return result


def read_user_memory(remote: str, user_id: str, bot_dir: str) -> str | None:
    """
    Read the user's memory file.

    Returns the content of the memory file, or None if it doesn't exist or is empty.
    """
    memory_path = get_user_memory_path(user_id, bot_dir)
    content = _rclone_cat(remote, memory_path)

    if content is None or not content.strip():
        return None

    return content


def init_user_memory(remote: str, user_id: str, bot_dir: str) -> bool:
    """
    Initialize the user's memory file with a template.

    Returns True on success.
    """
    memory_path = get_user_memory_path(user_id, bot_dir)
    return _rclone_rcat(remote, memory_path, MEMORY_TEMPLATE)


def get_memory_line_count(remote: str, user_id: str, bot_dir: str) -> int | None:
    """
    Get the line count of a user's memory file.

    Returns None if file doesn't exist.
    """
    content = read_user_memory(remote, user_id, bot_dir)
    if content is None:
        return None
    return len(content.splitlines())


def get_user_inbox_path(user_id: str) -> str:
    """Get the path to a user's inbox directory."""
    return f"{get_user_base_path(user_id)}/inbox"


def upload_file_to_inbox(
    remote: str,
    user_id: str,
    local_path: Path,
    remote_filename: str | None = None,
) -> str | None:
    """
    Upload a local file to the user's inbox directory.

    Args:
        remote: rclone remote name
        user_id: User ID
        local_path: Local file path to upload
        remote_filename: Optional filename to use on remote (defaults to local filename)

    Returns:
        The remote path on success, None on failure.
    """
    if not local_path.exists():
        return None

    filename = remote_filename or local_path.name
    inbox_path = get_user_inbox_path(user_id)
    remote_path = f"{inbox_path}/{filename}"

    result = subprocess.run(
        ["rclone", "copyto", str(local_path), f"{remote}:{remote_path}"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return None

    return remote_path


# =============================================================================
# Mount-aware storage functions
# =============================================================================


def _get_mount_path(config: "Config", path: str) -> Path:
    """Get the local mount path for a Nextcloud path."""
    return config.nextcloud_mount_path / path.lstrip("/")


def _migrate_old_layout(user_base: Path) -> None:
    """
    Migrate from old directory layout to new one.

    Old layout:
        context/memory.md → USER.md
        context/YYYY-MM-DD.md → memories/YYYY-MM-DD.md

    Only runs if context/ exists and target files don't. Safe to call repeatedly.
    """
    context_dir = user_base / "context"
    if not context_dir.is_dir():
        return

    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

    # Migrate memory.md → USER.md
    old_memory = context_dir / "memory.md"
    new_memory = user_base / "USER.md"
    if old_memory.exists() and not new_memory.exists():
        shutil.copy2(old_memory, new_memory)
        logger.info("Migrated %s → %s", old_memory, new_memory)

    # Migrate dated files → memories/
    memories_dir = user_base / "memories"
    memories_dir.mkdir(exist_ok=True)
    for f in context_dir.iterdir():
        if f.is_file() and date_pattern.match(f.name):
            dest = memories_dir / f.name
            if not dest.exists():
                shutil.copy2(f, dest)
                logger.info("Migrated %s → %s", f, dest)


def _migrate_notes_to_workspace(user_base: Path) -> None:
    """
    Migrate from notes/ to workspace/ directory.

    Only runs if notes/ exists and workspace/ doesn't. Safe to call repeatedly.
    """
    notes_dir = user_base / "notes"
    workspace_dir = user_base / "workspace"
    if notes_dir.is_dir() and not workspace_dir.exists():
        notes_dir.rename(workspace_dir)
        logger.info("Migrated %s → %s", notes_dir, workspace_dir)


def _migrate_workspace_files(user_base: Path) -> None:
    """
    Migrate USER.md and TASKS.md from user root into workspace/.

    Only moves files if the source exists and the destination doesn't.
    Safe to call repeatedly.
    """
    workspace_dir = user_base / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    for filename in ("USER.md", "TASKS.md"):
        src = user_base / filename
        dst = workspace_dir / filename
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))
            logger.info("Migrated %s → %s", src, dst)


# Config files that live in bot_name/config/
_CONFIG_FILES = (
    "USER.md", "TASKS.md", "BRIEFINGS.md", "HEARTBEAT.md",
    "INVOICING.md", "ACCOUNTING.md", "CRON.md", "FEEDS.md",
)


def _migrate_workspace_to_bot_dir(user_base: Path, bot_dir: str) -> None:
    """
    Migrate from workspace/ to bot directory layout.

    1. If workspace/ exists and bot dir doesn't → rename workspace/ → bot_dir/
    2. Move config .md files from bot_dir/ root into bot_dir/config/

    Safe to call repeatedly.
    """
    workspace_dir = user_base / "workspace"
    bot_dir_path = user_base / bot_dir

    # Step 1: rename workspace/ → bot_dir/
    if workspace_dir.is_dir() and not bot_dir_path.exists():
        workspace_dir.rename(bot_dir_path)
        logger.info("Migrated %s → %s", workspace_dir, bot_dir_path)

    # Step 2: move config files from bot_dir/ root into bot_dir/config/
    if bot_dir_path.is_dir():
        config_dir = bot_dir_path / "config"
        config_dir.mkdir(exist_ok=True)
        for filename in _CONFIG_FILES:
            src = bot_dir_path / filename
            dst = config_dir / filename
            if src.is_file() and not dst.exists():
                shutil.move(str(src), str(dst))
                logger.info("Migrated %s → %s", src, dst)


def ensure_user_directories_v2(config: "Config", user_id: str) -> bool:
    """
    Create the bot-managed directory structure for a user (mount-aware).

    Returns True if all directories were created or already exist.
    """
    bot_dir = config.bot_dir_name
    if config.use_mount:
        base = _get_mount_path(config, get_user_base_path(user_id))

        # Run migrations before creating directories
        _migrate_old_layout(base)
        _migrate_notes_to_workspace(base)
        _migrate_workspace_files(base)
        _migrate_workspace_to_bot_dir(base, bot_dir)

        subdirs = ["inbox", "memories", bot_dir, "shared", "scripts"]
        for subdir in subdirs:
            path = base / subdir
            path.mkdir(parents=True, exist_ok=True)
        logger.debug("Ensured user directories for %s via mount", user_id)

        # Ensure bot dir subdirectories
        bot_dir_path = base / bot_dir
        config_dir = bot_dir_path / "config"
        config_dir.mkdir(exist_ok=True)
        exports_dir = bot_dir_path / "exports"
        exports_dir.mkdir(exist_ok=True)

        # Migrate old exports/ to bot_dir/exports/
        old_exports = base / "exports"
        if old_exports.is_dir() and any(old_exports.iterdir()):
            for item in old_exports.iterdir():
                dst = exports_dir / item.name
                if not dst.exists():
                    shutil.move(str(item), str(dst))
                    logger.info("Migrated %s → %s", item, dst)

        # Seed bot dir with README
        readme = bot_dir_path / "README.md"
        if not readme.exists():
            readme.write_text(WORKSPACE_README)
            logger.debug("Created %s README for %s", bot_dir, user_id)

        # Seed config/ with default files
        tasks_file = config_dir / "TASKS.md"
        if not tasks_file.exists():
            tasks_file.write_text(TASKS_FILE_TEMPLATE)
            logger.debug("Created %s/config/TASKS.md for %s", bot_dir, user_id)

        briefings_file = config_dir / "BRIEFINGS.md"
        if not briefings_file.exists():
            briefings_file.write_text(_build_briefings_seed(config, user_id))
            logger.debug("Created %s/config/BRIEFINGS.md for %s", bot_dir, user_id)

        heartbeat_file = config_dir / "HEARTBEAT.md"
        if not heartbeat_file.exists():
            heartbeat_file.write_text(_build_heartbeat_seed(config, user_id))
            logger.debug("Created %s/config/HEARTBEAT.md for %s", bot_dir, user_id)

        invoicing_file = config_dir / "INVOICING.md"
        if not invoicing_file.exists():
            invoicing_file.write_text(INVOICING_TEMPLATE.format(user_id=user_id))
            logger.debug("Created %s/config/INVOICING.md for %s", bot_dir, user_id)

        accounting_file = config_dir / "ACCOUNTING.md"
        if not accounting_file.exists():
            accounting_file.write_text(ACCOUNTING_TEMPLATE)
            logger.debug("Created %s/config/ACCOUNTING.md for %s", bot_dir, user_id)

        cron_file = config_dir / "CRON.md"
        if not cron_file.exists():
            cron_file.write_text(_build_cron_seed(config, user_id))
            logger.debug("Created %s/config/CRON.md for %s", bot_dir, user_id)

        feeds_file = config_dir / "FEEDS.md"
        if not feeds_file.exists():
            feeds_file.write_text(FEEDS_TEMPLATE)
            logger.debug("Created %s/config/FEEDS.md for %s", bot_dir, user_id)

        # Seed PERSONA.md from global persona file
        persona_file = config_dir / "PERSONA.md"
        if not persona_file.exists():
            global_persona = config.skills_dir.parent / "persona.md"
            if global_persona.exists():
                persona_file.write_text(global_persona.read_text())
                logger.debug("Created %s/config/PERSONA.md for %s", bot_dir, user_id)

        # Write example files (always overwrite to stay current)
        examples_dir = bot_dir_path / "examples"
        examples_dir.mkdir(exist_ok=True)
        examples = {
            "README.md": WORKSPACE_README_EXAMPLE,
            "TASKS.md": TASKS_FILE_EXAMPLE,
            "BRIEFINGS.md": BRIEFINGS_EXAMPLE,
            "HEARTBEAT.md": HEARTBEAT_EXAMPLE,
            "INVOICING.md": INVOICING_EXAMPLE,
            "ACCOUNTING.md": ACCOUNTING_EXAMPLE,
            "CRON.md": CRON_EXAMPLE,
            "FEEDS.md": FEEDS_EXAMPLE,
        }
        for filename, content in examples.items():
            (examples_dir / filename).write_text(content)
        logger.debug("Updated %s examples for %s", bot_dir, user_id)

        # Auto-share bot dir back to the user
        bot_path = get_user_bot_path(user_id, bot_dir)
        share_folder_with_user(config, bot_path, user_id)

        return True
    else:
        result = ensure_user_directories(config.rclone_remote, user_id, bot_dir)
        if result:
            logger.debug("Ensured user directories for %s via rclone", user_id)
        return result


def user_directories_exist_v2(config: "Config", user_id: str) -> dict[str, bool]:
    """
    Check which user directories exist (mount-aware).

    Returns dict mapping directory name to existence status.
    """
    if config.use_mount:
        base = _get_mount_path(config, get_user_base_path(user_id))
        subdirs = ["inbox", "memories", config.bot_dir_name, "shared", "scripts"]
        return {subdir: (base / subdir).exists() for subdir in subdirs}
    else:
        return user_directories_exist(config.rclone_remote, user_id, config.bot_dir_name)


def read_user_memory_v2(config: "Config", user_id: str) -> str | None:
    """
    Read the user's memory file (mount-aware).

    Returns the content of the memory file, or None if it doesn't exist or is empty.
    """
    if config.use_mount:
        memory_path = _get_mount_path(config, get_user_memory_path(user_id, config.bot_dir_name))
        if not memory_path.exists():
            return None
        content = memory_path.read_text()
        if not content.strip():
            return None
        return content
    else:
        return read_user_memory(config.rclone_remote, user_id, config.bot_dir_name)


def init_user_memory_v2(config: "Config", user_id: str) -> bool:
    """
    Initialize the user's memory file with a template (mount-aware).

    Returns True on success.
    """
    if config.use_mount:
        memory_path = _get_mount_path(config, get_user_memory_path(user_id, config.bot_dir_name))
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(MEMORY_TEMPLATE)
        return True
    else:
        return init_user_memory(config.rclone_remote, user_id, config.bot_dir_name)


def get_memory_line_count_v2(config: "Config", user_id: str) -> int | None:
    """
    Get the line count of a user's memory file (mount-aware).

    Returns None if file doesn't exist.
    """
    content = read_user_memory_v2(config, user_id)
    if content is None:
        return None
    return len(content.splitlines())


def upload_file_to_inbox_v2(
    config: "Config",
    user_id: str,
    local_path: Path,
    remote_filename: str | None = None,
) -> str | None:
    """
    Upload a local file to the user's inbox directory (mount-aware).

    Args:
        config: Application config
        user_id: User ID
        local_path: Local file path to upload
        remote_filename: Optional filename to use on remote (defaults to local filename)

    Returns:
        The remote path on success, None on failure.
    """
    if not local_path.exists():
        return None

    filename = remote_filename or local_path.name
    inbox_path = get_user_inbox_path(user_id)
    remote_path = f"{inbox_path}/{filename}"

    if config.use_mount:
        dst = _get_mount_path(config, remote_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local_path), str(dst))
        return remote_path
    else:
        return upload_file_to_inbox(config.rclone_remote, user_id, local_path, remote_filename)


# Date pattern for dated memory files (YYYY-MM-DD.md)
_DATED_MEMORY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def read_dated_memories(
    config: "Config",
    user_id: str,
    max_days: int = 7,
    max_chars: int = 4000,
) -> str | None:
    """
    Read recent dated memory files from a user's memories directory.

    Scans /Users/{user_id}/memories/ for YYYY-MM-DD.md files within max_days,
    concatenates newest-first, and caps at max_chars.

    Returns concatenated content, or None if no dated files found.
    """
    if not config.use_mount:
        return None  # Only supported with mount

    context_dir = _get_mount_path(config, get_user_memories_path(user_id))
    if not context_dir.exists():
        return None

    cutoff = datetime.now() - timedelta(days=max_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    # Find matching files
    dated_files = []
    for path in context_dir.iterdir():
        if path.is_file() and _DATED_MEMORY_PATTERN.match(path.name):
            date_str = path.stem  # e.g. "2026-01-28"
            if date_str >= cutoff_str:
                dated_files.append((date_str, path))

    if not dated_files:
        return None

    # Sort newest-first
    dated_files.sort(key=lambda x: x[0], reverse=True)

    # Concatenate with headers, respecting max_chars
    parts = []
    total = 0
    for date_str, path in dated_files:
        content = path.read_text().strip()
        if not content:
            continue
        entry = f"### {date_str}\n\n{content}\n"
        if total + len(entry) > max_chars:
            # Include partial if we have nothing yet
            if not parts:
                remaining = max_chars - total
                parts.append(entry[:remaining] + "...[truncated]")
            break
        parts.append(entry)
        total += len(entry)

    if not parts:
        return None

    return "\n".join(parts)


# =============================================================================
# Channel memory functions
# =============================================================================

CHANNEL_MEMORY_TEMPLATE = """# Channel Memory

This file contains remembered information about this channel/room.
The bot can append to this file to remember things relevant to all participants.

## Notes

"""


def get_channel_base_path(conversation_token: str) -> str:
    """Get the base path for a channel's bot-managed directory."""
    return f"{CHANNEL_BASE}/{conversation_token}"


def get_channel_memory_path(conversation_token: str) -> str:
    """Get the path to a channel's memory file."""
    return f"{get_channel_base_path(conversation_token)}/CHANNEL.md"


def get_channel_memories_path(conversation_token: str) -> str:
    """Get the path to a channel's dated memories directory."""
    return f"{get_channel_base_path(conversation_token)}/memories"


def ensure_channel_directories(config: "Config", conversation_token: str) -> bool:
    """
    Create the bot-managed directory structure for a channel (mount-aware).

    Creates /Channels/{token}/memories/

    Returns True if directory was created or already exists.
    """
    if config.use_mount:
        base = _get_mount_path(config, get_channel_base_path(conversation_token))
        memories_dir = base / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)

        # Migrate old layout: context/memory.md → CHANNEL.md
        old_memory = base / "context" / "memory.md"
        new_memory = base / "CHANNEL.md"
        if old_memory.exists() and not new_memory.exists():
            shutil.copy2(old_memory, new_memory)
            logger.info("Migrated channel memory %s → %s", old_memory, new_memory)

        logger.debug("Ensured channel directories for %s via mount", conversation_token)
        return True
    else:
        path = get_channel_memories_path(conversation_token)
        if not _rclone_mkdir(config.rclone_remote, path):
            if not _rclone_path_exists(config.rclone_remote, path):
                return False
        return True


def read_channel_memory(config: "Config", conversation_token: str) -> str | None:
    """
    Read the channel's memory file (mount-aware).

    Returns the content of the memory file, or None if it doesn't exist or is empty.
    """
    if config.use_mount:
        memory_path = _get_mount_path(config, get_channel_memory_path(conversation_token))
        if not memory_path.exists():
            return None
        content = memory_path.read_text()
        if not content.strip():
            return None
        return content
    else:
        memory_path = get_channel_memory_path(conversation_token)
        content = _rclone_cat(config.rclone_remote, memory_path)
        if content is None or not content.strip():
            return None
        return content


def init_channel_memory(config: "Config", conversation_token: str) -> bool:
    """
    Initialize the channel's memory file with a template (mount-aware).

    Returns True on success.
    """
    if config.use_mount:
        memory_path = _get_mount_path(config, get_channel_memory_path(conversation_token))
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(CHANNEL_MEMORY_TEMPLATE)
        return True
    else:
        return _rclone_rcat(
            config.rclone_remote,
            get_channel_memory_path(conversation_token),
            CHANNEL_MEMORY_TEMPLATE,
        )


# =============================================================================
# Nextcloud OCS sharing functions
# =============================================================================


def share_folder_with_user(config: "Config", folder_path: str, user_id: str) -> bool:
    """
    Share a folder with a Nextcloud user via the OCS Sharing API.

    Creates a user share (shareType=0) with full permissions (read+write).
    Idempotent: checks existing shares first.

    Delegates to nextcloud_client.ocs_share_folder.
    """
    from .nextcloud_client import ocs_share_folder
    return ocs_share_folder(config, folder_path, user_id)
