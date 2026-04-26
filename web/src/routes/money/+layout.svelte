<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { Collapsible } from 'bits-ui';
	import { getLedgers, checkLedger, AuthError } from '$lib/money/api';
	import { selectedLedger, availableLedgers } from '$lib/money/stores/ledger';
	import { AppShell, ShellHeader, NavLink, Select } from '$lib/components/ui';

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

	const ledgerOptions = $derived($availableLedgers.map((l) => ({ value: l, label: l })));
</script>

{#if loading}
	<div class="loading">Loading…</div>
{:else if error}
	<div class="error-msg">{error}</div>
{:else}
	<AppShell>
		{#snippet header()}
			<ShellHeader title="Money">
				{#snippet nav()}
					<NavLink href="{moneyBase}/accounts" active={isActive('/accounts')}>Accounts</NavLink>
					<NavLink href="{moneyBase}/transactions" active={isActive('/transactions')}>Transactions</NavLink>
					<NavLink href="{moneyBase}/reports/income-statement" active={isActive('/reports')}>Reports</NavLink>
					<NavLink href="{moneyBase}/taxes" active={isActive('/taxes')}>Taxes</NavLink>
					<NavLink href="{moneyBase}/business/invoices" active={isActive('/business')}>Business</NavLink>
				{/snippet}
				{#snippet tools()}
					{#if $availableLedgers.length > 1}
						<Select
							value={$selectedLedger}
							options={ledgerOptions}
							onValueChange={(v) => selectedLedger.set(v)}
							ariaLabel="Ledger"
						/>
					{/if}
					{#if errorCount > 0}
						<Collapsible.Root bind:open={errorsOpen}>
							<Collapsible.Trigger class="error-badge">
								{errorCount} error{errorCount !== 1 ? 's' : ''}
							</Collapsible.Trigger>
						</Collapsible.Root>
					{/if}
				{/snippet}
			</ShellHeader>
		{/snippet}

		{#snippet extras()}
			{#if errorsOpen && errorMessages.length > 0}
				<div class="error-panel">
					{#each errorMessages as err}
						<div class="error-line">{err}</div>
					{/each}
				</div>
			{/if}
		{/snippet}

		{@render children()}
	</AppShell>
{/if}

<style>
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
		padding: 0.5rem 0.75rem;
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

	/* Shared section header pattern reused by sub-route layouts (transactions/reports/business). */
	:global(.money-section-header) {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		padding: 0.5rem 0.75rem;
		border-bottom: 1px solid var(--border-subtle);
		flex-shrink: 0;
	}

	:global(.money-section-nav) {
		display: flex;
		gap: var(--chip-gap);
		/* Hang: chip TEXT aligns with the section heading text above. */
		margin-inline-start: calc(-1 * var(--chip-padding-x));
	}

	:global(.money-section-nav a) {
		display: inline-flex;
		align-items: center;
		font-size: var(--text-sm);
		line-height: 1.2;
		color: var(--text-muted);
		text-decoration: none;
		padding: 0.15rem 0.5rem;
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
		:global(.money-section-header) {
			padding: 0.5rem 0.75rem;
			flex-wrap: wrap;
		}
	}
</style>
