---
name: money
triggers: [accounting, ledger, beancount, invoice, invoicing, expense, transaction, balance, tax, wash sale, bookkeeping, finances, billing, receivable, work log, work entry, monarch, sync-monarch, money, moneyman, portfolio, positions, holdings, allocation, asset class, brokerage]
description: Accounting operations (ledger, invoicing, transactions, work log, investment portfolio) — runs in-process via the vendored money package
cli: true
env: [{"var":"MONEY_USER","from":"user_id"},{"var":"MONARCH_SESSION_ID","from":"secret","service":"monarch","key":"session_id","sensitive":true,"fallback_var":"MONARCH_SESSION_ID"},{"var":"MONARCH_CSRFTOKEN","from":"secret","service":"monarch","key":"csrftoken","sensitive":true,"fallback_var":"MONARCH_CSRFTOKEN"}]
---
# Money Accounting Operations

Accounting operations via the in-process `money` package. Supports ledger queries, transaction management, invoicing, and work log tracking.

This is now an in-process facade — no subprocess, no HTTP. The skill imports the vendored `money` package directly and invokes its Click CLI in-process.

Multiple ledgers can be configured. Use `--ledger NAME` to select which ledger to operate on. Without the flag, the default ledger is used.

## CLI commands

Run `istota-skill money --help` (or `istota-skill money <subcommand> --help`) to see the live argument list — the examples below cover the common cases but flags evolve.

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

# Show open lots for a security (experimental — operator must enable `money_tax`)
istota-skill money lots SYMBOL [--ledger NAME]

# Detect wash sale violations (experimental — operator must enable `money_wash_sales`)
istota-skill money wash-sales [--year YYYY] [--ledger NAME]

# Add a transaction
istota-skill money add-transaction --date 2026-02-01 --payee "Whole Foods" --narration "Groceries" --debit Expenses:Food --credit Assets:Bank:Checking --amount 85.50 [--currency USD] [--ledger NAME]

# Edit a transaction by its stable id (recategorize, fix payee/narration/date/amount).
# Locate the id from the transaction list; --old-account/--old-position pick the leg to edit.
# Re-validated with bean-check; rolls back if the edit unbalances the entry.
istota-skill money edit-transaction --id <id> [--account Expenses:Food:Restaurants] [--old-account Expenses:Food --old-position "85.50 USD"] [--payee NAME] [--narration TEXT] [--date YYYY-MM-DD] [--position "-12.50 USD"] [--ledger NAME]

# Backfill stable ids onto legacy transactions (one-time, idempotent; runs automatically)
istota-skill money backfill-ids [--ledger NAME]

# Sync from Monarch Money (syncs all configured profiles by default)
istota-skill money sync-monarch [--dry-run] [--ledger NAME]

# Import from CSV
istota-skill money import-csv /path/to/export.csv --account Assets:Bank:Checking [--tag TAG] [--exclude-tag TAG] [--ledger NAME]

# Run periodic money tasks (Monarch sync + invoice scheduler) — invoked by cron, but usable ad hoc
istota-skill money run-scheduled [--dry-run] [--skip-monarch]
```

All output is JSON with `status: ok|error`.

**Concurrency rule:** mutation commands (`add-transaction`, `edit-transaction`, `backfill-ids`, `sync-monarch`, `import-csv`, `run-scheduled`, `work add/update/remove`, `invoice generate/paid/void/create`, `portfolio import/delete-snapshot/classify/unclassify`, and `portfolio accounts` when it carries a `--set-*`/`--exclude`/`--include` flag) must be called sequentially, never in parallel. Running concurrent writes causes duplicate entries and race conditions. Read-only commands (`list`, `check`, `balances`, `query`, `report`, `lots`, `wash-sales`, `work list`, `invoice list`, `portfolio snapshots/summary/history/diff/symbol/classifications`, and bare `portfolio accounts`) are safe to parallelize.

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

# Stamp a stable uid on any entry lacking one (idempotent; runs automatically on init)
istota-skill money work backfill-ids
```

**Entry identity.** `work list` returns both an `id` (1-based display index) and a
`uid` (stable). The `#N` index is what `work update` / `work remove` take, but it
shifts whenever an entry is inserted before it — so re-run `work list` immediately
before acting on an index, never reuse one from earlier in the conversation. The
web UI addresses entries by `uid` for exactly this reason.

