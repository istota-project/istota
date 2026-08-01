<script lang="ts">
  import { onMount } from 'svelte';
  import { SettingsCard } from '$lib/components/settings';
  import { Input } from '$lib/components/ui';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import { notifyError, notifySuccess } from '$lib/stores/notices';
  import {
    getPortfolioAccounts,
    patchPortfolioAccount,
    type PortfolioAccount,
  } from '$lib/money/api';

  // The account registry: rows are auto-created by imports; group, type and
  // the excluded flag are user-owned thereafter. A group is any label worth
  // filtering by — an owner, a purpose, a household. Excluded accounts stay
  // imported but drop out of every summary, chart and total — reversibly.
  // The whole card is page state, so it saves from the app bar with the rest
  // of the page (useSettingsSave) — no per-row buttons, and each row is a
  // stacked block rather than a table so it stays readable on a phone.

  interface EditRow {
    account: PortfolioAccount;
    group: string;
    account_type: string;
    excluded: boolean;
  }

  let rows: EditRow[] = $state([]);
  let loaded = $state(false);
  let loadError = $state('');
  let saving = $state(false);

  // Free text with the common values as suggestions — a new category is a
  // typed value, not a migration.
  const TYPE_SUGGESTIONS = ['retirement', 'trading', 'cash', 'taxable'];

  // Groups are pure free text with no canonical vocabulary, so the
  // suggestions are whatever the rows already carry (edits included, so a
  // group typed on one row autocompletes on the next).
  const groupSuggestions = $derived(
    [...new Set(rows.map((r) => r.group.trim()).filter(Boolean))].sort(),
  );

  function toRow(account: PortfolioAccount): EditRow {
    return {
      account,
      group: account.group,
      account_type: account.account_type,
      excluded: account.excluded,
    };
  }

  async function load() {
    try {
      const resp = await getPortfolioAccounts();
      rows = resp.accounts.map(toRow);
      loadError = '';
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to load portfolio accounts';
    } finally {
      loaded = true;
    }
  }

  onMount(load);

  function isDirty(row: EditRow): boolean {
    return (
      row.group !== row.account.group ||
      row.account_type !== row.account.account_type ||
      row.excluded !== row.account.excluded
    );
  }

  async function saveAll() {
    saving = true;
    try {
      let saved = 0;
      let failure = '';
      for (const row of rows) {
        if (!isDirty(row)) continue;
        try {
          const resp = await patchPortfolioAccount(row.account.id, {
            group: row.group,
            account_type: row.account_type,
            excluded: row.excluded,
          });
          row.account = resp.account;
          saved += 1;
        } catch (e) {
          // The row keeps its edits and stays dirty, so Save can retry it.
          failure = e instanceof Error ? e.message : 'Save failed';
        }
      }
      if (failure) {
        notifyError(failure, { key: 'portfolio:account-save' });
      } else if (saved > 0) {
        notifySuccess(saved === 1 ? 'Saved 1 account' : `Saved ${saved} accounts`, {
          key: 'portfolio:account-save',
        });
      }
    } finally {
      saving = false;
    }
  }

  useSettingsSave(() =>
    rows.length > 0 ? { dirty: rows.some(isDirty), saving, save: saveAll } : null,
  );
</script>

<SettingsCard title="Portfolio accounts ({rows.length})">
  {#if loadError}
    <div class="banner error">{loadError}</div>
  {:else if loaded && rows.length === 0}
    <p class="empty">No portfolio accounts yet — they appear with the first positions import.</p>
  {:else}
    <p class="card-hint">
      Excluded accounts stay imported but are hidden from every chart and total. Group and type
      drive the Portfolio filters — a group is any label worth filtering by (an owner, a purpose).
      Edits save with the page's Save button.
    </p>
    <datalist id="portfolio-account-types">
      {#each TYPE_SUGGESTIONS as t (t)}<option value={t}></option>{/each}
    </datalist>
    <datalist id="portfolio-account-groups">
      {#each groupSuggestions as g (g)}<option value={g}></option>{/each}
    </datalist>
    <ul class="account-list">
      {#each rows as row (row.account.id)}
        <li class="account-row" class:excluded={row.excluded}>
          <div class="account-head">
            <span class="account-name">{row.account.account_name}</span>
            {#if row.account.account_number}
              <code class="account-number">{row.account.account_number}</code>
            {/if}
          </div>
          <div class="account-controls control-row">
            <label class="ctl">
              <span class="micro-label">Group</span>
              <Input
                bind:value={row.group}
                placeholder="Group"
                list="portfolio-account-groups"
                aria-label="Group of {row.account.account_name}"
              />
            </label>
            <label class="ctl">
              <span class="micro-label">Type</span>
              <Input
                bind:value={row.account_type}
                placeholder="taxable"
                list="portfolio-account-types"
                aria-label="Type of {row.account.account_name}"
              />
            </label>
            <label class="excluded-toggle">
              <input
                type="checkbox"
                bind:checked={row.excluded}
                aria-label="Exclude {row.account.account_name} from summaries"
              />
              <span>Excluded</span>
            </label>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</SettingsCard>

<style>
  .card-hint {
    margin: 0 0 var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .account-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .account-row {
    padding: var(--space-2) 0;
  }

  .account-row + .account-row {
    border-top: 1px solid var(--border-subtle);
  }

  .account-head {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    margin-bottom: var(--space-1);
  }

  /* Only the head dims when excluded — the controls that un-exclude it stay
     legible. */
  .account-row.excluded .account-head {
    opacity: 0.55;
  }

  .account-name {
    font-size: var(--text-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .account-number {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-dim);
    background: var(--surface-raised);
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
  }

  .account-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--space-2) var(--space-3);
  }

  .ctl {
    display: grid;
    gap: var(--space-1);
    flex: 1 1 9rem;
    min-width: 0;
  }

  .excluded-toggle {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    min-height: var(--control-height-md);
    font-size: var(--text-sm);
    color: var(--text-secondary);
    white-space: nowrap;
  }
</style>
