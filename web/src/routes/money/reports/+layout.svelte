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
		<a href="{base}/money/reports/cash-flow" class:active={isActive('/money/reports/cash-flow')}>Cash flow</a>
		<a href="{base}/money/reports/income-statement" class:active={isActive('/money/reports/income-statement')}>Income statement</a>
		<a href="{base}/money/reports/balance-sheet" class:active={isActive('/money/reports/balance-sheet')}>Balance sheet</a>
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

<div class="money-section-body">
	{@render children()}
</div>
