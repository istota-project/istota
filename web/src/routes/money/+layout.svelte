<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import { Collapsible } from 'bits-ui';
  import { getLedgers, checkLedger, AuthError } from '$lib/money/api';
  import { selectedLedger, availableLedgers } from '$lib/money/stores/ledger';
  import { AppShell, ShellHeader, HeaderNav, Select, Chip } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { Cog } from 'lucide-svelte';

  let { children } = $props();

  let loading = $state(true);
  let error = $state('');
  let errorCount = $state(0);
  let errorMessages: string[] = $state([]);
  let errorsOpen = $state(false);

  const moneyBase = $derived(`${base}/money`);

  onMount(async () => {
    try {
      const ledgers = await getLedgers();
      availableLedgers.set(ledgers);
      if (ledgers.length > 0 && !$selectedLedger) {
        selectedLedger.set(ledgers[0]);
      }
    } catch (e) {
      if (e instanceof AuthError) {
        window.location.href = `${base}/login`;
        return;
      }
      error = 'Failed to load money data';
    } finally {
      loading = false;
    }
  });

  async function loadErrors(ledger: string) {
    try {
      const resp = await checkLedger({ ledger: ledger || undefined });
      errorCount = resp.error_count ?? 0;
      errorMessages = resp.errors ?? [];
    } catch {
      errorCount = 0;
      errorMessages = [];
    }
  }

  $effect(() => {
    if ($selectedLedger !== undefined) {
      loadErrors($selectedLedger);
    }
  });

  function isActive(path: string): boolean {
    return page.url.pathname.startsWith(`${moneyBase}${path}`);
  }

  const navItems = $derived([
    { href: `${moneyBase}/accounts`, label: 'Accounts', active: isActive('/accounts') },
    { href: `${moneyBase}/transactions`, label: 'Transactions', active: isActive('/transactions') },
    {
      href: `${moneyBase}/reports/income-statement`,
      label: 'Reports',
      active: isActive('/reports'),
    },
    { href: `${moneyBase}/taxes`, label: 'Taxes', active: isActive('/taxes') },
    // Lands on Work — the daily-action tab, and upstream of invoices.
    { href: `${moneyBase}/business/work`, label: 'Business', active: isActive('/business') },
  ]);

  const ledgerOptions = $derived($availableLedgers.map((l) => ({ value: l, label: l })));

  const onSettings = $derived(page.url.pathname.startsWith(`${moneyBase}/settings`));

  function toggleSettings() {
    if (onSettings) goto(`${moneyBase}/accounts`);
    else goto(`${moneyBase}/settings`);
  }
</script>

