<script lang="ts">
	import { selectedYear } from '$lib/money/stores/transactions';
	import { accountFilter } from '$lib/money/stores/accounts';
	import { Select } from '$lib/components/ui';

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
	<div class="money-section-tools">
		<Select
			value={selectedYearValue}
			options={yearOptions}
			onValueChange={(v) => selectedYear.set(v === '' ? 0 : Number(v))}
			ariaLabel="Year"
		/>
		<input
			type="text"
			class="money-control-input"
			placeholder="Filter accounts..."
			value={$accountFilter}
			oninput={(e) => accountFilter.set(e.currentTarget.value)}
		/>
	</div>
</div>

<div class="money-section-body">
	{@render children()}
</div>
