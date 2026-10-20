<script lang="ts">
  import { onDestroy, onMount, untrack } from 'svelte';
  import { createDrawerClose } from '$lib/ui/drawerClose';
  import { page } from '$app/state';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  import { getAccounts, type AccountRow } from '$lib/money/api';
  import {
    selectedAccount,
    selectedYear,
    filterText,
    encodeTransactionSelection,
    setTransactionNavigation,
    type TransactionSelection,
  } from '$lib/money/stores/transactions';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import {
    CategoryGroup,
    Chip,
    Icon,
    ListRow,
    Sidebar,
    SidebarToggle,
    Select,
    Input,
  } from '$lib/components/ui';
  import { Search } from '@lucide/svelte';
  import { debounce } from '$lib/ui/debounce';

  let { children } = $props();

  let accounts: AccountRow[] = $state([]);
  let sidebarOpen = $state(false);
  const drawer = createDrawerClose(
    () => sidebarOpen,
    () => (sidebarOpen = false),
  );
  onDestroy(drawer.cancel);
  const handleFilterInput = debounce((value: string) => filterText.set(value), 300);
  onDestroy(() => handleFilterInput.cancel());

  const currentYear = new Date().getFullYear();
  const years = Array.from({ length: 11 }, (_, i) => currentYear - i);

  let selectionReady = $state(false);

  function applySelection(selection: TransactionSelection) {
    selectedAccount.set(selection.account);
    selectedYear.set(selection.year);
  }

  const transactionSel = createUrlSelection<TransactionSelection>({
    key: 'transactions',
    params: ['account', 'year'],
    encode: encodeTransactionSelection,
    decode(params) {
      const year = Number(params.year);
      const validYear = /^\d{4}$/.test(params.year ?? '') && year >= 1900 && year <= 2100;
      return {
        account: params.account ?? '',
        year: params.year === 'all' ? 0 : validYear ? year : currentYear,
      };
    },
    read: () => (selectionReady ? { account: $selectedAccount, year: $selectedYear } : null),
    apply: applySelection,
  });

  // A query-only navigation can reuse this layout, including Back after reload.
  let lastUrl = page.url;
  $effect(() => {
    const url = page.url;
    if (url === lastUrl) return;
    lastUrl = url;
    untrack(() => {
      if (!selectionReady) return;
      const selection = transactionSel.current();
      if (selection) applySelection(selection);
    });
  });
  transactionSel.start();
  onMount(() => {
    const selection = transactionSel.current();
    if (selection) applySelection(selection);
    selectionReady = true;
  });

  /** The ledger's accounts by their top-level segment, in ledger order. Each
   *  account keeps the rest of its path as its label, so a third level reads
   *  `AR:Acme` under Assets rather than nesting another indent. */
  const groups = $derived.by(() => {
    const out = new Map<string, { account: string; label: string }[]>();
    for (const row of accounts) {
      const [top, ...rest] = row.account.split(':');
      if (!out.has(top)) out.set(top, []);
      if (rest.length > 0) out.get(top)!.push({ account: row.account, label: rest.join(':') });
    }
    for (const rows of out.values()) rows.sort((a, b) => a.label.localeCompare(b.label));
    return [...out];
  });

  function navigateToAccount(account: string) {
    drawer.run(() => transactionSel.push({ account, year: $selectedYear }));
  }
  setTransactionNavigation(navigateToAccount);

  function selectAccount(fullName: string) {
    navigateToAccount($selectedAccount === fullName ? '' : fullName);
  }

  async function loadAccounts(ledger: string) {
    try {
      const resp = await getAccounts({ ledger: ledger || undefined });
      accounts = resp.accounts;
    } catch {
      // page handles its own loading/error
    }
  }

  let prevLedger = $state($selectedLedger);

  $effect(() => {
    loadAccounts($selectedLedger);
  });

  $effect(() => {
    const current = $selectedLedger;
    if (current !== prevLedger) {
      prevLedger = current;
      selectedAccount.set('');
    }
  });

  const yearOptions = $derived([
    { value: '', label: 'All' },
    ...($selectedYear && !years.includes($selectedYear) ? [$selectedYear, ...years] : years).map(
      (y) => ({ value: String(y), label: String(y) }),
    ),
  ]);

  const selectedYearValue = $derived($selectedYear ? String($selectedYear) : '');
</script>

<div class="money-section-header">
  {#if $selectedAccount}
    <Chip
      size="xs"
      removable
      class="active-filter"
      removeLabel="Clear the account filter"
      onremove={() => selectAccount($selectedAccount)}>{$selectedAccount}</Chip
    >
  {/if}
  <div class="money-section-tools control-row">
    <!-- The one placement that isn't a ShellHeader leading slot: the accounts
		     drawer belongs to this sub-route, but the app bar is owned by the money
		     layout above it. Leading position of this toolbar is the closest
		     equivalent — and on mobile the toolbar is already a full-width row, so
		     it costs no extra height either. -->
    <SidebarToggle
      open={sidebarOpen}
      label="Accounts"
      count={accounts.length}
      onclick={() => (sidebarOpen = !sidebarOpen)}
    />
    <Select
      value={selectedYearValue}
      options={yearOptions}
      onValueChange={(v) => selectedYear.set(v === '' ? 0 : Number(v))}
      ariaLabel="Year"
      minChars={4}
    />
    {#snippet searchIcon()}<Icon icon={Search} size="sm" />{/snippet}
    <Input
      type="search"
      size="toolbar"
      clearable
      prefix={searchIcon}
      class="money-control-input"
      aria-label="Filter transactions"
      placeholder="Filter by tag, payee..."
      value={$filterText}
      oninput={(e) => handleFilterInput(e.currentTarget.value)}
    />
  </div>
</div>

<div class="txn-body">
  <Sidebar open={sidebarOpen} onClose={() => (sidebarOpen = false)}>
    {#each groups as [top, rows] (top)}
      <CategoryGroup
        label={top}
        count={rows.length}
        collapsible
        active={$selectedAccount === top}
        onSelect={() => selectAccount(top)}
      >
        {#each rows as row (row.account)}
          <ListRow
            density="sm"
            title={row.account}
            active={$selectedAccount === row.account}
            onclick={() => selectAccount(row.account)}
          >
            {row.label}
          </ListRow>
        {/each}
      </CategoryGroup>
    {/each}
  </Sidebar>

  <div class="txn-main">
    {@render children()}
  </div>
</div>

<style>
  /* The chip precedes the toolbar in the DOM because on a wide screen it sits
     at the section's leading edge, with the tools pushed right by their own
     auto margin — one row, reading left to right. Below 768px the header wraps
     and the tools take the full width, so that same DOM order put the chip on
     the line *above* the controls it qualifies: the account filter appeared
     before there was any visible filter bar for it to belong to. Ordering it
     after leaves the toolbar where it always is — the first thing under the
     section nav — and hangs the chip beneath it. `order` rather than moving
     the markup, so the wide layout and the tab order are both untouched. */
  @media (max-width: 768px) {
    .money-section-header :global(.active-filter) {
      order: 1;
    }
  }

  .txn-body {
    display: flex;
    flex: 1;
    min-height: 0;
    position: relative;
  }

  .txn-main {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
</style>
