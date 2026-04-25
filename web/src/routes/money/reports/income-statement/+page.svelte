<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { Collapsible } from 'bits-ui';
	import { getReport, type AccountRow } from '$lib/money/api';
	import { selectedLedger } from '$lib/money/stores/ledger';
	import { selectedYear, selectedAccount } from '$lib/money/stores/transactions';
	import { buildTree, displayBalance, parseAmount, shouldInvert, formatAmount, type AccountNode } from '$lib/money/utils/accounts';

	function navigateToAccount(fullName: string) {
		selectedAccount.set(fullName);
		goto(`${base}/money/transactions`);
	}

	let loading = $state(true);
	let error = $state('');
	let incomeRows: AccountRow[] = $state([]);
	let expenseRows: AccountRow[] = $state([]);
	let incomeTree: AccountNode[] = $state([]);
	let expenseTree: AccountNode[] = $state([]);
	let incomeOpen = $state(true);
	let expenseOpen = $state(true);

	let totals = $derived.by(() => {
		let income = 0;
		let expenses = 0;
		let currency = '';
		for (const row of incomeRows) {
			const amt = parseAmount(row['sum(position)'] || '');
			if (!isNaN(amt)) income += Math.abs(amt);
			if (!currency) {
				const m = (row['sum(position)'] || '').match(/[A-Z]{2,}/);
				if (m) currency = m[0];
			}
		}
		for (const row of expenseRows) {
			const amt = parseAmount(row['sum(position)'] || '');
			if (!isNaN(amt)) expenses += Math.abs(amt);
			if (!currency) {
				const m = (row['sum(position)'] || '').match(/[A-Z]{2,}/);
				if (m) currency = m[0];
			}
		}
		return { income, expenses, net: income - expenses, currency };
	});

	async function loadReport() {
		loading = true;
		error = '';
		try {
			const resp = await getReport('income-statement', {
				ledger: $selectedLedger || undefined,
				year: $selectedYear || undefined,
			});
			incomeRows = resp.results.filter(r => r.account.startsWith('Income:'));
			expenseRows = resp.results.filter(r => r.account.startsWith('Expenses:'));
			incomeTree = buildTree(incomeRows);
			expenseTree = buildTree(expenseRows);
		} catch (e) {
			if (e instanceof Error) error = e.message;
			else error = 'Failed to load report';
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		$selectedLedger;
		$selectedYear;
		loadReport();
	});
</script>

{#if loading}
	<div class="loading">Loading...</div>
{:else if error}
	<div class="error-msg">{error}</div>
{:else}
	<div class="report-content">
		<Collapsible.Root bind:open={incomeOpen}>
			<div class="section-header">
				<Collapsible.Trigger class="section-toggle">
					<span class="caret" class:open={incomeOpen}>&#9654;</span>
					Income
				</Collapsible.Trigger>
				<span class="section-total income">{formatAmount(totals.income, totals.currency)}</span>
			</div>
			<Collapsible.Content>
				<div class="tree-section">
					{#each incomeTree as node (node.fullName)}
						{@render treeNode(node)}
					{/each}
				</div>
			</Collapsible.Content>
		</Collapsible.Root>

		<Collapsible.Root bind:open={expenseOpen}>
			<div class="section-header">
				<Collapsible.Trigger class="section-toggle">
					<span class="caret" class:open={expenseOpen}>&#9654;</span>
					Expenses
				</Collapsible.Trigger>
				<span class="section-total expense">{formatAmount(totals.expenses, totals.currency)}</span>
			</div>
			<Collapsible.Content>
				<div class="tree-section">
					{#each expenseTree as node (node.fullName)}
						{@render treeNode(node)}
					{/each}
				</div>
			</Collapsible.Content>
		</Collapsible.Root>

		<div class="net-row">
			<span class="net-label">Net income</span>
			<span class="net-amount" class:positive={totals.net >= 0} class:negative={totals.net < 0}>
				{formatAmount(totals.net, totals.currency)}
			</span>
		</div>
	</div>
{/if}

{#snippet treeNode(node: AccountNode)}
	<div class="tree-row" style="padding-left: {0.75 + node.depth * 1.25}rem">
		<button class="tree-name" type="button" onclick={() => navigateToAccount(node.fullName)}>{node.name}</button>
		{#if node.balance}
			<span class="tree-balance" class:income={node.fullName.startsWith('Income:')} class:expense={node.fullName.startsWith('Expenses:')}>
				{displayBalance(node.balance, node.fullName)}
			</span>
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

	.section-header {
		display: flex;
		align-items: baseline;
		padding: 0.75rem 0.75rem 0.4rem;
		border-bottom: 1px solid var(--border-subtle);
		margin-top: 0.5rem;
	}

	.section-header:first-child {
		margin-top: 0;
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
	}

	.section-total.income { color: #4adbc0; }
	.section-total.expense { color: #d46ab5; }

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

	.tree-balance.income { color: #4adbc0; }
	.tree-balance.expense { color: #d46ab5; }

	.net-row {
		display: flex;
		align-items: baseline;
		padding: 1rem 0.75rem 0.5rem;
		border-top: 2px solid var(--border-default);
		margin-top: 0.75rem;
	}

	.net-label {
		font-weight: 600;
		font-size: var(--text-base);
	}

	.net-amount {
		margin-left: auto;
		font-weight: 600;
		font-size: var(--text-base);
		font-variant-numeric: tabular-nums;
	}

	.net-amount.positive { color: #4adbc0; }
	.net-amount.negative { color: #d46ab5; }
</style>
