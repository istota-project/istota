<script lang="ts">
  import {
    getTransactions,
    getPostings,
    getAccounts,
    updateTransaction,
    type TransactionRow,
    type PostingRow,
  } from '$lib/money/api';
  import {
    selectedAccount,
    selectedYear,
    filterText,
    getTransactionNavigation,
  } from '$lib/money/stores/transactions';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import { displayBalance } from '$lib/money/utils/accounts';
  import { EmptyState, KebabMenu, LoadState, Pagination } from '$lib/components/ui';
  import TransactionForm from '$lib/components/money/TransactionForm.svelte';
  import { formatDate as formatIsoDate } from '$lib/dateFormat';

  const navigateToAccount = getTransactionNavigation();

  let transactions: TransactionRow[] = $state([]);
  let loading = $state(true);
  let error = $state('');
  let total = $state(0);
  let currentPage = $state(1);
  let perPage = 100;

  // Account names for the edit form's dropdown.
  let accountNames: string[] = $state([]);

  // Edit-transaction modal state.
  let editingTxn: TransactionRow | null = $state(null);

  function openEdit(txn: TransactionRow) {
    editingTxn = txn;
  }

  async function handleEditSave(data: {
    payee: string;
    narration: string;
    date: string;
    account: string;
    position: string;
  }) {
    if (!editingTxn) return;
    const original = editingTxn;
    if (!original.id) {
      throw new Error('This transaction has no stable id yet — run the id backfill first.');
    }
    await updateTransaction({
      ledger: $selectedLedger || undefined,
      id: original.id,
      old_account: original.account,
      old_position: original.position,
      new_payee: data.payee,
      new_narration: data.narration,
      new_date: data.date,
      new_account: data.account,
      new_position: data.position,
    });
    editingTxn = null;
    await load(currentOpts(currentPage));
  }

  // Track expanded transactions and their postings
  let expandedKeys = $state(new Set<string>());
  let postingsCache = $state(new Map<string, PostingRow[]>());
  let postingsLoading = $state(new Set<string>());

  function txnKey(txn: TransactionRow): string {
    return `${txn.date}|${txn.payee}|${txn.narration}|${txn.account}|${txn.position}`;
  }

  async function toggleExpand(txn: TransactionRow) {
    const key = txnKey(txn);
    if (expandedKeys.has(key)) {
      const next = new Set(expandedKeys);
      next.delete(key);
      expandedKeys = next;
      return;
    }

    // Expand and fetch postings if not cached
    const nextExpanded = new Set(expandedKeys);
    nextExpanded.add(key);
    expandedKeys = nextExpanded;

    if (!postingsCache.has(key)) {
      const nextLoading = new Set(postingsLoading);
      nextLoading.add(key);
      postingsLoading = nextLoading;
      try {
        const resp = await getPostings({
          ledger: $selectedLedger || undefined,
          date: txn.date,
          payee: txn.payee,
          narration: txn.narration,
          account: txn.account,
          position: txn.position,
        });
        const nextCache = new Map(postingsCache);
        nextCache.set(key, resp.postings);
        postingsCache = nextCache;
      } catch {
        // Silently fail — row stays expanded but empty
      } finally {
        const nl = new Set(postingsLoading);
        nl.delete(key);
        postingsLoading = nl;
      }
    }
  }

  async function load(opts: {
    ledger: string;
    account: string;
    year: number;
    filter: string;
    page: number;
  }) {
    loading = true;
    error = '';
    try {
      const resp = await getTransactions({
        ledger: opts.ledger || undefined,
        account: opts.account || undefined,
        year: opts.year || undefined,
        filter: opts.filter || undefined,
        page: opts.page,
        per_page: perPage,
      });
      transactions = resp.transactions;
      total = resp.total;
      // Clear expand state on reload
      expandedKeys = new Set();
      postingsCache = new Map();
    } catch (e) {
      if (e instanceof Error) error = e.message;
      else error = 'Failed to load transactions';
    } finally {
      loading = false;
    }
  }

  function currentOpts(page: number) {
    return {
      ledger: $selectedLedger,
      account: $selectedAccount,
      year: $selectedYear,
      filter: $filterText,
      page,
    };
  }

  // Reload and reset to page 1 when any filter changes
  $effect(() => {
    const opts = currentOpts(1);
    currentPage = 1;
    load(opts);
  });

  // Keep an account-name list for the edit form's dropdown.
  $effect(() => {
    const ledger = $selectedLedger;
    getAccounts({ ledger: ledger || undefined })
      .then((resp) => {
        accountNames = resp.accounts.map((a) => a.account);
      })
      .catch(() => {
        accountNames = [];
      });
  });

  function goToPage(page: number) {
    if (page < 1 || (page - 1) * perPage >= total) return;
    currentPage = page;
    load(currentOpts(currentPage));
  }

  interface TxnGroup {
    date: string;
    rows: TransactionRow[];
  }

  let grouped = $derived.by(() => {
    const groups: TxnGroup[] = [];
    let lastDate = '';
    for (const txn of transactions) {
      if (txn.date !== lastDate) {
        groups.push({ date: txn.date, rows: [] });
        lastDate = txn.date;
      }
      groups[groups.length - 1].rows.push(txn);
    }
    return groups;
  });

  let totalPages = $derived(Math.max(1, Math.ceil(total / perPage)));

  const formatDate = (iso: string) =>
    formatIsoDate(iso, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });

  function shortAccount(account: string): string {
    const parts = account.split(':');
    if (parts.length <= 2) return account;
    return parts.slice(-2).join(':');
  }
