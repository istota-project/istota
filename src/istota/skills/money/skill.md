---
name: money
triggers: [accounting, ledger, beancount, invoice, invoicing, expense, transaction, balance, tax, wash sale, bookkeeping, finances, billing, receivable, work log, work entry, monarch, sync-monarch, money, moneyman]
description: Accounting operations (ledger, invoicing, transactions, work log) — runs in-process via the vendored money package
cli: true
resource_types: [money, moneyman]
env: [{"var":"MONEY_CONFIG","from":"user_resource_config","resource_type":"money","field":"config_path"},{"var":"MONEY_USER","from":"user_id"}]
---
# Money Accounting Operations

Accounting operations via the in-process `money` package. Supports ledger queries, transaction management, invoicing, and work log tracking.

This is now an in-process facade — no subprocess, no HTTP. The skill imports the vendored `money` package directly and invokes its Click CLI in-process.

Multiple ledgers can be configured. Use `--ledger NAME` to select which ledger to operate on. Without the flag, the default ledger is used.

## CLI commands

```bash
# List available ledgers
istota-skill money list

# Validate ledger
istota-skill money check [--ledger NAME]

# Show account balances
istota-skill money balances [--ledger NAME] [--account PATTERN]

# Run a BQL query
istota-skill money query "SELECT date, narration, account, position WHERE account ~ 'Expenses:Food' ORDER BY date DESC LIMIT 10" [--ledger NAME]

# Generate financial reports
istota-skill money report income-statement [--year YYYY] [--ledger NAME]
istota-skill money report balance-sheet [--year YYYY] [--ledger NAME]

# Show open lots for a security
istota-skill money lots SYMBOL [--ledger NAME]

# Detect wash sale violations
istota-skill money wash-sales [--year YYYY] [--ledger NAME]

# Add a transaction
istota-skill money add-transaction --date 2026-02-01 --payee "Whole Foods" --narration "Groceries" --debit Expenses:Food --credit Assets:Bank:Checking --amount 85.50 [--currency USD] [--ledger NAME]

# Sync from Monarch Money (syncs all configured profiles by default)
istota-skill money sync-monarch [--dry-run] [--ledger NAME]

# Import from CSV
istota-skill money import-csv /path/to/export.csv --account Assets:Bank:Checking [--tag TAG] [--exclude-tag TAG] [--ledger NAME]
```

All output is JSON with `status: ok|error`.

**Concurrency rule:** mutation commands (`add-transaction`, `sync-monarch`, `import-csv`, `work add/update/remove`, `invoice generate/paid/void/create`) must be called sequentially, never in parallel. Running concurrent writes causes duplicate entries and race conditions. Read-only commands (`list`, `check`, `balances`, `query`, `report`, `lots`, `wash-sales`, `work list`, `invoice list`) are safe to parallelize.

## Adding transactions

Never manually type amounts into ledger files. Use CLI commands:

- **User tells you a specific amount**: use `add-transaction` with exact amount
- **Import from bank/Monarch export**: use `import-csv` or `sync-monarch` (syncs all profiles when no `--ledger` specified)
- **Check balances/transactions**: use `query` or `balances`

## Invoice commands

```bash
# Generate invoices for a billing period
istota-skill money invoice generate --period 2026-02 [--client acme] [--entity ENTITY] [--dry-run]

# List invoices (outstanding by default)
istota-skill money invoice list [--client acme] [--all]

# Record invoice payment (cash-basis: income recognized at payment time)
istota-skill money invoice paid INV-000001 --date 2026-02-15 [--bank Assets:Bank:Savings] [--no-post] [--ledger NAME]

# Create a manual single invoice
istota-skill money invoice create acme --service consulting --qty 40
istota-skill money invoice create acme --item "Travel expenses 340.50"

# Void an invoice (clears work entries, optionally deletes PDF)
istota-skill money invoice void INV-000001 [--force] [--delete-pdf]
```

Cash-basis accounting: no ledger entries at invoice time; income recognized when payment is recorded via `invoice paid`. Use `--no-post` when the bank transaction was already imported.

## Work log commands

```bash
# List work entries
istota-skill money work list [--client acme] [--period 2026-02] [--uninvoiced] [--invoiced]

# Add a work entry
istota-skill money work add --date 2026-02-01 --client acme --service consulting --qty 4 [--description "Architecture review"] [--amount 100] [--discount 10] [--entity ENTITY]

# Update a work entry
istota-skill money work update 5 [--qty 8] [--description "Updated"]

# Remove an uninvoiced work entry
istota-skill money work remove 3
```

## BQL query examples

```sql
-- Monthly expense summary
SELECT month, sum(position) WHERE account ~ '^Expenses:' GROUP BY month

-- Top merchants this year
SELECT payee, sum(position) WHERE year = 2026 AND account ~ '^Expenses:' GROUP BY payee ORDER BY sum(position) DESC LIMIT 10

-- Recent transactions
SELECT date, payee, narration, account, position WHERE date >= 2026-01-01 ORDER BY date DESC LIMIT 20

-- Open positions
SELECT account, units(sum(position)), cost(sum(position)) WHERE account ~ '^Assets:Investment' GROUP BY account
```

## Wash sale rules

A wash sale occurs when you sell a security at a loss and buy substantially identical securities within 30 days before or after. The `wash-sales` command scans for violations. Disallowed losses must be added to the cost basis of the replacement shares.

## Environment variables

| Variable | Description |
|---|---|
| `MONEY_CONFIG` | Path to money config file (from the user's `money` resource) |
| `MONEY_USER` | User key in the money config — defaults to the istota user_id |

## Adding the money resource

Add to user config (`config/users/{user}.toml`):

```toml
[[resources]]
type = "money"
name = "Money"
config_path = "/etc/istota/money/config.toml"
```

`MONEY_USER` defaults to the istota user_id (the per-user TOML basename). Override with `user_key = "alice"` on the resource if the money config uses a different key.

The `moneyman` resource type is also accepted for backward compatibility with the legacy out-of-process integration.
