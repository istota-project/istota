# Beancount Accounting Operations

Multiple ledgers can be configured (e.g., personal, business, trading). Use `--ledger NAME` to select which ledger to operate on. Without the flag, the first/default ledger is used.

```bash
# List available ledgers
python -m istota.skills.accounting list

# Work with a specific ledger
python -m istota.skills.accounting --ledger "Business" balances
python -m istota.skills.accounting -l "Trading" wash-sales
```

Environment variables:
- `LEDGER_PATH`: Default ledger path (first configured ledger)
- `LEDGER_PATHS`: JSON array of all ledgers `[{"name": "...", "path": "..."}]`
- `INVOICING_CONFIG`: Path to INVOICING.md config file (for invoice commands)

The user's beancount directory structure:

```
{ledger directory}/
├── main.beancount         # Master file (includes others)
├── accounts.beancount     # Chart of accounts
├── prices.beancount       # Stock/crypto prices (optional)
├── transactions/
│   └── 2026.beancount     # Year files
├── imports/               # CLI creates staging files here
└── invoices/generated/    # Invoice PDFs organized by year
    └── 2026/
```

## CLI Commands

Always validate the ledger after making changes:

```bash
# Validate ledger
python -m istota.skills.accounting check

# Show all account balances
python -m istota.skills.accounting balances

# Filter balances by account pattern
python -m istota.skills.accounting balances --account "Assets:Bank"

# Run a BQL query
python -m istota.skills.accounting query "SELECT date, narration, account, position WHERE account ~ 'Expenses:Food' ORDER BY date DESC LIMIT 10"

# Generate income statement for current year
python -m istota.skills.accounting report income-statement

# Generate balance sheet
python -m istota.skills.accounting report balance-sheet --year 2025

# Show open lots for a security
python -m istota.skills.accounting lots AAPL

# Detect wash sale violations
python -m istota.skills.accounting wash-sales --year 2025

# Import from Monarch Money CSV
python -m istota.skills.accounting import-monarch /path/to/export.csv --account Assets:Bank:Checking
```

Output is JSON with `status: ok|error`:

```json
{
  "status": "ok",
  "account_count": 45,
  "balances": [
    {"account": "Assets:Bank:Checking", "sum(position)": "5234.56 USD"},
    {"account": "Assets:Investment:Brokerage", "sum(position)": "12500.00 USD, 100 VTI"}
  ]
}
```

## Adding Transactions

**IMPORTANT: Never manually type amounts into ledger files.** Use CLI commands to ensure accuracy:

### For single transactions:
```bash
python -m istota.skills.accounting add-transaction \
  --date 2026-02-01 \
  --payee "Whole Foods" \
  --narration "Weekly groceries" \
  --debit "Expenses:Food:Groceries" \
  --credit "Assets:Bank:Checking" \
  --amount 85.50
```

### For bulk imports:
```bash
python -m istota.skills.accounting import-monarch export.csv --account Assets:Bank:Checking
```

### When to use each approach:
- **User tells you a specific amount** → Use `add-transaction` with exact amount
- **Import from bank/Monarch export** → Use `import-monarch`
- **User asks about balances/transactions** → Use `query` or `balances`

### What NOT to do:
- Never type `45.32 USD` directly into a .beancount file
- Never calculate totals manually - use `query` to get sums
- Never "fix" amounts by editing files - add correcting transactions instead

### Securities with Cost Basis

For investment transactions with cost tracking, use direct file editing with care:

```beancount
; Purchase with cost tracking
2026-01-15 * "Buy VTI"
  Assets:Investment:Brokerage    10 VTI {250.00 USD}
  Assets:Bank:Checking          -2500.00 USD

; Sale (FIFO by default, or specify lot)
2026-06-15 * "Sell VTI"
  Assets:Investment:Brokerage   -5 VTI {250.00 USD} @ 275.00 USD
  Assets:Bank:Checking          1375.00 USD
  Income:Investment:Gains       -125.00 USD
```

