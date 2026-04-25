<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { Collapsible } from 'bits-ui';
	import { getLedgers, checkLedger, AuthError } from '$lib/money/api';
	import { selectedLedger, availableLedgers } from '$lib/money/stores/ledger';

	let { children } = $props();

	let loading = $state(true);
	let error = $state('');
	let errorCount = $state(0);
	let errorMessages: string[] = $state([]);
	let errorsOpen = $state(false);

	const moneyBase = $derived(`${base}/money`);

	onMount(async () => {
		try {
			const ledgers = await getLedgers();
			availableLedgers.set(ledgers);
			if (ledgers.length > 0 && !$selectedLedger) {
				selectedLedger.set(ledgers[0]);
			}
		} catch (e) {
			if (e instanceof AuthError) {
				window.location.href = `${base}/login`;
				return;
			}
			error = 'Failed to load money data';
		} finally {
			loading = false;
		}
	});

	async function loadErrors(ledger: string) {
		try {
			const resp = await checkLedger({ ledger: ledger || undefined });
			errorCount = resp.error_count ?? 0;
			errorMessages = resp.errors ?? [];
		} catch {
			errorCount = 0;
			errorMessages = [];
		}
	}

	$effect(() => {
		if ($selectedLedger !== undefined) {
			loadErrors($selectedLedger);
		}
	});

	function isActive(path: string): boolean {
		const current = page.url.pathname;
		return current.startsWith(`${moneyBase}${path}`);
	}

	function handleLedgerChange(e: Event) {
		selectedLedger.set((e.target as HTMLSelectElement).value);
	}
</script>

{#if loading}
	<div class="money-loading">Loading…</div>
{:else if error}
	<div class="money-error">{error}</div>
{:else}
	<nav class="money-subnav">
		<a href="{moneyBase}/accounts" class:active={isActive('/accounts')}>Accounts</a>
		<a href="{moneyBase}/transactions" class:active={isActive('/transactions')}>Transactions</a>
		<a href="{moneyBase}/reports/income-statement" class:active={isActive('/reports')}>Reports</a>
		<a href="{moneyBase}/taxes" class:active={isActive('/taxes')}>Taxes</a>
		<a href="{moneyBase}/business/invoices" class:active={isActive('/business')}>Business</a>
		{#if $availableLedgers.length > 1}
			<select class="ledger-select" value={$selectedLedger} onchange={handleLedgerChange}>
				{#each $availableLedgers as l}
					<option value={l}>{l}</option>
				{/each}
			</select>
		{/if}
		{#if errorCount > 0}
			<Collapsible.Root bind:open={errorsOpen}>
				<Collapsible.Trigger class="error-badge">
					{errorCount} error{errorCount !== 1 ? 's' : ''}
				</Collapsible.Trigger>
			</Collapsible.Root>
		{/if}
	</nav>
	{#if errorsOpen && errorMessages.length > 0}
		<div class="error-panel">
			{#each errorMessages as err}
				<div class="error-line">{err}</div>
			{/each}
		</div>
	{/if}
	<div class="money-content">
		{@render children()}
	</div>
{/if}

<style>
	.money-subnav {
		display: flex;
		gap: 1rem;
		padding: 0.5rem 1rem;
		border-bottom: 1px solid #2a2a2a;
		align-items: center;
		background: #161616;
	}
	.money-subnav a {
		color: #c0c0c0;
		text-decoration: none;
		padding: 0.25rem 0.5rem;
		border-radius: 4px;
	}
	.money-subnav a.active {
		color: #fff;
		background: #2a2a2a;
	}
	.ledger-select {
		margin-left: auto;
		background: #1a1a1a;
		color: #e0e0e0;
		border: 1px solid #333;
		padding: 0.25rem 0.5rem;
	}
	:global(.error-badge) {
		background: #5a1f1f;
		color: #ffb;
		padding: 0.2rem 0.5rem;
		border-radius: 4px;
		border: none;
		font-size: 0.85rem;
	}
	.error-panel {
		background: #1a0e0e;
		padding: 0.5rem 1rem;
		border-bottom: 1px solid #5a1f1f;
		max-height: 200px;
		overflow-y: auto;
	}
	.error-line {
		font-family: monospace;
		font-size: 0.85rem;
		color: #ffb;
		padding: 0.25rem 0;
	}
	.money-loading,
	.money-error {
		padding: 2rem;
		text-align: center;
		color: #888;
	}
</style>
