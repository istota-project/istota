# Money

Beancount-backed accounting with a web dashboard: ledger queries, transactions, invoicing, a work log, quarterly tax estimates, and investment portfolio tracking. The `money` package is vendored in-tree and runs in-process — no external service, no HTTP hop, no subprocess.

The `money` module is on by default. Opt out per user with `disabled_modules = ["money"]`.

## Ledgers

Istota auto-discovers `*.beancount` files at the top level of `{workspace}/ledgers/`. There is no per-resource path to declare.

```bash
istota-skill money list                      # available ledgers
istota-skill money check                     # bean-check a ledger
istota-skill money balances                  # account balances
istota-skill money query "<bql>"             # BQL query
istota-skill money report                    # financial report
istota-skill money add-transaction …
istota-skill money edit-transaction …        # by stable id
istota-skill money import-csv …
```

The same operations are reachable operator-side as `istota money <op> -u USER`. See the [CLI reference](../reference/cli.md#money).

## Transaction sync

Monarch Money sync uses a stored cookie pair (`session_id`, `csrftoken`) in the encrypted secrets table — both keys are required. `debug-monarch` is a whoami probe for checking the credentials are still live before blaming the sync.

```bash
istota secret ensure --user alice --service monarch --key session_id --value …
istota secret ensure --user alice --service monarch --key csrftoken  --value …
```

Imports are content-hash deduped, so re-running one is safe.

### Transaction rules

Which accounts an imported transaction posts to is decided by one ordered rule table, shared by every importer. A rule matches one field of the normalized transaction (`category`, `account`, `payee`, `notes` or `tag`) by `exact`, `iexact` (the default, case-insensitive equality) or `contains`, and fills one action slot: `posting_account`, `contra_account`, or `skip` to drop the transaction outright. Scope is a ledger and a source (`monarch-api`, a CSV import); `''` in either column means "any", which is why both have to be typed rather than defaulted into on a write.

`priority` alone decides order — lower first, 100 by default. A single pass runs over the rules in scope and each slot is filled by the first rule that matches it, so one transaction can take its posting account from one rule and its contra account from another; a matching `skip` ends the pass. Nothing is ranked implicitly: a `contains` rule does not lose to an `exact` one. An unfilled slot falls back to what the importer did before rules existed — `Expenses:Uncategorized:<slug>` for the posting account, the profile's default account (or the CSV import's `--account`) for the contra account.

The old Monarch category map, account map and exclude-tag filters are views over the same table, and keep working; `monarch-category-map` is still the shorter thing to type for a plain category-to-account mapping. Rules carry an `origin` of `user`, `migrated` or `seed` so a hand-written rule is distinguishable from a migrated map entry or the shipped default.

Edit them in the money settings page under **Transactions**, which also shows what recent imports matched and what fell through, or from the CLI:

```bash
istota money rules list   -u USER [--ledger NAME] [--source monarch-api] [--enabled-only]
istota money rules add    -u USER --ledger personal --source monarch-api \
                          --field category --match-value "Software" \
                          --action posting_account --target Expenses:Business:Software
istota money rules update -u USER --id 7 --target Expenses:Business:Tools
istota money rules remove -u USER --id 7
istota money rules test   -u USER --ledger personal --source monarch-api --category Software
```

`test` resolves a transaction you describe on the command line against the stored rules and reports what they would do with it, importing nothing. It refuses on a deployment where the one-time migration from the legacy maps has not completed, since a preview drawn from the table would describe behaviour the deployment does not have. Editing a rule does not move transactions already in a ledger — correct those with `edit-transaction`.

The agent-side surface is `istota-skill money transaction-rules list|set|test`; it has no delete verb, so removing a rule is an operator command.

### Settling invoices from the feed

Automatic payment detection starts disabled for every billing entity, including entities created before this setting existed. To enable it, open Money → Settings → Invoicing → Edit entity, switch on “Automatically detect invoice payments”, then choose a ledger and its income account. The income account is the `Income:*` revenue account invoice payments are booked to. Declared accounts are available even before their first posting. Changing the ledger clears the old account; when there is exactly one eligible income account, it is selected for you. Save applies the settings to this entity independently of the page-wide ledger selection.

A new credit must match the entity’s selected ledger, exact income account and receiving bank account. The bank account remains the separate `Assets:*` condition; these settings do not change it or manual payment posting. The normal negative revenue posting in a payment is expected: matching uses the positive imported bank credit. Refunds and debits do not become payments. A profile name is not a ledger name, and identical account names in different ledgers do not match each other.

The operator CLI accepts the same settings on `company add`, `update` and the idempotent `ensure` command:

```bash
istota money company ensure -u alice --key acme \
  --payment-detection-enabled --payment-detection-ledger business \
  --payment-detection-income-account Income:Consulting
istota money company update -u alice --key acme --no-payment-detection-enabled
```

