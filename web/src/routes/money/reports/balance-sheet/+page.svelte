<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { Collapsible } from 'bits-ui';
	import { getReport, type AccountRow } from '$lib/money/api';
	import { selectedLedger } from '$lib/money/stores/ledger';
	import { selectedAccount } from '$lib/money/stores/transactions';
	import { buildTree, displayBalance, parseAmount, formatAmount, type AccountNode } from '$lib/money/utils/accounts';

	function navigateToAccount(fullName: string) {
		selectedAccount.set(fullName);
		goto(`${base}/transactions`);
	}

	let loading = $state(true);
	let error = $state('');
	let assetRows: AccountRow[] = $state([]);
	let liabilityRows: AccountRow[] = $state([]);
	let equityRows: AccountRow[] = $state([]);
	let assetTree: AccountNode[] = $state([]);
	let liabilityTree: AccountNode[] = $state([]);
	let equityTree: AccountNode[] = $state([]);
	let assetsOpen = $state(true);
	let liabilitiesOpen = $state(true);
	let equityOpen = $state(true);

	let totals = $derived.by(() => {
		let assetsRaw = 0;
		let liabilitiesRaw = 0;
		let equityRaw = 0;
		let currency = '';

		function extractCurrency(pos: string) {
			if (!currency) {
				const m = pos.match(/[A-Z]{2,}/);
				if (m) currency = m[0];
			}
		}

		for (const row of assetRows) {
			const amt = parseAmount(row['sum(position)'] || '');
			if (!isNaN(amt)) assetsRaw += amt;
			extractCurrency(row['sum(position)'] || '');
		}
		for (const row of liabilityRows) {
			const amt = parseAmount(row['sum(position)'] || '');
			if (!isNaN(amt)) liabilitiesRaw += amt;
			extractCurrency(row['sum(position)'] || '');
		}
		for (const row of equityRows) {
			const amt = parseAmount(row['sum(position)'] || '');
			if (!isNaN(amt)) equityRaw += amt;
			extractCurrency(row['sum(position)'] || '');
		}
		// Assets are positive in beancount. Liabilities/equity are negative (credit accounts).
		// Display liabilities/equity as positive numbers (negate the raw sum).
		// Net worth = assets + liabilities (raw), since liabilities raw is negative.
		return {
			assets: assetsRaw,
			liabilities: -liabilitiesRaw,
			equity: -equityRaw,
			netWorth: assetsRaw + liabilitiesRaw,
			currency,
		};
	});

	async function loadReport() {
		loading = true;
		error = '';
		try {
			const resp = await getReport('balance-sheet', {
				ledger: $selectedLedger || undefined,
			});
			assetRows = resp.results.filter(r => r.account.startsWith('Assets:'));
			liabilityRows = resp.results.filter(r => r.account.startsWith('Liabilities:'));
			equityRows = resp.results.filter(r => r.account.startsWith('Equity:'));
			assetTree = buildTree(assetRows);
			liabilityTree = buildTree(liabilityRows);
			equityTree = buildTree(equityRows);
		} catch (e) {
			if (e instanceof Error) error = e.message;
			else error = 'Failed to load report';
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		$selectedLedger;
		loadReport();
	});
</script>

