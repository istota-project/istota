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
		return page.url.pathname.startsWith(`${moneyBase}${path}`);
	}

	function handleLedgerChange(e: Event) {
		selectedLedger.set((e.target as HTMLSelectElement).value);
	}
</script>

{#if loading}
	<div class="loading">Loading…</div>
{:else if error}
	<div class="error-msg">{error}</div>
{:else}
	<div class="money-shell">
		<div class="money-header">
			<h1>Money</h1>
			<div class="money-nav">
				<a href="{moneyBase}/accounts" class:active={isActive('/accounts')}>Accounts</a>
				<a href="{moneyBase}/transactions" class:active={isActive('/transactions')}>Transactions</a>
				<a href="{moneyBase}/reports/income-statement" class:active={isActive('/reports')}>Reports</a>
				<a href="{moneyBase}/taxes" class:active={isActive('/taxes')}>Taxes</a>
				<a href="{moneyBase}/business/invoices" class:active={isActive('/business')}>Business</a>
			</div>
			<div class="money-tools">
				{#if $availableLedgers.length > 1}
					<select class="money-select" value={$selectedLedger} onchange={handleLedgerChange}>
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
			</div>
		</div>

		{#if errorsOpen && errorMessages.length > 0}
			<div class="error-panel">
				{#each errorMessages as err}
					<div class="error-line">{err}</div>
				{/each}
			</div>
		{/if}

		<div class="money-body">
			{@render children()}
		</div>
	</div>
{/if}

<style>
	.money-shell {
		display: flex;
		flex-direction: column;
		margin: -1.5rem;
		height: calc(100vh - 42px);
		overflow: hidden;
	}

	.money-header {
		display: flex;
		align-items: baseline;
		gap: 1rem;
		padding: 0.75rem 1.5rem;
		border-bottom: 1px solid var(--border-subtle);
		flex-shrink: 0;
	}

	.money-header h1 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0;
	}

	.money-nav {
		display: flex;
		gap: 0.35rem;
	}

	.money-nav a {
		font-size: var(--text-sm);
		color: var(--text-muted);
		text-decoration: none;
		padding: 0.2rem 0.55rem;
		border-radius: var(--radius-pill);
		transition: all var(--transition-fast);
	}

	.money-nav a:hover { color: var(--text-primary); }
	.money-nav a.active {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	.money-tools {
		margin-left: auto;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.money-select {
		background: var(--surface-card);
		color: var(--text-primary);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		padding: 0.2rem 0.55rem;
		font-size: var(--text-xs);
		font-family: inherit;
	}

	:global(.error-badge) {
		background: var(--surface-card);
		color: #d46ab5;
		border: 1px solid #5a1f1f;
		border-radius: var(--radius-pill);
		padding: 0.2rem 0.55rem;
		font-size: var(--text-xs);
		cursor: pointer;
	}

	.error-panel {
		background: #1a0e0e;
		padding: 0.5rem 1.5rem;
		border-bottom: 1px solid #5a1f1f;
		max-height: 200px;
		overflow-y: auto;
		flex-shrink: 0;
	}

	.error-line {
		font-family: ui-monospace, SFMono-Regular, monospace;
		font-size: var(--text-xs);
		color: #ffb;
		padding: 0.15rem 0;
	}

	.money-body {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	/* Shared section header pattern reused by sub-route layouts. */
	:global(.money-section-header) {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		padding: 0.5rem 1.5rem;
		border-bottom: 1px solid var(--border-subtle);
		flex-shrink: 0;
	}

	:global(.money-section-nav) {
		display: flex;
		gap: 0.35rem;
	}

	:global(.money-section-nav a) {
		font-size: var(--text-sm);
		color: var(--text-muted);
		text-decoration: none;
		padding: 0.2rem 0.55rem;
		border-radius: var(--radius-pill);
		transition: all var(--transition-fast);
	}

	:global(.money-section-nav a:hover) { color: var(--text-primary); }
	:global(.money-section-nav a.active) {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	:global(.money-section-tools) {
		margin-left: auto;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	:global(.money-control-select) {
		background: var(--surface-card);
		color: var(--text-primary);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		padding: 0.2rem 0.55rem;
		font-size: var(--text-xs);
		font-family: inherit;
	}

	:global(.money-control-input) {
		background: var(--surface-card);
		color: var(--text-primary);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		padding: 0.2rem 0.6rem;
		font-size: var(--text-xs);
		font-family: inherit;
		min-width: 12rem;
	}

	:global(.money-control-input::placeholder) {
		color: var(--text-dim);
	}

	:global(.money-section-body) {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		overflow: auto;
	}

	@media (max-width: 768px) {
		.money-shell {
			margin: -1rem -0.75rem;
			height: calc(100vh - 36px);
		}

		.money-header {
			padding: 0.5rem 0.75rem;
			flex-wrap: wrap;
			gap: 0.5rem;
		}

		.money-nav {
			flex-wrap: wrap;
		}

		.money-tools {
			width: 100%;
		}

		:global(.money-section-header) {
			padding: 0.5rem 0.75rem;
			flex-wrap: wrap;
		}
	}
</style>
