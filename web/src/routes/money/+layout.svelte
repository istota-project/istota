<script lang="ts">
  // The record-table shell every money list page is built out of. A plain
  // stylesheet rather than 380 lines of :global() in this file's own style
  // block, and imported here rather than from app.css so it stays on the
  // money route chunk instead of downloading on every page.
  import '$lib/styles/moneyTable.css';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import { Collapsible } from 'bits-ui';
  import { getLedgers, checkLedger, AuthError } from '$lib/money/api';
  import { forgetLastUserId } from '$lib/offline/lastUser';
  import { selectedLedger, availableLedgers } from '$lib/money/stores/ledger';
  import {
    AppShell,
    ShellHeader,
    HeaderNav,
    Select,
    Chip,
    Sidebar,
    SidebarToggle,
  } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { MONEY_SETTINGS_SECTIONS } from '$lib/money/settingsSections';
  import { Cog } from 'lucide-svelte';

  let { children } = $props();

  let settingsSidebarOpen = $state(false);

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
        // Same as the root layout's own 401 branch: the session is over, so
        // the offline cache's last-user pointer goes with it (ISSUE-202).
        forgetLastUserId();
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
      href: `${moneyBase}/reports/cash-flow`,
      label: 'Reports',
      active: isActive('/reports'),
    },
    // Beside Reports rather than after Taxes: the two are the analysis views
    // and now share a framed content column, so they read as a pair.
    {
      href: `${moneyBase}/portfolio/overview`,
      label: 'Portfolio',
      active: isActive('/portfolio'),
    },
    { href: `${moneyBase}/taxes`, label: 'Taxes', active: isActive('/taxes') },
    // Lands on Work — the daily-action tab, and upstream of invoices.
    { href: `${moneyBase}/business/work`, label: 'Business', active: isActive('/business') },
  ]);

  const ledgerOptions = $derived($availableLedgers.map((l) => ({ value: l, label: l })));

  const onSettings = $derived(page.url.pathname.startsWith(`${moneyBase}/settings`));
  // Portfolio is snapshot-backed, not ledger-scoped, so the picker is inert
  // there for the same reason it is on settings.
  const onPortfolio = $derived(page.url.pathname.startsWith(`${moneyBase}/portfolio`));

  function toggleSettings() {
    if (onSettings) goto(`${moneyBase}/accounts`);
    else goto(`${moneyBase}/settings`);
  }

  // `/money/settings` is a prefix of every settings sub-route, so the index
  // section matches exactly rather than by prefix — otherwise Connections stays
  // lit on every section.
  function settingsSectionActive(href: string): boolean {
    const path = page.url.pathname.replace(/\/$/, '');
    return path === `${moneyBase}/settings${href}`.replace(/\/$/, '');
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
      <ShellHeader
        title="Money"
        onTitleClick={onSettings ? () => (settingsSidebarOpen = !settingsSidebarOpen) : undefined}
        titleActionLabel={onSettings ? 'open settings sections' : undefined}
      >
        <!-- Only on settings: that is the one money section with a sidebar, and
             the toggle drives it. Every other section navigates from the nav. -->
        {#snippet leading()}
          {#if onSettings}
            <SidebarToggle
              open={settingsSidebarOpen}
              label="Settings sections"
              onclick={() => (settingsSidebarOpen = !settingsSidebarOpen)}
            />
          {/if}
        {/snippet}
        {#snippet nav()}
          <HeaderNav items={navItems} ariaLabel="Money section" />
        {/snippet}
        {#snippet tools()}
          <!-- Not on settings: nothing there is scoped to a ledger, so the
				     picker is inert and only crowds the bar beside the Save button. -->
          {#if !onSettings && !onPortfolio && $availableLedgers.length > 1}
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

    <!-- Rendered by the module layout rather than by `settings/+layout.svelte`,
         because a `Sidebar` has to be a sibling of `.shell-main` to be a column
         beside it — from inside the settings layout it would be a block
         scrolling within the pane. The section list is imported so the two
         cannot disagree about it. -->
    {#snippet sidebar()}
      {#if onSettings}
        <Sidebar
          title="Settings"
          open={settingsSidebarOpen}
          onClose={() => (settingsSidebarOpen = false)}
        >
          <div class="views">
            {#each MONEY_SETTINGS_SECTIONS as section (section.href)}
              {@const Icon = section.icon}
              {@const active = settingsSectionActive(section.href)}
              <a
                class="view-btn"
                class:active
                href="{moneyBase}/settings{section.href}"
                aria-current={active ? 'page' : undefined}
                onclick={() => (settingsSidebarOpen = false)}
              >
                <Icon size={14} />
                <span class="view-name">{section.label}</span>
              </a>
            {/each}
          </div>
        </Sidebar>
      {/if}
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
</style>
