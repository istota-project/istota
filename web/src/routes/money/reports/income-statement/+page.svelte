<script lang="ts">
  import { LoadState, Disclosure } from '@minot/ui';
  import { goto } from '$app/navigation';
  import { getReport, type AccountRow } from '$lib/money/api';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import { selectedYear, transactionsUrl } from '$lib/money/stores/transactions';
  import {
    buildTree,
    displayBalance,
    parseAmount,
    shouldInvert,
    formatAmount,
    type AccountNode,
  } from '$lib/money/utils/accounts';

  function navigateToAccount(fullName: string) {
    goto(transactionsUrl(fullName, $selectedYear));
  }

  let loading = $state(true);
  let error = $state('');
  let incomeRows: AccountRow[] = $state([]);
  let expenseRows: AccountRow[] = $state([]);
  let incomeTree: AccountNode[] = $state([]);
  let expenseTree: AccountNode[] = $state([]);
  let incomeOpen = $state(true);
  let expenseOpen = $state(true);

  let totals = $derived.by(() => {
    let income = 0;
    let expenses = 0;
    let currency = '';
    for (const row of incomeRows) {
      const amt = parseAmount(row['sum(position)'] || '');
      if (!isNaN(amt)) income += Math.abs(amt);
      if (!currency) {
        const m = (row['sum(position)'] || '').match(/[A-Z]{2,}/);
        if (m) currency = m[0];
      }
    }
    for (const row of expenseRows) {
      const amt = parseAmount(row['sum(position)'] || '');
      if (!isNaN(amt)) expenses += Math.abs(amt);
      if (!currency) {
        const m = (row['sum(position)'] || '').match(/[A-Z]{2,}/);
        if (m) currency = m[0];
      }
    }
    return { income, expenses, net: income - expenses, currency };
  });

  async function loadReport() {
    loading = true;
    error = '';
    try {
      const resp = await getReport('income-statement', {
        ledger: $selectedLedger || undefined,
        year: $selectedYear || undefined,
      });
      incomeRows = resp.results.filter((r) => r.account.startsWith('Income:'));
      expenseRows = resp.results.filter((r) => r.account.startsWith('Expenses:'));
      incomeTree = buildTree(incomeRows);
      expenseTree = buildTree(expenseRows);
    } catch (e) {
      if (e instanceof Error) error = e.message;
      else error = 'Failed to load report';
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    $selectedLedger;
    $selectedYear;
    loadReport();
  });
</script>

<LoadState {loading} {error}>
  <div class="report-content">
    <Disclosure variant="section" summary="Income" bind:open={incomeOpen}>
      {#snippet trailing()}<span class="section-total income"
          >{formatAmount(totals.income, totals.currency)}</span
        >{/snippet}
      <div class="tree-section">
        {#each incomeTree as node (node.fullName)}
          {@render treeNode(node)}
        {/each}
      </div>
    </Disclosure>

    <Disclosure variant="section" summary="Expenses" bind:open={expenseOpen}>
      {#snippet trailing()}<span class="section-total expense"
          >{formatAmount(totals.expenses, totals.currency)}</span
        >{/snippet}
      <div class="tree-section">
        {#each expenseTree as node (node.fullName)}
          {@render treeNode(node)}
        {/each}
      </div>
    </Disclosure>

    <div class="net-row">
      <span class="net-label">Net income</span>
      <span class="net-amount" class:positive={totals.net >= 0} class:negative={totals.net < 0}>
        {formatAmount(totals.net, totals.currency)}
      </span>
    </div>
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
      <span
        class="tree-balance num"
        class:income={node.fullName.startsWith('Income:')}
        class:expense={node.fullName.startsWith('Expenses:')}
      >
        {displayBalance(node.balance, node.fullName)}
      </span>
    {/if}
  </div>
  {#each node.children as child (child.fullName)}
    {@render treeNode(child)}
  {/each}
{/snippet}

<style>
  .section-total {
    margin-left: auto;
    font-weight: var(--weight-semibold);
    font-variant-numeric: tabular-nums;
    font-size: var(--text-base);
  }

  .section-total.income {
    color: var(--money-income);
  }
  .section-total.expense {
    color: var(--money-expense);
  }

  .tree-section {
    padding: var(--space-1) 0 var(--space-2);
  }

  .tree-balance {
    margin-left: auto;
  }

  .tree-balance.income {
    color: var(--money-income);
  }
  .tree-balance.expense {
    color: var(--money-expense);
  }

  .net-row {
    display: flex;
    align-items: baseline;
    padding: var(--space-4) var(--space-3) var(--space-2);
    border-top: 2px solid var(--border-default);
    margin-top: var(--space-3);
  }

  .net-label {
    font-weight: var(--weight-semibold);
    font-size: var(--text-base);
  }

  .net-amount {
    margin-left: auto;
    font-weight: var(--weight-semibold);
    font-size: var(--text-base);
    font-variant-numeric: tabular-nums;
  }

  .net-amount.positive {
    color: var(--money-income);
  }
  .net-amount.negative {
    color: var(--money-expense);
  }
</style>
