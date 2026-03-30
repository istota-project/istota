# Moneyman Accounting Operations

Accounting operations via the Moneyman API service. Supports ledger queries, transaction management, invoicing, and work log tracking.

Multiple ledgers can be configured on the server. Use `--ledger NAME` to select which ledger to operate on. Without the flag, the server uses its default ledger.

## CLI commands

```bash
# List available ledgers
istota-skill moneyman list

# Validate ledger
istota-skill moneyman check [--ledger NAME]

# Show account balances
istota-skill moneyman balances [--ledger NAME] [--account PATTERN]

# Run a BQL query
istota-skill moneyman query "SELECT date, narration, account, position WHERE account ~ 'Expenses:Food' ORDER BY date DESC LIMIT 10" [--ledger NAME]

# Generate financial reports
istota-skill moneyman report income-statement [--year YYYY] [--ledger NAME]
istota-skill moneyman report balance-sheet [--year YYYY] [--ledger NAME]

# Show open lots for a security
istota-skill moneyman lots SYMBOL [--ledger NAME]

# Detect wash sale violations
istota-skill moneyman wash-sales [--year YYYY] [--ledger NAME]

# Add a transaction
istota-skill moneyman add-transaction --date 2026-02-01 --payee "Whole Foods" --narration "Groceries" --debit Expenses:Food --credit Assets:Bank:Checking --amount 85.50 [--currency USD] [--ledger NAME]

# Sync from Monarch Money
istota-skill moneyman sync-monarch [--dry-run] [--ledger NAME]

# Import from CSV
istota-skill moneyman import-csv /path/to/export.csv --account Assets:Bank:Checking [--tag TAG] [--exclude-tag TAG] [--ledger NAME]
```

All output is JSON with `status: ok|error`.

## Adding transactions

Never manually type amounts into ledger files. Use CLI commands:

- **User tells you a specific amount**: use `add-transaction` with exact amount
- **Import from bank/Monarch export**: use `import-csv` or `sync-monarch`
- **Check balances/transactions**: use `query` or `balances`

## Invoice commands

```bash
# Generate invoices for a billing period
istota-skill moneyman invoice generate --period 2026-02 [--client acme] [--entity ENTITY] [--dry-run]

# List invoices (outstanding by default)
istota-skill moneyman invoice list [--client acme] [--all]

# Record invoice payment (cash-basis: income recognized at payment time)
istota-skill moneyman invoice paid INV-000001 --date 2026-02-15 [--bank Assets:Bank:Savings] [--no-post] [--ledger NAME]

# Create a manual single invoice
istota-skill moneyman invoice create acme --service consulting --qty 40
istota-skill moneyman invoice create acme --item "Travel expenses 340.50"
```

Cash-basis accounting: no ledger entries at invoice time; income recognized when payment is recorded via `invoice paid`. Use `--no-post` when the bank transaction was already imported.

## Work log commands

```bash
# List work entries
istota-skill moneyman work list [--client acme] [--period 2026-02] [--uninvoiced] [--invoiced]

# Add a work entry
istota-skill moneyman work add --date 2026-02-01 --client acme --service consulting --qty 4 [--description "Architecture review"] [--amount 100] [--discount 10] [--entity ENTITY]

# Update a work entry
istota-skill moneyman work update 5 [--qty 8] [--description "Updated"]

# Remove an uninvoiced work entry
istota-skill moneyman work remove 3
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
| `MONEYMAN_API_URL` | Moneyman service URL (HTTP fallback) |
| `MONEYMAN_API_SOCKET` | Unix socket path (preferred, used when set) |
| `MONEYMAN_API_KEY` | API key for authentication |

Connection priority: `MONEYMAN_API_SOCKET` (Unix socket) is used when set, otherwise falls back to `MONEYMAN_API_URL` (HTTP).

## Adding the moneyman resource

Add to user config (`config/users/{user}.toml`):

```toml
[[resources]]
type = "moneyman"
name = "Moneyman"
socket_path = "/run/moneyman/api.sock"
api_key = "your-api-key"
```
