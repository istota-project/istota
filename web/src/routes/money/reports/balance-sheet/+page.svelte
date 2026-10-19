<script lang="ts">
  import { LoadState, Disclosure } from '$lib/components/ui';
  import { goto } from '$app/navigation';
  import { getReport, type AccountRow } from '$lib/money/api';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import { selectedYear, transactionsUrl } from '$lib/money/stores/transactions';
  import {
    buildTree,
    displayBalance,
    parseAmount,
    formatAmount,
    type AccountNode,
  } from '$lib/money/utils/accounts';

  function navigateToAccount(fullName: string) {
    goto(transactionsUrl(fullName, $selectedYear));
  }

  let loading = $state(true);
  let error = $state('');
  let assetRows: AccountRow[] = $state([]);
  let liabilityRows: AccountRow[] = $state([]);
  let equityRows: AccountRow[] = $state([]);
  let assetTree: AccountNode[] = $state([]);
  let liabilityTree: AccountNode[] = $state([]);
  let equityTree: AccountNode[] = $state([]);
  let assetsOpen = $state(true);
  let liabilitiesOpen = $state(true);
  let equityOpen = $state(true);

  let totals = $derived.by(() => {
    let assetsRaw = 0;
    let liabilitiesRaw = 0;
    let equityRaw = 0;
    let currency = '';

    function extractCurrency(pos: string) {
      if (!currency) {
        const m = pos.match(/[A-Z]{2,}/);
        if (m) currency = m[0];
      }
    }

    for (const row of assetRows) {
      const amt = parseAmount(row['sum(position)'] || '');
      if (!isNaN(amt)) assetsRaw += amt;
      extractCurrency(row['sum(position)'] || '');
    }
    for (const row of liabilityRows) {
      const amt = parseAmount(row['sum(position)'] || '');
      if (!isNaN(amt)) liabilitiesRaw += amt;
      extractCurrency(row['sum(position)'] || '');
    }
    for (const row of equityRows) {
      const amt = parseAmount(row['sum(position)'] || '');
      if (!isNaN(amt)) equityRaw += amt;
      extractCurrency(row['sum(position)'] || '');
    }
    // Assets are positive in beancount. Liabilities/equity are negative (credit accounts).
    // Display liabilities/equity as positive numbers (negate the raw sum).
    // Net worth = assets + liabilities (raw), since liabilities raw is negative.
    return {
      assets: assetsRaw,
      liabilities: -liabilitiesRaw,
      equity: -equityRaw,
      netWorth: assetsRaw + liabilitiesRaw,
      currency,
    };
  });

  async function loadReport() {
    loading = true;
    error = '';
    try {
      const resp = await getReport('balance-sheet', {
        ledger: $selectedLedger || undefined,
      });
      assetRows = resp.results.filter((r) => r.account.startsWith('Assets:'));
      liabilityRows = resp.results.filter((r) => r.account.startsWith('Liabilities:'));
      equityRows = resp.results.filter((r) => r.account.startsWith('Equity:'));
      assetTree = buildTree(assetRows);
      liabilityTree = buildTree(liabilityRows);
      equityTree = buildTree(equityRows);
    } catch (e) {
      if (e instanceof Error) error = e.message;
      else error = 'Failed to load report';
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    $selectedLedger;
    loadReport();
  });
</script>

<LoadState {loading} {error}>
  <div class="report-content">
    <div class="net-worth-banner">
      <span class="nw-label">Net worth</span>
      <span
        class="nw-amount"
        class:positive={totals.netWorth >= 0}
        class:negative={totals.netWorth < 0}
      >
        {formatAmount(totals.netWorth, totals.currency)}
      </span>
    </div>

    <Disclosure variant="section" summary="Assets" bind:open={assetsOpen}>
      {#snippet trailing()}<span class="section-total"
          >{formatAmount(totals.assets, totals.currency)}</span
        >{/snippet}
      <div class="tree-section">
        {#each assetTree as node (node.fullName)}
          {@render treeNode(node)}
        {/each}
      </div>
    </Disclosure>

    <Disclosure variant="section" summary="Liabilities" bind:open={liabilitiesOpen}>
      {#snippet trailing()}<span class="section-total"
          >{formatAmount(totals.liabilities, totals.currency)}</span
        >{/snippet}
      <div class="tree-section">
        {#each liabilityTree as node (node.fullName)}
          {@render treeNode(node)}
        {/each}
      </div>
    </Disclosure>

    <Disclosure variant="section" summary="Equity" bind:open={equityOpen}>
      {#snippet trailing()}<span class="section-total"
          >{formatAmount(totals.equity, totals.currency)}</span
        >{/snippet}
      <div class="tree-section">
        {#each equityTree as node (node.fullName)}
          {@render treeNode(node)}
        {/each}
      </div>
    </Disclosure>
  </div>
</LoadState>

{#snippet treeNode(node: AccountNode)}
  <div class="record-row record-row--tree" style="padding-left: {0.75 + node.depth * 1.25}rem">
    <button
      class="record-name button-reset"
      type="button"
      onclick={() => navigateToAccount(node.fullName)}>{node.name}</button
    >
    {#if node.balance}
      <span class="tree-balance num">{displayBalance(node.balance, node.fullName)}</span>
    {/if}
  </div>
  {#each node.children as child (child.fullName)}
    {@render treeNode(child)}
  {/each}
{/snippet}

<style>
  .net-worth-banner {
    display: flex;
    align-items: baseline;
    padding: var(--space-4) var(--space-3);
    margin-bottom: var(--space-2);
    background: var(--surface-card);
    border-radius: var(--radius-card);
  }

  .nw-label {
    font-weight: var(--weight-semibold);
    font-size: var(--text-lg);
  }

  .nw-amount {
    margin-left: auto;
    font-weight: var(--weight-semibold);
    font-size: var(--text-lg);
    font-variant-numeric: tabular-nums;
  }

  .nw-amount.positive {
    color: var(--money-income);
  }
  .nw-amount.negative {
    color: var(--money-expense);
  }

  .section-total {
    margin-left: auto;
    font-weight: var(--weight-semibold);
    font-variant-numeric: tabular-nums;
    font-size: var(--text-base);
    color: var(--text-primary);
  }

  .tree-section {
    padding: var(--space-1) 0 var(--space-2);
  }

  .tree-balance {
    margin-left: auto;
  }
</style>
