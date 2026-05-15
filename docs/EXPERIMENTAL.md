# Experimental feature flags

Operator-scoped flags for features that ship in the tree but are off by default. Set the active list in `config.toml`:

```toml
[experimental]
features = ["money_tax", "money_wash_sales"]
```

Or via Ansible (`group_vars` / `host_vars`):

```yaml
istota_experimental_features:
  - money_tax
  - money_wash_sales
```

Run `uv run istota experimental list` on the host to see the current registry and which flags are on. Unknown names in the TOML list log a warning at startup but don't fail.

These flags are **operator-scoped** — not per-user, not exposed in the web UI, not toggleable via chat. The web UI implies "this is supported"; experimental features explicitly don't carry that contract. Per-user gating, when needed, reuses `disabled_skills` (opt-out) — experimental flags are opt-in.

## Naming convention

- `module_<name>` — a whole module (UI tab, API, scheduled jobs)
- `skill_<name>` — a whole skill (its docs are hidden from the prompt, its CLI is inert)
- free-form — a single CLI subcommand inside a shipping skill

## Registered flags

### `money_tax`

Money skill — `lots` subcommand (open lots for a security). Rough because the lot-cost machinery assumes US lot accounting and a `tax_config` per ledger that won't match every jurisdiction. Graduation: when the lot model has been validated against a non-US ledger and the tax_config schema is documented.

### `money_wash_sales`

Money skill — `wash-sales` subcommand (IRS wash-sale violation detector). Encodes US-only IRS rules (30-day window, substantially identical securities). Graduation: when the wash-sale detector is either generalised across jurisdictions or explicitly scoped to US filers with a fail-loud check, and has test coverage against historical broker data.

## Adding a new flag

1. Add an entry to `KNOWN_FEATURES` in `src/istota/experimental.py`.
2. Wire the check at the right surface:
   - CLI subcommand: `@requires_feature("name")` from `istota.experimental`.
   - Whole skill: `experimental: true` in the skill's `skill.md` frontmatter. The flag name becomes `skill_<skill_name>`.
   - Whole module: add `"<name>": "module_<name>"` to `EXPERIMENTAL_MODULES` in `src/istota/modules.py` and add the module to `MODULE_NAMES`.
   - Web route: handler returns 404 when `config.experimental.is_enabled("…")` is False.
3. Add an entry to this file.
4. Default `istota_experimental_features` stays `[]` — existing deployments don't pick up the new feature automatically.

## Graduation

A feature stops being experimental when:

- It has test coverage and the suite is green.
- It has a documented supported surface (web UI, stable CLI, or both).
- At least one non-operator user has used it end-to-end without operator hand-holding.

When a feature graduates, drop its entry here, drop the registry entry, drop the gate(s), and remove the flag from any deployed `istota_experimental_features` lists.

## Removal

A flag that no one has enabled six months after being added is dead weight. Periodic manual audit (no automation): drop the flag and the gated code together.
