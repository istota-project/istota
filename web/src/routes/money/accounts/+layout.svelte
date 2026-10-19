<script lang="ts">
  import { selectedYear } from '$lib/money/stores/transactions';
  import { accountFilter } from '$lib/money/stores/accounts';
  import { Select, Input } from '$lib/components/ui';
  import { Search } from '@lucide/svelte';

  let { children } = $props();

  const currentYear = new Date().getFullYear();
  const years = Array.from({ length: 11 }, (_, i) => currentYear - i);

  const yearOptions = $derived([
    { value: '', label: 'All' },
    ...years.map((y) => ({ value: String(y), label: String(y) })),
  ]);
  const selectedYearValue = $derived($selectedYear ? String($selectedYear) : '');
</script>

<div class="money-section-header">
  <div class="money-section-tools control-row">
    <Select
      value={selectedYearValue}
      options={yearOptions}
      onValueChange={(v) => selectedYear.set(v === '' ? 0 : Number(v))}
      ariaLabel="Year"
      minChars={4}
    />
    {#snippet searchIcon()}<Search size={14} />{/snippet}
    <Input
      type="search"
      size="toolbar"
      clearable
      prefix={searchIcon}
      class="money-control-input"
      aria-label="Filter accounts"
      placeholder="Filter accounts..."
      value={$accountFilter}
      oninput={(e) => accountFilter.set(e.currentTarget.value)}
    />
  </div>
</div>

<div class="money-section-body">
  {@render children()}
</div>
