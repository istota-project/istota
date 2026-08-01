<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { selectedYear } from '$lib/money/stores/transactions';
  import { HeaderNav, Select } from '$lib/components/ui';

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

  const navItems = $derived([
    {
      href: `${base}/money/reports/cash-flow`,
      label: 'Cash flow',
      active: isActive('/money/reports/cash-flow'),
    },
    {
      href: `${base}/money/reports/income-statement`,
      label: 'Income statement',
      active: isActive('/money/reports/income-statement'),
    },
    {
      href: `${base}/money/reports/balance-sheet`,
      label: 'Balance sheet',
      active: isActive('/money/reports/balance-sheet'),
    },
  ]);
</script>

<div class="money-section-header reports-header">
  <!-- Field tier at both widths: the inline links, and the Select they collapse
       into below 768px. Either way the nav comes out at the height and corner
       of the year filter beside it, so the header is one band rather than a
       tall control next to short ones — and it stays that height in a sibling
       section that has no filter. The app bar's compact pills above are what
       this should read as different from. -->
  <div class="money-section-nav control-row">
    <HeaderNav items={navItems} ariaLabel="Report" />
  </div>
  <div class="money-section-tools control-row">
    <Select
      value={selectedYearValue}
      options={yearOptions}
      onValueChange={(v) => selectedYear.set(v === '' ? 0 : Number(v))}
      ariaLabel="Year"
      minChars={4}
    />
  </div>
</div>

<div class="money-section-body report-frame">
  {@render children()}
</div>

<style>
  /* The `.reports-header` modifier on the header above is what reorders the
     year filter and the collapsed section nav onto one row below 768px. Its
     rules live with the rest of the `.money-*` shell in routes/money/+layout,
     which is where that shell is defined and the only place the design lint
     allows those globals to be written. */

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
