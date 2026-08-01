<script lang="ts">
  import { onMount } from 'svelte';
  import { SettingsCard } from '$lib/components/settings';
  import { Button, Input } from '$lib/components/ui';
  import { notifyError, notifySuccess } from '$lib/stores/notices';
  import {
    getPortfolioAccounts,
    patchPortfolioAccount,
    type PortfolioAccount,
  } from '$lib/money/api';

  // The account registry: rows are auto-created by imports; owner, type and
  // the excluded flag are user-owned thereafter. Excluded accounts stay
  // imported but drop out of every summary, chart and total — reversibly.
  // Per-record rows keep their own Save (they do not register with the
  // app-bar Save): each row is its own PATCH.

  interface EditRow {
    account: PortfolioAccount;
    owner: string;
    account_type: string;
    excluded: boolean;
    saving: boolean;
  }

  let rows: EditRow[] = $state([]);
  let loaded = $state(false);
  let loadError = $state('');

  // Free text with the common values as suggestions — a new category is a
  // typed value, not a migration.
  const TYPE_SUGGESTIONS = ['retirement', 'trading', 'cash', 'taxable'];

  function toRow(account: PortfolioAccount): EditRow {
    return {
      account,
      owner: account.owner,
      account_type: account.account_type,
      excluded: account.excluded,
      saving: false,
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
      row.owner !== row.account.owner ||
      row.account_type !== row.account.account_type ||
      row.excluded !== row.account.excluded
    );
  }

  async function save(row: EditRow) {
    row.saving = true;
    try {
      const resp = await patchPortfolioAccount(row.account.id, {
        owner: row.owner,
        account_type: row.account_type,
        excluded: row.excluded,
      });
      row.account = resp.account;
      notifySuccess(`Saved ${resp.account.account_name}`, { key: 'portfolio:account-save' });
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Save failed', {
        key: 'portfolio:account-save',
      });
    } finally {
      row.saving = false;
    }
  }
</script>

<SettingsCard title="Portfolio accounts ({rows.length})">
  {#if loadError}
    <div class="banner error">{loadError}</div>
  {:else if loaded && rows.length === 0}
    <p class="empty">No portfolio accounts yet — they appear with the first positions import.</p>
  {:else}
    <p class="card-hint">
      Excluded accounts stay imported but are hidden from every chart and total. Owner and type
      drive the Portfolio filters.
    </p>
    <datalist id="portfolio-account-types">
      {#each TYPE_SUGGESTIONS as t (t)}<option value={t}></option>{/each}
    </datalist>
    <div class="table-scroll">
      <table class="grid grid--dense">
        <thead>
          <tr>
            <th>Account</th>
            <th>Owner</th>
            <th>Type</th>
            <th>Excluded</th>
            <th class="actions" aria-label="Actions"></th>
          </tr>
        </thead>
        <tbody>
          {#each rows as row (row.account.id)}
            <tr class:excluded-row={row.excluded}>
              <td>
                {row.account.account_name}
                {#if row.account.account_number}
                  <span class="muted"><code>{row.account.account_number}</code></span>
                {/if}
              </td>
              <td>
                <Input bind:value={row.owner} placeholder="Owner" aria-label="Owner" />
              </td>
              <td>
                <Input
                  bind:value={row.account_type}
                  placeholder="taxable"
                  aria-label="Account type"
                  list="portfolio-account-types"
                />
              </td>
              <td>
                <input
                  type="checkbox"
                  bind:checked={row.excluded}
                  aria-label="Exclude {row.account.account_name} from summaries"
                />
              </td>
              <td class="actions">
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={!isDirty(row) || row.saving}
                  loading={row.saving}
                  onclick={() => save(row)}
                >
                  Save
                </Button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</SettingsCard>

<style>
  .card-hint {
    margin: 0 0 var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .excluded-row td {
    opacity: 0.55;
  }

  /* The controls in this row stay legible when the row itself is greyed. */
  .excluded-row td.actions,
  .excluded-row td:nth-last-child(2) {
    opacity: 1;
  }

  .muted {
    color: var(--text-dim);
    font-size: var(--text-xs);
  }
</style>