{#if loading}
	<div class="loading">Loading...</div>
{:else if error}
	<div class="error-msg">{error}</div>
{:else}
	<div class="report-content">
		<div class="net-worth-banner">
			<span class="nw-label">Net worth</span>
			<span class="nw-amount" class:positive={totals.netWorth >= 0} class:negative={totals.netWorth < 0}>
				{formatAmount(totals.netWorth, totals.currency)}
			</span>
		</div>

		<Collapsible.Root bind:open={assetsOpen}>
			<div class="section-header">
				<Collapsible.Trigger class="section-toggle">
					<span class="caret" class:open={assetsOpen}>&#9654;</span>
					Assets
				</Collapsible.Trigger>
				<span class="section-total">{formatAmount(totals.assets, totals.currency)}</span>
			</div>
			<Collapsible.Content>
				<div class="tree-section">
					{#each assetTree as node (node.fullName)}
						{@render treeNode(node)}
					{/each}
				</div>
			</Collapsible.Content>
		</Collapsible.Root>

		<Collapsible.Root bind:open={liabilitiesOpen}>
			<div class="section-header">
				<Collapsible.Trigger class="section-toggle">
					<span class="caret" class:open={liabilitiesOpen}>&#9654;</span>
					Liabilities
				</Collapsible.Trigger>
				<span class="section-total">{formatAmount(totals.liabilities, totals.currency)}</span>
			</div>
			<Collapsible.Content>
				<div class="tree-section">
					{#each liabilityTree as node (node.fullName)}
						{@render treeNode(node)}
					{/each}
				</div>
			</Collapsible.Content>
		</Collapsible.Root>

		<Collapsible.Root bind:open={equityOpen}>
			<div class="section-header">
				<Collapsible.Trigger class="section-toggle">
					<span class="caret" class:open={equityOpen}>&#9654;</span>
					Equity
				</Collapsible.Trigger>
				<span class="section-total">{formatAmount(totals.equity, totals.currency)}</span>
			</div>
			<Collapsible.Content>
				<div class="tree-section">
					{#each equityTree as node (node.fullName)}
						{@render treeNode(node)}
					{/each}
				</div>
			</Collapsible.Content>
		</Collapsible.Root>
	</div>
{/if}

{#snippet treeNode(node: AccountNode)}
	<div class="tree-row" style="padding-left: {0.75 + node.depth * 1.25}rem">
		<button class="tree-name" type="button" onclick={() => navigateToAccount(node.fullName)}>{node.name}</button>
		{#if node.balance}
			<span class="tree-balance">{displayBalance(node.balance, node.fullName)}</span>
		{/if}
	</div>
	{#each node.children as child (child.fullName)}
		{@render treeNode(child)}
	{/each}
{/snippet}

<style>
	.report-content {
		padding: 0.5rem 0.75rem;
	}

	.net-worth-banner {
		display: flex;
		align-items: baseline;
		padding: 1rem 0.75rem;
		margin-bottom: 0.5rem;
		background: var(--surface-card);
		border-radius: var(--radius-card);
	}

	.nw-label {
		font-weight: 600;
		font-size: 1rem;
	}

	.nw-amount {
		margin-left: auto;
		font-weight: 600;
		font-size: 1rem;
		font-variant-numeric: tabular-nums;
	}

	.nw-amount.positive { color: #4adbc0; }
	.nw-amount.negative { color: #d46ab5; }

	.section-header {
		display: flex;
		align-items: baseline;
		padding: 0.75rem 0.75rem 0.4rem;
		border-bottom: 1px solid var(--border-subtle);
		margin-top: 0.5rem;
	}

	:global(.section-toggle) {
		background: none;
		border: none;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-base);
		font-weight: 600;
		cursor: pointer;
		padding: 0;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.section-total {
		margin-left: auto;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		font-size: var(--text-base);
		color: var(--text-primary);
	}

	.caret {
		font-size: 0.5rem;
		color: var(--text-dim);
		transition: transform var(--transition-fast);
		display: inline-block;
	}

	.caret.open {
		transform: rotate(90deg);
	}

	.tree-section {
		padding: 0.25rem 0 0.5rem;
	}

	.tree-row {
		display: flex;
		align-items: baseline;
		gap: 0.25rem;
		padding: 0.2rem 0.75rem;
		font-size: var(--text-sm);
		border-radius: 0.25rem;
		transition: background var(--transition-fast);
	}

	.tree-row:hover {
		background: var(--surface-card);
	}

	.tree-name {
		flex: 1;
		min-width: 0;
		background: none;
		border: none;
		font: inherit;
		color: inherit;
		cursor: pointer;
		padding: 0;
		text-align: left;
	}

	.tree-name:hover {
		color: var(--text-primary);
	}

	.tree-balance {
		margin-left: auto;
		text-align: right;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
		color: var(--text-primary);
	}
</style>
