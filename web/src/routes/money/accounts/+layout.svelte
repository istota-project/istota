<script lang="ts">
	import { selectedYear } from '$lib/money/stores/transactions';
	import { accountFilter } from '$lib/money/stores/accounts';

	let { children } = $props();

	const currentYear = new Date().getFullYear();
	const years = Array.from({ length: 11 }, (_, i) => currentYear - i);

	function handleYearChange(e: Event) {
		const val = (e.target as HTMLSelectElement).value;
		selectedYear.set(val === '' ? 0 : Number(val));
	}
</script>

<div class="money-section-header">
	<div class="money-section-tools">
		<select class="money-control-select" value={$selectedYear || ''} onchange={handleYearChange}>
			<option value="">All years</option>
			{#each years as y}
				<option value={y}>{y}</option>
			{/each}
		</select>
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
