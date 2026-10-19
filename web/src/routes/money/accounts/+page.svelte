<script lang="ts">
  import { goto } from '$app/navigation';
  import { EmptyState, LoadState } from '$lib/components/ui';
  import { getAccounts, type AccountRow } from '$lib/money/api';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import { selectedYear, transactionsUrl } from '$lib/money/stores/transactions';
  import { accountFilter } from '$lib/money/stores/accounts';
  import { buildTree, displayBalance, type AccountNode } from '$lib/money/utils/accounts';

  function navigateToAccount(fullName: string) {
    goto(transactionsUrl(fullName, $selectedYear));
  }

  let accounts: AccountRow[] = $state([]);
  let loading = $state(true);
  let error = $state('');

  let tree: AccountNode[] = $state([]);
  let expandedSet = $state(new Set<string>());

  function toggleExpand(fullName: string) {
    const next = new Set(expandedSet);
    if (next.has(fullName)) {
      next.delete(fullName);
    } else {
      next.add(fullName);
    }
    expandedSet = next;
  }

  function flattenVisible(nodes: AccountNode[]): AccountNode[] {
    const result: AccountNode[] = [];
    for (const node of nodes) {
      if ($accountFilter && !node.fullName.toLowerCase().includes($accountFilter.toLowerCase())) {
        const childResults = flattenVisible(node.children);
        if (childResults.length === 0) continue;
        result.push(node);
        if (expandedSet.has(node.fullName) || $accountFilter) {
          result.push(...childResults);
        }
        continue;
      }
      result.push(node);
      if ((expandedSet.has(node.fullName) || $accountFilter) && node.children.length > 0) {
        result.push(...flattenVisible(node.children));
      }
    }
    return result;
  }

  let visibleNodes = $derived(flattenVisible(tree));

  async function loadAccounts() {
    loading = true;
    error = '';
    expandedSet = new Set<string>();
    try {
      const resp = await getAccounts({
        ledger: $selectedLedger || undefined,
        year: $selectedYear || undefined,
      });
      accounts = resp.accounts;
      tree = buildTree(accounts, expandedSet);
    } catch (e) {
      if (e instanceof Error) error = e.message;
      else error = 'Failed to load accounts';
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    $selectedLedger;
    $selectedYear;
    loadAccounts();
  });
</script>

<LoadState {loading} {error} empty={visibleNodes.length === 0}>
  {#snippet emptyState()}
    <EmptyState>No accounts found.</EmptyState>
  {/snippet}
  <div class="account-list">
    {#each visibleNodes as node (node.fullName)}
      <div class="record-row record-row--tree" style="padding-left: {0.75 + node.depth * 1.25}rem">
        <button
          class="account-toggle button-reset"
          type="button"
          onclick={() => (node.children.length > 0 ? toggleExpand(node.fullName) : null)}
        >
          {#if node.children.length > 0}
            <span
              class="record-caret"
              class:is-open={expandedSet.has(node.fullName) || !!$accountFilter}>&#9654;</span
            >
          {:else}
            <span class="caret-spacer"></span>
          {/if}
        </button>
        <button
          class="record-name button-reset"
          type="button"
          onclick={() => navigateToAccount(node.fullName)}>{node.name}</button
        >
        {#if node.balance}
          <span
            class="account-balance num"
            class:income={node.fullName.startsWith('Income:')}
            class:expense={node.fullName.startsWith('Expenses:')}
            >{displayBalance(node.balance, node.fullName)}</span
          >
        {/if}
      </div>
    {/each}
  </div>
</LoadState>

<style>
  .account-list {
    display: flex;
    flex-direction: column;
    padding: var(--space-1) 0;
  }

  .account-toggle {
    width: 0.75rem;
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
  }

  .caret-spacer {
    width: 0.5rem;
    display: inline-block;
  }

  .account-balance {
    margin-left: auto;
  }

  .account-balance.income {
    color: var(--money-income);
  }
  .account-balance.expense {
    color: var(--money-expense);
  }
</style>