Config import/export uses `payment_detection_enabled`, `payment_detection_ledger` and `payment_detection_income_account` under `[companies.acme]`. A merge import preserves omitted company fields; `--replace` uses defaults for omitted fields. Imports and dry runs validate the selected pair against the user’s ledgers, canonicalize the ledger name case-insensitively, and require an exact declared `Income:*` account. Exports include the enabled flag even when false so reimporting a disabled configuration disables detection.

Switching detection off keeps its selections, even if the saved ledger has disappeared. Existing reviews and settlement history remain visible, and saved reviews can still be settled or dismissed after settings change. Invalid saved settings skip that entity with a diagnostic while the ledger sync completes. Enabling detection does not backfill old credits; legacy match rows without ledger and income-account metadata remain historical. Physical bank accounts mapped to the same ledger, bank and income accounts remain indistinguishable.

A sync books incoming payments into the ledger. It also closes the invoice each one paid, where it can say which: a credit that fits exactly one open invoice — same amount, issued no later than the payment — marks that invoice paid. It records the payment directly rather than routing through `invoice paid`, because the sync has already written the income to the ledger and going through the command would post it twice.

Anything ambiguous is reported for you to settle rather than guessed at. Two open invoices at the same amount, two credits fitting one invoice, or a credit that fits to the cent but predates the invoice — all are named in the output and left alone. A credit is compared with the total stored when the invoice was issued. Paid and voided invoices are excluded from matching, and so is any invoice with a line whose service has left the config, a manual invoice's free-amount lines included.

`--tolerance` allows for a wire fee. `--no-match-invoices` turns matching off for the run. `--match-invoices` cannot override a disabled entity. `invoice unpaid` undoes a match that was wrong. `invoice void` is not the inverse: it voids the invoice and releases its work to be billed again.

Matching runs once across all sync profiles rather than once per profile. `sync_all_profiles` fetches from Monarch once and dedups per profile, so two profiles can each book a credit fitting the same invoice; a per-profile pass would let whichever ran first settle it and leave the other unreported, with profile order deciding the winner.

## Business

The web dashboard's Business section is **Work | Invoices | Clients**. Work is a full CRUD surface over the work log: entries addressed by stable id, with per-entry etags so a concurrent agent edit conflicts loudly instead of being silently reverted. Invoices lists every invoice, voided ones included, and is where invoices are generated, created by hand and voided. Clients, together with the money settings page, is the CRUD surface over the invoicing config (clients, entities, services), so nothing about invoicing needs the CLI.

### Work entries and invoices

Work entries and invoices live in the per-user money database. Every issued invoice number has a row there that is never deleted, holding the client, entity, issue date, the total computed when it was issued, its status (open, paid or voided) and the entries it covers. An entry counts as billed while an open or paid invoice lists it, and the database refuses a second active invoice for the same entry. Generating, voiding, marking paid or unpaid and creating an invoice each happen in one transaction. A generated PDF is written first and deleted again if its invoice cannot be recorded, so no PDF is left without an invoice behind it.

Numbers are never reused. The next number is above every number on record, voided ones included, and never below `next_invoice_number` in the invoicing config.

A stored total does not change when a service's rate changes later, so an invoice keeps showing what it billed. Payment matching uses the stored total too.

### Generating invoices

`invoice generate --period YYYY-MM` bills every unbilled entry dated on or before the end of that month, earlier months included. A client's work for one entity goes on one invoice, split by the client's bundles and the services it bills separately. An entry flagged **Invoice separately** (a checkbox on the work form, `--separate` on `istota money work add` and `update`) always gets an invoice of its own.

`invoice generate --entry UID` bills exactly the named entries and ignores `--period`; repeat `--entry` for more. Every named entry must exist and be unbilled, or nothing is written. In the web UI an unbilled entry's menu has **Generate invoice**, and the Invoices page has **Generate invoices** with optional client, entity and period filters. Both show a preview of the invoices, totals and entries before anything is issued. From the work page the issue is refused when the entry changed since the page loaded; from the Invoices page, a result that differs from the preview is shown as an error with the invoices that were issued. No email is sent to clients from either surface. **New manual invoice** takes the same lines as `invoice create`.

### Voiding

`invoice void NUMBER [--reason TEXT] [--replaced-by N,N] [--force] [--delete-pdf]`, or **Void invoice…** on an invoice's menu, voids the invoice and releases its entries so they can be billed again. The invoice stays on record with its total, the date it was voided, the reason and the invoices that replace it. Every replacement number must already exist; nothing links a void to its replacements automatically. A paid invoice needs `--force` (in the web dialog, a second checkbox) and keeps a note that it had been paid. Voiding an invoice twice is refused.

The PDF is kept and renamed with a `-VOID` suffix so it cannot be mistaken for a live invoice, unless `--delete-pdf` is given. A voided invoice still lists the entries it covered. Those entries can be edited and billed again but not removed, since the voided invoice keeps its record of them. `invoice list` and the Invoices page show voided invoices, muted with a Voided badge in the web UI, and leave them out of the outstanding count. `invoice list --active` hides them.

