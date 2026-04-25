<script lang="ts">
	import { getAccounts, type AccountRow } from '$lib/money/api';
	import { selectedAccount, selectedYear, filterText } from '$lib/money/stores/transactions';
	import { selectedLedger } from '$lib/money/stores/ledger';

	let { children } = $props();

	let accounts: AccountRow[] = $state([]);
	let sidebarOpen = $state(false);
	let filterTimeout: ReturnType<typeof setTimeout> | null = null;

	const currentYear = new Date().getFullYear();
	const years = Array.from({ length: 11 }, (_, i) => currentYear - i);

	interface AccountNode {
		name: string;
		fullName: string;
		children: AccountNode[];
	}

	let tree: AccountNode[] = $state([]);
	let expandedSet = $state(new Set<string>());

	function buildTree(rows: AccountRow[]): AccountNode[] {
		const root: AccountNode[] = [];
		const nodeMap = new Map<string, AccountNode>();

		for (const row of rows) {
			const parts = row.account.split(':');
			let current = root;
			let path = '';

			for (let i = 0; i < parts.length; i++) {
				path = path ? `${path}:${parts[i]}` : parts[i];
				let node = nodeMap.get(path);
				if (!node) {
					node = { name: parts[i], fullName: path, children: [] };
					nodeMap.set(path, node);
					current.push(node);
				}
				current = node.children;
			}
		}

		for (const node of root) {
			expandedSet.add(node.fullName);
		}

		return root;
	}

	function toggleExpand(e: MouseEvent, fullName: string) {
		e.stopPropagation();
		const next = new Set(expandedSet);
		if (next.has(fullName)) {
			next.delete(fullName);
		} else {
			next.add(fullName);
		}
		expandedSet = next;
	}

	function selectAccount(fullName: string) {
		selectedAccount.update(current => current === fullName ? '' : fullName);
		sidebarOpen = false;
	}

	function handleYearChange(e: Event) {
		const val = (e.target as HTMLSelectElement).value;
		selectedYear.set(val === '' ? 0 : Number(val));
	}

	function handleFilterInput(value: string) {
		if (filterTimeout) clearTimeout(filterTimeout);
		filterTimeout = setTimeout(() => filterText.set(value), 300);
	}

	async function loadAccounts(ledger: string) {
		expandedSet = new Set<string>();
		try {
			const resp = await getAccounts({ ledger: ledger || undefined });
			accounts = resp.accounts;
			tree = buildTree(accounts);
		} catch {
			// page handles its own loading/error
		}
	}

	let prevLedger = $state($selectedLedger);

	// Reload sidebar accounts when ledger changes
	$effect(() => {
		loadAccounts($selectedLedger);
	});

	// Clear account filter when ledger changes (but not on initial mount)
	$effect(() => {
		const current = $selectedLedger;
		if (current !== prevLedger) {
			prevLedger = current;
			selectedAccount.set('');
		}
	});
</script>

<div class="money-section-header">
	{#if $selectedAccount}
		<button class="active-filter" onclick={() => selectedAccount.set('')} type="button">
			{$selectedAccount} <span class="clear">&times;</span>
		</button>
	{/if}
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
			placeholder="Filter by tag, payee..."
			value={$filterText}
			oninput={(e) => handleFilterInput(e.currentTarget.value)}
		/>
		<button class="sidebar-toggle" onclick={() => sidebarOpen = !sidebarOpen} type="button">
			{sidebarOpen ? 'Close' : 'Accounts'}
		</button>
	</div>
</div>

<div class="txn-body">
	<aside class="txn-sidebar" class:open={sidebarOpen}>
		<div class="sidebar-header">
			<span class="sidebar-title">Accounts</span>
			<span class="sidebar-count">{accounts.length}</span>
		</div>
		<div class="sidebar-list">
			{#each tree as node (node.fullName)}
				{@render accountNode(node, 0)}
			{/each}
		</div>
	</aside>

	<div class="txn-main">
		{@render children()}
	</div>
</div>

{#snippet accountNode(node: AccountNode, depth: number)}
	<div class="acct-row" style="padding-left: {0.5 + depth * 0.75}rem">
		{#if node.children.length > 0}
			<button
				class="acct-caret"
				onclick={(e) => toggleExpand(e, node.fullName)}
				type="button"
			>
				<span class="caret" class:open={expandedSet.has(node.fullName)}>&#9654;</span>
			</button>
		{:else}
			<span class="caret-spacer"></span>
		{/if}
		<button
			class="acct-btn"
			class:selected={$selectedAccount === node.fullName}
			onclick={() => selectAccount(node.fullName)}
			type="button"
		>
			{node.name}
		</button>
	</div>
	{#if node.children.length > 0 && expandedSet.has(node.fullName)}
		{#each node.children as child (child.fullName)}
			{@render accountNode(child, depth + 1)}
		{/each}
	{/if}
{/snippet}

<style>
	.active-filter {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		background: var(--surface-raised);
		border: none;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-xs);
		padding: 0.2rem 0.55rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
		transition: background var(--transition-fast);
	}

	.active-filter:hover {
		background: var(--border-default);
	}

	.active-filter .clear {
		color: var(--text-dim);
		font-size: var(--text-sm);
		line-height: 1;
	}

	.sidebar-toggle {
		display: none;
		background: var(--surface-card);
		border: none;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.25rem 0.6rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
	}

	.txn-body {
		display: flex;
		flex: 1;
		min-height: 0;
	}

	.txn-sidebar {
		width: 220px;
		flex-shrink: 0;
		border-right: 1px solid var(--border-subtle);
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	.sidebar-header {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
		padding: 0.6rem 1rem 0.6rem 1.5rem;
		flex-shrink: 0;
	}

	.sidebar-title {
		font-size: var(--text-sm);
		font-weight: 500;
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.sidebar-count {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.sidebar-list {
		flex: 1;
		overflow-y: auto;
		padding: 0 0.25rem 0.5rem;
	}

	.sidebar-list::-webkit-scrollbar { width: 4px; }
	.sidebar-list::-webkit-scrollbar-track { background: transparent; }
	.sidebar-list::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 2px; }

	.acct-row {
		display: flex;
		align-items: center;
	}

	.acct-caret {
		background: none;
		border: none;
		color: var(--text-dim);
		cursor: pointer;
		padding: 0.15rem 0.25rem;
		display: inline-flex;
		align-items: center;
		flex-shrink: 0;
	}

	.caret {
		font-size: 0.45rem;
		display: inline-block;
		transition: transform var(--transition-fast);
	}

	.caret.open {
		transform: rotate(90deg);
	}

	.caret-spacer {
		width: 1.15rem;
		display: inline-block;
		flex-shrink: 0;
	}

	.acct-btn {
		display: block;
		flex: 1;
		min-width: 0;
		background: none;
		border: none;
		color: var(--text-secondary);
		font: inherit;
		font-size: var(--text-sm);
		cursor: pointer;
		padding: 0.2rem 0.5rem;
		border-radius: 0.3rem;
		text-align: left;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		transition: background var(--transition-fast);
	}

	.acct-btn:hover {
		background: var(--surface-raised);
	}

	.acct-btn.selected {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	.txn-main {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	@media (max-width: 768px) {
		.sidebar-toggle {
			display: block;
			margin-left: auto;
		}

		.txn-sidebar {
			display: none;
			position: absolute;
			top: 0;
			left: 0;
			bottom: 0;
			z-index: 20;
			width: 240px;
			background: var(--surface-base);
			border-right: 1px solid var(--border-default);
		}

		.txn-sidebar.open {
			display: flex;
		}

		.txn-body {
			position: relative;
		}
	}
</style>
