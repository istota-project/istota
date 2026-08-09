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

## Business

The web dashboard's Business section is **Work | Invoices | Clients**. Work is a full CRUD surface over the file-based work-entry store — entries addressed by stable id, with per-entry etags so a concurrent agent edit conflicts loudly instead of being silently reverted. Clients, together with the money settings page, is the CRUD surface over the invoicing config (clients, entities, services), so nothing about invoicing needs the CLI.

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
