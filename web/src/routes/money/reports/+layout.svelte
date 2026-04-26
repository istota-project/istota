<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { selectedYear } from '$lib/money/stores/transactions';

	let { children } = $props();

	const currentYear = new Date().getFullYear();
	const years = Array.from({ length: 11 }, (_, i) => currentYear - i);

	function isActive(path: string): boolean {
		return page.url.pathname.startsWith(`${base}${path}`);
	}

	function handleYearChange(e: Event) {
		const val = (e.target as HTMLSelectElement).value;
		selectedYear.set(val === '' ? 0 : Number(val));
	}
</script>

<div class="money-section-header">
	<div class="money-section-nav">
		<a href="{base}/money/reports/cash-flow" class:active={isActive('/money/reports/cash-flow')}>Cash flow</a>
		<a href="{base}/money/reports/income-statement" class:active={isActive('/money/reports/income-statement')}>Income statement</a>
		<a href="{base}/money/reports/balance-sheet" class:active={isActive('/money/reports/balance-sheet')}>Balance sheet</a>
	</div>
	<div class="money-section-tools">
		<select class="money-control-select" value={$selectedYear || ''} onchange={handleYearChange}>
			<option value="">All</option>
			{#each years as y}
				<option value={y}>{y}</option>
			{/each}
		</select>
	</div>
</div>

<div class="money-section-body">
	{@render children()}
</div>
