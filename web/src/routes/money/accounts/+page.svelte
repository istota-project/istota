<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { getAccounts, type AccountRow } from '$lib/money/api';
	import { selectedLedger } from '$lib/money/stores/ledger';
	import { selectedYear, selectedAccount } from '$lib/money/stores/transactions';
	import { accountFilter } from '$lib/money/stores/accounts';
	import { buildTree, displayBalance, type AccountNode } from '$lib/money/utils/accounts';

	function navigateToAccount(fullName: string) {
		selectedAccount.set(fullName);
		goto(`${base}/money/transactions`);
	}

	let accounts: AccountRow[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	let tree: AccountNode[] = $state([]);
	let expandedSet = $state(new Set<string>());

	function toggleExpand(fullName: string) {
		const next = new Set(expandedSet);
		if (next.has(fullName)) {
			next.delete(fullName);
		} else {
			next.add(fullName);
		}
		expandedSet = next;
	}

	function flattenVisible(nodes: AccountNode[]): AccountNode[] {
		const result: AccountNode[] = [];
		for (const node of nodes) {
			if ($accountFilter && !node.fullName.toLowerCase().includes($accountFilter.toLowerCase())) {
				const childResults = flattenVisible(node.children);
				if (childResults.length === 0) continue;
				result.push(node);
				if (expandedSet.has(node.fullName) || $accountFilter) {
					result.push(...childResults);
				}
				continue;
			}
			result.push(node);
			if ((expandedSet.has(node.fullName) || $accountFilter) && node.children.length > 0) {
				result.push(...flattenVisible(node.children));
			}
		}
		return result;
	}

	let visibleNodes = $derived(flattenVisible(tree));

	async function loadAccounts() {
		loading = true;
		error = '';
		expandedSet = new Set<string>();
		try {
			const resp = await getAccounts({
				ledger: $selectedLedger || undefined,
				year: $selectedYear || undefined,
			});
			accounts = resp.accounts;
			tree = buildTree(accounts, expandedSet);
		} catch (e) {
			if (e instanceof Error) error = e.message;
			else error = 'Failed to load accounts';
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		$selectedLedger;
		$selectedYear;
		loadAccounts();
	});
</script>

{#if loading}
	<div class="loading">Loading...</div>
{:else if error}
	<div class="error-msg">{error}</div>
{:else if visibleNodes.length === 0}
	<div class="empty">No accounts found.</div>
{:else}
	<div class="account-list">
		{#each visibleNodes as node (node.fullName)}
			<div
				class="account-row"
				style="padding-left: {0.75 + node.depth * 1.25}rem"
			>
				<button class="account-toggle" type="button" onclick={() => node.children.length > 0 ? toggleExpand(node.fullName) : null}>
					{#if node.children.length > 0}
						<span class="caret" class:open={expandedSet.has(node.fullName) || !!$accountFilter}>&#9654;</span>
					{:else}
						<span class="caret-spacer"></span>
					{/if}
				</button>
				<button class="account-name" type="button" onclick={() => navigateToAccount(node.fullName)}>{node.name}</button>
				{#if node.balance}
					<span class="account-balance" class:income={node.fullName.startsWith('Income:')} class:expense={node.fullName.startsWith('Expenses:')}>{displayBalance(node.balance, node.fullName)}</span>
				{/if}
			</div>
		{/each}
	</div>
{/if}

<style>
	.account-list {
		display: flex;
		flex-direction: column;
		padding: 0.25rem 0;
	}

	.account-row {
		display: flex;
		align-items: baseline;
		gap: 0.25rem;
		padding: 0.3rem 0.75rem;
		font-size: var(--text-sm);
		border-radius: 0.25rem;
		transition: background var(--transition-fast);
	}

	.account-row:hover {
		background: var(--surface-card);
	}

	.account-toggle {
		width: 0.75rem;
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		background: none;
		border: none;
		padding: 0;
		cursor: pointer;
		color: inherit;
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

	.caret-spacer {
		width: 0.5rem;
		display: inline-block;
	}

	.account-name {
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

	.account-name:hover {
		color: var(--text-primary);
	}

	.account-balance {
		margin-left: auto;
		text-align: right;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
		color: var(--text-primary);
	}

	.account-balance.income { color: #4adbc0; }
	.account-balance.expense { color: #d46ab5; }

	.empty {
		color: var(--text-dim);
		font-size: var(--text-base);
		padding: 2rem 1rem;
		text-align: center;
	}
</style>
