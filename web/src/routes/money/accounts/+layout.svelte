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

<div class="page-shell">
	<div class="page-header">
		<h1>Accounts</h1>
		<div class="header-right">
			<select class="header-select" value={$selectedYear || ''} onchange={handleYearChange}>
				<option value="">All years</option>
				{#each years as y}
					<option value={y}>{y}</option>
				{/each}
			</select>
			<input
				type="text"
				class="filter-input"
				placeholder="Filter accounts..."
				value={$accountFilter}
				oninput={(e) => accountFilter.set(e.currentTarget.value)}
			/>
		</div>
	</div>

	<div class="page-body">
		{@render children()}
	</div>
</div>
