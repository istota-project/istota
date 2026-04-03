<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getMoneymanLedgers,
		getMoneymanFava,
		type MoneymanLedger,
		type FavaInstance,
	} from '$lib/api';

	let ledgers: MoneymanLedger[] = $state([]);
	let favaInstances: FavaInstance[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	// Map fava instances by ledger name for quick lookup
	let favaByLedger: Map<string, FavaInstance> = $derived(
		new Map(favaInstances.map((inst) => [inst.ledger, inst]))
	);

	onMount(async () => {
		try {
			const [ledgerData, favaData] = await Promise.all([
				getMoneymanLedgers(),
				getMoneymanFava(),
			]);
			ledgers = ledgerData.ledgers || [];
			favaInstances = favaData.instances || [];
		} catch (e) {
			error = 'Failed to load ledger data';
		} finally {
			loading = false;
		}
	});
</script>

<div class="ledgers-page">
	<h1>Ledgers</h1>

	{#if loading}
		<div class="loading">Loading...</div>
	{:else if error}
		<div class="error-msg">{error}</div>
	{:else if ledgers.length === 0}
		<div class="empty">No ledgers configured.</div>
	{:else}
		<div class="ledger-grid">
			{#each ledgers as ledger}
				{@const fava = favaByLedger.get(ledger.name)}
				<div class="ledger-card">
					<div class="ledger-name">{ledger.name}</div>
					{#if fava}
						<a href={fava.prefix} class="fava-link">Open in Fava</a>
					{:else}
						<span class="no-fava">No Fava instance</span>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.ledgers-page {
		max-width: 800px;
		margin: 0 auto;
		padding: 1rem;
	}
	h1 {
		font-size: 1.1rem;
		font-weight: 600;
		margin: 0 0 1.5rem;
	}
	.ledger-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
		gap: 1rem;
	}
	.ledger-card {
		background: #1a1a1a;
		border-radius: 0.5rem;
		padding: 1.25rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}
	.ledger-name {
		font-weight: 600;
		font-size: 0.95rem;
	}
	.fava-link {
		display: inline-block;
		font-size: 0.8rem;
		color: #6ea8fe;
		text-decoration: none;
		padding: 0.35rem 0.7rem;
		border: 1px solid #333;
		border-radius: 0.25rem;
		transition: background 0.15s, border-color 0.15s;
		width: fit-content;
	}
	.fava-link:hover {
		background: #222;
		border-color: #555;
	}
	.no-fava {
		font-size: 0.8rem;
		color: #666;
	}
	.loading, .error-msg, .empty {
		color: #888;
		font-size: 0.9rem;
	}
	.error-msg {
		color: #f88;
	}
</style>
