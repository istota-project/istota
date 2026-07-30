<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { selectedYear } from '$lib/money/stores/transactions';
  import { Select } from '$lib/components/ui';

  let { children } = $props();

  const currentYear = new Date().getFullYear();
  const years = Array.from({ length: 11 }, (_, i) => currentYear - i);

  function isActive(path: string): boolean {
    return page.url.pathname.startsWith(`${base}${path}`);
  }

  const yearOptions = $derived([
    { value: '', label: 'All' },
    ...years.map((y) => ({ value: String(y), label: String(y) })),
  ]);
  const selectedYearValue = $derived($selectedYear ? String($selectedYear) : '');
</script>

<div class="money-section-header">
  <div class="money-section-nav">
    <a href="{base}/money/reports/cash-flow" class:active={isActive('/money/reports/cash-flow')}
      >Cash flow</a
    >
    <a
      href="{base}/money/reports/income-statement"
      class:active={isActive('/money/reports/income-statement')}>Income statement</a
    >
    <a
      href="{base}/money/reports/balance-sheet"
      class:active={isActive('/money/reports/balance-sheet')}>Balance sheet</a
    >
  </div>
  <div class="money-section-tools">
    <Select
      value={selectedYearValue}
      options={yearOptions}
      onValueChange={(v) => selectedYear.set(v === '' ? 0 : Number(v))}
      ariaLabel="Year"
    />
  </div>
</div>

<div class="money-section-body report-frame">
  {@render children()}
</div>

<style>
  /* The three report pages each declared these — .section-toggle three times
     byte-for-byte as a :global() rule, which leaks app-wide, so whichever
     stylesheet loaded last was the one in effect. Defined once here, where the
     pages that use them live, scoped under a frame class this layout owns.
     Mirrors .health-frame; deliberately not named .money-*, which is the
     record-table shell owned by routes/money/+layout.svelte. */
  :global(.report-frame .section-header) {
    display: flex;
    align-items: baseline;
    padding: var(--space-3) var(--space-3) var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
    margin-top: var(--space-2);
  }

  :global(.report-frame .section-header:first-child) {
    margin-top: 0;
  }

  :global(.report-frame .section-toggle) {
    background: none;
    border: none;
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-base);
    font-weight: 600;
    cursor: pointer;
    padding: 0;
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
</style>