**Hand-editing the year files.** `{workspace}/{BOT_DIR}/money/invoices/work/{year}.toml`
is meant to be hand-editable, but any programmatic write rewrites the whole file
from the serializer:

- Keep the `uid` line when you edit an entry — dropping it orphans the entry
  from the web UI until the next backfill (which assigns a *new* uid).
- Custom keys survive; **comments do not**, and neither do nested tables. Put
  anything you need to keep in a `description` or a scalar key of your own.

## Invoicing config: clients, entities and services

These are managed with the operator CLI (`istota money client|company|service
add|update|remove|list -u USER`) or from the web UI — the Clients tab and the
money settings page. Both surfaces enforce the same rules; four to know before
proposing a change:

- **Two fields are closed sets.** A service's `type` is `hours`, `days`, `flat`
  or `other`; a client's `schedule` is `on-demand` or `monthly`. A value outside
  either set is now rejected rather than stored — `--type hourly` used to be
  accepted and then silently billed as hours, and `--schedule weekly` was
  accepted and then never fired. An existing record that already holds such a
  value stays editable: only a field you actually change is checked.
- **A service any work entry names cannot be deleted.** Removing it would
  unbill that work *and* shrink the rendered total of every past invoice
  containing such an entry, since invoice totals are rebuilt from live config.
  Reassign or remove the entries first. An entity is likewise protected while a
  client names it, a work entry pins it, or it is the entity blank-entity
  clients bill under. Deleting a *client* is allowed — its entries and invoices
  survive, showing the raw key instead of a name. If a year file holds a row
  this version can't read, the two strict deletes refuse rather than counting
  it as zero: fix the row first.
- **Client keys are lowercase.** Work entries store the client lowercased, so a
  mixed-case key matches no entry and that client's work is never billed. Entity
  and service keys are unconstrained.
- **The key is the identity and cannot be renamed.** Work entries reference
  clients and services by key, and clients reference entities by key. To change
  one, create the new record, repoint what refers to it, then delete the old.

## Portfolio commands (positions snapshots)

Point-in-time investment portfolio state, imported from Fidelity "Portfolio
Positions" CSV exports (any format revision) or fina's history file. Snapshots,
not transactions — nothing here touches the beancount ledgers.

```bash
# Import a positions CSV the user supplied. The format is auto-detected;
# --source forces a parser when detection fails.
istota-skill money portfolio import /path/to/Portfolio_Positions.csv \
    [--source fidelity-positions-csv|fina-history-csv] [--dry-run] [--replace SNAPSHOT_ID]

# List imported snapshots (newest first, totals exclude excluded accounts)
istota-skill money portfolio snapshots

# Current state: total value, allocation by asset class / account / account
# type / group / geography, aggregated holdings with P&L
istota-skill money portfolio summary [--snapshot ID] [--group Retirement]

# Value over time, optionally stacked
istota-skill money portfolio history [--group-by total|group|account_type|asset_class] [--group G]

# What changed between two snapshots (opened / closed / changed positions)
istota-skill money portfolio diff OLDER_ID NEWER_ID

# One symbol's quantity/price/value across snapshots
istota-skill money portfolio symbol VTI

# Account registry — run it bare to read the rows and their ids, which every
# mutating flag below takes
istota-skill money portfolio accounts [--set-group ID GROUP] [--set-type ID TYPE] [--exclude ID] [--include ID]

# Symbol classifications: list what's on file, set one, remove one
istota-skill money portfolio classifications
istota-skill money portfolio classify GOOG --asset-class Stocks [--sub-class Technology] [--geography US]
istota-skill money portfolio unclassify GOOG

# Auto-classify everything still unclassified (public ticker metadata lookup,
# then description heuristics; never overwrites an existing classification)
istota-skill money portfolio autoclass

# Hard-delete a snapshot (irreversible; requires the flag)
istota-skill money portfolio delete-snapshot ID --confirmed
```

**Answering a question about the portfolio.** `summary` covers most of them: it
returns `total_value` plus `by_asset_class`, `by_account`, `by_account_type`,
`by_group` and `by_geography` — each a list of `{key, value, pct}` sorted by
value — and `holdings`, with per-symbol quantity, cost basis, `gain` and
`gain_pct`. Reach for `history` for "how has it changed", `diff` for "what moved
between these two dates", and `symbol` for one ticker. Symbols are normalized
(`SPAXX**` → `SPAXX`), so either spelling works.