</script>

<div class="txn-content">
  <!-- A page change keeps the rows on screen, faded, under the bar. -->
  <LoadState
    loading={loading && transactions.length === 0}
    {error}
    empty={transactions.length === 0}
  >
    {#snippet header()}
      <div class="money-toolbar toolbar toolbar--bar control-row">
        <span class="caption num">{total.toLocaleString()} entries</span>
      </div>
    {/snippet}
    {#snippet emptyState()}
      <EmptyState>No transactions found.</EmptyState>
    {/snippet}
    <div class="record-list txn-scroll" class:faded={loading}>
      {#each grouped as group (group.date)}
        <div class="record-group micro-label">{formatDate(group.date)}</div>
        {#each group.rows as txn, i (group.date + '-' + i)}
          {@const key = txnKey(txn)}
          {@const isExpanded = expandedKeys.has(key)}
          <div
            class="record-row"
            class:is-expanded={isExpanded}
            role="button"
            tabindex="0"
            aria-expanded={isExpanded}
            onclick={() => toggleExpand(txn)}
            onkeydown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                toggleExpand(txn);
              }
            }}
          >
            <div class="txn-main">
              {#if txn.payee}
                <span class="txn-payee">{txn.payee}</span>
              {/if}
              {#if txn.narration}
                <span class="txn-narration">{txn.narration}</span>
              {/if}
            </div>
            <button
              class="txn-account button-reset caption"
              onclick={(e) => {
                e.stopPropagation();
                navigateToAccount(txn.account);
              }}
              type="button">{shortAccount(txn.account)}</button
            >
            <span
              class="txn-amount num"
              class:income={txn.account.startsWith('Income:')}
              class:expense={txn.account.startsWith('Expenses:')}
              >{displayBalance(txn.position, txn.account)}</span
            >
            <KebabMenu items={[{ label: 'Edit transaction', onSelect: () => openEdit(txn) }]} />
          </div>
          {#if isExpanded}
            <div class="record-detail postings">
              {#if postingsLoading.has(key)}
                <div class="record-detail-row">
                  <span class="posting-account caption">Loading...</span>
                </div>
              {:else if postingsCache.has(key)}
                {#each postingsCache.get(key) ?? [] as posting}
                  <div class="record-detail-row">
                    <button
                      class="posting-account button-reset caption"
                      onclick={() => navigateToAccount(posting.account)}
                      type="button">{posting.account}</button
                    >
                    <span class="posting-amount num">{posting.position}</span>
                  </div>
                {/each}
              {/if}
            </div>
          {/if}
        {/each}
      {/each}
    </div>

    {#if totalPages > 1}
      <div class="txn-pages">
        <Pagination page={currentPage} pages={totalPages} onchange={goToPage} />
      </div>
    {/if}
  </LoadState>
</div>

{#if editingTxn}
  <TransactionForm
    txn={editingTxn}
    accounts={accountNames}
    onSave={handleEditSave}
    onCancel={() => (editingTxn = null)}
  />
{/if}

<style>
  .txn-content {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }

  /* Transactions scrolls here rather than in .money-section-body, so this is
     where the bottom safe area goes (see the money layout's insetBottom note). */
  .txn-scroll {
    padding-bottom: max(var(--space-2), var(--safe-bottom));
    transition: opacity var(--transition-fast);
  }

  .txn-scroll.faded {
    opacity: 0.5;
  }

  .txn-main {
    flex: 1;
    min-width: 0;
    display: flex;
    gap: var(--space-2);
    overflow: hidden;
  }

  .txn-payee {
    font-weight: var(--weight-medium);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .txn-narration {
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .txn-account {
    white-space: nowrap;
    flex-shrink: 0;
  }

  .txn-account:hover {
    color: var(--text-muted);
  }

  .txn-amount {
    min-width: 6rem;
  }

  .txn-amount.income {
    color: var(--money-income);
  }
  .txn-amount.expense {
    color: var(--money-expense);
  }

  .postings {
    /* design-lint-allow: under the payee, past the row's 0.75rem edge. */
    --record-detail-indent: 2.5rem;
  }

  .posting-account {
    text-align: left;
  }

  .posting-account:hover {
    color: var(--text-secondary);
  }

  .posting-amount {
    margin-left: auto;
    color: var(--text-secondary);
  }

  /* Pinned under the scrolling rows, ruled off from them. */
  .txn-pages {
    flex-shrink: 0;
    border-top: 1px solid var(--border-subtle);
  }

  @media (max-width: 640px) {
    .txn-account {
      display: none;
    }
    .txn-amount {
      min-width: 4rem;
    }
  }
</style>
