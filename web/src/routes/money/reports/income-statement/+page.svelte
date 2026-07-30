<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { Collapsible } from 'bits-ui';
  import { getReport, type AccountRow } from '$lib/money/api';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import { selectedYear, selectedAccount } from '$lib/money/stores/transactions';
  import {
    buildTree,
    displayBalance,
    parseAmount,
    shouldInvert,
    formatAmount,
    type AccountNode,
  } from '$lib/money/utils/accounts';

  function navigateToAccount(fullName: string) {
    selectedAccount.set(fullName);
    goto(`${base}/money/transactions`);
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

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="error-msg">{error}</div>
{:else}
  <div class="report-content">
    <Collapsible.Root bind:open={incomeOpen}>
      <div class="section-header">
        <Collapsible.Trigger class="section-toggle">
          <span class="caret" class:open={incomeOpen}>&#9654;</span>
          Income
        </Collapsible.Trigger>
        <span class="section-total income">{formatAmount(totals.income, totals.currency)}</span>
      </div>
      <Collapsible.Content>
        <div class="tree-section">
          {#each incomeTree as node (node.fullName)}
            {@render treeNode(node)}
          {/each}
        </div>
      </Collapsible.Content>
    </Collapsible.Root>

    <Collapsible.Root bind:open={expenseOpen}>
      <div class="section-header">
        <Collapsible.Trigger class="section-toggle">
          <span class="caret" class:open={expenseOpen}>&#9654;</span>
          Expenses
        </Collapsible.Trigger>
        <span class="section-total expense">{formatAmount(totals.expenses, totals.currency)}</span>
      </div>
      <Collapsible.Content>
        <div class="tree-section">
          {#each expenseTree as node (node.fullName)}
            {@render treeNode(node)}
          {/each}
        </div>
      </Collapsible.Content>
    </Collapsible.Root>

    <div class="net-row">
      <span class="net-label">Net income</span>
      <span class="net-amount" class:positive={totals.net >= 0} class:negative={totals.net < 0}>
        {formatAmount(totals.net, totals.currency)}
      </span>
    </div>
  </div>
{/if}

{#snippet treeNode(node: AccountNode)}
  <div
    class="money-table-row money-table-row--tree"
    style="padding-left: {0.75 + node.depth * 1.25}rem"
  >
    <button class="tree-name" type="button" onclick={() => navigateToAccount(node.fullName)}
      >{node.name}</button
    >
    {#if node.balance}
      <span
        class="tree-balance"
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
  .report-content {
    padding: var(--space-2) var(--space-3);
  }

  .section-total {
    margin-left: auto;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    font-size: var(--text-base);
  }

  .section-total.income {
    color: var(--money-income);
  }
  .section-total.expense {
    color: var(--money-expense);
  }

  .caret {
    font-size: 0.5rem;
    color: var(--text-dim);
    transition: transform var(--transition-fast);
    display: inline-block;
  }

  .caret.open {
    transform: rotate(90deg);
  }

  .tree-section {
    padding: var(--space-1) 0 var(--space-2);
  }

  .tree-name {
    flex: 1;
    min-width: 0;
    background: none;
    border: none;
    font: inherit;
    color: inherit;
    cursor: pointer;
    padding: 0;
    text-align: left;
  }

  .tree-name:hover {
    color: var(--text-primary);
  }

  .tree-balance {
    margin-left: auto;
    text-align: right;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
    color: var(--text-primary);
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
    font-weight: 600;
    font-size: var(--text-base);
  }

  .net-amount {
    margin-left: auto;
    font-weight: 600;
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