**Account groups are free-form.** A group is any label — an owner, a household
member, a purpose — not a fixed set. Every account flag takes the numeric
account **id**, never the account name, so run `accounts` bare first to read the
ids. `account_type` is guessed once from the account name when the account is
first seen and is the user's thereafter: a wrong guess stays wrong until
`--set-type` fixes it.

**Excluding an account hides it from every total.** `--exclude ID` keeps the
account and its positions imported but drops them from every summary, chart,
history point and snapshot total; `--include ID` reverses it. That is the right
tool for an account that isn't really part of the portfolio — someone else's
money, a pass-through cash account — rather than deleting snapshots.

**Classifications are retroactive.** `classify` writes one row per symbol and
nothing is stamped onto the stored positions, so classifying a symbol today
reclassifies every past snapshot the next time it is read. 30 common symbols
ship pre-classified; `classifications` lists what is on file, seeded rows
included. Cash and options are recognized automatically and need no row.
`--asset-class` is required; `--sub-class` and `--geography` are free text and
default to empty. Anything unrecognized reports as `Unclassified` — when
`summary` shows an `Unclassified` slice, or `import` returns
`unclassified_symbols`, offer to classify those symbols rather than leaving the
slice as is.

**New symbols classify themselves on import.** An import returns
`auto_classified` beside `unclassified_symbols`; what is left in the latter
resisted both the ticker lookup and the offline description heuristics. Run
`portfolio autoclass` later to retry those — its response carries
`lookups_available: false` when the ticker lookup is unavailable or the operator
has it switched off — or offer to classify the few by hand. A user's explicit
`classify` always wins: an automatic write is an insert-if-absent, so it cannot
replace an existing row whatever its value, including one deliberately set to
"Unclassified".

**Importing is safe to repeat.** Re-importing an identical file is a no-op that
returns `status: "duplicate"` with the existing snapshot id — a success, not an
error. `--dry-run` parses and previews without touching the database. A fina
history file holds many dates and imports as several snapshots, returning
`{"imported": N, "duplicates": M, "results": [...]}` rather than a single
result. Use `--replace ID` for a same-day re-export whose contents changed;
parsing happens before the delete, so a file that fails to parse leaves the old
snapshot intact.

Import only files the user supplied or named. Deleting a snapshot is
irreversible: confirm with the user first.

## Estimated taxes

The quarterly estimate lives on the web page at `/money/taxes`, and its rates
are managed at `/money/settings/taxes`. There is no skill subcommand for it.

If the user asks about it, three things are worth knowing:

- **Rates are bundled data with a citation, not fetched.** Each year names the
  document it came from and the date it was last verified. If a figure looks
  wrong, the answer is to check it against that authority and override it in
  settings — not to look it up online and assert a number.
- **The page says when its figures are stale.** If it is showing one year's
  rates for another year, it says so in a banner and in the footnote under the
  breakdown. Read those before doubting the arithmetic.
- **It is an estimate and not tax advice.** Local taxes, credits, AMT,
  itemized deductions and several 2025-onward federal deductions are not
  modelled; the disclaimer on the page lists them. Do not tell the user what
  they owe, or reassure them a figure is correct. Point them at the page, its
  provenance footnote, and the authority it cites.

Never invent a bracket, rate or deduction from memory to "help" — that is the
exact failure the provenance fields exist to prevent.

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
| `MONEY_USER` | User id — set automatically from the task's user_id |

## Workspace layout

Money is a default-on module — no per-user resource declaration is needed. Opt out via the user's `disabled_modules` profile field.

The skill resolves `{workspace}/{BOT_DIR}` as the money workspace and synthesizes a `UserContext` rooted there. The user's config lives under `{workspace}/{BOT_DIR}/money/config/` as `INVOICING.md` / `TAX.md` / `MONARCH.md` (each with a fenced ```toml block). Ledger files are auto-discovered from `{workspace}/{BOT_DIR}/money/ledgers/*.beancount` (top-level only). Monarch credentials live in the encrypted `secrets` table.