### Account Structure

Standard hierarchy:
- `Assets:` - Bank accounts, investments, receivables
- `Liabilities:` - Credit cards, loans, payables
- `Income:` - Salary, dividends, interest
- `Expenses:` - All spending categories
- `Equity:` - Opening balances, transfers

## Invoicing System

Config-driven invoicing with work log tracking and PDF generation. Uses cash-basis accounting: no ledger entries at invoice time, income recognized when payment is recorded. Outstanding invoices tracked via the work log. Requires `INVOICING_CONFIG` environment variable pointing to the user's INVOICING.md file.

### Invoicing Config Format (INVOICING.md)

Markdown file with embedded TOML code block:

````markdown
# Invoicing Configuration

```toml
accounting_path = "/path/to/accounting"
work_log = "/path/to/notes/_INVOICES.md"
invoice_output = "invoices/generated"
next_invoice_number = 1

[company]
name = "My Company"
address = "123 Main St\nCity, ST 12345"
email = "billing@company.com"
payment_instructions = "Wire to: ..."

[clients.acme]
name = "Acme Corp"
address = "456 Oak Ave"
email = "billing@acme.com"
terms = 30

[clients.acme.invoicing]
schedule = "monthly"
day = 1
reminder_days = 3        # send reminder N days before generation (0 = disabled)
notifications = "email"  # per-client override: "talk", "email", or "both"
bundles = [
  { services = ["consulting", "development"], name = "Professional Services" }
]
separate = ["expenses"]

[services.consulting]
display_name = "Consulting Services"
rate = 150
type = "hours"

[services.expenses]
display_name = "Reimbursable Expenses"
rate = 0
type = "other"
```
````

### Work Log Format (_INVOICES.md)

````markdown
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
description = "Architecture review"

[[entries]]
date = 2026-02-03
client = "acme"
service = "expenses"
amount = 340.50
description = "Flight to NYC"
```
````

### Invoice CLI Commands

```bash
# Generate invoices for a billing period (creates PDFs only, no ledger entries)
python -m istota.skills.accounting invoice generate --period 2026-02
python -m istota.skills.accounting invoice generate --period 2026-02 --client acme
python -m istota.skills.accounting invoice generate --period 2026-02 --dry-run

# List invoices (outstanding by default)
python -m istota.skills.accounting invoice list
python -m istota.skills.accounting invoice list --client acme
python -m istota.skills.accounting invoice list --all

# Record invoice payment (creates income posting - cash-basis)
python -m istota.skills.accounting invoice paid INV-000001 --date 2026-02-15
python -m istota.skills.accounting invoice paid INV-000001 --date 2026-02-15 --bank "Assets:Bank:Savings"
python -m istota.skills.accounting invoice paid INV-000001 --date 2026-02-15 --no-post

# Create a manual single invoice
python -m istota.skills.accounting invoice create acme --service consulting --qty 40
python -m istota.skills.accounting invoice create acme --item "Travel expenses" 340.50
```

### Invoice Generation Flow

1. Reads `INVOICING_CONFIG` for client/service/company definitions
2. Parses work log entries from the configured work log file
3. Filters by period (YYYY-MM) and optional client
4. Groups entries by client bundle rules (bundled services → one invoice, separate services → individual invoices)
5. Generates PDF via WeasyPrint with invoice number `Invoice-{padded_num}-{MM_DD_YYYY}.pdf`
6. Stamps processed entries with `invoice = "INV-XXXXXX"` in work log
7. Increments `next_invoice_number` in config

No ledger entries are created at invoice time (cash-basis accounting).

### Cash-Basis Income Recognition

Income is recognized when payment is received, not when invoiced:

`invoice paid` creates an income posting and stamps `paid_date` on work log entries:
```beancount
2026-02-15 * "Acme Corp" "Payment for INV-000001"
  Assets:Bank:Checking       6250.00 USD
  Income:Consulting         -5000.00 USD
  Income:Development        -1250.00 USD