On the Invoices page, **Preview** opens an invoice's PDF in the file viewer, and **Download PDF** saves it. Neither is offered when the invoice has no PDF.

### Invoice dates

Each invoice row holds its issue date. Generation stores the date it put on the PDF rather than taking a fresh `date.today()`, which would disagree with the document across midnight. The matcher and both invoice lists read that one date.

Invoices raised before issue dates were recorded were given a fallback date when they were imported: the latest work billed on them, which is also what their listed date shows.

### Moving from the yearly work files

Older installs kept the work log in `invoices/work/{year}.toml` files and had no invoice records. The first time the work log is used after the upgrade, those files are imported into the money database, building one invoice record per invoice number the entries carried. The import only goes ahead when it can re-read everything it wrote and match it field by field against the files. A voided invoice from before the upgrade left nothing in the files to rebuild, so its number stays a gap.

After a successful import each file is renamed `{year}.toml.migrated` in place, unchanged. Nothing reads the renamed files while the database holds the work log. A `{year}.toml` that appears again later, from a sync conflict or a restored copy, is renamed aside the same way and never read.

The renamed files are the copy to go back to if `money.db` is lost or restored from a backup taken before the import. The database then has no record of the import, and the import refuses rather than start an empty work log while a `{year}.toml.migrated` has no `{year}.toml` beside it. Rename each `{year}.toml.migrated` back to `{year}.toml` and run `istota money work migrate -u USER`.

The import refuses, writing nothing to the database and leaving the files untouched, when a year file cannot be read, an entry has no uid, two entries share a uid, an entry has a paid date but no invoice, or the entries on one invoice disagree on the paid date. Until it is fixed, every work and invoice command returns `work log migration blocked: <reason>` and the web money routes answer 503 with the same text. Fix it with the operator CLI:

```bash
istota money work backfill-ids -u USER    # gives every entry a uid
istota money work migrate -u USER --dry-run   # check the import, write nothing
istota money work migrate -u USER         # run it
```

### Backups

`money.db` is one of the per-user module databases the scheduled database backup snapshots, daily by default, so the work log and invoice records are backed up with everything else in it. The snapshots go to dated directories under `Backups/db/snapshots/` in the workspace (or `[scheduler] db_backup_dir`), the newest seven are kept by default (`db_backup_retention`), and `db_restore` puts one back. See [Database](../architecture/database.md).

## Taxes

A quarterly estimated-tax calculator at `/money/taxes` — the one page in the module that computes a number you send to a government, which shapes how it is built.

Rate data is versioned data, not constants: a bundled registry carries federal and per-state years, each naming the document it was transcribed from and the date it was last checked. Three signals fall out of that provenance and render on the page: attribution per jurisdiction, a **missing-year warning** when resolution fell back to a different year, and an **age warning** when the last check predates the tax year. A year the authority has not published yet resolves to the previous year's table *and says so*, rather than reporting last year's numbers as this year's.

State tax is a real dimension rather than a California special case: `state = ""` (the default) means no state tax, and an unsupported code returns an explicit reason (`no_income_tax`, `no_brackets`, `unknown_state`) so the page drops the state column entirely instead of rendering a misleading zero. Installment schedules are per-state.

Rates are deliberately not fetched from anywhere. The IRS publishes nothing machine-readable, and fetching would not have prevented the class of bug this design targets — a tax year whose *structure* changed returns current-looking thresholds from a bracket API and still produces a wrong answer. What is wanted is knowing when the numbers are stale, which needs no dependency.

A disclaimer naming what is not modelled — local taxes, credits, AMT, itemizing — renders persistently on both the estimate and its settings page.

## Portfolio

Point-in-time investment tracking. Fidelity Portfolio Positions CSV exports import as **snapshots** into the per-user money DB, content-hash deduped. Snapshots never touch the beancount ledgers.

The account registry and symbol classifications are per-user data, auto-populated on import and editable at `/money/settings/portfolio`. Classification resolves at read time, so an edit retroactively reclassifies history. New symbols auto-classify on import via ticker-metadata lookup then offline description heuristics; an automatic write can never replace an existing row, so a user edit always wins — including a deliberate `Unclassified`. `[money] autoclass_lookup` gates the third-party lookup.

```bash
istota-skill money portfolio <import|snapshots|summary|history|diff|accounts|classify>
```

The web tab is **Overview | History | Import**: allocation charts, holdings, value over time, and snapshot diffs.

## Experimental

Two operations are behind operator feature flags — `lots` (tax lots, `money_tax`) and `wash-sales` (`money_wash_sales`). See [experimental features](../EXPERIMENTAL.md).

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `[money] autoclass_lookup` | `true` | Allow portfolio auto-classification to look up unknown symbols |

Everything else — clients, entities, services, tax config, portfolio accounts — lives in the per-user money DB, not in `config.toml`.

A review saves its chosen invoice and marks it paid in one transaction. If settlement is interrupted, retry the same credit and invoice with `invoice review settle`; it finishes that decision without paying another candidate. A refusal before any invoice is chosen leaves the other candidates available.