{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="error-msg">{error}</div>
{:else}
  <!-- insetBottom only on settings. Every other money section puts its own
       scroller inside the shell (.money-section-body, or transactions'
       .txn-scroll), so the shell's inset would land *below* that scroller as a
       permanent dead band while its last row still ran under the home
       indicator — the inset has to go on the scrolling box itself. Settings is
       the one section that scrolls in .shell-main directly. -->
  <AppShell insetBottom={onSettings}>
    {#snippet header()}
      <ShellHeader title="Money">
        {#snippet nav()}
          <HeaderNav items={navItems} ariaLabel="Money section" />
        {/snippet}
        {#snippet tools()}
          <!-- Not on settings: nothing there is scoped to a ledger, so the
				     picker is inert and only crowds the bar beside the Save button. -->
          {#if !onSettings && $availableLedgers.length > 1}
            <Select
              value={$selectedLedger}
              options={ledgerOptions}
              onValueChange={(v) => selectedLedger.set(v)}
              ariaLabel="Ledger"
            />
          {/if}
          {#if errorCount > 0}
            <Collapsible.Root bind:open={errorsOpen}>
              <Collapsible.Trigger class="error-badge">
                {errorCount} error{errorCount !== 1 ? 's' : ''}
              </Collapsible.Trigger>
            </Collapsible.Root>
          {/if}
          <!-- Ahead of the cog, so the cog keeps the bar's right edge and stays
				     put whether or not the open page offers a save. Renders nothing
				     unless one is registered. -->
          <HeaderSave />
          <Chip icon checked={onSettings} onclick={toggleSettings} title="Money settings">
            <Cog size={14} />
          </Chip>
        {/snippet}
      </ShellHeader>
    {/snippet}

    {#snippet extras()}
      {#if errorsOpen && errorMessages.length > 0}
        <div class="error-panel">
          {#each errorMessages as err}
            <div class="error-line">{err}</div>
          {/each}
        </div>
      {/if}
    {/snippet}

    {@render children()}
  </AppShell>
{/if}

<style>
  :global(.error-badge) {
    background: var(--surface-card);
    color: var(--status-danger-fg);
    border: 1px solid var(--status-danger-bg);
    border-radius: var(--radius-pill);
    padding: 0.2rem 0.55rem;
    font-size: var(--text-xs);
    cursor: pointer;
  }

  .error-panel {
    background: var(--status-danger-bg);
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--status-danger-fg);
    max-height: 200px;
    overflow-y: auto;
    flex-shrink: 0;
  }

  .error-line {
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: var(--text-xs);
    color: var(--status-warn-fg);
    padding: 0.15rem 0;
  }

  /* Shared section header pattern reused by sub-route layouts (transactions/reports/business). */
  :global(.money-section-header) {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  :global(.money-section-nav) {
    display: flex;
    gap: var(--chip-gap);
    /* Hang: chip TEXT aligns with the section heading text above. */
    margin-inline-start: calc(-1 * var(--chip-padding-x));
  }

  :global(.money-section-nav a) {
    display: inline-flex;
    align-items: center;
    font-size: var(--text-sm);
    line-height: 1.2;
    color: var(--text-muted);
    text-decoration: none;
    padding: 0.15rem var(--space-2);
    border-radius: var(--radius-pill);
    transition: all var(--transition-fast);
  }

  :global(.money-section-nav a:hover) {
    color: var(--text-primary);
  }
  :global(.money-section-nav a.active) {
    background: var(--surface-raised);
    color: var(--text-primary);
  }

  :global(.money-section-tools) {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  :global(.money-control-input) {
    background: var(--surface-card);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-pill);
    padding: 0.2rem var(--space-2);
    font-size: var(--text-xs);
    font-family: inherit;
    min-width: 12rem;
  }

  :global(.money-control-input::placeholder) {
    color: var(--text-dim);
  }

  /* The section scroller, so it owns the bottom safe area (the shell hands it
     over via insetBottom={onSettings}): the last row scrolls clear of the home
     indicator instead of the shell reserving a strip below the scroll area that
     nothing can ever scroll into. Inert where the inset is 0. */
  :global(.money-section-body) {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: auto;
    padding-bottom: var(--safe-bottom);
  }

  /* Shared record-table shell ------------------------------------------------
     Work, invoices and transactions are the same table: a toolbar, a scrolling
     list, a row of column labels, one row per record. They each carried a
     near-identical private copy of these rules and had drifted — the toolbar
     sat at 1rem while its own rows sat at 1.25rem, so no two left edges on the
     page lined up. The shell lives here; a page only styles its own columns
     (widths, alignment) and any page-specific rows underneath. */

  /* min-height reserves the height of a Button/Select row (0.4rem padding +
     a 1.4rem md button) whether or not this toolbar has filters in it. Without
     it a bar holding only the result count sits ~6px shorter than one with
     controls, so the count and the table under it land at a different height
     on each tab. */
  :global(.money-toolbar) {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    min-height: 2.2rem;
    padding: var(--space-2) var(--space-3);
    flex-shrink: 0;
    flex-wrap: wrap;
  }

  /* Dismissible banner strip above a toolbar (a stale-read conflict, a delete
     that needs explaining). Sits on the same inline edge as the toolbar and
     leaves the vertical gap to the toolbar's own top padding. */
  :global(.money-notice-bar) {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3) 0;
  }

  :global(.money-result-count) {
    font-size: var(--text-xs);
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
  }

  /* No inline padding: rows carry it, so a row's hover fill spans the full
     width and its text still starts on the section header's left edge. */
  :global(.money-table) {
    flex: 1;
    overflow-y: auto;
    padding: 0 0 var(--space-2);
  }

  :global(.money-table-header) {
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    padding: var(--space-1) var(--space-3) var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-dim);
    border-bottom: 1px solid var(--border-subtle);
    margin-bottom: 0.15rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 500;
  }

  :global(.money-table-row) {
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    font-size: var(--text-sm);
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
    text-align: left;
    width: 100%;
  }

  :global(.money-table-row:hover),
  :global(.money-table-row.expanded) {
    background: var(--surface-card);
  }

  /* Only expandable rows are clickable; a plain row shouldn't claim to be. */
  :global(.money-table-row[role='button']) {
    cursor: pointer;
  }

  :global(.money-table-row:focus-visible) {
    outline-offset: calc(-1 * var(--focus-ring-width));
  }

  /* A tree row — an account hierarchy or a report's nested totals. Denser and
     tighter than a list row because it is a column of many short lines with an
     indent guide, not a table of records. The four implementations this
     replaces differed only in these two values. */
  :global(.money-table-row--tree) {
    gap: var(--space-1);
    padding-block: 0.2rem;
  }

  /* Column label that sorts. Sits in .money-table-header, so it has to shed the
     button defaults and inherit the label type. */
  :global(.money-sortable) {
    background: none;
    border: none;
    color: var(--text-dim);
    font: inherit;
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 500;
    cursor: pointer;
    padding: 0;
    text-align: left;
  }

  :global(.money-sortable:hover) {
    color: var(--text-muted);
  }

  :global(.money-sort-arrow) {
    font-size: var(--text-2xs);
    vertical-align: middle;
    margin-left: 0.15rem;
    opacity: 0.5;
  }

  :global(.money-sortable:hover .money-sort-arrow) {
    opacity: 1;
  }

  /* Status chip. The page sets min-width — the label set differs per table, and
     a fixed slot is what keeps the columns after it from shifting per row. */
  :global(.money-status) {
    font-size: var(--text-2xs);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    flex-shrink: 0;
    padding: 0.1rem var(--space-2);
    border-radius: var(--radius-pill);
    white-space: nowrap;
    text-align: center;
    box-sizing: border-box;
  }

  :global(.money-status.status-posted) {
    color: var(--status-warn-fg);
    background: var(--status-warn-bg);
  }

  :global(.money-status.status-paid) {
    color: var(--status-success-fg);
    background: var(--status-success-bg);
  }

  :global(.money-status.status-draft) {
    color: var(--text-muted);
    background: var(--surface-badge);
  }

  /* The header cell shares the class but is a column label, not a chip. */
  :global(.money-table-header .money-status) {
    color: var(--text-dim);
    background: none;
    padding: 0;
    font-size: var(--text-xs);
    font-weight: 500;
  }

  :global(.money-amount) {
    text-align: right;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
    color: var(--text-primary);
  }

  /* Reserves the width of the trailing KebabMenu so header labels line up with
     the values under them. */
  :global(.money-kebab-spacer) {
    width: 1.1rem;
    flex-shrink: 0;
  }

  :global(.money-table-empty) {
    color: var(--text-dim);
    font-size: var(--text-base);
    padding: var(--space-8) var(--space-4);
    text-align: center;
  }

  @media (max-width: 640px) {
    /* Too narrow for a status column — the pages fall back to a colored
       identifier in the first column. */
    :global(.money-table-header .money-status),
    :global(.money-table-row .money-status) {
      display: none;
    }
  }

  @media (max-width: 768px) {
    :global(.money-section-header) {
      padding: var(--space-2) var(--space-3);
      flex-wrap: wrap;
    }

    :global(.money-section-tools) {
      width: 100%;
      flex-wrap: nowrap;
    }

    :global(.money-section-tools .money-control-input) {
      flex: 1 1 auto;
      min-width: 0;
    }
  }
</style>
