<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { HeaderNav, Select } from '$lib/components/ui';
  import {
    portfolioGroup,
    portfolioGroupBy,
    portfolioKnownGroups,
    type PortfolioGroupBy,
  } from '$lib/money/stores/portfolio';

  let { children } = $props();

  function isActive(path: string): boolean {
    return page.url.pathname.startsWith(`${base}${path}`);
  }

  const navItems = $derived([
    {
      href: `${base}/money/portfolio/overview`,
      label: 'Overview',
      active: isActive('/money/portfolio/overview'),
    },
    {
      href: `${base}/money/portfolio/history`,
      label: 'History',
      active: isActive('/money/portfolio/history'),
    },
    {
      href: `${base}/money/portfolio/import`,
      label: 'Import',
      active: isActive('/money/portfolio/import'),
    },
  ]);

  const groupOptions = $derived([
    { value: '', label: 'All groups' },
    ...$portfolioKnownGroups.map((k) => ({ value: k, label: k })),
  ]);

  const groupByOptions = [
    { value: 'total', label: 'Total' },
    { value: 'group', label: 'By group' },
    { value: 'account_type', label: 'By account type' },
    { value: 'asset_class', label: 'By asset class' },
  ];

  // History's charted-symbol view is URL state (?symbol=VTI), which is what
  // lets the layout see that the group-by dimension doesn't apply and drop
  // the control while a single symbol is showing.
  const chartedSymbol = $derived(page.url.searchParams.get('symbol') ?? '');
</script>

<div class="money-section-header portfolio-header">
  <!-- Field tier for the collapsed Select below 768px — same reasoning as the
       business section header. Inert on the wide layout. -->
  <div class="money-section-nav control-row">
    <HeaderNav items={navItems} ariaLabel="Portfolio section" />
  </div>
  <!-- Each tab's one filter lives here beside the nav (the reports-header
       arrangement): pushed right on the wide layout, and beside the collapsed
       nav dropdown below 768px via the .portfolio-header rules in the money
       layout. The pages keep their body toolbars for notices only. -->
  {#if isActive('/money/portfolio/overview') && $portfolioKnownGroups.length > 1}
    <div class="money-section-tools control-row">
      <Select
        value={$portfolioGroup}
        options={groupOptions}
        onValueChange={(v) => portfolioGroup.set(v)}
        ariaLabel="Group filter"
      />
    </div>
  {:else if isActive('/money/portfolio/history') && !chartedSymbol}
    <div class="money-section-tools control-row">
      <Select
        value={$portfolioGroupBy}
        options={groupByOptions}
        onValueChange={(v) => portfolioGroupBy.set(v as PortfolioGroupBy)}
        ariaLabel="Group history by"
      />
    </div>
  {/if}
</div>

<div class="money-section-body">
  {@render children()}
</div>