```

Use `--no-post` when the bank transaction was already imported (e.g., via Monarch sync) — this stamps `paid_date` without creating a ledger entry.

### Outstanding Invoice Tracking

Outstanding invoices are tracked via the work log:
- Invoiced but unpaid: entry has `invoice = "INV-XXXX"` but no `paid_date`
- Paid: entry has both `invoice` and `paid_date`
- `invoice list` shows outstanding by default, `--all` includes paid

### Service Types

- **hours**: `qty × rate` — hourly billing
- **days**: `qty × rate` — daily billing
- **flat**: Fixed `rate` per entry
- **other**: Uses `amount` from work log entry directly (for expenses, reimbursements)

### Adding Invoicing Resource

Add to user config (`config/users/{user}.toml`):

```toml
[[resources]]
type = "invoicing"
path = "/Users/{user}/{BOT_DIR}/config/INVOICING.md"
name = "Invoicing"
permissions = "write"
```

### Scheduled Invoice Generation

Clients with `schedule = "monthly"` get invoices auto-generated on their configured `day`. The scheduler checks periodically and:

1. Sends a reminder `reminder_days` before generation (set to 0 to disable)
2. Calls `generate_invoices_for_period()` on the schedule day
3. Sends a notification summarizing what was created

Notification surface (talk/email/both) is resolved from: client override > INVOICING.md global `notifications` > user TOML `invoicing_notifications` > default "talk".

Top-level INVOICING.md config:
```toml
notifications = "talk"  # global default for all clients
```

User TOML (`config/users/{user}.toml`):
```toml
invoicing_notifications = "both"
invoicing_conversation_token = "room123"
```

State is tracked in `invoice_schedule_state` DB table to prevent duplicate generation within the same month.

### Overdue Invoice Detection

Unpaid invoices past a configurable threshold trigger a one-time notification. Configured via `days_until_overdue` (0 = disabled):

```toml
# INVOICING.md — global default
days_until_overdue = 30

[clients.acme.invoicing]
days_until_overdue = 15  # per-client override (0 = use global)
```

Resolution: client override > global. Invoice date = max `date` among entries sharing the same invoice number. Overdue when `today > invoice_date + days_until_overdue`. Each overdue invoice is notified once (tracked in `invoice_overdue_notified` DB table). Multiple overdue invoices are consolidated into a single notification per user. Paid invoices (entries with `paid_date`) are ignored.

## Workflow Guidelines

1. **Always validate after edits**: Run `check` command after modifying ledger files
2. **Use staging for imports**: Monarch imports go to `imports/` directory for review
3. **Review before merging**: Check imported transactions before adding to main ledger
4. **Track cost basis**: Use `{cost}` syntax for securities to enable wash sale detection

## Wash Sale Rules

A wash sale occurs when you sell a security at a loss and buy substantially identical securities within 30 days before or after. The `wash-sales` command scans for:
- Sales at a loss in the target year
- Purchases of the same symbol within 30-day window

Violations mean the loss is disallowed for tax purposes and must be added to the cost basis of the replacement shares.

## BQL Query Examples

```sql
-- Monthly expense summary
SELECT month, sum(position) WHERE account ~ '^Expenses:' GROUP BY month

-- Top merchants this year
SELECT payee, sum(position) WHERE year = 2026 AND account ~ '^Expenses:' GROUP BY payee ORDER BY sum(position) DESC LIMIT 10

-- Recent transactions
SELECT date, payee, narration, account, position WHERE date >= 2026-01-01 ORDER BY date DESC LIMIT 20

-- Open positions
SELECT account, units(sum(position)), cost(sum(position)) WHERE account ~ '^Assets:Investment' GROUP BY account

-- Outstanding invoices are tracked via work log, not A/R balances
-- Use: python -m istota.skills.accounting invoice list
```
